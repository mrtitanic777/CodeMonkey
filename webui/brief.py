"""The brief: what a task names, found and laid out before the model's first round.

Asked to work on a function, a small model spends its first rounds finding it: read the file,
read the tests, search for the callers, read again with an offset. Measured on one task, that was
eight requests and forty-eight thousand tokens read before the first edit. Every one of those
lookups is something the code graph answers without a model.

So when a message names code that exists in the project, the answers travel with the message:

    the definition     where it is, and its source with line numbers
    who calls it       file and line, for the callers a change must keep working
    its tests          the test functions that use it, and the tables of cases they run over

It is bounded, it is built from the files as they are now, and it says so. Nothing is brought
when the message names nothing that exists: a guess here would cost more than it saves.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, List, Tuple

MAX_CHARS = 7000
MAX_SYMBOLS = 2
_WORD = re.compile(r"`([^`\n]{2,80})`|([A-Za-z_][\w.]*[A-Za-z0-9_])(\s*\()?")
_PATH = re.compile(r"[\w./\\-]+\.[A-Za-z]{1,4}\b")
_LEAD = re.compile(r"(?:function|method|class|def|func|routine)\s+$", re.I)


def named(graph, task: str) -> List[Any]:
    """The symbols a message names. A plain word counts only when the wording marks it as code."""
    text = str(task or "")
    files = [f for f in (graph._match_file(p) for p in _PATH.findall(text)) if f]
    out: List[Any] = []
    for m in _WORD.finditer(text):
        word = (m.group(1) or m.group(2) or "").strip().rstrip("()")
        if not word or len(word) < 3:
            continue
        marked = bool(m.group(1) or m.group(3) or "_" in word or "." in word or re.search(r"[a-z][A-Z]", word)
                      or _LEAD.search(text[:m.start()]))
        if not marked:
            continue
        found = [s for s in graph.find(word) if s.kind != "module"]
        in_named = [s for s in found if s.file in files]
        found = in_named or found
        if len(found) != 1:        # not there, or more than one thing by that name: not ours to pick
            continue
        if found[0] not in out:
            out.append(found[0])
        if len(out) >= MAX_SYMBOLS:
            break
    return out


def _segment(lines: List[str], first: int, last: int, cap: int) -> str:
    chunk = lines[first - 1:last]
    if len(chunk) > cap:
        chunk = chunk[:cap] + [f"... ({len(chunk) - cap} more lines)"]
    return "\n".join(f"{first + n:>5}  {text}" for n, text in enumerate(chunk))


def _has_test_named(project_dir: str, rel: str, name: str) -> bool:
    try:
        with open(os.path.join(project_dir, rel), "r", encoding="utf-8", errors="replace") as fh:
            return bool(re.search(r"def test_?" + re.escape(name) + r"\b", fh.read()))
    except OSError:
        return False


def module_names(project_dir: str, rel: str, limit: int = 14) -> str:
    """The values a file defines at its top level, one line each: what a function there can use."""
    try:
        with open(os.path.join(project_dir, rel), "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return ""
    lines = source.splitlines()
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and not lines[node.lineno - 1].startswith("__"):
            more = " ..." if (node.end_lineno or node.lineno) > node.lineno else ""
            out.append(f"{node.lineno:>5}  {lines[node.lineno - 1][:110]}{more}")
    return "\n".join(out[:limit])


def tests_of(project_dir: str, graph, name: str, budget: int) -> List[str]:
    """The test functions that use *name*, with the tables of cases they are run over."""
    from .codegate import is_test_file

    parts: List[str] = []
    # the test file that is about this very thing first (test_number.py for intword in number.py)
    for rel in sorted(graph.files, key=lambda r: (not _has_test_named(project_dir, r, name), r)):
        if budget <= 0:
            break
        if not rel.endswith(".py") or not is_test_file(rel):
            continue
        try:
            with open(os.path.join(project_dir, rel), "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            if name not in source:
                continue
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = source.splitlines()
        tables: Dict[str, Tuple[int, int]] = {}
        for node in tree.body:
            target = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1
                      else node.target if isinstance(node, ast.AnnAssign) else None)
            if isinstance(target, ast.Name):
                tables[target.id] = (node.lineno, node.end_lineno or node.lineno)
        shown_tables: List[str] = []
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        hits = 0
        for func in funcs:
            uses = any(isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == name)
                or (isinstance(n.func, ast.Attribute) and n.func.attr == name)) for n in ast.walk(func))
            if not uses:
                continue
            hits += 1
            if hits > 3:
                parts.append(f"(more tests in {rel} use {name}: search for it there)")
                break
            first = min([d.lineno for d in func.decorator_list] + [func.lineno])
            text = f"{rel}: {func.name}\n" + _segment(lines, first, func.end_lineno or func.lineno, 25)
            parts.append(text)
            budget -= len(text)
            for n in ast.walk(func):   # the tables of cases the test is run over
                if isinstance(n, ast.Name) and n.id in tables and n.id not in shown_tables and n.id.isupper():
                    shown_tables.append(n.id)
                    a, b = tables[n.id]
                    table = f"{rel}: {n.id} (the cases)\n" + _segment(lines, a, b, 40)
                    parts.append(table)
                    budget -= len(table)
            if budget <= 0:
                break
    return parts


def brief(session, task: str) -> Tuple[str, List[str]]:
    """(the block that goes in front of a message, the files it shows a definition from).

    ("", []) when the message names no code that exists."""
    try:
        from .codegraph import graph_for

        graph = graph_for(session.project_dir)
        symbols = named(graph, task)
        if not symbols:
            return "", []
        parts: List[str] = []
        budget = MAX_CHARS
        for sym in symbols:
            body = graph.source(sym, 70)
            parts.append(f"{sym.qual} is defined in {sym.file}, lines {sym.line}-{sym.end}:\n{body}")
            budget -= len(parts[-1])
            if sym.file.endswith(".py"):
                top = module_names(session.project_dir, sym.file)
                if top:
                    parts.append(f"Values defined at the top of {sym.file}:\n{top}")
                    budget -= len(parts[-1])
            callers = [f"{c.file}:{line} in {c.qual}" for c, line, certain, _a in graph.callers(sym)
                       if certain and c is not sym]
            if callers:
                parts.append(f"{sym.name} is called from: " + "; ".join(callers[:8])
                             + (f" (and {len(callers) - 8} more)" if len(callers) > 8 else ""))
                budget -= len(parts[-1])
            found = tests_of(session.project_dir, graph, sym.name, max(0, budget - 600))
            if found:
                parts += found
                budget -= sum(len(p) for p in found)
            else:
                parts.append(f"No test that uses {sym.name} was found.")
            if budget < 800:
                break
        text = "\n\n".join(parts)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n... (cut to fit: read the files for the rest)"
        return ("[Found for this message by the system, from the project's files as they are now. Line numbers are "
                "real. You do not need to read these parts again before you act; read more only if this is not enough.]\n"
                + text + "\n[End of what was found]\n\n"), sorted({s.file for s in symbols})
    except Exception:  # noqa: BLE001 - the brief must never stop a turn
        return "", []
