"""
Seeds `airports`, `airport_attachments`, and `references_docs` from an
exported folder of airport briefing notes (one subfolder per airport, each
containing a .md file and an optional Attachments/ folder of PDFs/images --
the same shape iCloud Notes / Apple Notes gives you on export).

Run once after deploying (or any time you add/update briefing notes):

    python -m app.seed_airports --source data/airport_briefings

Re-running is safe -- it upserts by code, and re-copies attachments.
"""
import argparse
import os
import re
import shutil
import sys

from .db import init_db, get_conn

# Imported lazily (inside functions) so --skip-embed-index can run without
# the heavier PDF/embedding deps (PyMuPDF, fastembed) installed.

CODE_RE = re.compile(r"\b([A-Z]{3,4})\b")
# NOTE: "ALT" in these notes means the ALTERNATE diversion airport(s), not
# this airport's own ICAO -- e.g. AMM's note lists "ALT: HECA, HEGN" (Cairo,
# Luxor as alternates). Don't confuse this with the airport's own code.
ALTERNATES_RE = re.compile(r"ALT[:=\s]*\**\s*([A-Z]{4}(?:\s*,\s*[A-Z]{4})*)\b")
# The airport's own ICAO is the first standalone 4-letter code near the top
# of the note (e.g. "**DIAP**" right under the "# ABJ" title), before any
# ATC-phraseology text kicks in.
ICAO_HEADER_BLACKLIST = {
    "INFO", "INIT", "CTRL", "DEST", "FROM", "TAXI", "CROSS", "STAND",
    "AFTER", "THEN", "REPORT", "RADAR", "FINAL", "CLBG",
}
RW_RE = re.compile(r"RW[:=\s]*\**\s*([0-9A-Z/\s]+?)\s*(?:\n|\||$)")
ELEV_RE = re.compile(r"ELEV[:=\s]*\**\s*([0-9]+)")
UTC_RE = re.compile(r"UTC[:=\s]*\**\s*([+-]?[0-9]+)")
SID_RE = re.compile(r"SID[:=\s]*\**\s*([^\n]+)")
EXIT_RE = re.compile(r"EXIT[:=\s]*\**\s*([^\n]+)")


def guess_real_icao(body_md: str) -> str | None:
    lines = body_md.splitlines()
    # Look a few lines past the title for the airport's own 4-letter code
    window = "\n".join(lines[1:8])
    for m in re.finditer(r"\b([A-Z]{4})\b", window):
        if m.group(1) not in ICAO_HEADER_BLACKLIST:
            return m.group(1)
    return None

# Folders that are FIR-wide reference material, not single-airport briefings
REFERENCE_FOLDER_MAP = {
    "escape route": "escape_route",
    "sid from": "sid",
    "sid from the exit point": "sid",
}


def clean_markup(text: str) -> str:
    return text.replace("==", "").replace("**", "")


def guess_code(folder_name: str) -> str | None:
    # Strip a flag emoji / trailing annotations like "(NOT FULLY DONE)"
    label = re.sub(r"\(.*?\)", "", folder_name)
    m = CODE_RE.search(label)
    return m.group(1) if m else None


def guess_country_flag(folder_name: str) -> str | None:
    flags = re.findall(
        r"[\U0001F1E6-\U0001F1FF]{2}", folder_name
    )
    return flags[0] if flags else None


def is_reference_folder(folder_name: str) -> str | None:
    lower = folder_name.lower()
    for key, category in REFERENCE_FOLDER_MAP.items():
        if key in lower:
            return category
    return None


def upsert_reference(title, category, body_md, embed=True):
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM references_docs WHERE title=?", (title,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE references_docs SET body_md=?, category=?, updated_at=datetime('now') WHERE id=?",
            (body_md, category, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO references_docs (title, category, body_md) VALUES (?, ?, ?)",
            (title, category, body_md),
        )
    conn.commit()
    conn.close()
    if embed:
        from .ingest import reindex_text_doc
        reindex_text_doc(title, body_md, source_label=f"reference:{title}")


def upsert_airport(code, body_md, attachments_src_dir, attachments_dest_root, embed=True):
    icao_real = guess_real_icao(body_md)
    rw_match = RW_RE.search(body_md)
    elev_match = ELEV_RE.search(body_md)
    alt_match = ALTERNATES_RE.search(body_md)
    notes_parts = []
    if rw_match:
        notes_parts.append(f"RW {rw_match.group(1).strip()}")
    if alt_match:
        notes_parts.append(f"Alternates: {alt_match.group(1).strip()}")

    conn = get_conn()
    conn.execute(
        """INSERT INTO airports (icao, briefing_md, icao_real, notes)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(icao) DO UPDATE SET
             briefing_md=excluded.briefing_md,
             icao_real=COALESCE(excluded.icao_real, airports.icao_real),
             notes=excluded.notes,
             updated_at=datetime('now')""",
        (code, body_md, icao_real, " | ".join(notes_parts) or None),
    )
    if elev_match:
        conn.execute(
            "UPDATE airports SET elevation_ft=? WHERE icao=? AND elevation_ft IS NULL",
            (int(elev_match.group(1)), code),
        )
    conn.commit()
    conn.close()

    if embed:
        from .ingest import reindex_text_doc
        reindex_text_doc(f"{code} Airport Briefing", body_md, source_label=f"airport:{code}")

    if attachments_src_dir and os.path.isdir(attachments_src_dir):
        dest_dir = os.path.join(attachments_dest_root, code)
        os.makedirs(dest_dir, exist_ok=True)
        conn = get_conn()
        conn.execute("DELETE FROM airport_attachments WHERE icao=?", (code,))
        for fname in os.listdir(attachments_src_dir):
            if fname.startswith("."):
                continue
            src = os.path.join(attachments_src_dir, fname)
            dest = os.path.join(dest_dir, fname)
            shutil.copyfile(src, dest)
            conn.execute(
                "INSERT INTO airport_attachments (icao, filename, relpath, filetype) VALUES (?, ?, ?, ?)",
                (code, fname, os.path.join(code, fname), fname.split(".")[-1].lower()),
            )
        conn.commit()
        conn.close()


def run(source_dir: str, attachments_dest_root: str, embed: bool = True):
    init_db()
    seeded_airports, seeded_refs, skipped = 0, 0, []

    for entry in sorted(os.listdir(source_dir)):
        full = os.path.join(source_dir, entry)
        if not os.path.isdir(full) or entry.startswith("."):
            continue
        md_files = [f for f in os.listdir(full) if f.lower().endswith(".md")]
        if not md_files:
            skipped.append(entry)
            continue
        with open(os.path.join(full, md_files[0]), encoding="utf-8") as f:
            body_md = clean_markup(f.read())

        ref_category = is_reference_folder(entry)
        if ref_category:
            upsert_reference(entry.strip(), ref_category, body_md, embed=embed)
            seeded_refs += 1
            continue

        code = guess_code(entry)
        if not code:
            skipped.append(entry)
            continue

        attachments_src = os.path.join(full, "Attachments")
        upsert_airport(code, body_md, attachments_src, attachments_dest_root, embed=embed)
        seeded_airports += 1

    print(f"Seeded {seeded_airports} airport briefings, {seeded_refs} reference docs.")
    if skipped:
        print(f"Skipped (no .md or no code found): {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/airport_briefings")
    parser.add_argument(
        "--attachments-dest",
        default=os.getenv("AIRPORT_ATTACHMENTS_DIR", "/data/airport_attachments"),
    )
    parser.add_argument(
        "--skip-embed-index", action="store_true",
        help="Skip embedding into the cross-document search index (still populates "
             "the Airports/Escape Routes tabs). Useful for a quick local dry run.",
    )
    args = parser.parse_args()
    os.makedirs(args.attachments_dest, exist_ok=True)
    run(args.source, args.attachments_dest, embed=not args.skip_embed_index)
