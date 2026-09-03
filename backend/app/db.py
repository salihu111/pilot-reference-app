"""Lightweight SQLite layer with startup-only WAL configuration."""
import sqlite3
import os
import json

DB_PATH = os.getenv("DB_PATH", "storage/app.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def get_conn():
    # Do NOT execute PRAGMA journal_mode=WAL on every connection. Changing
    # journal mode can require an exclusive lock and was causing Telegram/API
    # startup and marketplace requests to race with seeders. WAL is configured
    # once by init_db().
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        # Configure WAL once during startup, not on every request/connection.
        current = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if str(current).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        c = conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, title TEXT,
                uploaded_at TEXT DEFAULT (datetime('now')), num_pages INTEGER, is_text_doc INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL, page_num INTEGER NOT NULL,
                text TEXT NOT NULL, embedding BLOB NOT NULL, FOREIGN KEY(doc_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS airports (
                icao TEXT PRIMARY KEY, iata TEXT, icao_real TEXT, name TEXT, city TEXT, country TEXT,
                elevation_ft INTEGER, notes TEXT, briefing_md TEXT, updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS airport_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, icao TEXT NOT NULL, filename TEXT NOT NULL, relpath TEXT NOT NULL,
                filetype TEXT, FOREIGN KEY(icao) REFERENCES airports(icao)
            );
            CREATE TABLE IF NOT EXISTS references_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, category TEXT, body_md TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS bulletins (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL, severity TEXT DEFAULT 'info',
                created_at TEXT DEFAULT (datetime('now')), pushed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS marketplace_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item TEXT NOT NULL, country TEXT,
                amount_usd REAL, exchange_rate_etb REAL, notes TEXT, contact TEXT, poster_name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
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
            except sqlite3.OperationalError as exc:
                # Existing-column errors are expected. Other errors are raised.
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.commit()
    finally:
        conn.close()


def blob_to_vec(blob):
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


def vec_to_blob(vec):
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()
