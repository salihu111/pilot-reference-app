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
import time
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


def _with_retry(fn, attempts=8, base_delay=0.15):
    """Run a DB operation and retry transient SQLite writer contention."""
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))
    raise last


def retry_sqlite(fn):
    """Retry a complete marketplace operation on transient SQLite lock/busy errors."""
    def wrapped(*args, **kwargs):
        return _with_retry(lambda: fn(*args, **kwargs), attempts=6, base_delay=0.2)
    wrapped.__name__ = getattr(fn, '__name__', 'wrapped')
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _ensure_schema():
    """Create/migrate marketplace tables once and seed visible demo posts."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        def setup():
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
                cols = {r[1] for r in conn.execute("PRAGMA table_info(marketplace_listings)").fetchall()}
                if "item_mode" not in cols:
                    conn.execute("ALTER TABLE marketplace_listings ADD COLUMN item_mode TEXT DEFAULT 'sell'")
                if "amazon_url" not in cols:
                    conn.execute("ALTER TABLE marketplace_listings ADD COLUMN amazon_url TEXT")
                if "is_demo" not in cols:
                    conn.execute("ALTER TABLE marketplace_listings ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
                bcols = {r[1] for r in conn.execute("PRAGMA table_info(marketplace_bids)").fetchall()}
                if "contact_phone" not in bcols:
                    conn.execute("ALTER TABLE marketplace_bids ADD COLUMN contact_phone TEXT")
                if "contact_email" not in bcols:
                    conn.execute("ALTER TABLE marketplace_bids ADD COLUMN contact_email TEXT")
                conn.commit()
            finally:
                conn.close()
        _with_retry(setup)
        _schema_ready = True
        _seed_demo_listings()


def _seed_demo_listings():
    """Seed examples only when this database has no demo posts."""
    def seed():
        conn = get_conn()
        try:
            exists = conn.execute("SELECT 1 FROM marketplace_listings WHERE is_demo=1 LIMIT 1").fetchone()
            if exists:
                return
            now = _now()
            samples = [
                ('currency','USD available in Addis','Selling USD cash in Addis. Named rate shown; crew can still make offers.','demo:currency:1',None,None,'sell',1200,155,1,'none',None,None,None,'Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('currency','Looking for USD before my trip','Buying USD for an upcoming trip. Best rate wins the conversation.','demo:currency:2',None,None,'buy',800,154,1,'none',None,None,None,'Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('sell','Boeing 737 headset case','Good condition, useful for line flying. Handover in Addis.','demo:item:sell:1',1200,'ETB',None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('sell','Universal travel adapter','Compact adapter, barely used. Happy to swap for a new USB-C charger.','demo:item:sell:2',900,'ETB',None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',1,None,None,'swap',None,1,'active',now),
                ('buy','Looking for compact power bank','Need a reliable 10,000–20,000 mAh power bank before my next rotation.','demo:item:buy:1',None,None,None,None,None,0,'none','Addis Ababa',None,None,'Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('buy','Need iPhone charging cable in Mumbai','USB-C cable needed at BOM before departure.','demo:item:buy:2',None,None,None,None,None,0,'none','Mumbai (BOM)','Sept 10',None,'Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('trip','Anyone with layover Mumbai (BOM) · Sept 10','I need a small crew item carried from Mumbai to Addis.','demo:trip:1',None,None,None,None,None,0,'none','Mumbai (BOM)','Sept 10','Can carry a small item back to Addis.','Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('trip','Layover Dubai (DXB) · Sept 14','Have this, tag my layover: happy to bring a small crew essential from DXB.','demo:trip:2',None,None,None,None,None,0,'none','Dubai (DXB)','Sept 14','Tag me if you need something from Dubai.','Crew Market example',1,None,None,'sell',None,1,'active',now),
                ('service','Amazon run to London','I am flying to London and can bring a small Amazon order back.','demo:service:1',None,None,None,None,None,0,'none','London (LHR)','Sept 18',None,'Crew Market example',1,None,None,'sell','https://www.amazon.co.uk/',1,'active',now),
                ('service','Amazon run to Dubai','Taking one small Amazon order on my next DXB rotation.','demo:service:2',None,None,None,None,None,0,'none','Dubai (DXB)','Sept 20',None,'Crew Market example',1,None,None,'sell','https://www.amazon.ae/',1,'active',now),
            ]
            conn.executemany("""INSERT INTO marketplace_listings
                (type,title,description,owner_client_id,price_amount,price_currency,
                 cur_direction,cur_amount,cur_rate,cur_open,trip_mode,trip_city,trip_date,
                 trip_note,poster_name,anonymous,contact_phone,contact_email,item_mode,amazon_url,is_demo,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", samples)
            conn.commit()
        finally:
            conn.close()
    _with_retry(seed)


@retry_sqlite
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

    def write():
        conn = get_conn()
        try:
            cur = conn.execute("""
                INSERT INTO marketplace_listings
                (type,title,description,owner_client_id,price_amount,price_currency,
                 cur_direction,cur_amount,cur_rate,cur_open,trip_mode,trip_city,trip_date,
                 trip_note,poster_name,anonymous,contact_phone,contact_email,item_mode,amazon_url,is_demo,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (kind,title,_clean(description),owner,price_amount,_clean(price_currency,12),
                  _clean(cur_direction,30),cur_amount,cur_rate,1 if cur_open else 0,
                  _clean(trip_mode,30) or 'none',_clean(trip_city,120),_clean(trip_date,40),
                  _clean(trip_note,1000),_clean(poster_name,120),1 if anonymous else 0,
                  _clean(contact_phone,80),_clean(contact_email,180),
                  _clean(item_mode,30) or 'sell',_clean(amazon_url,1000),0,
                  'active',_now()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
    return _with_retry(write)


@retry_sqlite
def list_listings(*, type: str = None, city: str = None, query: str = None,
                  viewer_client_id: str = None):
    _ensure_schema()
    clauses = ["l.status='active'"]
    params = []
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
            item['anonymous']=bool(item['anonymous']); item['cur_open']=bool(item['cur_open']); item['is_demo']=bool(item.get('is_demo',0))
            out.append(item)
        return out
    finally: conn.close()


@retry_sqlite
def list_bids(listing_id: int, viewer_client_id: str = None):
    _ensure_schema()
    conn=get_conn()
    try:
        listing=conn.execute("SELECT owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        rows=conn.execute("""
          SELECT id,listing_id,bidder_client_id,rate,bidder_name,anonymous,note,status,created_at
          FROM marketplace_bids WHERE listing_id=? ORDER BY rate DESC, created_at ASC
        """,(listing_id,)).fetchall()
        out=[]
        for r in rows:
            x=dict(r); x['anonymous']=bool(x['anonymous'])
            # Never expose the bidder's client identifier to non-owners.
            if viewer_client_id != listing['owner_client_id']:
                x['bidder_client_id']=None
            out.append(x)
        return out
    finally: conn.close()


@retry_sqlite
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


def _thread_payload(conn,row,include_messages=True):
    payload=dict(row); payload['deal_agreed']=bool(payload['deal_agreed'])
    # Contact data is only returned after the deal is agreed and only to a participant.
    if payload['deal_agreed']:
        contacts=_get_listing_contacts(conn,row['listing_id'])
        if contacts:
            payload['contact_phone']=contacts['contact_phone']
            payload['contact_email']=contacts['contact_email']
    else:
        payload['contact_phone']=None; payload['contact_email']=None
    if include_messages:
        messages=conn.execute("SELECT id,sender_client_id,text,created_at FROM marketplace_messages WHERE thread_id=? ORDER BY created_at ASC LIMIT 500",(row['id'],)).fetchall()
        payload['messages']=[dict(m) for m in messages]
    return payload


@retry_sqlite
def get_or_create_thread_for_viewer(listing_id:int,client_id:str):
    _ensure_schema(); client_id=_clean(client_id,160)
    if not client_id: raise ValueError('A client id is required.')
    conn=get_conn()
    try:
        listing=conn.execute("SELECT id,owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']==client_id: raise ValueError('Listing owner should use the owner inbox.')
        row=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? AND counterpart_client_id=?",(listing_id,client_id)).fetchone()
        if not row:
            now=_now()
            cur=conn.execute("INSERT INTO marketplace_threads(listing_id,owner_client_id,counterpart_client_id,created_at,updated_at) VALUES(?,?,?,?,?)",(listing_id,listing['owner_client_id'],client_id,now,now))
            conn.commit(); row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(cur.lastrowid,)).fetchone()
        return _thread_payload(conn,row)
    finally: conn.close()


@retry_sqlite
def get_or_create_thread_with(listing_id:int,*,owner_client_id:str,counterpart_client_id:str):
    _ensure_schema(); owner=_clean(owner_client_id,160); counterpart=_clean(counterpart_client_id,160)
    conn=get_conn()
    try:
        listing=conn.execute("SELECT id,owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']!=owner: raise PermissionError('Only the listing owner can open bidder threads.')
        row=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? AND counterpart_client_id=?",(listing_id,counterpart)).fetchone()
        if not row:
            now=_now(); cur=conn.execute("INSERT INTO marketplace_threads(listing_id,owner_client_id,counterpart_client_id,created_at,updated_at) VALUES(?,?,?,?,?)",(listing_id,owner,counterpart,now,now)); conn.commit(); row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(cur.lastrowid,)).fetchone()
        return _thread_payload(conn,row)
    finally: conn.close()


@retry_sqlite
def list_threads_for_listing(listing_id:int,owner_client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        listing=conn.execute("SELECT owner_client_id FROM marketplace_listings WHERE id=?",(listing_id,)).fetchone()
        if not listing: raise ValueError('Listing not found.')
        if listing['owner_client_id']!=owner_client_id: raise PermissionError('Only the listing owner can view the inbox.')
        rows=conn.execute("SELECT * FROM marketplace_threads WHERE listing_id=? ORDER BY updated_at DESC",(listing_id,)).fetchall()
        return [_thread_payload(conn,r,include_messages=False) for r in rows]
    finally: conn.close()


@retry_sqlite
def get_thread(thread_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(thread_id,)).fetchone()
        if not row: raise ValueError('Thread not found.')
        if client_id not in (row['owner_client_id'],row['counterpart_client_id']): raise PermissionError('You are not a participant in this thread.')
        return _thread_payload(conn,row)
    finally: conn.close()


@retry_sqlite
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
        return {'id':int(cur.lastrowid),'created_at':now,'text':message}
    finally: conn.close()


@retry_sqlite
def mark_deal_agreed(thread_id:int,client_id:str):
    _ensure_schema(); conn=get_conn()
    try:
        row=conn.execute("SELECT * FROM marketplace_threads WHERE id=?",(thread_id,)).fetchone()
        if not row: raise ValueError('Thread not found.')
        if client_id not in (row['owner_client_id'],row['counterpart_client_id']): raise PermissionError('You are not a participant in this thread.')
        now=_now(); conn.execute("UPDATE marketplace_threads SET deal_agreed=1,updated_at=? WHERE id=?",(now,thread_id)); conn.commit()
        return {'ok':True,'deal_agreed':True}
    finally: conn.close()
