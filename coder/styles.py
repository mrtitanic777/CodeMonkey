"""Output styles: how the agent writes its replies, without changing what it does.

Built in: default (as it is), concise, explanatory, teaching. Your own: a Markdown file in styles/
next to CodeMonkey, or in a project's .codemonkey/styles/ (which wins for that project):

    ---
    name: reviewer-voice
    description: Short, blunt, findings first.
    ---
    Lead with the problems you found, most serious first ...

The style's text is added at the end of the system prompt, where it is read last.
"""

from __future__ import annotations

import os
from typing import Dict, List

from coder.commands import _split_frontmatter

BUILT_IN: Dict[str, Dict[str, str]] = {
    "default": {"description": "As it is: clear and direct.", "text": ""},
    "concise": {
        "description": "Short replies: the result first, no narration.",
        "text": ("Reply briefly. Lead with the result or the answer. Do not restate the request, do not narrate "
                 "the steps you took, and do not end with a recap. Use short plain sentences; lists and headings "
                 "only when they carry real structure. Keep full detail for errors, failing tests and warnings."),
    },
    "explanatory": {
        "description": "Explains the why: choices made, trade-offs, how the code works.",
        "text": ("As you work and in your reply, explain your reasoning: why you chose this approach over the "
                 "others, what the trade-offs are, and how the relevant code works. Add a short 'Why' note after "
                 "a non-obvious change. Point out patterns in this codebase worth knowing. Stay accurate: explain "
                 "only what you have actually read or run."),
    },
    "teaching": {
        "description": "Helps you learn: explains, then leaves small parts for you to write.",
        "text": ("The user wants to learn while you work. Explain concepts as they come up, at the level of "
                 "someone capable but new to this codebase. For small, self-contained pieces (a condition, a "
                 "short function, a test case), describe what is needed and leave a clearly marked TODO for the "
                 "user to write instead of writing it yourself, then offer to check their version. Do the "
                 "large or risky parts yourself."),
    },
}


def load(app_dir: str, project_dir: str = "", extra=()) -> Dict[str, Dict[str, str]]:
    styles = {k: dict(v, origin="built in") for k, v in BUILT_IN.items()}
    folders = [(app_dir, "yours")] + list(extra)
    if project_dir:
        folders.append((os.path.join(project_dir, ".codemonkey", "styles"), "project"))
    for folder, origin in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(folder, fname), "r", encoding="utf-8") as fh:
                    front, body = _split_frontmatter(fh.read())
            except OSError:
                continue
            name = str(front.get("name") or fname[:-3]).strip().lower()
            if name and body.strip():
                styles[name] = {"description": str(front.get("description") or ""), "text": body.strip(), "origin": origin}
    return styles


def instruction(styles: Dict[str, Dict[str, str]], name: str) -> str:
    """The text added to the system prompt for style *name* ("" for default or unknown)."""
    text = (styles.get(str(name or "default")) or {}).get("text", "")
    return f"\n\n[Reply style: {name}]\n{text}" if text else ""


def listing(styles: Dict[str, Dict[str, str]]) -> List[Dict[str, str]]:
    return [{"name": k, "description": v.get("description", ""), "origin": v.get("origin", "")} for k, v in styles.items()]
