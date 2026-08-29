import os
import numpy as np
from groq import Groq
from .db import get_conn, blob_to_vec
from .ingest import get_embedder

GROQ_MODEL = "openai/gpt-oss-120b"
_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))


def cosine_sim(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def search_chunks(query: str, doc_ids: list[int] | None = None, top_k: int = 6):
    """Cross-document (or scoped) semantic search. Fine for a personal /
    small-crew tool at this doc volume -- swap for FAISS/pgvector if the
    corpus grows past a few thousand pages."""
    embedder = get_embedder()
    q_vec = list(embedder.embed([query]))[0]

    conn = get_conn()
    cur = conn.cursor()
    if doc_ids:
        placeholders = ",".join("?" * len(doc_ids))
        cur.execute(
            f"SELECT chunks.id, chunks.doc_id, chunks.page_num, chunks.text, "
            f"chunks.embedding, documents.filename, documents.title "
            f"FROM chunks JOIN documents ON documents.id = chunks.doc_id "
            f"WHERE chunks.doc_id IN ({placeholders})",
            doc_ids,
        )
    else:
        cur.execute(
            "SELECT chunks.id, chunks.doc_id, chunks.page_num, chunks.text, "
            "chunks.embedding, documents.filename, documents.title "
            "FROM chunks JOIN documents ON documents.id = chunks.doc_id"
        )
    rows = cur.fetchall()
    conn.close()

    scored = []
    for row in rows:
        vec = blob_to_vec(row["embedding"])
        score = cosine_sim(q_vec, vec)
        scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    return [
        {
            "score": round(s, 4),
            "doc_id": r["doc_id"],
            "filename": r["filename"],
            "title": r["title"],
            "page": r["page_num"],
            "text": r["text"],
        }
        for s, r in top
    ]


def answer_question(query: str, doc_ids: list[int] | None = None):
    hits = search_chunks(query, doc_ids=doc_ids, top_k=6)
    if not hits:
        return {"answer": "No documents indexed yet.", "citations": []}

    context_block = "\n\n".join(
        f"[Source {i+1} | {h['title']} p.{h['page']}]\n{h['text']}"
        for i, h in enumerate(hits)
    )

    system_prompt = (
        "You are an aviation reference assistant for a working pilot. "
        "Answer ONLY using the provided source excerpts -- never from outside "
        "knowledge. Be concise and operational (a pilot reading between duties). "
        "After the answer, list which [Source N] each part of your answer came "
        "from. If the excerpts don't contain the answer, say so plainly instead "
        "of guessing."
    )
    user_prompt = f"Question: {query}\n\nSource excerpts:\n{context_block}"

    resp = _groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
    )
    answer = resp.choices[0].message.content

    return {
        "answer": answer,
        "citations": [
            {
                "doc_id": h["doc_id"],
                "title": h["title"],
                "filename": h["filename"],
                "page": h["page"],
                "excerpt": h["text"][:300],
                "score": h["score"],
            }
            for h in hits
        ],
    }
