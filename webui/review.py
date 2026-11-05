"""The daily review: what went well, what went wrong, and what to do differently.

Larger agent runtimes run a nightly pass where the model reflects on recent conversations. Here
the review is read off the record instead, with no model call, so every finding has a count and
a cause behind it:

    steps that failed, grouped by tool and by why
    actions you denied, and actions the guard had to question
    replies whose claims contradicted a result or named something that does not exist
    messages where you corrected the agent or had to ask again

A problem that repeats becomes a lesson: one line the agent sees at the start of every session,
kept as a memory claim whose basis is the record itself. A lesson that stops being reinforced is
retired after two weeks, so the list reflects how recent work actually went, not old history.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

LESSON_DAYS = 14
_CORRECTION = re.compile(
    r"^\s*(no[,.! ]|nope|wrong|that'?s (not|wrong)|not what i|i (said|told you|asked)|you (didn'?t|forgot|missed|broke)"
    r"|stop\b|undo\b|why did you|again[,.!]|still (broken|failing|wrong|not))", re.I)


def _cause(tool: str, text: str) -> str:
    """A failed step's reason, reduced to something that can be counted."""
    text = str(text or "")
    low = text.lower()
    if ("old_text" in low or "search block" in low) and ("not found" in low or "no match" in low):
        return "the text to replace was not in the file"
    if "blocked by the guard" in low:
        return "the guard refused it"
    if "denied approval" in low:
        return "you denied it"
    if "file not found" in low or "no such file" in low:
        return "the file does not exist"
    if "unknown tool" in low:
        return "it called a tool that does not exist"
    if "timed out" in low:
        return "it timed out"
    if "turned off in settings" in low:
        return "that kind of action is switched off"
    if tool == "bash":
        m = re.match(r"\[exit (-?\d+)\]", text)
        return f"the command exited with code {m.group(1)}" if m else "the command failed"
    first = text.strip().splitlines()[0] if text.strip() else "it failed"
    return re.sub(r"['\"][^'\"]{0,80}['\"]", "…", first.replace("ERROR:", "").strip())[:80]


def build(app, since: str, until: str) -> Dict[str, Any]:
    """Everything the record says about the period, as counts."""
    store = app.store
    tasks = Counter()
    failures: Counter = Counter()
    examples: Dict[Tuple[str, str], str] = {}
    steps = 0
    claims = Counter()
    corrections: List[str] = []
    repeats = 0
    last_user: Dict[str, str] = {}
    for row in store.items_between(since, until):
        kind = row["kind"]
        if kind == "tool":
            steps += 1
            if row["success"] == 0 or re.match(r"\[exit [^0\]]", str(row["text"] or "")):
                key = (row["tool_name"] or "?", _cause(row["tool_name"] or "", row["text"]))
                failures[key] += 1
                examples.setdefault(key, str(row["text"] or "").strip().splitlines()[0][:160] if row["text"] else "")
        elif kind == "user":
            text = str(row["text"] or "").strip()
            if _CORRECTION.search(text):
                corrections.append(text[:140])
            if text and last_user.get(row["agent_id"]) == text:
                repeats += 1
            last_user[row["agent_id"]] = text
        elif kind == "assistant":
            try:
                c = (json.loads(row["data_json"]).get("claims") or {})
            except (json.JSONDecodeError, AttributeError):
                c = {}
            for state, n in (c.get("counts") or {}).items():
                claims[state] += int(n)
    denied: Counter = Counter()
    guard = 0
    for a in store.activity_between(since, until):
        if a["activity_type"] == "task":
            tasks[a["status"]] += 1
        elif a["activity_type"] == "denied":
            denied[a["title"]] += 1
        elif a["activity_type"] == "guard":
            guard += 1
    return {
        "since": since, "until": until,
        "tasks": {"done": tasks["success"], "failed": tasks["error"], "stopped": tasks["stopped"]},
        "steps": steps,
        "failures": [
            {"tool": tool, "cause": cause, "count": n, "example": examples.get((tool, cause), "")}
            for (tool, cause), n in failures.most_common(8)
        ],
        "denied": [{"what": what, "count": n} for what, n in denied.most_common(6)],
        "guard": guard,
        "claims": dict(claims),
        "corrections": corrections[:6],
        "asked_again": repeats,
    }


def lessons_from(summary: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(lesson, why) for each problem that repeated enough to be worth a rule."""
    out: List[Tuple[str, str]] = []
    for f in summary["failures"]:
        n, tool, cause = f["count"], f["tool"], f["cause"]
        if n < 2:
            continue
        if cause == "the text to replace was not in the file":
            out.append(("Read the file again right before editing it and copy the text to replace exactly",
                        f"{n} edits failed because the text to replace was not in the file"))
        elif cause == "the file does not exist":
            out.append(("Check that a file exists (glob or grep) before reading or editing it",
                        f"{n} steps named a file that does not exist"))
        elif cause == "it called a tool that does not exist":
            out.append(("Only call tools that are listed; load a group with tools_load before using its tools",
                        f"{n} calls went to tools that do not exist"))
        elif cause == "it timed out":
            out.append((f"Give long {tool} commands a longer timeout or run a narrower one first",
                        f"{n} {tool} steps timed out"))
        elif cause == "the guard refused it":
            out.append(("Read a file before changing it", f"the guard refused {n} changes to files that were not read"))
    if summary["guard"] >= 2:
        out.append(("Look before acting: read a file before editing it, and check a path exists before using it",
                    f"the guard had to question {summary['guard']} actions"))
    for d in summary["denied"]:
        if d["count"] >= 2:
            out.append((f"Ask before proposing this again, the user keeps declining it: {d['what'][:90]}",
                        f"denied {d['count']} times"))
    if summary["claims"].get("CONFLICTED", 0) >= 1:
        out.append(("Never report tests or commands as passing unless the last run actually passed",
                    f"{summary['claims']['CONFLICTED']} replies contradicted a result"))
    if summary["claims"].get("UNKNOWN_REFERENCE", 0) >= 2:
        out.append(("Name only files you have seen in this session; check a path before citing it",
                    f"{summary['claims']['UNKNOWN_REFERENCE']} files named in replies do not exist"))
    if len(summary["corrections"]) + summary["asked_again"] >= 3:
        out.append(("Restate what was asked in one line before a large change, and confirm when it is ambiguous",
                    f"the user corrected or repeated a request {len(summary['corrections']) + summary['asked_again']} times"))
    seen, unique = set(), []
    for text, why in out:
        if text not in seen:
            seen.add(text)
            unique.append((text, why))
    return unique


def run(app, now: Optional[datetime.datetime] = None, force: bool = False) -> Optional[Dict[str, Any]]:
    """Review the last day once per day. Returns the review, or None when today's exists."""
    now = now or datetime.datetime.now()
    day = now.date().isoformat()
    store = app.store
    if not force and store.review(day) is not None:
        return None
    since = (now - datetime.timedelta(days=1)).isoformat(timespec="seconds")
    summary = build(app, since, now.isoformat(timespec="seconds"))
    lessons = lessons_from(summary)
    for text, why in lessons:
        store.add_claim(text, "DERIVED", "from the record: " + why, kind="lesson", salience=0.9, confidence=0.9)
    # a lesson nobody has needed for two weeks has done its job or was wrong: retire it
    cutoff = (now - datetime.timedelta(days=LESSON_DAYS)).isoformat(timespec="seconds")
    retired = []
    for claim in store.claims():
        if claim["kind"] == "lesson" and claim["reinforced_at"] < cutoff:
            retired.append(claim["claim_text"])
            store.retract_claim(claim["claim_id"])
    summary["lessons"] = [{"text": t, "why": w} for t, w in lessons]
    summary["retired"] = retired
    store.save_review(day, summary)
    return summary


def due(app, now: datetime.datetime) -> bool:
    """Once a day, after 3 in the morning, when there was anything to review."""
    return now.hour >= 3 and app.store.review(now.date().isoformat()) is None
