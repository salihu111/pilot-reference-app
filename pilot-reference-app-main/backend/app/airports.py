import os
import json
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


def save_weather_cache(icao: str, weather, notams):
    conn = get_conn()
    conn.execute(
        """INSERT INTO airport_weather_cache (icao, weather_json, notams_json, updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(icao) DO UPDATE SET
             weather_json=excluded.weather_json,
             notams_json=excluded.notams_json,
             updated_at=datetime('now')""",
        (icao.upper(), json.dumps(weather), json.dumps(notams)),
    )
    conn.commit()
    conn.close()


def get_weather_cache(icao: str):
    """Last cached sync for this airport, or None if it's never been synced."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM airport_weather_cache WHERE icao=?", (icao.upper(),)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "icao": row["icao"],
        "weather": json.loads(row["weather_json"]) if row["weather_json"] else None,
        "notams": json.loads(row["notams_json"]) if row["notams_json"] else None,
        "updated_at": row["updated_at"],
        "cached": True,
    }


def sync_airport(icao: str):
    """Pull FRESH METAR/TAF + NOTAMs for one airport (live network calls)
    and cache the result. Call this from the frontend's manual refresh
    button, or its auto-refresh timer while an airport card is open --
    NOT on every page load, to stay inside AVWX's free-tier daily quota."""
    weather = fetch_metar_taf(icao)
    notams = fetch_notams(icao)
    save_weather_cache(icao, weather, notams)
    return {
        "icao": icao.upper(),
        "weather": weather,
        "notams": notams,
        "updated_at": "now",
        "cached": False,
    }
