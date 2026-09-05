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
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

API_VERSION = os.getenv("SQUARE_API_VERSION", "2026-08-19")
SQUARE_BASE_URL = os.getenv("SQUARE_BASE_URL", "https://connect.squareup.com").rstrip("/")
ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
WEBHOOK_SIGNATURE_KEY = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
WEBHOOK_URL = os.getenv("SQUARE_WEBHOOK_URL", "")
LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
MILK_MODIFIER_LIST_IDS = {x.strip() for x in os.getenv("MILK_MODIFIER_LIST_IDS", "").split(",") if x.strip()}

PRINT_WIDTH = int(os.getenv("PRINT_WIDTH_DOTS", "256"))
MIN_HEIGHT = int(os.getenv("MIN_LABEL_HEIGHT_DOTS", "195"))
COMMON_MAX_FONT = int(os.getenv("COMMON_MAX_FONT_PX", "64"))
COMMON_MIN_FONT = int(os.getenv("COMMON_MIN_FONT_PX", "30"))
SEGMENT_GAP_DOTS = int(os.getenv("SEGMENT_GAP_DOTS", "4"))
LINE_GAP_DOTS = int(os.getenv("LINE_GAP_DOTS", "6"))
MILK_FONT_SCALE = float(os.getenv("MILK_FONT_SCALE", "1.12"))

FONT_BLACK_PATH = os.getenv("FONT_BLACK", "")
FONT_BOLD_PATH = os.getenv("FONT_BOLD", "")
STAR_PRINTER_MAC = os.getenv("STAR_PRINTER_MAC", "").lower().replace("-", ":")

DB = Path(__file__).with_name("speedbar.sqlite3")
app = FastAPI(title="Speed Bar Square → Star Label Engine", version="0.6.0")


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
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_modifier_cache(
            modifier_id TEXT PRIMARY KEY,
            modifier_list_id TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
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
        )""")


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
        return bool(conn.execute(
            "SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)
        ).fetchone())


def mark_event(event_id: str):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events(event_id) VALUES (?)",
            (event_id,)
        )


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
    if order.get("ticket_name"):
        return str(order["ticket_name"]).strip().upper()

    for f in order.get("fulfillments") or []:
        for key in ("pickup_details", "delivery_details", "shipment_details", "in_store_details"):
            details = f.get(key) or {}
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


def metrics(draw, text, fnt):
    if not text:
        return 0, 0
    box = draw.textbbox((0, 0), text, font=fnt, anchor="lt")
    return box[2] - box[0], box[3] - box[1]


def segment_width(draw, seg, code_font, milk_font):
    use_font = milk_font if seg.get("milk") else code_font
    w, _ = metrics(draw, seg["text"], use_font)
    return w


def choose_primary_fonts(draw, name: str, segments: list, width: int):
    """
    Lines 1 and 2 share the same base font size.
    We choose the largest size that fits the customer name and at least the
    first code segment on line 2. We do NOT shrink line 2 merely to force all
    modifiers onto it; overflow goes to line 3 instead.
    """
    for size in range(COMMON_MAX_FONT, COMMON_MIN_FONT - 1, -1):
        f_name = font(FONT_BLACK_PATH, size)
        f_code = font(FONT_BOLD_PATH, size)
        milk_size = max(size, int(round(size * MILK_FONT_SCALE)))
        f_milk = font(FONT_BOLD_PATH, milk_size)

        name_w, _ = metrics(draw, name, f_name)
        if name_w > width:
            continue

        if segments:
            first_w = segment_width(draw, segments[0], f_code, f_milk)
            if first_w > width:
                continue

        return f_name, f_code, f_milk, size

    base = COMMON_MIN_FONT
    return (
        font(FONT_BLACK_PATH, base),
        font(FONT_BOLD_PATH, base),
        font(FONT_BOLD_PATH, max(base, int(round(base * MILK_FONT_SCALE)))),
        base,
    )


def split_primary_and_overflow(draw, segments, code_font, milk_font, width):
    """Pack as many complete modifier blocks as possible onto line 2."""
    line2, line3 = [], []
    used = 0

    for seg in segments:
        seg_w = segment_width(draw, seg, code_font, milk_font)
        gap = SEGMENT_GAP_DOTS if line2 else 0
        if not line3 and (not line2 or used + gap + seg_w <= width):
            if line2:
                used += SEGMENT_GAP_DOTS
            line2.append(seg.copy())
            used += seg_w
        else:
            line3.append(seg.copy())

    return line2, line3


def choose_overflow_fonts(draw, segments, width):
    """Line 3 gets its own largest possible font, independent of line 2."""
    if not segments:
        return None, None, None

    for size in range(COMMON_MAX_FONT, COMMON_MIN_FONT - 1, -1):
        f_code = font(FONT_BOLD_PATH, size)
        f_milk = font(FONT_BOLD_PATH, max(size, int(round(size * MILK_FONT_SCALE))))
        total = 0
        for idx, seg in enumerate(segments):
            total += segment_width(draw, seg, f_code, f_milk)
            if idx < len(segments) - 1:
                total += SEGMENT_GAP_DOTS
        if total <= width:
            return f_code, f_milk, size

    # Absolute fallback: keep line 2 unchanged and shrink only line 3.
    for size in range(COMMON_MIN_FONT - 1, 11, -1):
        f_code = font(FONT_BOLD_PATH, size)
        f_milk = font(FONT_BOLD_PATH, max(size, int(round(size * MILK_FONT_SCALE))))
        total = 0
        for idx, seg in enumerate(segments):
            total += segment_width(draw, seg, f_code, f_milk)
            if idx < len(segments) - 1:
                total += SEGMENT_GAP_DOTS
        if total <= width:
            return f_code, f_milk, size

    return font(FONT_BOLD_PATH, 12), font(FONT_BOLD_PATH, max(12, int(round(12 * MILK_FONT_SCALE)))), 12


def line_font_metrics(line, code_font, milk_font):
    ascents, descents = [], []
    for seg in line:
        use_font = milk_font if seg.get("milk") else code_font
        try:
            ascent, descent = use_font.getmetrics()
        except AttributeError:
            ascent, descent = use_font.size, 0
        ascents.append(ascent)
        descents.append(descent)
    max_ascent = max(ascents) if ascents else code_font.size
    max_descent = max(descents) if descents else 0
    return max_ascent, max_descent, max_ascent + max_descent


def draw_code_line(draw, line, y, code_font, milk_font):
    max_ascent, max_descent, line_h = line_font_metrics(line, code_font, milk_font)
    baseline_y = y + max_ascent
    x = 0
    for idx, seg in enumerate(line):
        txt = seg["text"]
        use_font = milk_font if seg.get("milk") else code_font
        seg_w, _ = metrics(draw, txt, use_font)
        if seg.get("milk"):
            draw.rectangle([x, y, x + seg_w - 1, y + line_h - 1], fill=0)
            draw.text((x, baseline_y), txt, fill=255, font=use_font, anchor="ls")
        else:
            draw.text((x, baseline_y), txt, fill=0, font=use_font, anchor="ls")
        x += seg_w
        if idx < len(line) - 1:
            x += SEGMENT_GAP_DOTS
    return line_h


def render_label(name: str, base_item_text: str, modifiers: list[dict]) -> bytes:
    scratch = Image.new("L", (PRINT_WIDTH, 1000), 255)
    d = ImageDraw.Draw(scratch)

    segments = [{"text": base_item_text, "milk": False}] + modifiers

    # Lines 1 + 2: biggest shared font. Line 2 is never reduced simply to
    # avoid overflow; extra modifier blocks are sent to line 3.
    name_font, code_font, milk_font, _ = choose_primary_fonts(d, name, segments, PRINT_WIDTH)
    line2, overflow = split_primary_and_overflow(d, segments, code_font, milk_font, PRINT_WIDTH)

    # Line 3: independent maximum font, only when required.
    overflow_font = overflow_milk_font = None
    if overflow:
        overflow_font, overflow_milk_font, _ = choose_overflow_fonts(d, overflow, PRINT_WIDTH)

    _, name_h = metrics(d, name, name_font)
    line2_h = line_font_metrics(line2, code_font, milk_font)[2] if line2 else 0
    line3_h = line_font_metrics(overflow, overflow_font, overflow_milk_font)[2] if overflow else 0

    content_h = name_h + LINE_GAP_DOTS + line2_h
    if overflow:
        content_h += line3_h

    final_h = max(MIN_HEIGHT, content_h)
    img = Image.new("L", (PRINT_WIDTH, final_h), 255)
    draw = ImageDraw.Draw(img)

    draw.text((0, 0), name, fill=0, font=name_font, anchor="lt")
    y = name_h + LINE_GAP_DOTS

    if line2:
        y += draw_code_line(draw, line2, y, code_font, milk_font)

    if overflow:
        draw_code_line(draw, overflow, y, overflow_font, overflow_milk_font)

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
        base = (item.get("name") or "").strip()

        mods = []
        for m in item.get("modifiers") or []:
            text = (m.get("name") or "").strip()
            if not text:
                continue
            is_milk = False
            modifier_id = m.get("catalog_object_id")
            if MILK_MODIFIER_LIST_IDS and modifier_id:
                mlid = await modifier_list_id(modifier_id)
                is_milk = (mlid in MILK_MODIFIER_LIST_IDS)
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

    if event_seen(event_id):
        return {"ok": True, "duplicate": True}

    typ = payload.get("type")
    payment = (((payload.get("data") or {}).get("object") or {}).get("payment") or {})

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


@app.post("/cloudprnt")
async def cloudprnt_poll(request: Request):
    if not printer_allowed(request):
        raise HTTPException(status_code=401, detail="Unknown printer")

    job = next_job()
    if not job:
        return {"jobReady": False}

    return {
        "jobReady": True,
        "mediaTypes": ["image/png"],
        "jobToken": job["id"],
        "deleteMethod": "DELETE"
    }


@app.get("/cloudprnt")
async def cloudprnt_get(request: Request):
    if not printer_allowed(request):
        raise HTTPException(status_code=401, detail="Unknown printer")

    token = (
        request.headers.get("x-star-token")
        or request.query_params.get("token")
        or request.query_params.get("jobToken")
    )
    if not token:
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


@app.delete("/cloudprnt")
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
        ready = conn.execute(
            "SELECT COUNT(*) c FROM print_jobs WHERE state='READY'"
        ).fetchone()["c"]

    return {
        "ok": True,
        "ready_jobs": ready,
        "width_dots": PRINT_WIDTH,
        "min_height_dots": MIN_HEIGHT,
        "version": "0.6.0"
    }


@app.get("/preview")
def preview(
    name: str = "JAMIE",
    code: str = "LCAC2eq",
    milk_start: int = 2,
    milk_len: int = 1
):
    before = code[:milk_start]
    milk = code[milk_start:milk_start + milk_len]
    after = code[milk_start + milk_len:]

    png = render_label(
        name=name,
        base_item_text=before,
        modifiers=[
            {"text": milk, "milk": True},
            {"text": after, "milk": False},
        ],
    )
    return Response(content=png, media_type="image/png")
