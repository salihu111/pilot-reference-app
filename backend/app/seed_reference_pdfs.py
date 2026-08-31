"""
Seeds whole PDF reference documents (FOM, ops manuals, checklists, etc.)
straight into the same vector search index used by /api/ask (`documents` +
`chunks`) -- same pattern as seed_airports.py, just for full PDFs instead of
per-airport markdown notes.

Why this exists: /api/documents/upload writes into ephemeral container
storage. That's fine for day-to-day use, but every Railway redeploy wipes
it -- so a large manual uploaded by hand through the Documents tab
disappears again on the next deploy, and re-uploading a big PDF through the
browser can time out on Railway's proxy. PDFs placed under
data/reference_pdfs/ instead ship inside the Docker image itself, so they
survive every redeploy and get embedded automatically at boot with no
upload step at all.

Usage (also runs automatically on startup -- see main.py):

    python -m app.seed_reference_pdfs --source data/reference_pdfs

Re-running is safe: a PDF already present (matched by filename) is skipped,
not re-embedded. To force a re-index after replacing a file (e.g. a new FOM
revision with the SAME filename), delete its row from `documents` (and the
matching rows in `chunks`) first, or just give the new revision a new
filename (e.g. FOM_REV_25C.pdf) -- both will show up side by side in the
Documents tab and in /api/ask results until you remove the old one.
"""
import argparse
import os
import shutil

from .db import init_db, get_conn


def _title_from_filename(filename: str) -> str:
    name = os.path.splitext(filename)[0]
    return name.replace("_", " ").replace("-", " ").strip()


def upsert_reference_pdf(src_path: str, filename: str, upload_dir: str,
                          title: str | None = None) -> tuple[int, bool]:
    """Copies the PDF into UPLOAD_DIR (so the existing screenshot/citation
    endpoints keep working) and embeds it if it isn't already indexed.
    Returns (doc_id, created).

    "Already indexed" means a documents row that actually has chunks --
    ingest_pdf now commits the documents row immediately and embeds/commits
    in batches (see ingest.py), so a doc row with zero chunks means a
    previous attempt started but never finished (crash/OOM/redeploy
    mid-embed). That stale empty row is deleted and the PDF is re-ingested
    from scratch, instead of being skipped forever."""
    os.makedirs(upload_dir, exist_ok=True)
    dest = os.path.join(upload_dir, filename)
    shutil.copyfile(src_path, dest)

    conn = get_conn()
    existing = conn.execute(
        "SELECT documents.id, COUNT(chunks.id) AS n "
        "FROM documents LEFT JOIN chunks ON chunks.doc_id = documents.id "
        "WHERE documents.filename=? GROUP BY documents.id",
        (filename,),
    ).fetchone()
    if existing and existing["n"] > 0:
        conn.close()
        return existing["id"], False
    if existing and existing["n"] == 0:
        print(f"  {filename}: found stale unindexed row (id={existing['id']}) -- re-ingesting")
        conn.execute("DELETE FROM documents WHERE id=?", (existing["id"],))
        conn.commit()
    conn.close()

    from .ingest import ingest_pdf  # lazy: keeps PyMuPDF/fastembed off the
    # import path for callers (like main.py's module-level imports) that
    # don't need them just to call run()/upsert_reference_pdf conditionally
    doc_id = ingest_pdf(dest, filename, title or _title_from_filename(filename))
    return doc_id, True


def run(source_dir: str, upload_dir: str):
    init_db()
    seeded, skipped = 0, 0
    for fname in sorted(os.listdir(source_dir)):
        if fname.startswith(".") or not fname.lower().endswith(".pdf"):
            continue
        src = os.path.join(source_dir, fname)
        _, created = upsert_reference_pdf(src, fname, upload_dir)
        if created:
            seeded += 1
        else:
            skipped += 1
    print(f"Seeded {seeded} reference PDF(s), skipped {skipped} already-indexed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/reference_pdfs")
    parser.add_argument("--upload-dir", default=os.getenv("UPLOAD_DIR", "storage/uploads"))
    args = parser.parse_args()
    run(args.source, args.upload_dir)
