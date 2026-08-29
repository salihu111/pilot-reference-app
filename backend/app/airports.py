import os
import requests
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
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def sync_airport(icao: str):
    """Pull fresh METAR/TAF + NOTAMs for one airport. Call this from the
    frontend's refresh button, or on a schedule (see main.py note)."""
    return {
        "icao": icao.upper(),
        "weather": fetch_metar_taf(icao),
        "notams": fetch_notams(icao),
    }
