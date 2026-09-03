import os
import shutil
import asyncio
import json
import time
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from .db import init_db, get_conn
from .ingest import ingest_pdf, render_page_image
from .rag import answer_question
from .legal_check import check_duty_legality
from .salary import SalaryInput, calculate
from .airports import add_airport, list_airports, sync_airport, parse_briefing_fields
from .bulletins import create_bulletin, list_bulletins, push_bulletin, add_subscriber
from .news import get_news
from .marketplace import (
    create_listing, list_listings, list_bids, place_bid,
    get_or_create_thread_for_viewer, get_or_create_thread_with,
    list_threads_for_listing, get_thread, add_message, mark_deal_agreed,
)

app = FastAPI(title="Pilot Reference Mini App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Storage paths are env-overridable so a Railway Volume can be dropped in
# with zero code changes: mount a Volume (e.g. at /data), then set
# DB_PATH=/data/app.db, UPLOAD_DIR=/data/uploads, SCREENSHOT_DIR=/data/screenshots,
# AIRPORT_ATTACHMENTS_DIR=/data/airport_attachments as service Variables.
# Without a Volume, these default to storage/ inside the container, which
# is ephemeral -- fine to run this way, but uploaded PDFs, bulletins, subscriber
# lists, and marketplace posts reset on every redeploy. Airport briefings baked
# into the repo re-seed automatically on boot either way (see startup() below).
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "storage/uploads")
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "storage/screenshots")
ATTACHMENTS_DIR = os.getenv("AIRPORT_ATTACHMENTS_DIR", "storage/airport_attachments")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_webapp_url_raw = os.getenv("WEBAPP_URL")
WEBAPP_URL = None
if _webapp_url_raw:
    WEBAPP_URL = _webapp_url_raw if _webapp_url_raw.startswith("http") else f"https://{_webapp_url_raw}"

telegram_app = None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id, update.effective_user.username if update.effective_user else None)
    if WEBAPP_URL:
        fresh_url = f"{WEBAPP_URL}?v={int(time.time())}"
        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Pilot Reference", web_app=WebAppInfo(url=fresh_url))]]
            )
            await update.message.reply_text(
                "Welcome. Tap below to open the reference dashboard.",
                reply_markup=keyboard,
            )
        except Exception as e:
            await update.message.reply_text(f"Welcome. Open the dashboard here: {fresh_url}\n\n(button error: {e})")
    else:
        await update.message.reply_text(
            "Bot is up, but WEBAPP_URL isn't set yet -- add it in Railway env vars."
        )


def seed_reference_pdfs_blocking():
    """Seed committed reference PDFs in a background thread."""
    try:
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "reference_pdfs")
        if os.path.isdir(data_dir):
            from .seed_reference_pdfs import run as seed_ref_run
            print("Reference PDF auto-seed starting in background...")
            seed_ref_run(data_dir, UPLOAD_DIR)
            print("Reference PDF auto-seed finished.")
    except Exception as e:
        print(f"Reference PDF auto-seed skipped/failed (non-fatal): {e}")


def seed_airports_blocking():
    """Seed airport briefings in a background thread."""
    try:
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) c FROM airports").fetchone()["c"]
        conn.close()
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "airport_briefings")
        if count == 0 and os.path.isdir(data_dir):
            from .seed_airports import run as seed_run
            print("Airport auto-seed starting in background...")
            seed_run(data_dir, ATTACHMENTS_DIR, embed=True)
            print("Airport auto-seed finished.")
    except Exception as e:
        print(f"Airport auto-seed skipped/failed (non-fatal): {e}")


def seed_all_blocking():
    seed_airports_blocking()
    seed_reference_pdfs_blocking()


@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(asyncio.to_thread(seed_all_blocking))
    global telegram_app
    if BOT_TOKEN:
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_cmd))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        print("Telegram bot polling started.")
    else:
        print("TELEGRAM_BOT_TOKEN not set -- bot not started (API still runs).")


@app.on_event("shutdown")
async def shutdown():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app.mount("/screenshots", StaticFiles(directory=SCREENSHOT_DIR), name="screenshots")
app.mount("/airport-files", StaticFiles(directory=ATTACHMENTS_DIR), name="airport-files")


@app.get("/")
def serve_frontend():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/marketplace.html")
def serve_marketplace():
    return FileResponse(
        os.path.join(STATIC_DIR, "marketplace.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ---------- Documents ----------
@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), title: str = Form(None)):
    dest = os.path.join(UPLOAD_DIR, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    doc_id = await asyncio.to_thread(ingest_pdf, dest, file.filename, title)
    return {"doc_id": doc_id, "filename": file.filename}


@app.get("/api/documents")
def get_documents():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/documents/{doc_id}/page/{page_num}/screenshot")
def get_page_screenshot(doc_id: int, page_num: int, highlight: str = ""):
    conn = get_conn()
    row = conn.execute("SELECT filename FROM documents WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "document not found")
    filepath = os.path.join(UPLOAD_DIR, row["filename"])
    out_name = f"{doc_id}_{page_num}.png"
    out_path = os.path.join(SCREENSHOT_DIR, out_name)
    render_page_image(filepath, page_num, out_path, highlight_text=highlight or None)
    return FileResponse(out_path, media_type="image/png")


# ---------- Q&A / cross-document search ----------
@app.post("/api/ask")
def ask(query: str = Form(...), doc_ids: str = Form(None)):
    ids = [int(x) for x in doc_ids.split(",")] if doc_ids else None
    return answer_question(query, doc_ids=ids)


# ---------- Legality / FTL advisory ----------
@app.post("/api/legal-check")
def legal_check(duty_description: str = Form(...), doc_ids: str = Form(None)):
    ids = [int(x) for x in doc_ids.split(",")] if doc_ids else None
    return check_duty_legality(duty_description, doc_ids=ids)


# ---------- Salary calculator ----------
@app.post("/api/salary")
def salary(inp: SalaryInput):
    return calculate(inp)


# ---------- Airports / NOTAM / weather ----------
@app.post("/api/airports")
def create_airport(icao: str = Form(...), iata: str = Form(None), name: str = Form(None),
                   city: str = Form(None), country: str = Form(None),
                   elevation_ft: int = Form(None), notes: str = Form(None)):
    add_airport(icao, iata, name, city, country, elevation_ft, notes)
    return {"ok": True}


@app.get("/api/airports")
def get_airports():
    return list_airports()


@app.get("/api/airports/{icao}/sync")
def sync_one_airport(icao: str):
    return sync_airport(icao)


@app.get("/api/airports/{icao}/briefing")
def get_airport_briefing(icao: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM airports WHERE icao=?", (icao.upper(),)).fetchone()
    attachments = conn.execute(
        "SELECT filename, relpath, filetype FROM airport_attachments WHERE icao=?",
        (icao.upper(),),
    ).fetchall()
    conn.close()
    if not row:
        raise HTTPException(404, "airport not found")
    data = dict(row)
    data["attachments"] = [dict(a) for a in attachments]
    data["fields"] = parse_briefing_fields(row["briefing_md"] or "")
    data["weather"] = json.loads(row["weather_json"]) if row["weather_json"] else None
    data["notams"] = json.loads(row["notam_json"]) if row["notam_json"] else None
    return data


# ---------- FIR-wide reference docs (SIDs / escape routes) ----------
@app.get("/api/references")
def get_references(category: str = None):
    conn = get_conn()
    if category:
        rows = conn.execute(
            "SELECT id, title, category, updated_at FROM references_docs WHERE category=? ORDER BY title",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, category, updated_at FROM references_docs ORDER BY title"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/references/{ref_id}")
def get_reference(ref_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM references_docs WHERE id=?", (ref_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "reference not found")
    return dict(row)


# ---------- Bulletins / push ----------
@app.post("/api/bulletins")
def new_bulletin(title: str = Form(...), body: str = Form(...), severity: str = Form("info")):
    bid = create_bulletin(title, body, severity)
    return {"id": bid}


@app.get("/api/bulletins")
def get_bulletins():
    return list_bulletins()


@app.post("/api/bulletins/{bulletin_id}/push")
def push_one_bulletin(bulletin_id: int):
    return push_bulletin(bulletin_id)


@app.post("/api/subscribe")
def subscribe(chat_id: int = Form(...), username: str = Form(None)):
    add_subscriber(chat_id, username)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok", "bot_running": telegram_app is not None}


# ---------- News ----------
@app.get("/api/news")
def news(limit: int = 30):
    return {"articles": get_news(limit)}


# ---------- Layover Market: listings ----------
@app.get("/api/marketplace/listings")
def get_marketplace_listings(type: str = None, city: str = None, query: str = None, client_id: str = None):
    return list_listings(type=type, city=city, query=query, viewer_client_id=client_id)


@app.post("/api/marketplace/listings")
def post_marketplace_listing(
    type: str = Form(...), title: str = Form(None), description: str = Form(None),
    client_id: str = Form(...),
    price_amount: float = Form(None), price_currency: str = Form(None),
    cur_direction: str = Form(None), cur_amount: float = Form(None),
    cur_rate: float = Form(None), cur_open: bool = Form(False),
    trip_mode: str = Form("none"), trip_city: str = Form(None),
    trip_date: str = Form(None), trip_note: str = Form(None),
    poster_name: str = Form(None), anonymous: bool = Form(False),
    contact_phone: str = Form(None), contact_email: str = Form(None),
    item_mode: str = Form("sell"), amazon_url: str = Form(None),
):
    try:
        listing_id = create_listing(
            type=type, title=title, description=description,
            owner_client_id=client_id,
            price_amount=price_amount, price_currency=price_currency,
            cur_direction=cur_direction, cur_amount=cur_amount,
            cur_rate=cur_rate, cur_open=cur_open,
            trip_mode=trip_mode, trip_city=trip_city, trip_date=trip_date,
            trip_note=trip_note, poster_name=poster_name, anonymous=anonymous,
            contact_phone=contact_phone, contact_email=contact_email,
            item_mode=item_mode, amazon_url=amazon_url,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": listing_id}


# ---------- Layover Market: bid board ----------
@app.get("/api/marketplace/listings/{listing_id}/bids")
def get_marketplace_bids(listing_id: int, client_id: str = None):
    try:
        return list_bids(listing_id, viewer_client_id=client_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/marketplace/listings/{listing_id}/bids")
def post_marketplace_bid(
    listing_id: int, client_id: str = Form(...), rate: float = Form(...),
    bidder_name: str = Form(None), anonymous: bool = Form(False), note: str = Form(None),
    contact_phone: str = Form(None), contact_email: str = Form(None),
):
    try:
        bid_id = place_bid(
            listing_id,
            bidder_client_id=client_id,
            rate=rate,
            bidder_name=bidder_name,
            anonymous=anonymous,
            note=note,
            contact_phone=contact_phone,
            contact_email=contact_email,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": bid_id}


# ---------- Layover Market: chat threads ----------
@app.get("/api/marketplace/listings/{listing_id}/chat")
def get_marketplace_chat(listing_id: int, client_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT owner_client_id FROM marketplace_listings WHERE id=?",
        (listing_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "listing not found")
    if row["owner_client_id"] == client_id:
        return {"is_owner": True, "threads": list_threads_for_listing(listing_id, client_id)}
    return {"is_owner": False, "thread": get_or_create_thread_for_viewer(listing_id, client_id)}


@app.get("/api/marketplace/listings/{listing_id}/thread-with")
def get_marketplace_thread_with(listing_id: int, client_id: str, counterpart_client_id: str):
    try:
        return get_or_create_thread_with(
            listing_id,
            owner_client_id=client_id,
            counterpart_client_id=counterpart_client_id,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/marketplace/threads/{thread_id}")
def get_marketplace_thread(thread_id: int, client_id: str):
    try:
        return get_thread(thread_id, client_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/marketplace/threads/{thread_id}/messages")
def post_marketplace_message(thread_id: int, client_id: str = Form(...), text: str = Form(...)):
    try:
        return add_message(thread_id, client_id, text)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/marketplace/threads/{thread_id}/agree")
def post_marketplace_agree(thread_id: int, client_id: str = Form(...)):
    try:
        return mark_deal_agreed(thread_id, client_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/overview")
def overview():
    conn = get_conn()
    docs = conn.execute("SELECT COUNT(*) c FROM documents WHERE is_text_doc=0").fetchone()["c"]
    airports = conn.execute("SELECT COUNT(*) c FROM airports").fetchone()["c"]
    bulletins_active = conn.execute("SELECT COUNT(*) c FROM bulletins WHERE pushed=0").fetchone()["c"]
    references = conn.execute("SELECT COUNT(*) c FROM references_docs").fetchone()["c"]
    latest_bulletin = conn.execute(
        "SELECT title, created_at FROM bulletins ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "documents": docs,
        "airports": airports,
        "bulletins_active": bulletins_active,
        "references": references,
        "latest_bulletin": dict(latest_bulletin) if latest_bulletin else None,
    }
