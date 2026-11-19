"""A quick look at a file the agent just wrote: does it still parse?

A model on this computer often finds out a file is broken only steps later, when a test run fails
with a message far from the cause. Checking right after the write, in the same tool result, puts
the line and the reason next to the change that caused them, while the model still has it in mind.

Only fast, certain checks: Python's own compiler, JSON's parser, `node --check` for JavaScript
when Node is installed. Nothing here runs the code. A file that parses gets no note at all, so a
result is only longer when there is something to fix. (C and C++ have their own check, with the
project's compiler, in webui/codeknow.py.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: Files bigger than this are not checked (a generated bundle, a data dump).
MAX_BYTES = 2_000_000


def _python(path: str, text: str) -> Optional[str]:
    try:
        compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" + (f", column {exc.offset}" if exc.offset else "")
        shown = (exc.text or "").rstrip("\n")
        return f"{exc.msg} ({where})" + (f":\n    {shown.strip()}" if shown.strip() else "")
    except (ValueError, TypeError) as exc:   # null bytes and the like
        return str(exc)
    return None


def _json(path: str, text: str) -> Optional[str]:
    if not text.strip():
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
    return None


def _javascript(path: str, text: str) -> Optional[str]:
    node = shutil.which("node")
    if not node:
        return None
    try:
        done = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return None
    lines = [ln for ln in (done.stderr or done.stdout).splitlines() if ln.strip()]
    # node prints: file:line, the line, a caret, a blank, "SyntaxError: ..."; keep the useful part
    where = next((ln for ln in lines if ln.startswith(path) or ln.endswith(tuple("0123456789"))), "")
    reason = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "it does not parse")
    line_no = where.rsplit(":", 1)[-1] if ":" in where else ""
    return reason.strip() + (f" (line {line_no})" if line_no.isdigit() else "")


CHECKS = {".py": _python, ".pyw": _python, ".json": _json, ".js": _javascript, ".mjs": _javascript,
          ".cjs": _javascript}


def problem(path: str) -> Optional[str]:
    """What is wrong with the file at *path*, in a line or two, or None (fine, or not checked)."""
    check = CHECKS.get(os.path.splitext(path)[1].lower())
    if check is None:
        return None
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if path.lower().endswith(".json") and os.path.basename(path).lower() in ("tsconfig.json", "jsconfig.json"):
        return None   # these allow comments
    return check(path, text)


def note(path: str, shown: str) -> str:
    """The line to add to a write's result: "" when the file is fine."""
    found = problem(path)
    if not found:
        return ""
    return (f"\n\nCHECK FAILED: {shown} no longer parses: {found}\n"
            "Fix this before going on; nothing that imports it will work.")
