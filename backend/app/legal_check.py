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
    # Pull the most relevant FRM/FOM limit sections for this specific duty
    hits = search_chunks(
        f"flight time limitation duty period rest requirement rule relevant to: {duty_description}",
        doc_ids=doc_ids,
        top_k=8,
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
        "You are assisting a pilot in cross-checking a planned duty against "
        "flight-time-limitation / rest rules found ONLY in the provided FRM/FOM "
        "excerpts. You are not a lawyer and this is not a legal ruling -- say so. "
        "Structure your reply as:\n"
        "1) VERDICT: one of LIKELY LEGAL / LIKELY EXCEEDS LIMITS / CANNOT DETERMINE\n"
        "2) The specific limit(s) that apply, quoting page numbers\n"
        "3) The gap or margin between the duty as described and that limit\n"
        "4) What to double check with crew scheduling/OCC before acting\n"
        "Never advise the pilot to conceal, falsify, or misreport anything. "
        "If the duty appears to exceed limits, the correct action is to raise it "
        "with crew scheduling / declare fatigue through official channels -- say this."
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
