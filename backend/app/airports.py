import os
import json
import re
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
    translation = get_weather_translation(lookup_code)
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
        "weather_translation": translation["weather_translation"],

    # Keep these only if you want the translated

    # source data available to the frontend.

        "translation_metar": translation["translation_metar"],
        "translation_taf": translation["translation_taf"],
        "notams": notams,
        "updated_at": now,
    }
# ============================================================
# FREE WORLDWIDE METAR / TAF PLAIN-ENGLISH TRANSLATION
# Source: AviationWeather.gov
# This is ONLY used for translation.
# AVWX remains the primary weather source/display.
# ============================================================

AWC_TRANSLATION_BASE = "https://aviationweather.gov/api/data"


def fetch_free_metar_taf_for_translation(icao: str):
    """
    Get worldwide METAR + TAF from AviationWeather.gov.

    This does NOT replace AVWX.
    It is only used to translate the weather into plain words.
    """

    icao = icao.upper().strip()

    result = {
        "metar": "",
        "taf": "",
    }

    try:
        # -----------------------------
        # METAR
        # -----------------------------
        r = requests.get(
            f"{AWC_TRANSLATION_BASE}/metar",
            params={
                "ids": icao,
                "format": "json",
                "hours": 2,
            },
            timeout=8,
        )

        if r.ok:

            data = r.json()

            if isinstance(data, list) and data:

                metar = data[0]

                result["metar"] = (
                    metar.get("rawOb")
                    or metar.get("raw_text")
                    or metar.get("rawText")
                    or ""
                )

        # -----------------------------
        # TAF
        # -----------------------------
        r = requests.get(
            f"{AWC_TRANSLATION_BASE}/taf",
            params={
                "ids": icao,
                "format": "json",
            },
            timeout=8,
        )

        if r.ok:

            data = r.json()

            if isinstance(data, list) and data:

                taf = data[0]

                result["taf"] = (
                    taf.get("rawTAF")
                    or taf.get("raw_text")
                    or taf.get("rawText")
                    or ""
                )

    except Exception as e:

        print(
            f"Free METAR/TAF translation source error "
            f"{icao}: {e}"
        )

    return result


def translate_weather_to_plain_words(metar, taf):
    """
    Return ONLY operationally relevant adverse weather.

    If nothing significant is detected:
        Weather is good.
    """

    text = (
        f"{metar or ''} "
        f"{taf or ''}"
    ).upper()

    adverse = []

    # --------------------------------------------------------
    # THUNDERSTORMS / CONVECTION
    # --------------------------------------------------------

    if "TSRA" in text or "TS" in text:
        adverse.append("thunderstorms")

    if "CB" in text:
        adverse.append("cumulonimbus clouds")

    if "TCU" in text:
        adverse.append("towering cumulus clouds")

    # --------------------------------------------------------
    # RAIN / DRIZZLE
    # --------------------------------------------------------

    if "+RA" in text:
        adverse.append("heavy rain")

    elif "-RA" in text:
        adverse.append("light rain")

    elif "RA" in text:
        adverse.append("rain")

    if "+DZ" in text:
        adverse.append("heavy drizzle")

    elif "-DZ" in text:
        adverse.append("light drizzle")

    elif "DZ" in text:
        adverse.append("drizzle")

    # --------------------------------------------------------
    # SHOWERS
    # --------------------------------------------------------

    if "SHRA" in text:
        adverse.append("rain showers")

    if "SHSN" in text:
        adverse.append("snow showers")

    # --------------------------------------------------------
    # SNOW / FREEZING PRECIPITATION
    # --------------------------------------------------------

    if "+SN" in text:
        adverse.append("heavy snow")

    elif "-SN" in text:
        adverse.append("light snow")

    elif "SN" in text:
        adverse.append("snow")

    if "FZRA" in text:
        adverse.append("freezing rain")

    if "FZDZ" in text:
        adverse.append("freezing drizzle")

    if "PL" in text:
        adverse.append("ice pellets")

    if "GR" in text:
        adverse.append("hail")

    # --------------------------------------------------------
    # VISIBILITY / OBSCURATION
    # --------------------------------------------------------

    if "FG" in text:
        adverse.append("fog")

    if "BR" in text:
        adverse.append("mist")

    if "HZ" in text:
        adverse.append("haze")

    if "DU" in text:
        adverse.append("dust")

    if "SA" in text:
        adverse.append("sand")

    if "DS" in text:
        adverse.append("dust storm")

    if "SS" in text:
        adverse.append("sandstorm")

    # --------------------------------------------------------
    # WIND SHEAR
    # --------------------------------------------------------

    if "WS" in text:
        adverse.append("wind shear")

    # --------------------------------------------------------
    # VISIBILITY
    #
    # Detect 4-digit visibility values.
    # Only report values below 5000 m.
    # --------------------------------------------------------

    # --------------------------------------------------------
# VISIBILITY
#
# Only accept a standalone 4-digit METAR/TAF visibility
# token.
#
# This prevents:
#   Q1013      -> being treated as 1013 m visibility
#   291743Z    -> being treated as visibility
#   3117/0124  -> being treated as visibility
#   22009KT    -> being treated as visibility
# --------------------------------------------------------

visibility_values = re.findall(
    r"(?<![A-Z0-9/])(\d{4})(?![A-Z0-9/])",
    text
)

for value in visibility_values:

    vis = int(value)

    # 9999 = 10 km or more
    if vis == 9999:
        continue

    if vis < 1000:

        adverse.append(
            f"very poor visibility ({vis} m)"
        )

        break

    elif vis < 3000:

        adverse.append(
            f"poor visibility ({vis} m)"
        )

        break

    elif vis < 5000:

        adverse.append(
            f"reduced visibility ({vis} m)"
        )

        break

    # --------------------------------------------------------
    # LOW CLOUD / LOW CEILING
    # --------------------------------------------------------

    cloud_matches = re.findall(
        r"\b(SCT|BKN|OVC|VV)(\d{3})\b",
        text
    )

    for cover, height in cloud_matches:

        altitude = int(height) * 100

        if cover == "SCT" and altitude < 1000:

            adverse.append(
                f"low scattered cloud at {altitude} ft"
            )

        elif cover in ("BKN", "OVC", "VV"):

            if altitude < 1500:

                adverse.append(
                    f"low ceiling around {altitude} ft"
                )

    # --------------------------------------------------------
    # WIND / GUSTS
    # --------------------------------------------------------

    wind_matches = re.findall(
        r"\b(\d{3})(\d{2,3})(?:G(\d{2,3}))?KT\b",
        text
    )

    for direction, speed, gust in wind_matches:

        speed = int(speed)

        if speed >= 25:

            adverse.append(
                f"strong wind around {speed} kt"
            )

        if gust:

            gust = int(gust)

            if gust >= 30:

                adverse.append(
                    f"gusts up to {gust} kt"
                )

    # --------------------------------------------------------
    # REMOVE DUPLICATES
    # --------------------------------------------------------

    adverse = list(
        dict.fromkeys(adverse)
    )

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    if not adverse:

        return "Weather is good."

    return (
        "Adverse weather: "
        + "; ".join(adverse)
        + "."
    )

def get_weather_translation(icao: str):

    data = fetch_free_metar_taf_for_translation(
        icao
    )

    return {
        "weather_translation":
            translate_weather_to_plain_words(
                data.get("metar", ""),
                data.get("taf", ""),
            ),

        "translation_metar":
            data.get("metar", ""),

        "translation_taf":
            data.get("taf", ""),
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
