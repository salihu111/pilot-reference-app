import os
import requests
from .db import get_conn

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def create_bulletin(title: str, body: str, severity: str = "info"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bulletins (title, body, severity) VALUES (?, ?, ?)",
        (title, body, severity),
    )
    bulletin_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bulletin_id


def list_bulletins(limit: int = 20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bulletins ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_subscriber(chat_id: int, username: str | None = None):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO subscribers (chat_id, username) VALUES (?, ?)",
        (chat_id, username),
    )
    conn.commit()
    conn.close()


def push_bulletin(bulletin_id: int):
    """Send a bulletin to every subscribed chat via the Telegram Bot API,
    then mark it pushed."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM bulletins WHERE id=?", (bulletin_id,)).fetchone()
    subs = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    if not row:
        conn.close()
        return {"error": "bulletin not found"}

    text = f"\U0001F4E2 {row['title']}\n\n{row['body']}"
    sent, failed = 0, 0
    for s in subs:
        try:
            requests.post(
                f"{TG_API}/sendMessage",
                json={"chat_id": s["chat_id"], "text": text},
                timeout=10,
            )
            sent += 1
        except Exception:
            failed += 1

    conn.execute("UPDATE bulletins SET pushed=1 WHERE id=?", (bulletin_id,))
    conn.commit()
    conn.close()
    return {"sent": sent, "failed": failed}
