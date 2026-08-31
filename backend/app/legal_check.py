"""
Flight-legality / FTL advisory checker.

IMPORTANT FRAMING: this does not invent aviation law. It takes the duty
details the pilot enters, retrieves the relevant limit tables from their
OWN uploaded FRM/FOM/company manuals via RAG, and asks the model to compare
the numbers against those retrieved limits -- citing page numbers so the
pilot can verify against the source before relying on it. It is a
cross-check tool, not a substitute for OCC/rostering sign-off.
"""
from .rag import search_chunks, _groq, GROQ_MODEL


def check_duty_legality(duty_description: str, doc_ids: list[int] | None = None):
    # Pull the most relevant FRM/FOM limit sections for this specific duty.
    # When the caller hasn't scoped this to specific doc_ids, exclude airport
    # briefings and FIR reference docs -- an FTL/rest question should only
    # ever be answered from the actual FOM/FRM manual, never from an airport
    # note that happened to score highest by embedding similarity.
    hits = search_chunks(
        f"flight time limitation duty period rest requirement rule relevant to: {duty_description}",
        doc_ids=doc_ids,
        top_k=8,
        exclude_filename_prefixes=None if doc_ids else ("airport:", "reference:"),
    )
    if not hits:
        return {
            "verdict": "UNKNOWN",
            "explanation": "No FRM/FOM sections indexed yet -- upload them first.",
            "citations": [],
        }

    context_block = "\n\n".join(
        f"[Source {i+1} | {h['title']} p.{h['page']}]\n{h['text']}"
        for i, h in enumerate(hits)
    )

    system_prompt = (
        "You are cross-checking a planned duty against flight-time-limitation / "
        "rest rules found ONLY in the provided FRM/FOM excerpts, for a pilot "
        "reading this on a phone between duties. You are not a lawyer -- this "
        "is advisory only, not a ruling.\n\n"
        "Be precise, not exhaustive. Target well under 100 words unless the "
        "limit itself genuinely requires more to state accurately.\n\n"
        "Format:\n"
        "- Line 1: 'VERDICT: LIKELY LEGAL' / 'VERDICT: LIKELY EXCEEDS LIMITS' / "
        "'VERDICT: CANNOT DETERMINE'.\n"
        "- If the excerpts contain the relevant limit: 1-2 sentences giving the "
        "number and page, then 1 sentence on the margin. Nothing else.\n"
        "- If the excerpts do NOT contain the relevant limit: ONE sentence "
        "saying so, then stop. Do not restate what a duty-time rule usually "
        "covers, do not list generic categories to check, do not produce a "
        "table of things to verify.\n"
        "- Plain prose only. Never use markdown tables -- the app renders them "
        "as raw '|' characters, not tables.\n"
        "- Never invent a limit that isn't in the excerpts.\n\n"
        "Never advise the pilot to conceal, falsify, or misreport anything. If "
        "the duty appears to exceed limits, add one closing sentence: raise it "
        "with crew scheduling / declare fatigue through official channels."
    )
    user_prompt = f"Planned duty: {duty_description}\n\nFRM/FOM excerpts:\n{context_block}"

    resp = _groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
    )
    return {
        "assessment": resp.choices[0].message.content,
        "citations": [
            {"title": h["title"], "page": h["page"], "excerpt": h["text"][:300]}
            for h in hits
        ],
        "disclaimer": (
            "Advisory only, generated from your own uploaded manuals. "
            "Confirm any limit question with crew scheduling / OCC / your "
            "flight ops department before acting on it."
        ),
    }
