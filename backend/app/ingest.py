import fitz  # PyMuPDF
from fastembed import TextEmbedding
from .db import get_conn, vec_to_blob

_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        # Same model as FOMbot -> consistent embedding space across projects
        _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedder


def chunk_page_text(text: str, max_chars: int = 900, overlap: int = 150):
    """Simple sliding-window chunker. Good enough for manual-style PDFs
    where each page is already a fairly self-contained unit."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


def ingest_pdf(filepath: str, filename: str, title: str | None = None,
                batch_size: int = 64) -> int:
    """Extract every page, chunk it, embed the chunks, store everything.
    Returns the new document id.

    Embeds and commits in batches instead of one giant embed-everything-
    then-commit-once transaction. On a large manual (the FOM is 800+ pages,
    thousands of chunks), embedding it all in a single call before any
    commit means a crash, OOM, or restart partway through loses EVERYTHING
    -- the document row included -- leaving zero rows even though most of
    the work finished. Batching keeps peak memory down, commits progress as
    it goes (so a later crash only loses the current batch, not the whole
    document), and prints progress so this is visible in Railway logs
    instead of silently failing."""
    doc = fitz.open(filepath)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO documents (filename, title, num_pages) VALUES (?, ?, ?)",
        (filename, title or filename, doc.page_count),
    )
    doc_id = cur.lastrowid
    conn.commit()  # document row survives even if embedding fails/crashes below

    all_chunks = []  # (page_num, text)
    for page_num in range(doc.page_count):
        page = doc[page_num]
        text = page.get_text("text")
        for c in chunk_page_text(text):
            all_chunks.append((page_num + 1, c))  # 1-indexed for humans
    doc.close()

    embedder = get_embedder()
    total = len(all_chunks)
    for start in range(0, total, batch_size):
        batch = all_chunks[start:start + batch_size]
        vectors = list(embedder.embed([t for _, t in batch]))
        for (page_num, text), vec in zip(batch, vectors):
            cur.execute(
                "INSERT INTO chunks (doc_id, page_num, text, embedding) VALUES (?, ?, ?, ?)",
                (doc_id, page_num, text, vec_to_blob(vec)),
            )
        conn.commit()
        print(f"  ingest_pdf({filename}): {min(start + batch_size, total)}/{total} chunks embedded")

    conn.close()
    return doc_id


def ingest_text(title: str, text: str, source_label: str) -> int:
    """Index plain-text/markdown content (airport briefings, reference docs)
    into the same searchable RAG index as the PDFs, so 'Ask' can pull from
    them too. These have no real page/screenshot -- stored as page 1 of a
    text-only document, flagged so the UI skips the screenshot preview."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO documents (filename, title, num_pages, is_text_doc) VALUES (?, ?, 1, 1)",
        (source_label, title),
    )
    doc_id = cur.lastrowid

    embedder = get_embedder()
    chunks = chunk_page_text(text, max_chars=900, overlap=150)
    if chunks:
        vectors = list(embedder.embed(chunks))
        for chunk_text, vec in zip(chunks, vectors):
            cur.execute(
                "INSERT INTO chunks (doc_id, page_num, text, embedding) VALUES (?, 1, ?, ?)",
                (doc_id, chunk_text, vec_to_blob(vec)),
            )
    conn.commit()
    conn.close()
    return doc_id


def reindex_text_doc(title: str, text: str, source_label: str) -> int:
    """Upsert-by-title for text docs so re-running the seed script doesn't
    pile up duplicate entries every time."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM documents WHERE title=? AND is_text_doc=1", (title,)
    ).fetchone()
    if existing:
        doc_id = existing["id"]
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.commit()
        conn.close()
        embedder = get_embedder()
        chunks = chunk_page_text(text, max_chars=900, overlap=150)
        conn = get_conn()
        cur = conn.cursor()
        if chunks:
            vectors = list(embedder.embed(chunks))
            for chunk_text, vec in zip(chunks, vectors):
                cur.execute(
                    "INSERT INTO chunks (doc_id, page_num, text, embedding) VALUES (?, 1, ?, ?)",
                    (doc_id, chunk_text, vec_to_blob(vec)),
                )
        conn.commit()
        conn.close()
        return doc_id
    conn.close()
    return ingest_text(title, text, source_label)


def render_page_image(filepath: str, page_num: int, out_path: str, dpi: int = 150,
                       highlight_text: str | None = None):
    """Render a page (1-indexed) to a PNG for the screenshot/citation preview.
    If highlight_text is given, tries to box the matching text on the page."""
    doc = fitz.open(filepath)
    page = doc[page_num - 1]
    if highlight_text:
        # Search for a short distinctive slice of the chunk (first ~60 chars)
        needle = highlight_text.strip()[:60]
        for rect in page.search_for(needle):
            page.draw_rect(rect, color=(1, 0.3, 0), width=1.5)
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    pix.save(out_path)
    doc.close()
    return out_path
