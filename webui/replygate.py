"""The reply gate: a reply may not claim what the session's record does not show.

Labels on a reply tell you which statements are backed. This goes one step further for the
statements where being wrong costs you time: a test result, and "I have done it".

    1. The finished reply is checked against the record (no model involved).
    2. If it states a result that no command produced, one that is out of date because files
       changed afterwards, or work that was not done in this turn, it goes back to the model
       once: back the statement with a tool, or write the reply without it.
    3. Whatever is still unbacked after that is taken out of the reply, and the reply says so.

What it cannot do: tell whether backed statements are *true*, or check an opinion. It stops the
agent reporting things that did not happen.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

_BULLET = re.compile(r"\s*(?:[-*•]|\d+[.)])\s+")
_CODE = re.compile(r"```.*?```", re.S)


def hard(claims: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The claims in a labelled reply that may not stand: unbacked results and work not done."""
    out = []
    for c in (claims or {}).get("claims") or []:
        if c["kind"] == "result" and c["state"] in ("CONFABULATED", "CONFLICTED"):
            out.append(c)
        elif c["kind"] == "action" and c["state"] == "CONFABULATED":
            out.append(c)
    return out


def correction(found: List[Dict[str, Any]], broken: List[str]) -> str:
    """What the model is told when its reply is sent back."""
    lines = ["[Reply check, from the system, not the user] Your reply states things the record of this "
             "session does not support:"]
    lines += [f"- “{c['text']}”: {c['why']}" for c in found[:8]]
    if broken:
        lines.append("- Tests that passed before your changes and fail now: " + ", ".join(broken[:6]))
    lines.append(
        "Do one of two things now. Either back each statement by using a tool (make the change, run the "
        "tests after your last change), or write the reply again without it and say plainly what you did "
        "not do or did not verify. Do not repeat a statement you have not backed."
    )
    return "\n".join(lines)


def _sentence(text: str, start: int, end: int) -> Tuple[int, int]:
    """The bounds of the sentence (or list item) around text[start:end]."""
    begin = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start), text.rfind("! ", 0, start),
                text.rfind("? ", 0, start))
    begin = 0 if begin < 0 else begin + (1 if text[begin] == "\n" else 2)
    m = _BULLET.match(text, begin)
    if m and m.end() <= start:
        begin = m.end()
    stops = [i for i in (text.find(". ", end), text.find(".\n", end), text.find("! ", end),
                         text.find("? ", end), text.find("\n", end)) if i >= 0]
    if stops:
        finish = min(stops)
        finish += 0 if text[finish] == "\n" else 1
    else:
        finish = len(text)
    return begin, finish


def strike(reply: str, found: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Take the sentences holding unbacked claims out of *reply*. Returns (text, what was removed)."""
    removed: List[Dict[str, Any]] = []
    text = str(reply or "")
    for c in found:
        blocks = [(m.start(), m.end()) for m in _CODE.finditer(text)]
        at = -1
        search_from = 0
        while True:
            at = text.find(c["text"], search_from)
            if at < 0 or not any(a <= at < b for a, b in blocks):
                break
            search_from = at + 1   # inside a code block: content, not a claim
        if at < 0:
            continue   # it went with a sentence already removed
        begin, finish = _sentence(text, at, at + len(c["text"]))
        note = f"[Removed: an unbacked statement ({c['why']}).]"
        text = text[:begin] + note + text[finish:]
        removed.append(c)
    return text, removed


def footer(broken: List[str], stale: bool, changed: bool) -> str:
    """Plain lines the system adds under a reply, from the record alone."""
    lines = []
    if broken:
        lines.append("Not finished: tests that passed before this session's changes and fail now: "
                     + ", ".join(broken[:6]) + ("…" if len(broken) > 6 else "") + ".")
    elif changed and stale:
        lines.append("Not verified: files were changed in this turn and no tests were run afterwards.")
    return ("\n\n" + "\n".join(lines)) if lines else ""
