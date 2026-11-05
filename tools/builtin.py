"""Built-in tools: shell, file I/O, web fetching, and delegation.

File tools are sandboxed to the agent workspace: any path that resolves
outside of it is rejected. ``shell_exec``, ``write_file`` and ``edit_file``
require user approval unless the registry runs in auto-approve mode.
"""

from __future__ import annotations

import html
import os
import re
import urllib.request
from typing import Callable, List

from .registry import Tool

_MAX_OUTPUT = 6000
_FETCH_TIMEOUT = 20
_FETCH_MAX_BYTES = 300_000


def _resolve(workspace: str, path: str) -> str:
    """Resolve *path* inside *workspace*, rejecting escapes."""
    base = os.path.abspath(workspace)
    target = os.path.abspath(os.path.join(base, os.path.expanduser(path)))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"Path escapes the workspace: {path!r}")
    return target


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
    return text


# ---------------------------------------------------------------- shell ---


def _shell_exec(command: str, timeout: int = 60, workspace: str = ".") -> str:
    """Run a shell command, returning combined stdout/stderr."""
    from procutil import run_shell

    done = run_shell(command, cwd=workspace, timeout=timeout)
    if done.timed_out:
        return f"ERROR: command timed out after {timeout}s and was stopped: {command[:200]}"
    out = done.output.strip() or "(no output)"
    return f"[exit {done.code}]\n" + _truncate(out)


# ---------------------------------------------------------------- files ---


def _read_file(path: str, max_chars: int = 20000, workspace: str = ".") -> str:
    target = _resolve(workspace, path)
    if not os.path.isfile(target):
        return f"ERROR: file not found: {path!r}"
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        return _truncate(fh.read(), max_chars)


def _write_file(path: str, content: str, workspace: str = ".") -> str:
    target = _resolve(workspace, path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"Wrote {len(content)} chars to {path!r}"


def _edit_file(
    path: str, old_text: str, new_text: str, workspace: str = "."
) -> str:
    target = _resolve(workspace, path)
    if not os.path.isfile(target):
        return f"ERROR: file not found: {path!r}"
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    if old_text not in body:
        return f"ERROR: old_text not found in {path!r}; no changes made"
    body = body.replace(old_text, new_text, 1)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    return f"Edited {path!r} (replaced first occurrence)"


# ------------------------------------------------------------------ web ---


def _strip_html(page: str) -> str:
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    text = re.sub(r"<[^>]+>", " ", page)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _web_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return plain text. Failures become ERROR strings."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "ERROR: only http(s) URLs are supported"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CodeMonkey (https://github.com/mrtitanic777/CodeMonkey)",
            "Accept": "text/html,text/plain",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(_FETCH_MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - reported to the model
        return f"ERROR: could not fetch {url!r}: {type(exc).__name__}: {exc}"
    if len(raw) > _FETCH_MAX_BYTES:
        raw = raw[:_FETCH_MAX_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return f"ERROR: could not decode {url!r} as text"
    if "html" in content_type.lower():
        text = _strip_html(text)
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate(text, max_chars) or "(empty page)"


# -------------------------------------------------------------- delegate ---


def make_delegate_tool(spawn: Callable[[str], str]) -> Tool:
    """Build the ``delegate`` tool around a child-agent spawner.

    ``spawn`` receives the child's brief and returns its final result text.
    The AgentLoop wires this up so the tool exists only when nesting is
    allowed (one level deep).
    """
    return Tool(
        name="delegate",
        description=(
            "Spawn a child agent to handle a self-contained sub-task. "
            "Give a clear brief: the goal, the expected output format, and "
            "any constraints. The child's final answer is returned to you. "
            "Use it for parallelizable research or well-scoped chores; do "
            "the simple things yourself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "Complete instructions for the child agent.",
                }
            },
            "required": ["brief"],
        },
        func=lambda brief: spawn(brief),
        needs_approval=False,
    )


# -------------------------------------------------------------- bundle ---


def builtin_tools(workspace: str) -> List[Tool]:
    """Return the standard tool set, bound to *workspace*."""
    ws = workspace
    return [
        Tool(
            name="shell_exec",
            description=(
                "Run a shell command in the workspace directory. "
                "Returns exit code plus combined stdout/stderr. "
                "Requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "string",
                        "description": "Timeout in seconds (default 60).",
                    },
                },
                "required": ["command"],
            },
            func=lambda command, timeout=60: _shell_exec(
                command, int(timeout), ws
            ),
            needs_approval=True,
        ),
        Tool(
            name="read_file",
            description="Read a text file inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "string"},
                },
                "required": ["path"],
            },
            func=lambda path, max_chars=20000: _read_file(
                path, int(max_chars), ws
            ),
        ),
        Tool(
            name="write_file",
            description=(
                "Write (create or overwrite) a text file inside the workspace. "
                "Requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            func=lambda path, content: _write_file(path, content, ws),
            needs_approval=True,
        ),
        Tool(
            name="edit_file",
            description=(
                "Replace the first occurrence of old_text with new_text in a "
                "workspace file. Requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            func=lambda path, old_text, new_text: _edit_file(
                path, old_text, new_text, ws
            ),
            needs_approval=True,
        ),
        Tool(
            name="web_fetch",
            description=(
                "Fetch a web page and return its text content "
                "(HTML stripped, truncated). For research and lookups."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "string"},
                },
                "required": ["url"],
            },
            func=lambda url, max_chars=6000: _web_fetch(url, int(max_chars)),
        ),
    ]
