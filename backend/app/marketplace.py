"""Pilot Reference Crew Market backend.

Designed for the existing FastAPI main.py marketplace API.
- Creates marketplace tables once (not on every request) to avoid SQLite lock storms.
- Uses the existing get_conn() WAL/busy-timeout connection helper.
- Keeps phone/email private until a participant marks a deal agreed.
- Supports currency bids and bid retrieval.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from .db import get_conn

_schema_lock = threading.Lock()
_schema_ready = False


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(value, max_len=4000):
    if value is None:
        return None
    value = str(value).strip()
    return value[:max_len] if value else None


def _ensure_schema():
    """Create marketplace tables once, not once per request.

    The old implementation executed CREATE TABLE/CREATE INDEX + COMMIT on every
    listing/chat call. Under Telegram polling + FastAPI + background indexing,
    that pattern can create unnecessary SQLite writer contention.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = get_conn()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS marketplace_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('buy','sell','currency','trip','service')),
                title TEXT NOT NULL,
                description TEXT,
                owner_client_id TEXT NOT NULL,
                price_amount REAL,
                price_currency TEXT,
                cur_direction TEXT,
                cur_amount REAL,
                cur_rate REAL,
                cur_open INTEGER NOT NULL DEFAULT 0,
                trip_mode TEXT DEFAULT 'none',
                trip_city TEXT,
                trip_date TEXT,
                trip_note TEXT,
                poster_name TEXT,
                anonymous INTEGER NOT NULL DEFAULT 0,
                contact_phone TEXT,
                contact_email TEXT,
                item_mode TEXT DEFAULT 'sell',
                amazon_url TEXT,
                is_demo INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_listings_status_created
                ON marketplace_listings(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_marketplace_listings_type
                ON marketplace_listings(type);
            CREATE INDEX IF NOT EXISTS idx_marketplace_listings_city
                ON marketplace_listings(trip_city);

            CREATE TABLE IF NOT EXISTS marketplace_bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                bidder_client_id TEXT NOT NULL,
                rate REAL NOT NULL,
                bidder_name TEXT,
                anonymous INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                contact_phone TEXT,
                contact_email TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY(listing_id) REFERENCES marketplace_listings(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_bids_listing_rate
                ON marketplace_bids(listing_id, rate DESC, created_at ASC);

            CREATE TABLE IF NOT EXISTS marketplace_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                owner_client_id TEXT NOT NULL,
                counterpart_client_id TEXT NOT NULL,
                deal_agreed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(listing_id, counterpart_client_id),
                FOREIGN KEY(listing_id) REFERENCES marketplace_listings(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_threads_listing
                ON marketplace_threads(listing_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS marketplace_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                sender_client_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES marketplace_threads(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_messages_thread
                ON marketplace_messages(thread_id, created_at);
            """)
            # Safe migrations for databases created by older versions.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(marketplace_listings)").fetchall()}
            if "item_mode" not in cols:
                conn.execute("ALTER TABLE marketplace_listings ADD COLUMN item_mode TEXT DEFAULT 'sell'")
            if "amazon_url" not in cols:
                conn.execute("ALTER TABLE marketplace_listings ADD COLUMN amazon_url TEXT")
            if "is_demo" not in cols:
                conn.execute("ALTER TABLE marketplace_listings ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
            bid_cols = {r[1] for r in conn.execute("PRAGMA table_info(marketplace_bids)").fetchall()}
            if "contact_phone" not in bid_cols:
                conn.execute("ALTER TABLE marketplace_bids ADD COLUMN contact_phone TEXT")
            if "contact_email" not in bid_cols:
                conn.execute("ALTER TABLE marketplace_bids ADD COLUMN contact_email TEXT")
            conn.commit()
            _seed_demo_rows(conn)
            _schema_ready = True
        finally:
            conn.close()


def _seed_demo_rows(conn):
    """Ensure visible sample posts exist once. Samples use a reserved demo owner."""
    count = conn.execute("SELECT COUNT(*) FROM marketplace_listings WHERE is_demo=1").fetchone()[0] if 'is_demo' in {r[1] for r in conn.execute('PRAGMA table_info(marketplace_listings)').fetchall()} else None
    if count is None:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        count = 0
    if count:
        return
    now = _now()
    demos = [
        ('currency','USD cash available · Addis','Selling USD at a named ETB rate. Open to crew offers.','demo:crew-market',None,'ETB','sell',500,'155.00',1,'none',None,None,None,'Crew Market example',0,None,None,'sell',None,'active',now,1),
        ('currency','Looking to buy USD · BOM layover','Need USD before the next Mumbai layover. Best rate wins.','demo:crew-market',None,'ETB','buy',300,'153.50',1,'none','Mumbai','2026-09-10',None,'Crew Market example',0,None,None,'sell',None,'active',now,1),
        ('sell','iPhone charger · USB-C','Good condition, easy handover at Addis.','demo:crew-market',25,'ETB',None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',0,None,None,'sell',None,'active',now,1),
        ('buy','Wanted: universal travel adapter','Looking for a compact adapter before a Europe rotation.','demo:crew-market',None,None,None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',0,None,None,'buy',None,'active',now,1),
        ('trip','Anyone with layover Mumbai (BOM) · Sept 10','Can you bring a small item from Mumbai on your next flight?','demo:crew-market',None,None,None,None,None,0,'carry','Mumbai','2026-09-10','Example destination request','Crew Market example',0,None,None,'sell',None,'active',now,1),
        ('trip','Crew on Dubai layover · Sept 12','Looking for someone who can carry a small crew item back.','demo:crew-market',None,None,None,None,None,0,'carry','Dubai','2026-09-12','Example layover match','Crew Market example',0,None,None,'sell',None,'active',now,1),
        ('service','Amazon run · Addis delivery','Post an Amazon link and find crew flying there.','demo:crew-market',None,None,None,None,None,0,'carry','Dubai','2026-09-14','Example Amazon run','Crew Market example',0,None,None,'service','https://www.amazon.com/','active',now,1),
        ('service','Crew luggage help','Example crew-to-crew service request.','demo:crew-market',None,None,None,None,None,0,'none','Addis Ababa',None,'Example service','Crew Market example',0,None,None,'service',None,'active',now,1),
        ('sell','Crew meal prep box','Example pay-it-forward item for the next rotation.','demo:crew-market',None,None,None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',0,None,None,'free',None,'active',now,1),
        ('buy','Wanted: spare power bank','Example wanted post for a layover.','demo:crew-market',None,None,None,None,None,0,'none','Istanbul',None,None,'Crew Market example',0,None,None,'buy',None,'active',now,1),
    ]
    conn.executemany("""INSERT INTO marketplace_listings
      (type,title,description,owner_client_id,price_amount,price_currency,cur_direction,cur_amount,cur_rate,cur_open,trip_mode,trip_city,trip_date,trip_note,poster_name,anonymous,contact_phone,contact_email,item_mode,amazon_url,status,created_at,is_demo)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", demos)
    conn.commit()


def create_listing(*, type: str, title: str, description: str = None,
                   owner_client_id: str, price_amount: float = None,
                   price_currency: str = None, cur_direction: str = None,
                   cur_amount: float = None, cur_rate: float = None,
                   cur_open: bool = False, trip_mode: str = "none",
                   trip_city: str = None, trip_date: str = None,
                   trip_note: str = None, poster_name: str = None,
                   anonymous: bool = False, contact_phone: str = None,
                   contact_email: str = None, item_mode: str = "sell",
                   amazon_url: str = None) -> int:
    _ensure_schema()
    kind = (_clean(type, 30) or '').lower()
    if kind not in {"buy", "sell", "currency", "trip", "service"}:
        raise ValueError("Invalid marketplace type.")
    title = _clean(title, 180)
    owner = _clean(owner_client_id, 160)
    if not title:
        raise ValueError("A listing title is required.")
    if not owner:
        raise ValueError("A client id is required.")
    if kind == 'currency':
        if cur_amount not in (None, '') and float(cur_amount) <= 0:
            raise ValueError("Currency amount must be greater than zero.")
        if cur_rate not in (None, '') and float(cur_rate) <= 0:
            raise ValueError("Currency rate must be greater than zero.")
    conn = get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO marketplace_listings
            (type,title,description,owner_client_id,price_amount,price_currency,
             cur_direction,cur_amount,cur_rate,cur_open,trip_mode,trip_city,trip_date,
             trip_note,poster_name,anonymous,contact_phone,contact_email,item_mode,amazon_url,status,created_at,is_demo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (kind,title,_clean(description),owner,price_amount,_clean(price_currency,12),
              _clean(cur_direction,30),cur_amount,cur_rate,1 if cur_open else 0,
              _clean(trip_mode,30) or 'none',_clean(trip_city,120),_clean(trip_date,40),
              _clean(trip_note,1000),_clean(poster_name,120),1 if anonymous else 0,
              _clean(contact_phone,80),_clean(contact_email,180),
              _clean(item_mode,30) or 'sell',_clean(amazon_url,1000),
              'active',_now(),0))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_listings(*, type: str = None, city: str = None, query: str = None,
                  viewer_client_id: str = None):
    _ensure_schema()
    clauses = ["(l.status='active' OR (l.owner_client_id=? AND l.status IN ('closed','archived')))"]
    params = [viewer_client_id or '']
    if type and type.lower() in {"buy","sell","currency","trip","service"}:
        clauses.append('l.type=?'); params.append(type.lower())
    if city and city.strip():
        clauses.append("LOWER(COALESCE(l.trip_city,'')) LIKE ?")
        params.append('%'+city.strip().lower()+'%')
    if query and query.strip():
        q='%'+query.strip().lower()+'%'
        clauses.append("(LOWER(l.title) LIKE ? OR LOWER(COALESCE(l.description,'')) LIKE ? OR LOWER(COALESCE(l.trip_city,'')) LIKE ? OR LOWER(COALESCE(l.trip_note,'')) LIKE ?)")
        params.extend([q,q,q,q])
    conn=get_conn()
    try:
        rows=conn.execute(f"""
          SELECT l.*,
            (SELECT COUNT(*) FROM marketplace_bids b WHERE b.listing_id=l.id) bid_count,
            (SELECT COUNT(*) FROM marketplace_messages m JOIN marketplace_threads t ON t.id=m.thread_id WHERE t.listing_id=l.id) message_count
          FROM marketplace_listings l
          WHERE {' AND '.join(clauses)}
          ORDER BY CASE WHEN l.type='trip' THEN 0 WHEN l.type='currency' THEN 1 ELSE 2 END, l.created_at DESC
          LIMIT 200
        """,params).fetchall()
        out=[]
        for row in rows:
            item=dict(row)
            item['is_owner']=bool(viewer_client_id and row['owner_client_id']==viewer_client_id)
            if not item['is_owner']:
                item['contact_phone']=None; item['contact_email']=None
            item['anonymous']=bool(item['anonymous']); item['cur_open']=bool(item['cur_open'])
            out.append(item)
        return out
    finally: conn.close()


def list_bids(listing_id: int, viewer_client_id: str = None):
    _ensure_schema()
    conn=get_conn()
    try:
        listing=conn.execute("SELECT owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        rows=conn.execute("""
          SELECT id,listing_id,bidder_client_id,rate,bidder_name,anonymous,note,contact_phone,contact_email,status,created_at
          FROM marketplace_bids WHERE listing_id=? ORDER BY rate DESC, created_at ASC
        """,(listing_id,)).fetchall()
        out=[]
        for r in rows:
            x=dict(r); x['anonymous']=bool(x['anonymous'])
            # Contact details stay locked until the associated chat deal is agreed.
            x['contact_phone']=None; x['contact_email']=None
            # Never expose the bidder's client identifier to non-owners.
            if viewer_client_id != listing['owner_client_id']:
                x['bidder_client_id']=None
            out.append(x)
        return out
    finally: conn.close()


def place_bid(listing_id: int, *, bidder_client_id: str, rate: float,
              bidder_name: str = None, anonymous: bool = False, note: str = None,
              contact_phone: str = None, contact_email: str = None) -> int:
    _ensure_schema()
    bidder=_clean(bidder_client_id,160)
    if not bidder: raise ValueError('A client id is required.')
    try: rate_value=float(rate)
    except (TypeError,ValueError): raise ValueError('Bid/rate must be a number.')
    if rate_value<=0: raise ValueError('Bid/rate must be greater than zero.')
    conn=get_conn()
    try:
        listing=conn.execute("SELECT id,owner_client_id,status FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['status']!='active': raise ValueError('This listing is no longer active.')
        if listing['owner_client_id']==bidder: raise ValueError('You cannot bid on your own listing.')
        cur=conn.execute("""INSERT INTO marketplace_bids
          (listing_id,bidder_client_id,rate,bidder_name,anonymous,note,contact_phone,contact_email,status,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?)""",(listing_id,bidder,rate_value,_clean(bidder_name,120),1 if anonymous else 0,_clean(note,1000),_clean(contact_phone,80),_clean(contact_email,180),'pending',_now()))
        conn.commit(); return int(cur.lastrowid)
    finally: conn.close()


def _get_listing_contacts(conn, listing_id):
    return conn.execute("SELECT contact_phone,contact_email,poster_name,anonymous FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()

def _get_bid_contacts(conn, listing_id, counterpart_client_id):
    return conn.execute("SELECT contact_phone,contact_email,bidder_name,anonymous FROM marketplace_bids WHERE listing_id=? AND bidder_client_id=? ORDER BY created_at DESC LIMIT 1",(listing_id,counterpart_client_id)).fetchone()

def _thread_payload(conn,row,include_messages=True):
    payload=dict(row); payload['deal_agreed']=bool(payload['deal_agreed'])
    listing_meta=conn.execute(
        "SELECT title,type FROM marketplace_listings WHERE id=?",
        (row['listing_id'],),
    ).fetchone()
    if listing_meta:
        payload['listing_title']=listing_meta['title']
        payload['listing_type']=listing_meta['type']
    if payload['deal_agreed']:
        if row['counterpart_client_id'] == row['owner_client_id']:
            contacts=_get_listing_contacts(conn,row['listing_id'])
        else:
            contacts=_get_bid_contacts(conn,row['listing_id'],row['counterpart_client_id'])
        if contacts:
            payload['contact_phone']=contacts['contact_phone']
            payload['contact_email']=contacts['contact_email']
    else:
        payload['contact_phone']=None; payload['contact_email']=None
    if include_messages:
        messages=conn.execute("SELECT id,sender_client_id,text,created_at FROM marketplace_messages WHERE thread_id=? ORDER BY created_at ASC LIMIT 500",(row['id'],)).fetchall()
        payload['messages']=[dict(m) for m in messages]
    return payload

def get_listing_info(listing_id:int):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT id,title,type,owner_client_id,status,cur_direction,cur_rate FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not row: raise ValueError('Listing not found.')
        return dict(row)
    finally: conn.close()

def close_listing(listing_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT owner_client_id,status FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not row: raise ValueError('Listing not found.')
        if row['owner_client_id']!=client_id: raise PermissionError('Only the listing owner can close bidding.')
        conn.execute("UPDATE marketplace_listings SET status='closed' WHERE id=?",(listing_id,)); conn.commit()
        return {'ok':True,'status':'closed'}
    finally: conn.close()

def archive_listing(listing_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not row: raise ValueError('Listing not found.')
        if row['owner_client_id']!=client_id: raise PermissionError('Only the listing owner can archive this post.')
        conn.execute("UPDATE marketplace_listings SET status='archived' WHERE id=?",(listing_id,)); conn.commit()
        return {'ok':True,'status':'archived'}
    finally: conn.close()


def get_or_create_thread_for_viewer(listing_id:int,client_id:str):
    _ensure_schema(); client_id=_clean(client_id,160)
    if not client_id: raise ValueError('A client id is required.')
    conn=get_conn()
    try:
        listing=conn.execute("SELECT id,owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']==client_id: raise ValueError('Listing owner should use the owner inbox.')
        row=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? AND counterpart_client_id=?",(listing_id,client_id)).fetchone()
        created=False
        if not row:
            now=_now()
            cur=conn.execute("INSERT INTO marketplace_threads(listing_id,owner_client_id,counterpart_client_id,created_at,updated_at) VALUES(?,?,?,?,?)",(listing_id,listing['owner_client_id'],client_id,now,now))
            conn.commit(); row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(cur.lastrowid,)).fetchone()
            created=True
        payload=_thread_payload(conn,row)
        payload['_created']=created
        return payload
    finally: conn.close()


def get_or_create_thread_with(listing_id:int,*,owner_client_id:str,counterpart_client_id:str):
    _ensure_schema(); owner=_clean(owner_client_id,160); counterpart=_clean(counterpart_client_id,160)
    conn=get_conn()
    try:
        listing=conn.execute("SELECT id,owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']!=owner: raise PermissionError('Only the listing owner can open bidder threads.')
        row=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? AND counterpart_client_id=?",(listing_id,counterpart)).fetchone()
        created=False
        if not row:
            now=_now(); cur=conn.execute("INSERT INTO marketplace_threads(listing_id,owner_client_id,counterpart_client_id,created_at,updated_at) VALUES(?,?,?,?,?)",(listing_id,owner,counterpart,now,now)); conn.commit(); row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(cur.lastrowid,)).fetchone()
            created=True
        payload=_thread_payload(conn,row)
        payload['_created']=created
        return payload
    finally: conn.close()


def list_threads_for_listing(listing_id:int,owner_client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        listing=conn.execute("SELECT owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']!=owner_client_id: raise PermissionError('Only the listing owner can view the inbox.')
        rows=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? ORDER BY updated_at DESC",(listing_id,)).fetchall()
        return [_thread_payload(conn,r,include_messages=False) for r in rows]
    finally: conn.close()


def get_thread(thread_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(thread_id,)).fetchone()
        if not row: raise ValueError('Thread not found.')
        if client_id not in (row['owner_client_id'],row['counterpart_client_id']): raise PermissionError('You are not a participant in this thread.')
        return _thread_payload(conn,row)
    finally: conn.close()


def add_message(thread_id:int,client_id:str,text:str):
    _ensure_schema(); client=_clean(client_id,160); message=_clean(text,3000)
    if not client: raise ValueError('A client id is required.')
    if not message: raise ValueError('Message cannot be empty.')
    conn=get_conn()
    try:
        row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(thread_id,)).fetchone()
        if not row: raise ValueError('Thread not found.')
        if client not in (row['owner_client_id'],row['counterpart_client_id']): raise PermissionError('You are not a participant in this thread.')
        now=_now(); cur=conn.execute("INSERT INTO marketplace_messages(thread_id,sender_client_id,text,created_at) VALUES(?,?,?,?)",(thread_id,client,message,now)); conn.execute("UPDATE marketplace_threads SET updated_at=? WHERE id=?",(now,thread_id)); conn.commit()
        recipient = row['counterpart_client_id'] if client == row['owner_client_id'] else row['owner_client_id']
        listing = conn.execute("SELECT title FROM marketplace_listings WHERE id=?", (row['listing_id'],)).fetchone()
        return {'id':int(cur.lastrowid),'created_at':now,'text':message,
                'recipient_client_id':recipient,'listing_id':row['listing_id'],
                'listing_title':listing['title'] if listing else 'Crew Market'}
    finally: conn.close()


def mark_deal_agreed(thread_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(thread_id,)).fetchone()
        if not row: raise ValueError('Thread not found.')
        if client_id not in (row['owner_client_id'],row['counterpart_client_id']): raise PermissionError('You are not a participant in this thread.')
        now=_now(); conn.execute("UPDATE marketplace_threads SET deal_agreed=1,updated_at=? WHERE id=?",(now,thread_id)); conn.commit()
        return {'ok':True,'deal_agreed':True}
    finally: conn.close()
