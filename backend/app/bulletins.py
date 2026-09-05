"""Bulletins and Telegram subscriber helpers with SQLite lock protection."""
from __future__ import annotations

import os
import time
import sqlite3

from .db import get_conn


def _with_retry(fn, attempts: int = 8, delay: float = 0.75):
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if "database is locked" not in str(exc).lower():
                raise
            if attempt < attempts - 1:
                time.sleep(delay * (attempt + 1))
    raise last


def create_bulletin(title: str, body: str, severity: str = "info") -> int:
    def work():
        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO bulletins (title, body, severity) VALUES (?, ?, ?)",
                (title, body, severity),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    return _with_retry(work)


def list_bulletins():
    def work():
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM bulletins ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    return _with_retry(work)


def add_subscriber(chat_id: int, username: str | None = None):
    """Register a Telegram chat without allowing a transient SQLite lock to break /start."""
    def work():
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id, username) VALUES (?, ?)",
                (chat_id, username),
            )
            # Keep the username current when Telegram provides one.
            if username:
                conn.execute(
                    "UPDATE subscribers SET username=? WHERE chat_id=?",
                    (username, chat_id),
                )
            conn.commit()
        finally:
            conn.close()
    return _with_retry(work)


def push_bulletin(bulletin_id: int):
    """Push one bulletin to registered subscribers and mark it pushed."""
    conn = get_conn()
    try:
        bulletin = conn.execute(
            "SELECT * FROM bulletins WHERE id=?", (bulletin_id,)
        ).fetchone()
        subscribers = conn.execute(
            "SELECT chat_id FROM subscribers"
        ).fetchall()
    finally:
        conn.close()

    if not bulletin:
        return {"ok": False, "error": "bulletin not found"}

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is not set"}

    try:
        from telegram import Bot
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    import asyncio

    async def send_all():
        bot = Bot(token=token)
        sent = 0
        failed = 0
        async with bot:
            for row in subscribers:
                try:
                    await bot.send_message(
                        chat_id=row["chat_id"],
                        text=f"📢 {bulletin['title']}\n\n{bulletin['body']}",
                    )
                    sent += 1
                except Exception:
                    failed += 1
        return sent, failed

    try:
        sent, failed = asyncio.run(send_all())
    except RuntimeError:
        # If called from an active event loop, use a temporary thread.
        import threading
        result = {}
        def runner():
            result["value"] = asyncio.run(send_all())
        t = threading.Thread(target=runner)
        t.start(); t.join()
        sent, failed = result["value"]

    def mark():
        c = get_conn()
        try:
            c.execute("UPDATE bulletins SET pushed=1 WHERE id=?", (bulletin_id,))
            c.commit()
        finally:
            c.close()
    _with_retry(mark)
    return {"ok": True, "sent": sent, "failed": failed}
