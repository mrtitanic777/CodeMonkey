"""Coding tools for the harness, sandboxed to a project directory.

Tools:
    read(path, offset, limit)   file read with line numbers
    glob(pattern)               filename search (skips .git, simple .gitignore)
    grep(pattern, path, context) regex search over file contents
    edit(path, old_text, new_text) exact-match replacement with diff preview
    write_file(path, content)   create/overwrite a file
    bash(cmd, timeout)          run a shell command in the project dir
    todo_write(todos)           replace the agent's todo list
    todo_list()                 show the current todo list

``edit``, ``write_file`` and ``bash`` require approval through the registry
gate; ``edit`` additionally shows a unified-diff preview. A *tracker* object
with ``record_edit(path, existed)`` / ``record_write(path, existed)`` methods
receives every successful edit/write so sessions can rewind them later.
"""

from __future__ import annotations

import difflib
import fnmatch
import functools
import json
import os
import re
from typing import Any, Dict, List, Optional

from tools import Tool

_MAX_OUTPUT = 6000
_GREP_MAX_MATCHES = 100
_GITIGNORE_FILES = (".gitignore",)


# ------------------------------------------------------------ sandbox ---


def _resolve_project(project_dir: str, path: str) -> str:
    """Resolve *path* inside *project_dir*, rejecting escapes."""
    base = os.path.abspath(project_dir)
    target = os.path.abspath(os.path.join(base, os.path.expanduser(path)))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"Path escapes the project directory: {path!r}")
    return target


def _rel(project_dir: str, abs_path: str) -> str:
    # forward slashes everywhere: the model, the ledger and the code graph all name files that way
    return os.path.relpath(abs_path, os.path.abspath(project_dir)).replace(os.sep, "/")


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
    return text


# ------------------------------------------------------------ ignores ---


def _load_gitignore(project_dir: str) -> List[str]:
    """Read simple .gitignore patterns (subset: names, *.ext, dir/)."""
    patterns: List[str] = []
    for name in _GITIGNORE_FILES:
        path = os.path.join(project_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                patterns.append(line)
    return patterns


@functools.lru_cache(maxsize=64)
def _compile_patterns(patterns: tuple) -> tuple:
    """Each pattern once, as (dir_only, has_slash, text, matcher).

    The matchers are fnmatch's own translation, compiled once instead of on every call, and
    case-blind where the file system is (fnmatch.fnmatch folds case through normcase).
    """
    flags = re.IGNORECASE if os.path.normcase("A") == "a" else 0
    out = []
    for pat in patterns:
        dir_only = pat.rstrip().endswith("/")
        p = pat.strip().strip("/")
        if p:
            out.append((dir_only, "/" in p, p, re.compile(fnmatch.translate(p), flags).match))
    return tuple(out)


def _is_ignored(rel_path: str, patterns: List[str], folders_checked: bool = False) -> bool:
    """Check *rel_path* (posix style) against simple gitignore patterns.

    A directory is passed with a trailing slash. "build/" ignores a folder of that name
    anywhere, with everything under it; "docs/build/" only that path; a plain name or glob
    matches a file or a folder. *folders_checked*: the folders above it were already let
    through (a walk that prunes), so only the last part is tested.
    """
    rel = rel_path.replace(os.sep, "/")
    is_dir = rel.endswith("/")
    rel = rel.strip("/")
    parts = rel.split("/")
    base = parts[-1]
    folders = [base] if (folders_checked and is_dir) else [] if folders_checked else (parts if is_dir else parts[:-1])
    for dir_only, has_slash, p, match in _compile_patterns(tuple(patterns)):
        if dir_only:
            if has_slash:
                if rel == p or rel.startswith(p + "/"):
                    return True
            elif any(match(folder) for folder in folders):
                return True
            continue
        if has_slash:
            if match(rel) or rel.startswith(p + "/"):
                return True
        elif match(base) or any(match(folder) for folder in folders):
            return True
    return False


_ALWAYS_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}


def _walk_entries(project_dir: str, patterns: List[str]):
    """Yield (abs_path, rel_path, DirEntry) for searchable files.

    Ignored folders are never entered, and each file is tested on its own name only (its
    folders were tested on the way down). The DirEntry carries the file's size and time, read
    with the folder listing on Windows, so callers need no stat of their own.
    """
    base = os.path.abspath(project_dir)
    stack = [(base, "")]
    while stack:
        folder, prefix = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name not in _ALWAYS_SKIP_DIRS and not _is_ignored(
                        prefix + entry.name + "/", patterns, folders_checked=True):
                    subdirs.append((entry.path, prefix + entry.name + "/"))
            elif not _is_ignored(prefix + entry.name, patterns, folders_checked=True):
                yield entry.path, prefix + entry.name, entry
        stack.extend(reversed(subdirs))


def _walk_files(project_dir: str, patterns: List[str]):
    """Yield (abs_path, rel_path) for searchable files."""
    for abs_path, rel, _ in _walk_entries(project_dir, patterns):
        yield abs_path, rel


def _looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(4096)
    except OSError:
        return True


# ---------------------------------------------------------------- read ---


def _read(path: str, offset: int = 1, limit: int = 200, project_dir: str = ".") -> str:
    target = _resolve_project(project_dir, path)
    if not os.path.isfile(target):
        return f"ERROR: file not found: {path!r}"
    import images

    if target.lower().endswith(".ipynb"):
        from coder import notebook

        return notebook.render(target)   # cells and outputs, not the raw JSON
    if images.is_image(target):
        # the picture itself, for a model that can see; the loop attaches it (images.py)
        return f"Image file {path} ({os.path.getsize(target):,} bytes).\n" + images.mark(target)
    if _looks_binary(target):
        return (f"ERROR: {path!r} is a binary file ({os.path.getsize(target):,} bytes), not text. "
                "Use bash with a tool that understands this format to inspect it.")
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    lines: List[str] = []
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if i < offset:
                    continue
                if i >= offset + limit:
                    break
                lines.append(f"{i:6d}\t{line.rstrip()}")
    except OSError as exc:
        return f"ERROR: cannot read {path!r}: {exc}"
    if not lines:
        return f"(file {path!r} has fewer than {offset} lines)"
    return "\n".join(lines)


# ----------------------------------------------------------------- git ---

#: The most of one file's diff returned at once; the rest is named, not sent.
GIT_DIFF_CHARS = 12_000


def _run_git(project_dir: str, *args: str) -> tuple:
    """(exit code, output) of a read-only git command in the project."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", os.path.abspath(project_dir), "--no-pager", *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"git could not run: {exc}"
    # stderr only when it failed: git warns on stderr about things like line endings
    return proc.returncode, (proc.stdout or "") + ((proc.stderr or "") if proc.returncode else "")


def _git_status(project_dir: str) -> str:
    code, out = _run_git(project_dir, "status", "--short", "--branch")
    if code != 0:
        return "ERROR: " + (out.strip() or "not a git repository")
    _, stat = _run_git(project_dir, "diff", "HEAD", "--stat")
    return out.rstrip() + ("\n\nChanged lines since the last commit:\n" + stat.rstrip() if stat.strip() else "")


def _git_diff(project_dir: str, path: str = "", staged: Any = False) -> str:
    staged = str(staged).lower() in ("1", "true", "yes")
    base = ["diff", "--cached"] if staged else ["diff"]
    if not path:
        code, out = _run_git(project_dir, *base, "--stat")
        if code != 0:
            return "ERROR: " + (out.strip() or "not a git repository")
        if not out.strip():
            return "No " + ("staged " if staged else "") + "changes."
        return out.rstrip() + "\n\nPass path to see one file's changes."
    _resolve_project(project_dir, path)   # inside the project, or an error
    code, out = _run_git(project_dir, *base, "-U3", "--", path)
    if code != 0:
        return "ERROR: " + out.strip()
    if not out.strip():
        return f"No {'staged ' if staged else ''}changes in {path}."
    if len(out) > GIT_DIFF_CHARS:
        hunks = out.count("\n@@")
        out = out[:GIT_DIFF_CHARS] + f"\n... (cut short: {len(out):,} characters, {hunks} hunks in all; read the file for the rest)"
    return out


# ---------------------------------------------------------------- glob ---


def _glob(pattern: str, project_dir: str = ".") -> str:
    patterns = _load_gitignore(project_dir)
    matches: List[str] = []
    for _, rel in _walk_files(project_dir, patterns):
        base = rel.rsplit("/", 1)[-1]
        if (
            fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch(base, pattern)
            or fnmatch.fnmatch("/" + rel, pattern)
        ):
            matches.append(rel)
    matches.sort()
    if not matches:
        return f"(no files matched {pattern!r})"
    return "\n".join(matches[:200]) + (
        f"\n... [{len(matches) - 200} more]" if len(matches) > 200 else ""
    )


# ---------------------------------------------------------------- grep ---


def _grep(
    pattern: str,
    path: str = ".",
    context: int = 0,
    project_dir: str = ".",
) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: invalid regex {pattern!r}: {exc}"
    start = _resolve_project(project_dir, path)
    patterns = _load_gitignore(project_dir)
    files: List[str] = []
    if os.path.isfile(start):
        files = [start]
    elif os.path.isdir(start):
        files = [
            abs_p
            for abs_p, _ in _walk_files(project_dir, patterns)
            if abs_p == start or abs_p.startswith(start + os.sep)
        ]
    else:
        return f"ERROR: path not found: {path!r}"
    context = max(0, int(context))
    out: List[str] = []
    total = 0
    for abs_p in sorted(files):
        if _looks_binary(abs_p):
            continue
        try:
            with open(abs_p, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        rel = _rel(project_dir, abs_p)
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            total += 1
            if total > _GREP_MAX_MATCHES:
                out.append(f"... [stopped after {_GREP_MAX_MATCHES} matches]")
                return "\n".join(out)
            lo = max(0, i - context)
            hi = min(len(lines), i + context + 1)
            for j in range(lo, hi):
                mark = ":" if j == i else "-"
                out.append(f"{rel}{mark}{j + 1}{mark}{lines[j][:300]}")
    if not out:
        return f"(no matches for {pattern!r})"
    return _truncate("\n".join(out))


# ---------------------------------------------------------------- edits ---


def _unified_diff(path: str, old_body: str, new_body: str) -> str:
    diff = difflib.unified_diff(
        old_body.splitlines(),
        new_body.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    text = "\n".join(diff)
    return text if text else "(no visible changes)"


def _diff_stat(path: str, old_body: str, new_body: str) -> str:
    """One-line summary like 'src/a.py (+3/-1 lines)' for approval UX."""
    diff = _unified_diff(path, old_body, new_body)
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"{path} (+{added}/-{removed} lines)"


_FUZZY_THRESHOLD = 0.8


def _squash(line: str) -> str:
    """Remove all whitespace: the fuzzy matcher's normalization."""
    return re.sub(r"\s+", "", line)


def _fuzzy_find(
    lines: List[str], old_lines: List[str], threshold: float = _FUZZY_THRESHOLD
) -> Optional[int]:
    """Find the best fuzzy match for *old_lines* in *lines*.

    Aider-style fallback for when the exact SEARCH block doesn't match:
    compares whitespace-collapsed text with difflib over a sliding window
    of the same line count, so "x=1" finds "    x = 1". Returns the start
    index, or None when nothing is similar enough. All-blank search blocks
    are refused (too risky).
    """
    n = len(old_lines)
    if n == 0 or n > len(lines):
        return None
    norm_old = [_squash(ln) for ln in old_lines]
    if not any(norm_old):
        return None
    target = "\n".join(norm_old)
    best_idx, best_score = -1, 0.0
    for i in range(len(lines) - n + 1):
        window = "\n".join(_squash(ln) for ln in lines[i : i + n])
        score = difflib.SequenceMatcher(None, target, window).ratio()
        if score > best_score:
            best_score, best_idx = score, i
    if best_score >= threshold:
        return best_idx
    return None


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(new_text: str, old_indent: str, file_indent: str) -> str:
    """Shift *new_text*'s indentation from *old_indent* to *file_indent*.

    Used by the fuzzy matcher: the model's SEARCH block may be indented
    differently than the file. Lines carrying the old base indent are
    shifted; other lines keep their relative shape.
    """
    if old_indent == file_indent:
        return new_text
    out: List[str] = []
    for line in new_text.splitlines():
        if not line.strip():
            out.append(line)
        elif line.startswith(old_indent):
            out.append(file_indent + line[len(old_indent) :])
        else:
            out.append(file_indent + line.lstrip())
    return "\n".join(out)


def _apply_line_replace(
    body: str, start_idx: int, n_old: int, new_text: str
) -> str:
    body_lines = body.splitlines()
    out = body_lines[:start_idx] + new_text.splitlines() + body_lines[start_idx + n_old :]
    text = "\n".join(out)
    if body.endswith("\n"):
        text += "\n"
    return text


def _compute_edit(body: str, old_text: str, new_text: str):
    """Compute the edited body, or None if old_text can't be matched.

    Returns (new_body, fuzzy_used). Exact substring match wins; otherwise
    an Aider-style fuzzy block match (whitespace-tolerant, re-indented).
    """
    if old_text in body:
        return body.replace(old_text, new_text, 1), False
    lines = body.splitlines()
    old_lines = old_text.splitlines()
    idx = _fuzzy_find(lines, old_lines)
    if idx is None:
        return None
    matched = lines[idx : idx + len(old_lines)]
    old_indent = _indent_of(old_lines[0]) if old_lines else ""
    file_indent = _indent_of(matched[0]) if matched else ""
    shifted = _reindent(new_text, old_indent, file_indent)
    return _apply_line_replace(body, idx, len(old_lines), shifted), True


def _diff_preview(project_dir: str, path: str, old_text: str, new_text: str) -> str:
    """Render the pending edit as a stat line plus unified diff.

    Shown inside the approval prompt (Cline-style) so the user sees exactly
    what applying the edit will do. Mirrors the fuzzy logic of the edit
    itself, so the preview never lies about the outcome.
    """
    try:
        target = _resolve_project(project_dir, path)
    except ValueError as exc:
        return f"(preview unavailable: {exc})"
    if not os.path.isfile(target):
        return f"(new content will be merged into {path!r} — file does not exist yet)"
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    computed = _compute_edit(body, old_text, new_text)
    if computed is None:
        return f"(preview unavailable: SEARCH block not found in {path!r})"
    new_body, fuzzy = computed
    stat = _diff_stat(path, body, new_body)
    note = " [fuzzy match: whitespace/indentation differed]" if fuzzy else ""
    return f"{stat}{note}\n{_unified_diff(path, body, new_body)}"


def _make_edit(project_dir: str, tracker: Any):
    def _edit(path: str, old_text: str, new_text: str) -> str:
        target = _resolve_project(project_dir, path)
        if not os.path.isfile(target):
            return f"ERROR: file not found: {path!r}"
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        computed = _compute_edit(body, old_text, new_text)
        if computed is None:
            return (
                f"ERROR: SEARCH block not found in {path!r} (tried exact "
                "and fuzzy matching); no changes made. Re-read the file "
                "and copy the block exactly."
            )
        new_body, fuzzy = computed
        diff = _unified_diff(path, body, new_body)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(new_body)
        if tracker is not None:
            tracker.record_edit(_rel(project_dir, target), existed=True)
        fuzzy_note = (
            " (fuzzy match applied: whitespace/indentation differed)"
            if fuzzy
            else ""
        )
        return f"Edited {path!r}{fuzzy_note}:\n{diff}"

    return _edit


def _make_write_file(project_dir: str, tracker: Any):
    def _write_file(path: str, content: str) -> str:
        target = _resolve_project(project_dir, path)
        existed = os.path.exists(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        if tracker is not None:
            tracker.record_write(_rel(project_dir, target), existed=existed)
        verb = "Overwrote" if existed else "Wrote"
        return f"{verb} {len(content)} chars to {path!r}"

    return _write_file


# ---------------------------------------------------------------- bash ---


def _shell_name() -> str:
    from procutil import shell_name

    return shell_name()


def _make_bash(project_dir: str):
    def _bash(cmd: str, timeout: int = 60, description: str = "") -> str:   # description: shown, not run
        try:
            timeout = max(1, int(timeout))
        except (TypeError, ValueError):
            timeout = 60
        from coder import sandbox
        from procutil import run_shell

        lvl = sandbox.level()
        refused = sandbox.check(cmd, os.path.abspath(project_dir), lvl)
        if refused:
            return (f"ERROR: blocked by the command sandbox ({lvl}): {refused}. Do it another way inside the "
                    "project, or tell the user what you need.")
        done = run_shell(cmd, cwd=os.path.abspath(project_dir), timeout=timeout, sandbox=lvl)
        if done.timed_out:
            return f"ERROR: command timed out after {timeout}s and was stopped: {cmd[:200]}"
        out = done.output.strip() or "(no output)"
        return f"[exit {done.code}]\n" + _truncate(out)

    return _bash


# ---------------------------------------------------------------- todos ---


class TodoState:
    """The agent's working todo list, optionally persisted to disk."""

    VALID = ("pending", "in_progress", "completed")

    def __init__(self, save_path: Optional[str] = None):
        self.items: List[Dict[str, str]] = []
        self.save_path = save_path
        if save_path and os.path.isfile(save_path):
            try:
                with open(save_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    self.items = [
                        {"title": str(i.get("title", "")), "status": "pending"}
                        for i in data
                        if isinstance(i, dict)
                    ]
            except (OSError, json.JSONDecodeError):
                pass

    def write(self, todos: List[Dict[str, str]]) -> str:
        clean: List[Dict[str, str]] = []
        for entry in todos:
            title = str(entry.get("title", "")).strip()
            status = str(entry.get("status", "pending")).strip()
            if not title:
                continue
            if status not in self.VALID:
                return (
                    f"ERROR: invalid status {status!r}; "
                    f"use one of {', '.join(self.VALID)}"
                )
            clean.append({"title": title, "status": status})
        self.items = clean
        self._save()
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "(todo list is empty)"
        marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = ["Todos:"]
        for i, item in enumerate(self.items, start=1):
            lines.append(f"  {marks[item['status']]} {i}. {item['title']}")
        return "\n".join(lines)

    def _save(self) -> None:
        if not self.save_path:
            return
        try:
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            with open(self.save_path, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, indent=2)
        except OSError:
            pass


# -------------------------------------------------------------- bundle ---


def _str_prop(description: str = "") -> Dict[str, Any]:
    return {"type": "string", "description": description}


def coder_tools(
    project_dir: str,
    auto_approve: bool = False,
    tracker: Any = None,
    todo_state: Optional[TodoState] = None,
    plan_mode: bool = False,
    processes: Any = None,
) -> List[Tool]:
    """Return the coding tool set bound to *project_dir*.

    In *plan_mode* only read-only tools are returned (read/glob/grep plus
    todo tracking); edit, write_file and bash are withheld.
    """
    state = todo_state or TodoState()
    tools = [
        Tool(
            name="read",
            description=(
                "Read a file in the project with line numbers. "
                "offset is 1-based; limit caps the number of lines. "
                "An image file (.png, .jpg, .gif, .webp) is shown to you as the picture itself."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_prop("Project-relative file path."),
                    "offset": _str_prop("First line to read (1-based, default 1)."),
                    "limit": _str_prop("Max lines to read (default 200)."),
                },
                "required": ["path"],
            },
            func=lambda path, offset=1, limit=200: _read(
                path, offset, limit, project_dir
            ),
        ),
        Tool(
            name="glob",
            description=(
                "Find files by glob pattern, e.g. '**/*.py' or 'src/*.js'. "
                "Skips .git and gitignored paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": _str_prop("Glob pattern to match."),
                },
                "required": ["pattern"],
            },
            func=lambda pattern: _glob(pattern, project_dir),
        ),
        Tool(
            name="grep",
            description=(
                "Search file contents with a regex. path may be a file or a "
                "directory (default: whole project). context adds surrounding "
                "lines. Skips .git, binaries, and gitignored paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": _str_prop("Regular expression to search for."),
                    "path": _str_prop("File or directory (default '.')."),
                    "context": _str_prop("Context lines around matches."),
                },
                "required": ["pattern"],
            },
            func=lambda pattern, path=".", context=0: _grep(
                pattern, path, context, project_dir
            ),
        ),
        Tool(
            name="todo_write",
            description=(
                "Replace the todo list with a new one. Each entry needs a "
                "'title' and a 'status': pending, in_progress, or completed. "
                "Use for multi-step work so progress stays visible."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The full new todo list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "status": {"type": "string"},
                            },
                            "required": ["title", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
            func=lambda todos: state.write(todos),
        ),
        Tool(
            name="todo_list",
            description="Show the current todo list.",
            parameters={"type": "object", "properties": {}},
            func=lambda: state.render(),
        ),
    ]
    tools.extend([
        Tool(
            name="git_status",
            description=("The project's git state: branch, changed and untracked files, and how many "
                         "lines changed in each since the last commit."),
            parameters={"type": "object", "properties": {}},
            func=lambda: _git_status(project_dir),
        ),
        Tool(
            name="git_diff",
            description=("What changed since the last commit. Without path: one line per changed file "
                         "with its added and removed lines. With path: that file's changes in full."),
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_prop("Project-relative file to show in full (optional)."),
                    "staged": _str_prop("true for changes already staged with git add (default false)."),
                },
            },
            func=lambda path="", staged=False: _git_diff(project_dir, path, staged),
        ),
    ])
    if plan_mode:
        return tools
    edit_func = _make_edit(project_dir, tracker)
    tools.extend(
        [
            Tool(
                name="edit",
                description=(
                    "Apply a SEARCH/REPLACE edit to a project file: "
                    "old_text is the SEARCH block (copy it exactly from the "
                    "file, including surrounding context lines), new_text "
                    "is the replacement. Exact match is tried first; if it "
                    "fails, a fuzzy block match tolerates whitespace and "
                    "indentation differences (the replacement is re-indented "
                    "to the file). Shows a unified-diff preview and requires "
                    "approval before applying."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": _str_prop("Project-relative file path."),
                        "old_text": _str_prop("Exact text to find."),
                        "new_text": _str_prop("Replacement text."),
                    },
                    "required": ["path", "old_text", "new_text"],
                },
                func=edit_func,
                needs_approval=True,
                preview=lambda args: _diff_preview(
                    project_dir,
                    str(args.get("path", "")),
                    str(args.get("old_text", "")),
                    str(args.get("new_text", "")),
                ),
            ),
            Tool(
                name="write_file",
                description=(
                    "Create or overwrite a file in the project. "
                    "Requires approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": _str_prop("Project-relative file path."),
                        "content": _str_prop("Full file content."),
                    },
                    "required": ["path", "content"],
                },
                func=_make_write_file(project_dir, tracker),
                needs_approval=True,
            ),
            Tool(
                name="bash",
                description=(
                    f"Run a shell command in the project directory. The shell is {_shell_name()}. "
                    "Returns exit code plus combined stdout/stderr. Requires approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "cmd": _str_prop("Shell command to run."),
                        "timeout": _str_prop("Timeout in seconds (default 60)."),
                        "description": _str_prop("A few words on what the command does, in the past tense, "
                                                 "for the person watching: 'Listed the project files', "
                                                 "'Ran the parser tests'."),
                    },
                    "required": ["cmd"],
                },
                func=_make_bash(project_dir),
                needs_approval=True,
            ),
        ]
    )
    tools.extend(_process_tools(project_dir, processes))

    def _nb_edit(path: str, cell: Any, source: str = "", cell_type: str = "", mode: str = "replace") -> str:
        from coder import notebook

        target = _resolve_project(project_dir, path)
        if not target.lower().endswith(".ipynb") or not os.path.isfile(target):
            return f"ERROR: not a notebook in the project: {path!r}"
        result = notebook.edit(target, cell, source, cell_type, mode)
        if tracker is not None and not result.startswith("ERROR"):
            tracker.record_edit(_rel(project_dir, target), existed=True)
        return result

    tools.append(Tool(
        name="notebook_edit",
        description=("Change one cell of a Jupyter notebook (.ipynb): mode replace (default), insert (a new cell "
                     "before cell; cell = the cell count adds it at the end) or delete. Cells are numbered from 0 as "
                     "read shows them. A changed code cell's old outputs are cleared. Requires approval."),
        parameters={
            "type": "object",
            "properties": {
                "path": _str_prop("Project-relative path of the notebook."),
                "cell": _str_prop("Cell number, from 0."),
                "source": _str_prop("The cell's new text (not needed to delete)."),
                "cell_type": _str_prop("code, markdown or raw (default: keep the cell's, or code for a new one)."),
                "mode": _str_prop("replace, insert or delete."),
            },
            "required": ["path", "cell"],
        },
        func=_nb_edit,
        needs_approval=True,
    ))
    return tools


def _process_tools(project_dir: str, processes: Any = None) -> List[Tool]:
    """Background programs (coder/processes.py) and the focused test run (coder/testfocus.py)."""
    from coder import testfocus
    from coder.processes import ProcessManager

    procs = processes if processes is not None else ProcessManager(project_dir)
    return [
        Tool(
            name="process_start",
            description=("Start a long-running program in the background (a dev server, a watcher) and "
                         "return at once; bash would wait for it or end it. Give it a short name to "
                         "refer to it later. wait_for: a regular expression for the line that says it "
                         "is ready (e.g. 'listening on|ready'), waited for up to wait_seconds. "
                         "Requires approval."),
            parameters={
                "type": "object",
                "properties": {
                    "command": _str_prop("The command, run in the project directory."),
                    "name": _str_prop("Short name for it, e.g. 'web' (default: the program's name)."),
                    "wait_for": _str_prop("Regular expression for its 'ready' line (optional)."),
                    "wait_seconds": _str_prop("How long to wait for it (default 20, at most 120)."),
                },
                "required": ["command"],
            },
            func=lambda command, name="", wait_for="", wait_seconds=20: procs.start(
                command, name, wait_for, float(wait_seconds or 20)),
            needs_approval=True,
        ),
        Tool(
            name="process_logs",
            description=("Output of a background program: what it printed since you last looked "
                         "(new_only, the default) or its last lines. wait_for: wait until a new line "
                         "matches this regular expression."),
            parameters={
                "type": "object",
                "properties": {
                    "name": _str_prop("The program's name."),
                    "lines": _str_prop("At most this many lines (default 60)."),
                    "new_only": _str_prop("false to see the last lines even if already read."),
                    "wait_for": _str_prop("Regular expression to wait for (optional)."),
                    "wait_seconds": _str_prop("How long to wait (default 20, at most 120)."),
                },
                "required": ["name"],
            },
            func=lambda name, lines=60, new_only=True, wait_for="", wait_seconds=20: procs.logs(
                name, int(lines or 60), str(new_only).lower() not in ("false", "0", "no"), wait_for,
                float(wait_seconds or 20)),
        ),
        Tool(
            name="process_stop",
            description="Stop a background program and everything it started.",
            parameters={"type": "object", "properties": {"name": _str_prop("The program's name.")},
                        "required": ["name"]},
            func=lambda name: procs.stop(name),
        ),
        Tool(
            name="process_list",
            description="The background programs started in this session and whether they are running.",
            parameters={"type": "object", "properties": {}},
            func=lambda: procs.listing(),
        ),
        Tool(
            name="test_focus",
            description=("Run only the tests that cover what changed: by default the files changed since "
                         "the last commit (git), or the paths you give. Python: changed test files, "
                         "test_<name>.py, and tests importing a changed module (pytest). JS/TS: Jest or "
                         "Vitest related tests. Says so when nothing matches. Requires approval."),
            parameters={"type": "object", "properties": {
                "paths": _str_prop("Changed files, comma-separated (default: from git).")}},
            func=lambda paths="": testfocus.run(project_dir, paths),
            needs_approval=True,
        ),
    ]
