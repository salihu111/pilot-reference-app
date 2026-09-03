"""
Crew classifieds: "I need X from country Y", "selling USD cash at rate Z
ETB", etc. Same simple create+list pattern as bulletins.py.

Note (see db.py): without a Railway Volume, this resets on every redeploy
like everything else in SQLite here. Fine for a rolling "what's currently
being offered" board; not a permanent record.
"""
from .db import get_conn

VALID_KINDS = {"buy", "sell", "currency"}


def create_post(kind: str, item: str, country: str | None = None,
                 amount_usd: float | None = None, exchange_rate_etb: float | None = None,
                 notes: str | None = None, contact: str | None = None,
                 poster_name: str | None = None) -> int:
    if kind not in VALID_KINDS:
        kind = "sell"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO marketplace_posts "
        "(kind, item, country, amount_usd, exchange_rate_etb, notes, contact, poster_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, item, country, amount_usd, exchange_rate_etb, notes, contact, poster_name),
    )
    post_id = cur.lastrowid
    conn.commit()
    conn.close()
    return post_id


def list_posts(limit: int = 100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM marketplace_posts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
