import os
import json
import requests
from datetime import datetime, timezone
from .db import get_conn

# Free, no-key METAR/TAF source (US gov, covers ICAO stations worldwide)
AWC_BASE = "https://aviationweather.gov/api/data"

# NOTAMs need a key. FAA's API only covers US-ish airspace reliably; for
# worldwide coverage use AVWX (avwx.rest) or CheckWX (checkwxapi.com) free
# tier -- both give ~ a few hundred free calls/month. Set the key via env.
AVWX_TOKEN = os.getenv("AVWX_TOKEN")


def add_airport(icao, iata=None, name=None, city=None, country=None,
                 elevation_ft=None, notes=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO airports (icao, iata, name, city, country, elevation_ft, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(icao) DO UPDATE SET
             iata=excluded.iata, name=excluded.name, city=excluded.city,
             country=excluded.country, elevation_ft=excluded.elevation_ft,
             notes=excluded.notes, updated_at=datetime('now')""",
        (icao.upper(), iata, name, city, country, elevation_ft, notes),
    )
    conn.commit()
    conn.close()


def list_airports():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM airports ORDER BY icao").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_metar_taf(icao: str):
    try:
        metar = requests.get(
            f"{AWC_BASE}/metar", params={"ids": icao, "format": "json"}, timeout=10
        ).json()
        taf = requests.get(
            f"{AWC_BASE}/taf", params={"ids": icao, "format": "json"}, timeout=10
        ).json()
        return {"metar": metar, "taf": taf}
    except Exception as e:
        return {"error": str(e)}


def fetch_notams(icao: str):
    if not AVWX_TOKEN:
        return {
            "error": "No AVWX_TOKEN set -- get a free key at avwx.rest and set "
            "it as an env var to enable live NOTAMs."
        }
    try:
        r = requests.get(
            f"https://avwx.rest/api/notam/{icao}",
            headers={"Authorization": AVWX_TOKEN},
            timeout=10,
        )
        if r.status_code in (401, 403):
            return {
                "error": "AVWX's free tier covers METAR/TAF only -- NOTAMs need a "
                "paid AVWX plan (account.avwx.rest). Weather above is still live. "
                "For now, check NOTAMs manually via your usual OCC/AIS briefing "
                "source and post anything relevant as a Bulletin."
            }
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def sync_airport(icao: str, persist: bool = True):
    """Pull fresh METAR/TAF + NOTAMs for one airport and cache them on the
    row so the UI can show 'last updated' and display instantly next time,
    even before a fresh refresh completes. Uses the real 4-letter ICAO
    (icao_real) for the external lookups when we have one on file -- your
    briefings are keyed by IATA-style codes, but weather/NOTAM APIs need
    the actual ICAO identifier."""
    code = icao.upper()
    conn = get_conn()
    row = conn.execute("SELECT icao_real FROM airports WHERE icao=?", (code,)).fetchone()
    conn.close()
    lookup_code = (row["icao_real"] if row and row["icao_real"] else code)

    weather = fetch_metar_taf(lookup_code)
    notams = fetch_notams(lookup_code)
    now = datetime.now(timezone.utc).isoformat()

    if persist:
        conn = get_conn()
        conn.execute(
            "UPDATE airports SET weather_json=?, notam_json=?, wx_updated_at=? WHERE icao=?",
            (json.dumps(weather), json.dumps(notams), now, code),
        )
        conn.commit()
        conn.close()

    return {
        "icao": code,
        "lookup_code": lookup_code,
        "weather": weather,
        "notams": notams,
        "updated_at": now,
    }


def parse_briefing_fields(body_md: str) -> dict:
    """Pulls the INFO block (OPS/RW/ELEV/UTC/CAT/ALT/SID/EXIT/TL-TA/EO Alt/
    SET CRS) out of a briefing note as key/value pairs, for the styled
    widget view -- instead of dumping the whole note as raw text."""
    import re
    fields = {}
    patterns = {
        "ops": r"OPS[:=\s]*\**\s*([^\n]+)",
        "rw": r"\bRW[:=\s]*\**\s*([0-9A-Z/\s]+?)\s*(?:\n|\||$)",
        "elev": r"ELEV[:=\s]*\**\s*([0-9]+)",
        "utc": r"UTC[:=\s]*\**\s*([+-]?[0-9]+)",
        "cat": r"\bCAT[:=\s]*\**\s*([A-Z0-9-]+)",
        "alternates": r"ALT[:=\s]*\**\s*([A-Z]{4}(?:\s*,\s*[A-Z]{4})*)\b",
        "sid": r"\bSID[:=\s]*\**\s*([^\n]+)",
        "exit": r"\bEXIT[:=\s]*\**\s*([^\n]+)",
        "tl_ta": r"TL/?TA[:=\s]*\**\s*([^\n]+)",
        "eo_alt": r"EO Alt[:=\s]*\**\s*([^\n]+)",
        "set_crs": r"SET CRS[:=\s]*\**\s*([^\n]+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, body_md, re.IGNORECASE)
        if m:
            fields[key] = m.group(1).strip()
    return fields
