import os
import io
import json
import hmac
import base64
import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

API_VERSION = os.getenv("SQUARE_API_VERSION", "2026-08-19")
SQUARE_BASE_URL = os.getenv("SQUARE_BASE_URL", "https://connect.squareup.com").rstrip("/")
ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
WEBHOOK_SIGNATURE_KEY = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
WEBHOOK_URL = os.getenv("SQUARE_WEBHOOK_URL", "")
LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
MILK_MODIFIER_LIST_ID = os.getenv("MILK_MODIFIER_LIST_ID", "")

PRINT_WIDTH = int(os.getenv("PRINT_WIDTH_DOTS", "256"))
MIN_HEIGHT = int(os.getenv("MIN_LABEL_HEIGHT_DOTS", "195"))
NAME_MAX_FONT = int(os.getenv("NAME_MAX_FONT_PX", "72"))
CODE_MAX_FONT = int(os.getenv("CODE_MAX_FONT_PX", "64"))
MIN_CODE_FONT = int(os.getenv("MIN_CODE_FONT_PX", "34"))
LINE_GAP = int(os.getenv("LINE_GAP_DOTS", "0"))

FONT_BLACK_PATH = os.getenv("FONT_BLACK", "")
FONT_BOLD_PATH = os.getenv("FONT_BOLD", "")
STAR_PRINTER_MAC = os.getenv("STAR_PRINTER_MAC", "").lower().replace("-", ":")

DB = Path(__file__).with_name("speedbar.sqlite3")

app = FastAPI(title="Speed Bar Square → Star Label Engine", version="0.1.0")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_events(
            event_id TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_modifier_cache(
            modifier_id TEXT PRIMARY KEY,
            modifier_list_id TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS print_jobs(
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            line_uid TEXT NOT NULL,
            unit_index INTEGER NOT NULL,
            png BLOB NOT NULL,
            state TEXT NOT NULL DEFAULT 'READY',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            printed_at DATETIME,
            UNIQUE(order_id, line_uid, unit_index)
        )
        """)


@app.on_event("startup")
def startup():
    init_db()


def verify_square_signature(raw_body: bytes, signature: str) -> bool:
    if not WEBHOOK_SIGNATURE_KEY or not WEBHOOK_URL:
        return False
    msg = WEBHOOK_URL.encode("utf-8") + raw_body
    digest = hmac.new(
        WEBHOOK_SIGNATURE_KEY.encode("utf-8"),
        msg,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature or "")


async def square_get(path: str):
    if not ACCESS_TOKEN:
        raise RuntimeError("SQUARE_ACCESS_TOKEN is not configured")
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Square-Version": API_VERSION,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.get(f"{SQUARE_BASE_URL}{path}", headers=headers)
        r.raise_for_status()
        return r.json()


def event_seen(event_id: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone()
        return bool(row)


def mark_event(event_id: str):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO processed_events(event_id) VALUES (?)", (event_id,))


async def modifier_list_id(modifier_id: str) -> Optional[str]:
    if not modifier_id:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT modifier_list_id FROM catalog_modifier_cache WHERE modifier_id=?",
            (modifier_id,)
        ).fetchone()
        if row:
            return row["modifier_list_id"]

    data = await square_get(f"/v2/catalog/object/{modifier_id}")
    obj = data.get("object") or {}
    mlid = (obj.get("modifier_data") or {}).get("modifier_list_id")
    if mlid:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO catalog_modifier_cache(modifier_id, modifier_list_id) VALUES (?,?)",
                (modifier_id, mlid)
            )
    return mlid


def customer_name(order: dict) -> str:
    # Best source for POS "ticket name"
    if order.get("ticket_name"):
        return str(order["ticket_name"]).strip().upper()

    # Fallback: fulfillment recipient
    for f in order.get("fulfillments") or []:
        for details_key in ("pickup_details", "delivery_details", "shipment_details", "in_store_details"):
            details = f.get(details_key) or {}
            recipient = details.get("recipient") or {}
            name = recipient.get("display_name")
            if name:
                return str(name).strip().upper()

    return "ORDER"


def font(path: str, size: int):
    candidates = [
        path,
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def text_width(draw, text, fnt):
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def fit_font(draw, text, path, max_px, min_px=20, width=PRINT_WIDTH):
    size = max_px
    while size > min_px:
        f = font(path, size)
        if text_width(draw, text, f) <= width:
            return f, size
        size -= 1
    return font(path, min_px), min_px


def layout_code_segments(segments, max_width, draw):
    """
    segments = list of {text:str, milk:bool}
    Fit largest font that allows one line; if impossible at MIN_CODE_FONT,
    wrap greedily without interpreting the modifier text.
    """
    joined = "".join(s["text"] for s in segments)

    # Try largest possible single-line font first.
    fnt, size = fit_font(draw, joined, FONT_BOLD_PATH, CODE_MAX_FONT, MIN_CODE_FONT, max_width)
    if text_width(draw, joined, fnt) <= max_width:
        return [[s.copy() for s in segments]], fnt, size

    # Wrap at modifier segment boundaries at minimum allowed code font.
    fnt = font(FONT_BOLD_PATH, MIN_CODE_FONT)
    lines = []
    current = []
    current_w = 0
    for seg in segments:
        w = text_width(draw, seg["text"], fnt)
        if current and current_w + w > max_width:
            lines.append(current)
            current = []
            current_w = 0
        current.append(seg.copy())
        current_w += w
    if current:
        lines.append(current)
    return lines, fnt, MIN_CODE_FONT


def render_label(name: str, base_item_text: str, modifiers: list[dict]) -> bytes:
    """
    modifiers: [{"text": "...", "milk": True/False}]
    No logical margins. Height expands only when required.
    """
    scratch = Image.new("L", (PRINT_WIDTH, 1000), 255)
    d = ImageDraw.Draw(scratch)

    name_font, _ = fit_font(d, name, FONT_BLACK_PATH, NAME_MAX_FONT, 30, PRINT_WIDTH)
    name_bbox = d.textbbox((0, 0), name, font=name_font)
    name_h = name_bbox[3] - name_bbox[1]

    segments = [{"text": base_item_text, "milk": False}] + modifiers
    code_lines, code_font, _ = layout_code_segments(segments, PRINT_WIDTH, d)

    line_heights = []
    for line in code_lines:
        txt = "".join(s["text"] for s in line) or " "
        box = d.textbbox((0, 0), txt, font=code_font)
        line_heights.append(box[3] - box[1])

    content_h = name_h + LINE_GAP + sum(line_heights)
    final_h = max(MIN_HEIGHT, content_h)

    img = Image.new("L", (PRINT_WIDTH, final_h), 255)
    draw = ImageDraw.Draw(img)

    # Name: left-aligned, top at y=0.
    draw.text((0, 0), name, fill=0, font=name_font)
    y = name_h + LINE_GAP

    for i, line in enumerate(code_lines):
        x = 0
        for seg in line:
            txt = seg["text"]
            w = text_width(draw, txt, code_font)
            box = draw.textbbox((x, y), txt, font=code_font)
            if seg.get("milk"):
                # Black rectangle exactly behind milk modifier text.
                draw.rectangle([x, y, x + w, y + line_heights[i]], fill=0)
                draw.text((x, y), txt, fill=255, font=code_font)
            else:
                draw.text((x, y), txt, fill=0, font=code_font)
            x += w
        y += line_heights[i]

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def make_jobs_from_order(order: dict):
    if LOCATION_ID and order.get("location_id") != LOCATION_ID:
        return

    name = customer_name(order)

    for item in order.get("line_items") or []:
        line_uid = item.get("uid") or str(uuid.uuid4())
        qty = int(float(item.get("quantity", "1")))
        # IMPORTANT:
        # This uses the item's kitchen/display name exactly as Square returns it.
        # If your catalog already encodes e.g. "LC" for Large Caramel Cappuccino,
        # it will print "LC". Otherwise later we can choose kitchen_name/code source.
        base = (item.get("name") or "").strip()

        mods = []
        for m in item.get("modifiers") or []:
            text = (m.get("name") or "").strip()
            if not text:
                continue
            is_milk = False
            modifier_id = m.get("catalog_object_id")
            if MILK_MODIFIER_LIST_ID and modifier_id:
                mlid = await modifier_list_id(modifier_id)
                is_milk = (mlid == MILK_MODIFIER_LIST_ID)
            mods.append({"text": text, "milk": is_milk})

        png = render_label(name, base, mods)

        for unit_index in range(1, qty + 1):
            job_id = str(uuid.uuid4())
            with db() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO print_jobs
                    (id, order_id, line_uid, unit_index, png, state)
                    VALUES (?,?,?,?,?,'READY')""",
                    (job_id, order.get("id", ""), line_uid, unit_index, png)
                )


@app.post("/square/webhook")
async def square_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-square-hmacsha256-signature", "")

    if not verify_square_signature(raw, signature):
        raise HTTPException(status_code=403, detail="Invalid Square signature")

    payload = json.loads(raw)
    event_id = payload.get("event_id")
    if not event_id:
        return {"ok": True}

    # Square can retry webhooks; never print twice.
    if event_seen(event_id):
        return {"ok": True, "duplicate": True}

    typ = payload.get("type")
    payment = (((payload.get("data") or {}).get("object") or {}).get("payment") or {})

    # Only act once a payment is actually completed.
    if typ in ("payment.created", "payment.updated") and payment.get("status") == "COMPLETED":
        if LOCATION_ID and payment.get("location_id") != LOCATION_ID:
            mark_event(event_id)
            return {"ok": True, "ignored_location": True}

        order_id = payment.get("order_id")
        if order_id:
            order_data = await square_get(f"/v2/orders/{order_id}")
            order = order_data.get("order") or {}
            await make_jobs_from_order(order)

    mark_event(event_id)
    return {"ok": True}


def printer_allowed(request: Request):
    if not STAR_PRINTER_MAC:
        return True
    mac = (
        request.headers.get("x-star-mac")
        or request.query_params.get("mac")
        or ""
    ).lower().replace("-", ":")
    return mac == STAR_PRINTER_MAC


def next_job():
    with db() as conn:
        return conn.execute(
            "SELECT id FROM print_jobs WHERE state='READY' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()


@app.api_route("/cloudprnt", methods=["POST"])
async def cloudprnt_poll(request: Request):
    if not printer_allowed(request):
        raise HTTPException(status_code=401, detail="Unknown printer")

    job = next_job()
    if not job:
        return {"jobReady": False}

    token = job["id"]
    return {
        "jobReady": True,
        "mediaTypes": ["image/png"],
        "jobToken": token,
        "deleteMethod": "DELETE"
    }


@app.api_route("/cloudprnt", methods=["GET"])
async def cloudprnt_get(request: Request):
    if not printer_allowed(request):
        raise HTTPException(status_code=401, detail="Unknown printer")

    token = (
        request.headers.get("x-star-token")
        or request.query_params.get("token")
        or request.query_params.get("jobToken")
    )
    if not token:
        # CloudPRNT server-info request can hit a derived URL; keep this endpoint
        # focused on print jobs.
        raise HTTPException(status_code=404, detail="No job token")

    with db() as conn:
        row = conn.execute(
            "SELECT png FROM print_jobs WHERE id=? AND state IN ('READY','SENT')",
            (token,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        conn.execute("UPDATE print_jobs SET state='SENT' WHERE id=?", (token,))
        return Response(content=row["png"], media_type="image/png")


@app.api_route("/cloudprnt", methods=["DELETE"])
async def cloudprnt_delete(request: Request):
    if not printer_allowed(request):
        raise HTTPException(status_code=401, detail="Unknown printer")

    token = (
        request.headers.get("x-star-token")
        or request.query_params.get("token")
        or request.query_params.get("jobToken")
    )
    if token:
        with db() as conn:
            conn.execute(
                "UPDATE print_jobs SET state='PRINTED', printed_at=CURRENT_TIMESTAMP WHERE id=?",
                (token,)
            )
    return Response(status_code=200)


@app.get("/health")
def health():
    with db() as conn:
        ready = conn.execute("SELECT COUNT(*) c FROM print_jobs WHERE state='READY'").fetchone()["c"]
    return {"ok": True, "ready_jobs": ready, "width_dots": PRINT_WIDTH, "min_height_dots": MIN_HEIGHT}


@app.get("/preview")
def preview(name: str = "JAMIE", code: str = "LCAC2eq", milk_start: int = 2, milk_len: int = 1):
    # Standalone visual test without Square.
    before = code[:milk_start]
    milk = code[milk_start:milk_start+milk_len]
    after = code[milk_start+milk_len:]
    png = render_label(
        name=name,
        base_item_text=before,
        modifiers=[
            {"text": milk, "milk": True},
            {"text": after, "milk": False},
        ],
    )
    return Response(content=png, media_type="image/png")
