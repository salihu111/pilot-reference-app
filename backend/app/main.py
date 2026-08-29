import os
import shutil
import tempfile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .db import init_db, get_conn
from .ingest import ingest_pdf, render_page_image
from .rag import answer_question
from .legal_check import check_duty_legality
from .salary import SalaryInput, calculate
from .airports import add_airport, list_airports, sync_airport
from .bulletins import create_bulletin, list_bulletins, push_bulletin, add_subscriber

app = FastAPI(title="Pilot Reference Mini App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your Telegram WebApp origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "/data/screenshots")
ATTACHMENTS_DIR = os.getenv("AIRPORT_ATTACHMENTS_DIR", "/data/airport_attachments")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)


@app.on_event("startup")
def startup():
    init_db()


app.mount("/screenshots", StaticFiles(directory=SCREENSHOT_DIR), name="screenshots")
app.mount("/airport-files", StaticFiles(directory=ATTACHMENTS_DIR), name="airport-files")


# ---------- Documents ----------

@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), title: str = Form(None)):
    dest = os.path.join(UPLOAD_DIR, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    doc_id = ingest_pdf(dest, file.filename, title)
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
    """Full parsed briefing note (SIDs, exit points, ATC phraseology,
    frequencies) plus any attached charts for one airport."""
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
    return {"status": "ok"}
