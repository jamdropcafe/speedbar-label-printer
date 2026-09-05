# Speed Bar — Square → Star TSP100IVSK label engine

This is the first working scaffold for the custom label path we designed.

## What it does

1. Receives `payment.created` / `payment.updated` from Square.
2. Ignores everything until the payment is `COMPLETED`.
3. Reads the `order_id`.
4. Retrieves the full Square order.
5. Uses the Square order `ticket_name` as the customer name when available.
6. Reads each line item and its modifiers.
7. Identifies the milk modifier by its Square **Modifier List ID**.
8. Renders a 40 mm monochrome PNG:
   - customer name on the first line;
   - item/code + all modifiers concatenated;
   - milk modifier in white text on black;
   - largest font that fits;
   - wraps only when needed;
   - no logical margins.
9. Queues the PNG.
10. Serves it to the Star TSP100IVSK through CloudPRNT.
11. Prevents duplicate prints using Square `event_id` and order/line/unit uniqueness.

## Important before production

The physical Star printer setting still controls the real 40 mm printing width and cutter/top-margin behaviour.
`PRINT_WIDTH_DOTS` and `MIN_LABEL_HEIGHT_DOTS` are intentionally configurable until we print a calibration sample on your exact printer.

## Local test

```bash
python -m venv .venv
# activate the environment
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

`http://localhost:8000/preview?name=JAMIE&code=LCAC2eq&milk_start=2&milk_len=1`

This generates the label renderer without requiring Square or the printer.

## Square setup

Create a Square Developer application and use production credentials only when ready.

Required permissions:
- `PAYMENTS_READ`
- `ORDERS_READ`
- `ITEMS_READ`

Webhook event types:
- `payment.created`
- `payment.updated`

Notification URL:
- `https://YOUR-PUBLIC-DOMAIN/square/webhook`

Copy the webhook Signature Key into:
- `SQUARE_WEBHOOK_SIGNATURE_KEY`

Set the **exact same public notification URL** in:
- `SQUARE_WEBHOOK_URL`

Set your production token in:
- `SQUARE_ACCESS_TOKEN`

Strongly recommended:
- set `SQUARE_LOCATION_ID` to the Speed Bar location, so Jamdrop or any other location cannot trigger this printer.

## Milk highlighting

Set:
- `MILK_MODIFIER_LIST_ID=<Square ID of the milk modifier list>`

The program looks up the selected modifier's Catalog object and reads its `modifier_list_id`. This means the black highlight remains correct even if you later change `A` to another code.

## Star CloudPRNT

Configure the printer CloudPRNT URL to:

`https://YOUR-PUBLIC-DOMAIN/cloudprnt`

The printer polls this URL. If a job is ready, the service advertises `image/png`, the printer GETs the PNG, prints it, then confirms with DELETE.

Optionally set:
- `STAR_PRINTER_MAC`

to prevent another Star printer from pulling Speed Bar jobs.

## Next calibration

We will calibrate these values on the real printer:
- `PRINT_WIDTH_DOTS`
- `MIN_LABEL_HEIGHT_DOTS`
- `NAME_MAX_FONT_PX`
- `CODE_MAX_FONT_PX`
- `MIN_CODE_FONT_PX`

Do not tune them by guessing. Print a calibration sample first.
