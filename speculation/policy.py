"""Speculative execution safety policy.

What may be pre-executed, and under what arguments. This is the first
line of defense: only read-only tools with conservative, project-contained
arguments are ever eligible for prefetch. Everything else — writes, edits,
shell, delegation, unknown tools — is rejected here, before any compute is
spent. The second line of defense is exact-match verification in
:mod:`speculation.engine`: a cached result is served only for a
byte-identical (tool, normalized-args) call.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

# The only tools that may ever be speculatively pre-executed. Read-only by
# construction. The engine additionally refuses any tool whose
# needs_approval flag is set, so a future approval-gated read can never
# slip through on name alone.
SAFE_TOOLS = frozenset({"read", "glob", "grep"})

# Per-tool allowlisted argument names. Unknown kwargs are rejected: a
# proposal with args the real tool would reject is a wasted prefetch by
# definition, so there is no reason to run it.
_ALLOWED_ARGS = {
    "read": frozenset({"path", "offset", "limit"}),
    "glob": frozenset({"pattern"}),
    "grep": frozenset({"pattern", "path", "context"}),
}

# Cost bounds. Prefetch must stay cheap, and small outputs keep a cache hit
# byte-consistent with what the live (possibly condensing) stack would have
# returned for the same call — below the condensation thresholds the live
# stack passes results through untouched.
MAX_PATH_CHARS = 500
MAX_PATTERN_CHARS = 300
MAX_READ_LIMIT = 2000
MAX_GREP_CONTEXT = 10

_SCALAR_TYPES = (str, int, float, bool)


def _within_project(project_dir: Optional[str], path: str) -> bool:
    """True if *path* stays inside *project_dir* (or is lexically safe).

    Mirrors the containment rule of the coding tools' ``_resolve_project``
    but returns a bool instead of raising: speculation must never raise.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    if path.startswith("~"):
        return False  # expanduser could leave the project; reject outright
    if path.startswith("\\") or (len(path) > 1 and path[1] == ":" and os.sep != "\\"):
        return False  # a Windows-rooted path is never inside the project, on any system
    if project_dir:
        base = os.path.abspath(project_dir)
        target = os.path.abspath(
            os.path.join(base, os.path.expanduser(path))
        )
        try:
            return os.path.commonpath([base, target]) == base
        except ValueError:
            return False
    # No project root known: pure lexical check on the relative path.
    # Rooted or drive paths are refused on every system. os.path.isabs alone lets "/etc/passwd"
    # through on Windows, where a path is only "absolute" with a drive letter.
    if os.path.isabs(path) or path.startswith(("/", "\\")) or (len(path) > 1 and path[1] == ":"):
        return False
    norm = os.path.normpath(path)
    return norm != ".." and not norm.startswith(".." + os.sep)


def _to_int(value: Any) -> Optional[int]:
    """Coerce an int-like value; None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _path_ok(args: Dict[str, Any], key: str, project_dir: Optional[str],
             required: bool = True) -> bool:
    if key not in args:
        return not required
    path = args[key]
    return (
        isinstance(path, str)
        and 0 < len(path) <= MAX_PATH_CHARS
        and _within_project(project_dir, path)
    )


def _pattern_ok(args: Dict[str, Any], key: str = "pattern") -> bool:
    pattern = args.get(key)
    return (
        isinstance(pattern, str) and 0 < len(pattern) <= MAX_PATTERN_CHARS
    )


def _read_ok(args: Dict[str, Any], project_dir: Optional[str]) -> bool:
    if not _path_ok(args, "path", project_dir, required=True):
        return False
    if "offset" in args:
        offset = _to_int(args["offset"])
        if offset is None or offset < 1:
            return False
    if "limit" in args:
        limit = _to_int(args["limit"])
        if limit is None or limit < 1 or limit > MAX_READ_LIMIT:
            return False
    return True


def _glob_ok(args: Dict[str, Any]) -> bool:
    return _pattern_ok(args)


def _grep_ok(args: Dict[str, Any], project_dir: Optional[str]) -> bool:
    if not _pattern_ok(args):
        return False
    if not _path_ok(args, "path", project_dir, required=False):
        return False
    if "context" in args:
        context = _to_int(args["context"])
        if context is None or context < 0 or context > MAX_GREP_CONTEXT:
            return False
    return True


def is_speculatable(tool_name: str, args: Dict[str, Any],
                    project_dir: Optional[str] = None,
                    tool: Any = None) -> bool:
    """Conservative eligibility check for speculative pre-execution.

    Returns True only for read-only tools (``read``/``glob``/``grep``)
    with allowlisted, scalar, project-contained arguments inside the cost
    bounds. Never raises: any doubt returns False.
    """
    try:
        if tool_name not in SAFE_TOOLS:
            return False
        # Defense in depth: even a safe *name* is refused if the registered
        # tool demands approval (e.g. a custom approval-gated "read").
        if tool is not None and getattr(tool, "needs_approval", False):
            return False
        if not isinstance(args, dict):
            return False
        allowed = _ALLOWED_ARGS[tool_name]
        if any(key not in allowed for key in args):
            return False
        if any(
            not isinstance(value, _SCALAR_TYPES) and value is not None
            for value in args.values()
        ):
            return False
        if tool_name == "read":
            return _read_ok(args, project_dir)
        if tool_name == "glob":
            return _glob_ok(args)
        return _grep_ok(args, project_dir)
    except Exception:
        return False
