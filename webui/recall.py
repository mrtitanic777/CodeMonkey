"""Recall: what is brought to a message, kept apart from what never changes.

The system prompt is the same on every turn of a session: who you are, the stable part of
memory, the rules. A local model server keeps its work on a prompt it has already read, so an
unchanged opening costs nothing on the next turn; an opening that is reordered for every
message is read again from the top, every time.

What does depend on the message travels *with* the message instead:

    remembered claims that bear on it and are not already in the stable part
    notes about the people it names
    skills that fit it
    on the first message of a session, what was last done in this folder

It is bounded (a handful of items, a character budget) and says where to look for more. This
is the layout larger memory systems use: stable context in the system prompt, per-turn recall
in front of the user's words.

Recall is not evidence. What is brought back here does not count as something the user said or
the agent saw in this session.
"""

from __future__ import annotations

import re
from typing import Any, List

MAX_CLAIMS = 8
MAX_CHARS = 2800
STABLE_LIMIT = 30
_MORE = "(More may be remembered: use memory_search or chat_search.)"


def stable_ids(store, project_dir: str) -> set:
    """The claims that are in the system prompt on every turn."""
    claims = [c for c in store.claims(project_dir) if not c.get("subject")]
    lessons = [c for c in claims if c["kind"] == "lesson"]
    rest = [c for c in claims if c["kind"] != "lesson"][:STABLE_LIMIT]
    return {c["claim_id"] for c in lessons + rest}


def people_notes(store, query: str) -> List[str]:
    """Notes about the people a message names, by full name or first name."""
    text = " " + re.sub(r"[^\w']+", " ", str(query or "").lower()) + " "
    lines: List[str] = []
    for person in store.people():
        full = person["name"].lower()
        first = full.split()[0]
        if f" {full} " in text or (len(first) >= 3 and f" {first} " in text):
            lines.append(f"What you were told about {person['name']}:")
            for c in person["claims"][:10]:
                mark = "" if c["state"] in ("KNOWN", "DERIVED") else " (unverified: your own conclusion)"
                lines.append(f"- {c['claim_text']}{mark}")
    return lines


def recent_work(store, project_dir: str, session_id: str, limit: int = 5) -> List[str]:
    """What was last done in this folder, from the ledger of finished tasks."""
    rows = store.recent_tasks(project_dir, exclude_session=session_id, limit=limit)
    if not rows:
        return []
    word = {"success": "done", "error": "failed", "stopped": "stopped"}
    return ["Last work in this folder (from the record; check before relying on it):"] + [
        f"- {r['created_at'][:10]}: {r['title'][:110]} ({word.get(r['status'], r['status'])}; {r['subtitle']})"
        for r in rows
    ]


_COMMON = frozenset(
    "this that with from what when where which there their about have will would could should thanks "
    "please into your they them then than some more most just like make made does doing done need "
    "want look also been were here over only very much many each other after before again".split())


def fitting_skills(query: str, skills: List[Any], top_n: int = 2) -> List[Any]:
    """Skills a message really calls for. A whole playbook is attached, so one common word in a
    description is not enough: the name must match, or a keyword and something else."""
    words = {w for w in re.findall(r"[a-z0-9]{4,}", str(query or "").lower()) if w not in _COMMON}
    scored = []
    for skill in skills or []:
        name = set(re.findall(r"[a-z0-9]{4,}", skill.name.lower()))
        keys = set(re.findall(r"[a-z0-9]{4,}", " ".join(skill.keywords).lower()))
        desc = set(re.findall(r"[a-z0-9]{4,}", skill.description.lower())) - _COMMON
        score = 3 * len(words & name) + 2 * len(words & keys) + len(words & desc)
        if score >= 3:
            scored.append((score, skill))
    scored.sort(key=lambda pair: -pair[0])
    return [skill for _, skill in scored[:top_n]]


def recalled(session, query: str, first_message: bool = False) -> str:
    """The block that goes in front of a message, or "" when nothing bears on it."""
    store = session.app.store
    parts: List[str] = []
    try:
        fixed = stable_ids(store, session.project_dir)
        found = [c for c in store.search_claims(query, session.project_dir, MAX_CLAIMS * 2)
                 if c["claim_id"] not in fixed and not c.get("subject")][:MAX_CLAIMS]
        if found:
            parts.append("Remembered, and related to this message:")
            parts += [
                f"- {c['claim_text']}" + ("" if c["state"] in ("KNOWN", "DERIVED") else " (unverified)")
                for c in found
            ]
        parts += people_notes(store, query)
        if first_message:
            parts += recent_work(store, session.project_dir, session.id)
    except Exception:  # noqa: BLE001 - recall must never stop a turn
        pass
    body = "\n".join(parts)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS].rsplit("\n", 1)[0] + "\n" + _MORE
    skills: List[Any] = []
    try:
        skills = fitting_skills(query, session.app.skills)
    except Exception:  # noqa: BLE001
        skills = []
    sent = session.__dict__.setdefault("_skills_sent", set())
    for skill in skills:
        if skill.name in sent:
            continue   # it is already in the conversation; sending it again only fills the window
        sent.add(skill.name)
        body += ("\n" if body else "") + f"--- skill that fits this message: {skill.name} ---\n{skill.body}"
    if not body:
        return ""
    return ("[Recalled for this message by the system. It is background, not something the user just said.]\n"
            + body + "\n[End of recall]\n\n")
