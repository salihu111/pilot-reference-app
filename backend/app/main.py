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

app = FastAPI(title="Pilot Reference Mini App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# No Railway Volume required: everything lives under storage/ inside the
# container. Trade-off: uploaded PDFs, bulletins, and subscriber lists
# reset on every redeploy. Airport briefings baked into the repo re-seed
# automatically on boot either way (see startup() below), so that part is
# unaffected by not having a Volume.
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "storage/uploads")
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "storage/screenshots")
ATTACHMENTS_DIR = os.getenv("AIRPORT_ATTACHMENTS_DIR", "storage/airport_attachments")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_webapp_url_raw = os.getenv("WEBAPP_URL")  # your Railway public URL
# Telegram rejects web_app buttons without an explicit https:// scheme --
# auto-fix a bare domain instead of crashing /start on a BadRequest.
WEBAPP_URL = None
if _webapp_url_raw:
    WEBAPP_URL = _webapp_url_raw if _webapp_url_raw.startswith("http") else f"https://{_webapp_url_raw}"

telegram_app = None  # set on startup if BOT_TOKEN is present


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id, update.effective_user.username if update.effective_user else None)
    if WEBAPP_URL:
        # Cache-bust every /start: Telegram's WebView caches by URL and
        # ignores our HTTP no-store header, so a static URL can keep
        # serving a stale build even after you redeploy. Appending a
        # fresh query param forces a genuinely new load each time.
        fresh_url = f"{WEBAPP_URL}?v={int(time.time())}"
        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Pilot Reference", web_app=WebAppInfo(url=fresh_url))]]
            )
            await update.message.reply_text("Welcome. Tap below to open the reference dashboard.",
                                             reply_markup=keyboard)
        except Exception as e:
            await update.message.reply_text(f"Welcome. Open the dashboard here: {fresh_url}\n\n(button error: {e})")
    else:
        await update.message.reply_text(
            "Bot is up, but WEBAPP_URL isn't set yet -- add it in Railway env vars."
        )


def seed_reference_pdfs_blocking():
    """Runs in a background thread, same reasoning as seed_airports_blocking
    below: embedding a large PDF (e.g. the FOM) takes longer than Railway's
    startup health-check window, so this must never block app startup.
    Seeds any PDFs committed under data/reference_pdfs (FOM, ops manuals,
    checklists, etc.) straight into the /api/ask vector index -- no manual
    upload through the Documents tab, so nothing is lost or times out on
    redeploy. See seed_reference_pdfs.py for details."""
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
    """Runs in a background thread -- downloading the embedding model and
    embedding 51+ airport briefings takes way longer than Railway's
    startup health-check window, so this must never block app startup."""
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
    """Runs both seed jobs one after another in the SAME background thread,
    instead of two parallel threads. Both jobs do CPU-bound PDF parsing +
    ONNX embedding -- running them concurrently at boot roughly doubles
    peak memory/CPU right when the process is already starting up
    everything else, which is enough to get OOM-killed on a
    memory-constrained Railway plan. Sequential costs a bit more wall-clock
    time to finish indexing, which is fine since neither blocks the API."""
    seed_airports_blocking()
    seed_reference_pdfs_blocking()


@app.on_event("startup")
async def startup():
    init_db()

    # Fire-and-forget: don't await this. The FastAPI/uvicorn "startup
    # complete" signal (which Railway's health check waits on) must return
    # immediately, or Railway kills the container as unresponsive and
    # retries forever -- looking like a silent failure with no traceback.
    asyncio.create_task(asyncio.to_thread(seed_all_blocking))

    # Start the Telegram bot in the SAME process/event loop as the API --
    # one Railway service, one process, nothing extra to manage.
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
    """Serves the Mini App itself -- same service as the API, so there's
    only one URL and one thing to deploy. no-store prevents mobile Safari
    from serving a stale cached copy after you push a frontend update."""
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ---------- Documents ----------

@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), title: str = Form(None)):
    dest = os.path.join(UPLOAD_DIR, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # ingest_pdf is a slow, synchronous, CPU-bound call (PDF parsing +
    # embedding). Running it directly in this async def blocks the whole
    # event loop for the duration -- every other request (including the
    # browser's own GET /api/documents right after) queues up behind it,
    # and on Railway a slow-enough upload can trip the proxy's own request
    # timeout, which returns an HTML/JSON error body instead of the
    # expected array. Push it to a thread so the loop stays free.
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
    """doc_ids: comma-separated list, or omit to search across ALL uploaded docs."""
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
