"""Permission rules: allow, ask and deny patterns for tool calls.

A rule is ``Tool`` or ``Tool(pattern)``. The tool part may use * (``mcp__github__*``). What the
pattern is matched against depends on the tool:

  bash, process_start   the command. ``npm test*`` is a glob over the whole command;
                        ``git:*`` matches any command that starts with ``git``. A chained
                        command (&&, ||, ;, |) is split: allow needs every part allowed,
                        deny takes any part denied.
  read, edit, write_file, glob, grep, notebook_edit, diagnostics
                        the file path, relative to the project, with / separators.
                        ``src/**`` is everything under src, ``**/.env`` any .env file.
  web_fetch             ``domain:example.com`` (and its subdomains), or a URL glob.
  web_search            the query.
  anything else         the first argument's value; no pattern means every call.

Precedence: deny, then ask, then allow. Deny refuses without asking, in every mode. Ask always
asks, even in Auto. Allow lets the call run without asking (the kernel guard can still ask).

Rules come from three places, all applied together:
  Settings > Permissions (this computer, every project), and in the project
  ``.codemonkey/settings.json`` (shared with the team, committed) and
  ``.codemonkey/settings.local.json`` (yours, gitignored), as
  {"permissions": {"allow": [...], "ask": [...], "deny": [...]}}.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

KINDS = ("deny", "ask", "allow")
_RULE = re.compile(r"^\s*([A-Za-z0-9_*?.\-]+)\s*(?:\((.*)\))?\s*$", re.S)
_CHAIN = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_PATH_TOOLS = {"read", "edit", "write_file", "glob", "grep", "notebook_edit", "diagnostics",
               "lsp_definition", "lsp_references", "lsp_hover"}


def parse(rule: str) -> Optional[Tuple[str, Optional[str]]]:
    """(tool glob, pattern or None), or None for text that is not a rule."""
    m = _RULE.match(str(rule or ""))
    if not m:
        return None
    pattern = m.group(2)
    return m.group(1), (pattern.strip() if pattern is not None else None)


def check(rule: str) -> str:
    """Why *rule* is not valid, or "" when it is."""
    parsed = parse(rule)
    if parsed is None:
        return "write Tool or Tool(pattern), e.g. bash(npm test:*) or read(**/.env)"
    if parsed[1] == "":
        return "the pattern in () is empty; leave out the () to match every call"
    return ""


def _glob(pattern: str, text: str) -> bool:
    """fnmatch, where ** also crosses folders and a leading **/ also matches at the top."""
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*") \
        .replace(r"\?", "[^/]")
    return re.fullmatch(rx, text, re.I if os.name == "nt" else 0) is not None


def _command_matches(pattern: str, command: str) -> bool:
    command = " ".join(str(command).split())
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        return command == prefix or command.startswith(prefix + " ")
    return fnmatch.fnmatchcase(command, pattern)


def _rel_path(path: str, project_dir: str) -> str:
    path = str(path or "").replace("\\", "/")
    if project_dir and os.path.isabs(path.replace("/", os.sep)):
        try:
            rel = os.path.relpath(path, project_dir).replace("\\", "/")
            if not rel.startswith(".."):
                return rel
        except ValueError:   # another drive
            pass
        return path
    return path[2:] if path.startswith("./") else path


def _target(tool: str, args: Dict[str, Any], project_dir: str) -> Any:
    args = args or {}
    if tool in ("bash", "process_start"):
        return str(args.get("cmd") or args.get("command") or "")
    if tool in _PATH_TOOLS:
        return _rel_path(args.get("path") or args.get("file") or "", project_dir)
    if tool == "web_fetch":
        return str(args.get("url") or "")
    if tool == "web_search":
        return str(args.get("query") or "")
    for value in args.values():
        return str(value)
    return ""


def matches(rule: str, tool: str, args: Dict[str, Any], project_dir: str = "") -> bool:
    parsed = parse(rule)
    if parsed is None:
        return False
    tool_glob, pattern = parsed
    if not fnmatch.fnmatchcase(tool, tool_glob):
        return False
    if pattern is None:
        return True
    target = _target(tool, args, project_dir)
    if tool in ("bash", "process_start"):
        return _command_matches(pattern, target)
    if tool in _PATH_TOOLS:
        return _glob(pattern.replace("\\", "/").lstrip("./") if pattern.startswith("./") else pattern.replace("\\", "/"),
                     target)
    if tool == "web_fetch" and pattern.startswith("domain:"):
        host = (urllib.parse.urlparse(target).hostname or "").lower()
        want = pattern[7:].strip().lower()
        return host == want or host.endswith("." + want)
    return fnmatch.fnmatchcase(target, pattern)


def _parts(tool: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A chained command as its separate commands; any other call as itself."""
    if tool not in ("bash", "process_start"):
        return [args or {}]
    key = "cmd" if tool == "bash" else "command"
    command = str((args or {}).get(key) or "")
    pieces = [p for p in _CHAIN.split(command) if p.strip()]
    return [dict(args or {}, **{key: p}) for p in pieces] or [args or {}]


def decide(rules: Dict[str, List[str]], tool: str, args: Dict[str, Any],
           project_dir: str = "") -> Tuple[Optional[str], str]:
    """("deny" | "ask" | "allow" | None, the rule that decided)."""
    parts = _parts(tool, args)
    whole = args or {}
    for rule in rules.get("deny") or []:
        if matches(rule, tool, whole, project_dir) or any(matches(rule, tool, p, project_dir) for p in parts):
            return "deny", rule
    for rule in rules.get("ask") or []:
        if matches(rule, tool, whole, project_dir) or any(matches(rule, tool, p, project_dir) for p in parts):
            return "ask", rule
    allows = rules.get("allow") or []
    for rule in allows:
        if matches(rule, tool, whole, project_dir):
            return "allow", rule
    if len(parts) > 1:   # a chain is allowed only when each of its commands is
        hits = [next((r for r in allows if matches(r, tool, p, project_dir)), None) for p in parts]
        if all(hits):
            return "allow", ", ".join(dict.fromkeys(hits))
    return None, ""


def project_rules(project_dir: str) -> Dict[str, List[str]]:
    """The rules in the project's .codemonkey/settings.json and settings.local.json."""
    out: Dict[str, List[str]] = {k: [] for k in KINDS}
    for name in ("settings.json", "settings.local.json"):
        path = os.path.join(project_dir or ".", ".codemonkey", name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        for kind in KINDS:
            for rule in (perms or {}).get(kind) or []:
                if isinstance(rule, str) and not check(rule):
                    out[kind].append(rule)
    return out


def merged(*sources: Optional[Dict[str, Iterable[str]]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {k: [] for k in KINDS}
    for source in sources:
        for kind in KINDS:
            for rule in (source or {}).get(kind) or []:
                if isinstance(rule, str) and rule not in out[kind] and not check(rule):
                    out[kind].append(rule)
    return out


def describe(rules: Dict[str, List[str]]) -> str:
    rows = [f"{kind}: {', '.join(rules.get(kind) or [])}" for kind in KINDS if rules.get(kind)]
    return "\n".join(rows) or "(no rules)"
