"""
Lightweight SQLite layer. Storage location is controlled by DB_PATH (env
var), defaulting to a local `storage/` folder inside the container.

Without a Railway Volume mounted, storage/ is ephemeral -- everything
under it (uploaded PDFs, bulletins, subscribers, marketplace posts) resets
on every redeploy. Airport briefings baked into the repo re-seed
automatically on every boot regardless, so that part is unaffected either
way.

To make storage persistent: attach a Railway Volume to this service (e.g.
mounted at /data), then set DB_PATH=/data/app.db (and UPLOAD_DIR,
SCREENSHOT_DIR, AIRPORT_ATTACHMENTS_DIR similarly -- see main.py) as
service Variables. No code changes needed beyond that -- every path here
already reads from an env var first.
"""
import sqlite3
import os
import json

DB_PATH = os.getenv("DB_PATH", "storage/app.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_conn():
    # WAL mode lets a writer (e.g. the background airport-seeding thread)
    # and another writer (e.g. the bot recording a subscriber) coexist
    # without "database is locked" -- busy_timeout makes SQLite retry for
    # up to 30s instead of failing instantly if a brief lock does happen.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            title TEXT,
            uploaded_at TEXT DEFAULT (datetime('now')),
            num_pages INTEGER,
            is_text_doc INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            FOREIGN KEY(doc_id) REFERENCES documents(id)
        );

        CREATE TABLE IF NOT EXISTS airports (
            icao TEXT PRIMARY KEY,   -- the short code used as the key (often IATA, ICAO where known)
            iata TEXT,
            icao_real TEXT,          -- 4-letter ICAO parsed from the briefing, when found
            name TEXT,
            city TEXT,
            country TEXT,            -- raw flag emoji from the source note, if present
            elevation_ft INTEGER,
            notes TEXT,
            briefing_md TEXT,        -- full original briefing markdown (SIDs, exits, freqs, ATC phraseology)
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS airport_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            filename TEXT NOT NULL,
            relpath TEXT NOT NULL,
            filetype TEXT,
            FOREIGN KEY(icao) REFERENCES airports(icao)
        );

        -- FIR-wide reference docs that aren't tied to one airport,
        -- e.g. "Addis FIR Escape Routes from exit points", "Addis SID from exit point"
        CREATE TABLE IF NOT EXISTS references_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT,           -- 'escape_route' | 'sid' | 'other'
            body_md TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bulletins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            created_at TEXT DEFAULT (datetime('now')),
            pushed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TEXT DEFAULT (datetime('now'))
        );

        -- Crew classifieds: "I need X from country Y", "selling USD at rate Z ETB", etc.
        CREATE TABLE IF NOT EXISTS marketplace_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,              -- 'buy' | 'sell' | 'currency'
            item TEXT NOT NULL,              -- what they want/have, e.g. "iPhone charger", "USD cash"
            country TEXT,                    -- where it's from / where they are
            amount_usd REAL,                 -- optional, for currency posts
            exchange_rate_etb REAL,          -- optional, ETB per 1 USD being offered
            notes TEXT,
            contact TEXT,                    -- how to reach them (Telegram @handle, phone, etc.)
            poster_name TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # Safe no-op migrations if an older DB already exists without these columns
    for stmt in (
        "ALTER TABLE airports ADD COLUMN icao_real TEXT",
        "ALTER TABLE airports ADD COLUMN briefing_md TEXT",
        "ALTER TABLE documents ADD COLUMN is_text_doc INTEGER DEFAULT 0",
        "ALTER TABLE airports ADD COLUMN weather_json TEXT",
        "ALTER TABLE airports ADD COLUMN notam_json TEXT",
        "ALTER TABLE airports ADD COLUMN wx_updated_at TEXT",
    ):
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def blob_to_vec(blob):
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


def vec_to_blob(vec):
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()
