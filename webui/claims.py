"""Memory as claims: what the agent keeps between sessions, each with a standing.

The structure follows the claim ledgers used by larger agent runtimes (text, source, confidence,
what it replaced). The standing is ours: it comes from the MSHMOE kernel's states, decided by
checking the claim against what the session actually saw.

    KNOWN         you said it, or you confirmed it
    DERIVED       it is in something the agent read or ran
    CONFABULATED  the agent concluded it by itself; nothing it saw states it

Unverified claims are still kept and still shown to the model, but marked, so it checks before
relying on them, and you can confirm or forget them in Settings.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from tools.registry import Tool

from .epistemic import LABELS, Ledger, State, _norm

STANDING = {
    "KNOWN": "Told or confirmed by you",
    "DERIVED": "Read from your files",
    "CONFABULATED": "Its own conclusion",
}
_WORD = re.compile(r"[a-z0-9_]{3,}")
_LEGACY_LINE = re.compile(r"^- (?:\[(\d{4}-\d{2}-\d{2})\]\s*)?(.+)$")


_FILLER = frozenset("the user users they them their this that with and for are was has have".split())


def _words(text: str) -> set:
    """Content words, with a plural or verb "s" dropped so "prefers" matches "prefer"."""
    out = set()
    for word in _WORD.findall(str(text or "").lower()):
        if word in _FILLER:
            continue
        out.add(word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return out


_NAME_BAD = re.compile(r"[^\w .'\-]", re.UNICODE)
_NOT_PEOPLE = frozenset("user me myself i you the agent assistant codemonkey nobody everyone someone".split())


def person_name(raw: str) -> str:
    """A person's name as it is filed: tidied, or "" when it is not a usable name."""
    name = " ".join(_NAME_BAD.sub(" ", str(raw or "")).split())[:40].strip(" .'-")
    if len(name) < 2 or name.lower() in _NOT_PEOPLE or name.isdigit():
        return ""
    return name if any(ch.isupper() for ch in name) else name.title()


def people_roster(store) -> str:
    """Who there are notes about. The notes themselves come with a message that names the person."""
    names = [p["name"] for p in store.people()]
    if not names:
        return ""
    return ("There are notes about these people: " + ", ".join(names[:30])
            + ". Their notes are brought to you when the user mentions them; memory_search finds them too.")


def standing_for(ledger: Ledger, text: str, source_path: str = "") -> "tuple[str, str]":
    """(state, basis in plain words) for a claim the agent wants to save."""
    words = _words(text)
    if not words:
        return State.CONFABULATED.value, "there is nothing in it to check"
    said = _words(" ".join(ledger.user_texts[-12:]))
    if len(words & said) / len(words) >= 0.7:
        return State.KNOWN.value, "you said this in the conversation"
    path = _norm(source_path)
    if path and path in ledger.read:
        return State.DERIVED.value, f"from {source_path}, which the agent read"
    seen = _words(" ".join(ledger.seen_texts()))
    if len(words & seen) / len(words) >= 0.7:
        return State.DERIVED.value, "from what the agent read or ran in the session"
    return State.CONFABULATED.value, "the agent concluded this itself; nothing it saw states it"


def public(claim: Dict[str, Any]) -> Dict[str, Any]:
    """A claim as the interface shows it."""
    return {
        "id": claim["claim_id"],
        "text": claim["claim_text"],
        "kind": claim["kind"],
        "about": claim.get("subject") or "",
        "state": claim["state"],
        "standing": STANDING.get(claim["state"], LABELS.get(State.CONFABULATED)),
        "basis": claim["basis"],
        "source_path": claim["source_path"] or "",
        "project": os.path.basename(claim["project_dir"]) if claim["project_dir"] else "",
        "first_seen": claim["first_seen"][:10],
        "reinforced": claim["reinforced_count"],
        "replaced": bool(claim["supersedes_claim_id"]),
    }


def import_legacy(store, memory_file: str) -> int:
    """Once: bring the lines of the old MEMORY.md in as claims you stated."""
    if store.get_meta("memory_imported"):
        return 0
    count = 0
    try:
        with open(memory_file, "r", encoding="utf-8") as fh:
            for line in fh:
                m = _LEGACY_LINE.match(line.strip())
                if m and m.group(2).strip():
                    store.add_claim(m.group(2).strip(), "KNOWN", "remembered before claims existed")
                    count += 1
    except OSError:
        pass
    store.set_meta("memory_imported", "1")
    return count


def prompt_section(store, project_dir: str, limit: int = 30) -> str:
    """The stable part of memory, for the system prompt: solid claims plainly, unverified marked.

    It does not depend on the message at hand, so the prompt reads the same from turn to turn
    (see recall.py: what bears on a message travels with the message).
    """
    # notes about people are kept apart: they come in only when the person is mentioned
    claims = [c for c in store.claims(project_dir) if not c.get("subject")]
    claims = [c for c in claims if c["kind"] == "lesson"] + [c for c in claims if c["kind"] != "lesson"][:limit]
    if not claims:
        return "(nothing remembered yet)"
    lessons = [c for c in claims if c["kind"] == "lesson"]
    claims = [c for c in claims if c["kind"] != "lesson"]
    solid = [c for c in claims if c["state"] in ("KNOWN", "DERIVED")]
    unsure = [c for c in claims if c["state"] not in ("KNOWN", "DERIVED")]
    lines = [f"- {c['claim_text']}" for c in solid]
    if lessons:
        lines.append("Lessons from the record of recent work (each one repeated before it was written down):")
        lines += [f"- {c['claim_text']}" for c in lessons[:8]]
    if unsure:
        lines.append("Unverified (your own earlier conclusions; check before relying on them):")
        lines += [f"- {c['claim_text']}" for c in unsure]
    return "\n".join(lines)


class SessionMemory:
    """Stands in for MemoryManager in a web session: the prompt summary comes from claims."""

    def __init__(self, base, store, session):
        self._base = base
        self._store = store
        self._session = session

    def summary(self, max_chars: int = 1500) -> str:
        text = prompt_section(self._store, self._session.project_dir)[: max(max_chars, 4000)]
        roster = people_roster(self._store)
        if roster:
            text += "\n" + roster
        goals = self._store.goals(self._session.project_dir, active_only=True)
        if goals:
            lines = ["", "[Goals the user is working toward in this folder]"]
            for g in goals[:8]:
                last = g["updates"][0]["note"][:160] if g["updates"] else "no progress logged yet"
                lines.append(f"- {g['title']} (id {g['goal_id']}; latest: {last})")
            lines.append("Log real progress on a goal with goal_log. A goal is finished when the user says so, "
                         "not because one step worked.")
            text += "\n".join(lines)
        return text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def memory_tools(store, session) -> List[Tool]:
    """The agent's own memory tools for one session."""

    def save(fact: str, source_path: str = "", scope: str = "project", kind: str = "fact",
             about: str = "", importance: str = "") -> str:
        state, basis = standing_for(session.ledger, fact, source_path)
        who = person_name(about)
        if about and not who:
            return "ERROR: 'about' must be the name of a person other than the user. Leave it out otherwise."
        if who:
            kind = "person"
        result = store.add_claim(
            fact, state, basis, kind=kind if kind in ("fact", "preference", "convention", "person") else "fact",
            source_path=source_path or None, session_id=session.id,
            # a person is the same person in every folder
            project_dir=None if scope == "everywhere" or who else session.project_dir,
            confidence=1.0 if state == "KNOWN" else 0.8 if state == "DERIVED" else 0.4,
            subject=who or None, salience=0.9 if str(importance).lower() == "high" else 0.5,
        )
        store.add_activity(session.id, "memory_saved", "Remembered: " + fact[:120], STANDING[state])
        note = {"added": "Saved", "reinforced": "Already known; strengthened", "superseded": "Saved; it replaces an older claim"}
        text = f"{note[result['outcome']]}. Standing: {STANDING[state]} ({basis})."
        if who:
            text = f"Filed under {who}. " + text
        if state == "CONFABULATED":
            text += " It is marked unverified until the user confirms it or you find it stated somewhere."
        return text

    def search(query: str) -> str:
        found = store.search_claims(query, session.project_dir)
        if not found:
            return "Nothing in memory matches."
        return "\n".join(
            f"- {c['claim_text']}  [{STANDING.get(c['state'], 'unverified')}; since {c['first_seen'][:10]}]"
            for c in found
        )

    def goal_create(title: str, description: str = "") -> str:
        try:
            goal = store.add_goal(title, description, session.project_dir)
        except ValueError as exc:
            return f"ERROR: {exc}"
        store.add_activity(session.id, "goal", "New goal: " + goal["title"][:120])
        return f"Goal {goal['goal_id']} created. Log progress on it with goal_log as real steps are finished."

    def goal_list() -> str:
        goals = store.goals(session.project_dir)
        if not goals:
            return "No goals in this folder."
        return "\n".join(
            f"{g['goal_id']}  {g['status']:9} {g['title']}  ({len(g['updates'])} progress notes)"
            + (f"\n    latest: {g['updates'][0]['note'][:200]}" if g["updates"] else "")
            for g in goals
        )

    def goal_log(goal_id: str, note: str) -> str:
        if not store.log_goal(goal_id, note, "agent", session.id):
            return f"ERROR: no goal {goal_id}, or the note was empty."
        store.add_activity(session.id, "goal", "Progress: " + str(note)[:120])
        return "Logged."

    def goal_complete(goal_id: str) -> str:
        if not store.set_goal_status(goal_id, "completed"):
            return f"ERROR: no goal {goal_id}."
        return "Marked completed. It can be reopened from Settings."

    def chat_search(query: str) -> str:
        rows = store.search_items(query)
        if not rows:
            return "No earlier message mentions all of those words. Try fewer or different words."
        return "\n".join(
            f"{r['created_at'][:10]} · {r['title'] or 'session'} · {'user' if r['kind'] == 'user' else 'you'}: "
            + " ".join(str(r["text"]).split())[:300]
            for r in rows
        )

    text_prop = {"type": "string"}
    goal_tools = [
        Tool(name="chat_search",
             description="Search earlier conversations (all sessions) for messages that mention the given words. "
                         "Use when the user refers to something said before that is not in memory.",
             parameters={"type": "object", "properties": {"query": text_prop}, "required": ["query"]},
             func=chat_search),
        Tool(name="goal_create",
             description="Create a durable goal the user is working toward in this folder. Only when the user states one.",
             parameters={"type": "object", "properties": {"title": text_prop, "description": text_prop}, "required": ["title"]},
             func=goal_create),
        Tool(name="goal_list", description="List this folder's goals with their latest progress.",
             parameters={"type": "object", "properties": {}}, func=goal_list),
        Tool(name="goal_log",
             description="Add a dated progress note to a goal: what actually got done, with evidence. Never log a plan as progress.",
             parameters={"type": "object", "properties": {"goal_id": text_prop, "note": text_prop}, "required": ["goal_id", "note"]},
             func=goal_log),
        Tool(name="goal_complete",
             description="Mark a goal completed. Only when the user says it is done.",
             parameters={"type": "object", "properties": {"goal_id": text_prop}, "required": ["goal_id"]},
             func=goal_complete, needs_approval=True),
    ]

    return goal_tools + [
        Tool(
            name="memory_save",
            description=(
                "Remember a lasting fact, preference or convention for future sessions. Save what the "
                "user tells you about themselves or how they want things done, and what you verify "
                "about this project. Pass source_path when the fact comes from a file you read. "
                "When the user tells you something about another person (a colleague, a client, "
                "a relative), pass that person's name as 'about': it is filed on their page and "
                "comes back when they are mentioned. Only what the user told you or a file states; "
                "never your impressions of someone. Each fact must stand on its own without this "
                "conversation. Something you or the user only proposed is not a decision: save it "
                "as decided only once the user agreed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "One self-contained sentence."},
                    "source_path": {"type": "string", "description": "Project file it comes from, if any."},
                    "scope": {"type": "string", "description": "'project' (default) or 'everywhere'."},
                    "kind": {"type": "string", "description": "fact, preference or convention (a way of working, a rule, something never to do)."},
                    "importance": {"type": "string", "description": "'high' for a rule that must always hold or a decision that shapes the work; otherwise leave out."},
                    "about": {"type": "string", "description": "A person's name, when the fact is about someone other than the user."},
                },
                "required": ["fact"],
            },
            func=save,
        ),
        Tool(
            name="memory_search",
            description="Search what was remembered in earlier sessions. Check here before saying what is or is not remembered.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Words to look for."}},
                "required": ["query"],
            },
            func=search,
        ),
    ]
