"""Project context packs: stable, deterministic, cached project prefixes.

The client-side half of persistent project memory. True KV-cache
persistence lives in the inference engine (vLLM/SGLang), which we do not
control. What we *can* control is the prompt prefix: if every session
starts with a byte-identical project block for an unchanged project, the
engine's own prefix caching hits and the project is effectively "already
loaded".

A pack is a plain-JSON-serializable dict::

    {
        "version": 1,
        "project_dir": "/abs/path",
        "files": [
            {"path": "a/b.py", "mtime": 1234.0, "size": 100,
             "summary": "one line", "symbols": [
                 {"kind": "class", "name": "Foo", "line": 12, "doc": "..."},
             ]},
            ...
        ],
    }

Summaries are derived *deterministically without model calls*:
for Python, the module docstring's first line (via ``ast``); otherwise the
first comment line; otherwise the first non-empty line. v1 deliberately
uses no model-based summaries — they would be non-deterministic and would
break prefix-cache hits.

Ignore logic (gitignore subset, always-skip dirs, binary detection) is
reused from ``coder.tools`` so packs see exactly the files the agent's
glob/grep see.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
from typing import Dict, List, Optional, Tuple

from coder.tools import _load_gitignore, _looks_binary, _walk_entries

PACK_VERSION = 1
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_FILE_BYTES = 100_000
SUMMARY_MAX_CHARS = 160

# First-comment-line prefixes, longest first (matched in order).
_COMMENT_PREFIXES = ("<!--", "//", "/*", "--", "#", "%", ";", "*")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:SUMMARY_MAX_CHARS]
    return ""


def _first_comment_line(text: str) -> str:
    """First meaningful *leading* comment line (shebangs skipped).

    Returns "" when the file starts with code rather than a comment block,
    so the caller falls back to the first non-empty line.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        matched = False
        for prefix in _COMMENT_PREFIXES:
            if stripped.startswith(prefix):
                matched = True
                body = stripped[len(prefix):].strip(" -*#/<>!").strip()
                if body:
                    return body[:SUMMARY_MAX_CHARS]
                break
        if not matched:
            return ""
    return ""


def _python_summary_and_symbols(text: str) -> Tuple[str, List[Dict]]:
    """Module docstring first line + class/function index via ast.

    Never raises: syntax errors fall back to comment/first-line logic with
    an empty symbol list.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return "", []
    doc = ast.get_docstring(tree) or ""
    summary = doc.strip().splitlines()[0][:SUMMARY_MAX_CHARS] if doc.strip() else ""
    symbols: List[Dict] = []

    def visit(node, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qn = f"{qualname}.{child.name}" if qualname else child.name
                symbols.append(_symbol("class", qn, child))
                visit(child, qn)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = f"{qualname}.{child.name}" if qualname else child.name
                symbols.append(_symbol("function", qn, child))
                visit(child, qn)

    visit(tree, "")
    symbols.sort(key=lambda s: (s["line"], s["name"]))
    return summary, symbols


def _symbol(kind: str, name: str, node) -> Dict:
    doc = ast.get_docstring(node) or ""
    first = doc.strip().splitlines()[0][:SUMMARY_MAX_CHARS] if doc.strip() else ""
    return {"kind": kind, "name": name, "line": node.lineno, "doc": first}


def _summarize(rel_path: str, text: str) -> Tuple[str, List[Dict]]:
    if rel_path.endswith(".py"):
        summary, symbols = _python_summary_and_symbols(text)
        if summary:
            return summary, symbols
        if symbols:
            # No module docstring, but we found symbols: name the first one.
            first = symbols[0]
            return (
                f"{first['kind']} {first['name']}".strip()[:SUMMARY_MAX_CHARS],
                symbols,
            )
        # fall through to generic logic below (keeps symbols == [])
    comment = _first_comment_line(text)
    if comment:
        return comment, []
    return _first_line(text), []


def _scan_file(abs_path: str, rel_path: str) -> Optional[Dict]:
    """Read and summarize one file; None if it should be skipped."""
    try:
        size = os.path.getsize(abs_path)
        if size > DEFAULT_MAX_FILE_BYTES:
            return None
        if _looks_binary(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(DEFAULT_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(text) > DEFAULT_MAX_FILE_BYTES:
        return None
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return None
    summary, symbols = _summarize(rel_path, text)
    return {
        "path": rel_path,
        "mtime": mtime,
        "size": size,
        "summary": summary,
        "symbols": symbols,
    }


def _cache_path(project_dir: str, cache_dir: str) -> str:
    digest = hashlib.sha256(
        os.path.abspath(project_dir).encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir, f"pack_{digest}.json")


def _fresh_entry(entry: Dict, abs_path: str, st=None) -> bool:
    """True if the cached entry matches the file on disk (*st*: its stat, when already known)."""
    try:
        st = st or os.stat(abs_path)
    except OSError:
        return False
    return (
        entry.get("size") == st.st_size
        and abs(entry.get("mtime", 0) - st.st_mtime) < 1e-6
    )


#: What the model reads first, and what keeps its detail when the project is too big for the
#: pack. Alphabetical order lets one deep folder of noise take every slot; this puts the code and
#: docs of the whole project first, and the shallow ones before the deeply nested ones.
_SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".go",
    ".rs", ".rb", ".java", ".kt", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue",
    ".s", ".asm", ".sql", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".html", ".css",
    ".scss", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".rst",
    ".txt", ".mak", "makefile", ".cmake", ".gradle", ".xml", ".proto", ".graphql",
})


def _rank(rel_path: str) -> Tuple[int, int, str]:
    """Sort key for the pack: code and docs before other files, shallow before deep, then path."""
    ext = os.path.splitext(rel_path)[1].lower()
    return (0 if ext in _SOURCE_EXTS else 1, rel_path.count("/"), rel_path)


#: The last pack read or written per cache file, with the file's mtime: a session opened again
#: in the same process does not parse the whole pack again. A parsed pack is tens of kilobytes,
#: so the memo is capped: the oldest project drops out when a new one arrives.
_MEMO: Dict[str, Tuple[float, Dict]] = {}
_MEMO_MAX = 12


def _memo_put(cache_path: str, mtime: float, data: Dict) -> None:
    """Remember one parsed pack, keeping the memo to the last _MEMO_MAX projects."""
    _MEMO[cache_path] = (mtime, data)
    while len(_MEMO) > _MEMO_MAX:
        _MEMO.pop(next(iter(_MEMO)))


def _load_cached(cache_path: str) -> Optional[Dict]:
    try:
        mtime = os.stat(cache_path).st_mtime
        memo = _MEMO.get(cache_path)
        if memo is not None and memo[0] == mtime:
            data = memo[1]
        else:
            with open(cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            _memo_put(cache_path, mtime, data)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != PACK_VERSION:
        return None
    if not isinstance(data.get("files"), list):
        return None
    return data


def build_pack(
    project_dir: str,
    cache_dir: str,
    max_files: int = DEFAULT_MAX_FILES,
) -> Dict:
    """Build a pack, reusing cached per-file entries when fresh.

    Walks the project with the repo's ignore logic, re-scans only files
    whose mtime/size changed since the cached pack, drops deleted files,
    and writes the updated pack back to *cache_dir*. Returns the pack dict.
    Ephemeral ``_cache_hits``/``_cache_misses`` counters are attached (not
    serialized) for :func:`pack_stats`.
    """
    base = os.path.abspath(project_dir)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = _cache_path(base, cache_dir)
    cached = _load_cached(cache_path) or {}
    cached_files = {
        e["path"]: e for e in cached.get("files", []) if isinstance(e, dict)
    }

    patterns = _load_gitignore(base)
    found = sorted(((rel, entry) for _, rel, entry in _walk_entries(base, patterns)),
                   key=lambda t: _rank(t[0]))[:max_files]

    files: List[Dict] = []
    hits = misses = 0
    for rel, dir_entry in found:
        abs_path = os.path.join(base, rel)
        old = cached_files.get(rel)
        try:
            st = dir_entry.stat()   # from the folder listing on Windows: no extra disk call
        except OSError:
            st = None
        if old is not None and _fresh_entry(old, abs_path, st):
            files.append(old)
            hits += 1
            continue
        entry = _scan_file(abs_path, rel)
        if entry is not None:
            files.append(entry)
            misses += 1
        # skipped files (binary/oversize/unreadable) are simply absent and
        # are re-examined on the next build; they count as neither hit nor
        # miss

    unchanged = misses == 0 and len(files) == len(cached.get("files", [])) and cached.get("project_dir") == base
    pack = {
        "version": PACK_VERSION,
        "project_dir": base,
        "built_at": cached.get("built_at", time.time()) if unchanged else time.time(),
        "files": files,
    }
    if not unchanged:   # nothing changed: the pack on disk is already this one
        try:
            serializable = dict(pack)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(serializable, fh)
            _memo_put(cache_path, os.stat(cache_path).st_mtime, serializable)
        except OSError:
            pass
    pack = dict(pack)
    pack["_cache_hits"] = hits
    pack["_cache_misses"] = misses
    return pack


def load_pack(
    project_dir: str,
    cache_dir: str,
    max_files: int = DEFAULT_MAX_FILES,
) -> Dict:
    """Return the pack for *project_dir*, rebuilding stale entries.

    This is the main entry point: identical to :func:`build_pack` — the
    cache lives inside the build, so "load" and "build" are one operation.
    A fully fresh project does zero file re-scans.
    """
    return build_pack(project_dir, cache_dir, max_files=max_files)


def render_prefix(pack: Dict, max_chars: int = 30_000) -> str:
    """Render the stable prompt prefix for a pack, most useful files first.

    Fixed ordering, fixed format, no timestamps: byte-identical across
    sessions for an unchanged project, which is what lets the inference
    engine's prefix cache hit.

    The budget is shared by kind, best-ranked file first: the paths (up to a
    third of it), then summaries and symbol names, each taking about half of
    what the paths leave. Listing every path, then every summary, then every
    symbol and cutting the result at the limit instead loses whichever
    section happens to be last — for a project of any size, that is the
    symbols, the one part the model cannot recover from a path name.
    """
    files = sorted(pack.get("files", []), key=lambda f: _rank(f.get("path", "")))
    #: Room kept back for the closing note, so it is never cut off mid-sentence.
    limit = max_chars - 200
    lines: List[str] = [
        "[PROJECT CONTEXT]",
        f"Project: {pack.get('project_dir', '')}",
        f"Files: {len(files)}",
        "",
        "## File tree",
    ]
    used = sum(len(line) + 1 for line in lines)

    def fits(text: str, room: int) -> bool:
        return used + len(text) + 1 <= room

    # The tree is the cheap part and the map the rest is read against, so it
    # goes first — but never more than a share of the budget, or a project
    # with long paths leaves no room for anything else.
    tree_cap = max_chars - (max_chars // 3)
    shown = 0
    for f in files:
        if not fits(f.get("path", ""), tree_cap):
            break
        lines.append(f["path"])
        used += len(f["path"]) + 1
        shown += 1
    if shown < len(files):
        more = f"... and {len(files) - shown} more files"
        lines.append(more)
        used += len(more) + 1

    lines.extend(["", "## Files"])
    used += len("## Files") + 1
    #: Half of whatever the tree left is held back for symbols. Filling sections
    #: greedily in one order lets whichever is largest take everything: here the
    #: paths and summaries alone need 31,661 chars, so the symbols lost all of it.
    #: A fixed share of the whole budget fails the other way, at a smaller budget
    #: where the tree already fills that share and no summary fits at all.
    summary_room = limit - (limit - used) // 2
    detailed = 0
    for f in files:
        head = f"### {f.get('path', '')}"
        summary = f.get("summary") or "(no summary)"
        if not fits(head, summary_room) or not fits(summary, summary_room):
            break
        lines.append(head)
        lines.append(summary)
        used += len(head) + len(summary) + 2
        detailed += 1
    if detailed < len(files):
        more = (f"({len(files) - detailed} more files are in the tree above without a "
                "summary; use glob or grep to look them up.)")
        lines.append(more)
        used += len(more) + 1

    # What is left buys symbol names, best-ranked file first: the index the
    # model uses to aim at a function without reading the file.
    indexed = 0
    for f in files:
        names = ", ".join(s["name"] for s in f.get("symbols", []))
        if not names:
            continue
        line = f"{f['path']}: {names}"
        if not fits(line, limit):
            break
        if not indexed:
            lines.extend(["", "## Symbols"])
            used += len("## Symbols") + 1
        lines.append(line)
        used += len(line) + 1
        indexed += 1
    text = "\n".join(lines) + "\n"
    if len(text) > max_chars:      # only the header can overrun it, on a huge path
        text = text[:max_chars] + "\n... [pack truncated]"
    return text


def pack_stats(pack: Dict) -> Dict:
    """Small stats dict: files, symbols, chars, cache hits/misses."""
    files = pack.get("files", [])
    symbols = sum(len(f.get("symbols", [])) for f in files)
    chars = sum(
        len(f.get("path", "")) + len(f.get("summary", ""))
        + sum(len(s.get("name", "")) + len(s.get("doc", "")) for s in f.get("symbols", []))
        for f in files
    )
    return {
        "files": len(files),
        "symbols": symbols,
        "chars": chars,
        "cache_hits": pack.get("_cache_hits", 0),
        "cache_misses": pack.get("_cache_misses", 0),
    }
