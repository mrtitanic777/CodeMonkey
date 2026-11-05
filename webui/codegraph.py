"""The code graph: which function is where, who calls it, and what a change would touch.

Reading files one after another to find out who calls a function is slow, fills the context
window, and a small model often skips it and guesses. The graph answers those questions from
an index instead:

    code_search    find a function, method or class by name
    code_explore   the code that bears on a question, grouped by file
    code_node      one symbol: where it is, its signature, its source
    code_callers   who calls it
    code_callees   what it calls
    code_impact    everything that depends on it, level by level, and the tests that reach it

Python is read with the language's own parser, so definitions, signatures and calls are exact.
Other languages (JavaScript, TypeScript, Go, Rust, Java, C#, C, C++) are read by pattern: good
for finding things, approximate for who-calls-what, and marked as such in every answer.

A call is linked to a definition only when that can be told: the same file, an import, the same
class, or a name that exists once in the project. Anything else is listed as *possible*, never
passed off as certain.

The index lives in memory and follows the files: a file is read again when it changes. The
code gate uses it too (a call that does not fit the function it names is refused; callers left
behind by a changed signature are pointed out), and so does the working set.

The idea of serving search, callers, callees and impact from one index is the shape larger
agent-memory systems use for code; this one is written here, on the standard library alone.
"""

from __future__ import annotations

import ast
import os
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

MAX_FILES = 4000
MAX_BYTES = 400_000
_PATTERN_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc")
_DEF_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\(([^)]*)\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)"), "function"),              # Go
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(([^)]*)"), "function"),   # Rust
    (re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|interface|type)\s+([A-Za-z_]\w*)"), "class"),
    (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|virtual|override|async|abstract|inline|extern)\s+)+"
                r"[\w<>\[\],.?*&\s]+?\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:\{|$|throws|where|const)"), "function"),  # Java, C#, C++
    (re.compile(r"^(?:[A-Za-z_][\w*&<>:\s]*?)\s+\**([A-Za-z_]\w*)\s*\(([^;{)]*)\)\s*\{?\s*$"), "function"),  # C at column 0
]
_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_METHOD_CALL = re.compile(r"\.([A-Za-z_$][\w$]*)\s*\(")
_NOT_CALLS = frozenset(
    "if for while switch catch return function typeof new await sizeof defer go match fn func class "
    "else do try throw yield import export from super this self print println printf".split())
_WORD = re.compile(r"[A-Za-z][a-z0-9]+|[A-Z]+(?![a-z])|\d+")


def _parts(name: str) -> Set[str]:
    """`parse_user_id` and `parseUserId` both give {parse, user, id}."""
    return {w.lower() for w in _WORD.findall(name) if len(w) > 1}


class Symbol:
    __slots__ = ("sid", "name", "qual", "kind", "file", "line", "end", "sig", "doc", "calls", "params", "plain", "exact")

    def __init__(self, sid: int, name: str, qual: str, kind: str, file: str, line: int, end: int,
                 sig: str, doc: str = "", params: Optional[Dict[str, Any]] = None, plain: bool = False,
                 exact: bool = True):
        self.sid, self.name, self.qual, self.kind = sid, name, qual, kind
        self.file, self.line, self.end, self.sig, self.doc = file, line, end, sig, doc
        self.calls: List[Tuple[str, str, int, int, Tuple[str, ...], bool]] = []   # name, receiver, line, nargs, kwargs, starred
        self.params = params      # {"min": n, "max": n or None, "names": [...], "kwonly": [...], "kwargs": bool}
        self.plain = plain        # an undecorated top-level function: its signature is what callers must fit
        self.exact = exact        # read with a real parser (False: by pattern)

    def where(self) -> str:
        return f"{self.file}:{self.line}"


def arg_spec(args: ast.arguments, skip_first: bool = False) -> Dict[str, Any]:
    positional = [a.arg for a in args.posonlyargs + args.args]
    if skip_first and positional:
        positional = positional[1:]
    required = len(positional) - len(args.defaults)
    return {
        "min": max(0, required), "max": None if args.vararg else len(positional),
        "names": positional, "posonly": len(args.posonlyargs),
        "kwonly": [a.arg for a in args.kwonlyargs],
        "kwrequired": [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None],
        "kwargs": args.kwarg is not None,
    }


def misfit(spec: Dict[str, Any], nargs: int, kwargs: Tuple[str, ...], starred: bool) -> str:
    """Why a call with these arguments cannot fit this signature, or ""."""
    if starred or spec is None:
        return ""
    if spec["max"] is not None and nargs > spec["max"]:
        return f"passes {nargs} positional argument{'s' if nargs != 1 else ''}, but it takes at most {spec['max']}"
    names = spec["names"]
    known = set(names[spec.get("posonly", 0):]) | set(spec["kwonly"])
    if not spec["kwargs"]:
        for kw in kwargs:
            if kw not in known:
                return f"passes `{kw}=`, which it does not take"
    given = set(names[:nargs]) | set(kwargs)
    missing = [n for n in names[:spec["min"]] if n not in given] + [n for n in spec["kwrequired"] if n not in kwargs]
    if missing:
        return "does not pass " + ", ".join(f"`{m}`" for m in missing[:3])
    return ""


class _PyReader(ast.NodeVisitor):
    def __init__(self, graph: "CodeGraph", rel: str, source: str):
        self.g, self.rel, self.lines = graph, rel, source.splitlines()
        self.stack: List[Symbol] = []
        self.classes: List[str] = []
        self.out: List[Symbol] = []
        self.imports: Dict[str, Tuple[str, int, str]] = {}   # local name -> (module, level, original name or "")
        self.module = self.g._new(self.rel, "<module>", "<module>", "module", 1, len(self.lines) or 1, "", exact=True)
        self.out.append(self.module)

    def _def(self, node: Any, kind: str) -> Symbol:
        qual = ".".join([s.name for s in self.stack if s.kind != "module"] + [node.name])
        in_class = bool(self.stack) and self.stack[-1].kind == "class"
        params = None
        plain = False
        if kind != "class":
            static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
            params = arg_spec(node.args, skip_first=in_class and not static)
            plain = not node.decorator_list and not in_class and len(self.stack) == 0
            try:
                sig = f"def {node.name}({ast.unparse(node.args)})"
            except Exception:  # noqa: BLE001
                sig = f"def {node.name}(...)"
        else:
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            sig = f"class {node.name}" + (f"({bases})" if bases else "")
        doc = (ast.get_docstring(node) or "").strip().split("\n")[0][:160]
        sym = self.g._new(self.rel, node.name, qual, "method" if in_class and kind != "class" else kind,
                          node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, sig, doc, params, plain)
        self.out.append(sym)
        return sym

    def visit_FunctionDef(self, node: Any) -> None:
        sym = self._def(node, "function")
        self.stack.append(sym)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        sym = self._def(node, "class")
        self.stack.append(sym)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.asname or "." not in a.name:
                self.imports[a.asname or a.name] = (a.name, 0, "")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            if a.name != "*":
                self.imports[a.asname or a.name] = (node.module or "", node.level, a.name)

    def visit_Call(self, node: ast.Call) -> None:
        owner = self.stack[-1] if self.stack else self.module
        if owner.kind == "class":   # a call in a class body belongs to the module that runs it
            owner = self.module
        starred = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        kwargs = tuple(k.arg for k in node.keywords if k.arg)
        nargs = len(node.args)
        if isinstance(node.func, ast.Name):
            owner.calls.append((node.func.id, "", node.lineno, nargs, kwargs, starred))
        elif isinstance(node.func, ast.Attribute):
            recv = node.func.value.id if isinstance(node.func.value, ast.Name) else "?"
            owner.calls.append((node.func.attr, recv, node.lineno, nargs, kwargs, starred))
        self.generic_visit(node)


class CodeGraph:
    """The index for one project folder."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        self._lock = threading.RLock()
        self._next = 0
        self.symbols: Dict[int, Symbol] = {}
        self.files: Dict[str, Dict[str, Any]] = {}   # rel -> {stamp, ids, imports, exact}
        self.by_name: Dict[str, List[int]] = {}
        self.skipped = 0

    def _new(self, rel: str, name: str, qual: str, kind: str, line: int, end: int, sig: str, doc: str = "",
             params: Optional[Dict[str, Any]] = None, plain: bool = False, exact: bool = True) -> Symbol:
        self._next += 1
        return Symbol(self._next, name, qual, kind, rel, line, end, sig, doc, params, plain, exact)

    # --------------------------------------------------------------- build ---

    def _read_python(self, rel: str, source: str) -> Optional[Tuple[List[Symbol], Dict[str, Any]]]:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return None
        reader = _PyReader(self, rel, source)
        try:
            reader.visit(tree)
        except RecursionError:
            return None
        return reader.out, reader.imports

    def _read_pattern(self, rel: str, source: str) -> Tuple[List[Symbol], Dict[str, Any]]:
        lines = source.splitlines()
        module = self._new(rel, "<module>", "<module>", "module", 1, len(lines) or 1, "", exact=False)
        out = [module]
        current = module
        for n, line in enumerate(lines, 1):
            if len(line) > 400:
                continue
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*", "#")):
                continue
            made = None
            for pattern, kind in _DEF_PATTERNS:
                m = pattern.match(line)
                if m and m.group(1) not in _NOT_CALLS:
                    sig = stripped.rstrip("{").strip()[:140]
                    made = self._new(rel, m.group(1), m.group(1), kind, n, n, sig, exact=False)
                    break
            if made is not None:
                current.end = max(current.line, n - 1) if current is not module else current.end
                out.append(made)
                if made.kind != "class":
                    current = made
                continue
            if current is not module:
                current.end = n
            for m in _CALL.finditer(line):
                if m.group(1) not in _NOT_CALLS:
                    current.calls.append((m.group(1), "", n, -1, (), True))
            for m in _METHOD_CALL.finditer(line):
                current.calls.append((m.group(1), "?", n, -1, (), True))
        return out, {}

    def _drop(self, rel: str) -> None:
        for sid in self.files.pop(rel, {}).get("ids", []):
            sym = self.symbols.pop(sid, None)
            if sym is not None:
                ids = self.by_name.get(sym.name, [])
                if sid in ids:
                    ids.remove(sid)

    def _load(self, abs_path: str, rel: str, stamp: Tuple[float, int]) -> None:
        self._drop(rel)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            return
        if rel.endswith((".py", ".pyi")):
            read = self._read_python(rel, source)
            exact = True
            if read is None:
                self.files[rel] = {"stamp": stamp, "ids": [], "imports": {}, "exact": True, "broken": True}
                return
        else:
            read, exact = self._read_pattern(rel, source), False
        symbols, imports = read
        for sym in symbols:
            self.symbols[sym.sid] = sym
            if sym.kind != "module":
                self.by_name.setdefault(sym.name, []).append(sym.sid)
        self.files[rel] = {"stamp": stamp, "ids": [s.sid for s in symbols], "imports": imports, "exact": exact}

    def refresh(self, force: bool = False) -> "CodeGraph":
        """Bring the index up to date with the folder. Cheap when nothing changed."""
        import time

        from coder.tools import _load_gitignore, _walk_files

        with self._lock:
            if not force and time.monotonic() - getattr(self, "_walked", -9.0) < 0.3:
                return self   # asked several times in one step: one look at the folder is enough
            self._walked = time.monotonic()
            seen: Set[str] = set()
            self.skipped = 0
            try:
                patterns = _load_gitignore(self.project_dir)
            except Exception:  # noqa: BLE001
                patterns = []
            for abs_path, rel in _walk_files(self.project_dir, patterns):
                if not rel.endswith((".py", ".pyi") + _PATTERN_EXT):
                    continue
                if len(seen) >= MAX_FILES:
                    self.skipped += 1
                    continue
                try:
                    st = os.stat(abs_path)
                except OSError:
                    continue
                if st.st_size > MAX_BYTES:
                    self.skipped += 1
                    continue
                seen.add(rel)
                stamp = (st.st_mtime, st.st_size)
                if self.files.get(rel, {}).get("stamp") != stamp:
                    self._load(abs_path, rel, stamp)
            for rel in [r for r in self.files if r not in seen]:
                self._drop(rel)
        return self

    # ------------------------------------------------------------- resolve ---

    def _module_file(self, from_rel: str, module: str, level: int) -> Optional[str]:
        parts = [p for p in module.split(".") if p]
        here = os.path.dirname(from_rel)
        if level:
            base = here
            for _ in range(level - 1):
                base = os.path.dirname(base)
            roots = [base]
        else:
            roots = ["", "src", here]
        for root in roots:
            target = "/".join([p for p in [root.replace("\\", "/")] + parts if p])
            for candidate in (target + ".py", target + "/__init__.py"):
                if candidate in self.files:
                    return candidate
        return None

    def _in_file(self, rel: str, name: str, top_only: bool = True) -> List[Symbol]:
        out = []
        for sid in self.files.get(rel, {}).get("ids", []):
            sym = self.symbols[sid]
            if sym.name == name and sym.kind != "module" and (not top_only or "." not in sym.qual):
                out.append(sym)
        return out

    def targets(self, caller: Symbol, name: str, recv: str) -> Tuple[List[Symbol], bool]:
        """(the definitions a call may mean, whether that is certain)."""
        info = self.files.get(caller.file, {})
        imports = info.get("imports", {})
        if recv in ("self", "cls"):
            owner = caller.qual.rsplit(".", 1)[0] if "." in caller.qual else ""
            mine = [s for s in self._in_file(caller.file, name, top_only=False) if s.qual == f"{owner}.{name}"]
            if mine:
                return mine, True
        elif recv == "":
            local = self._in_file(caller.file, name)
            if local:
                return local, True
            if name in imports:
                module, level, original = imports[name]
                target = self._module_file(caller.file, module, level)
                if target:
                    found = self._in_file(target, original or name)
                    if found:
                        return found, True
                return [], True   # imported from outside the project: not ours
        elif recv in imports and recv != "?":
            module, level, original = imports[recv]
            target = self._module_file(caller.file, module + ("." + original if original else ""), level) \
                or self._module_file(caller.file, module, level)
            if target:
                return self._in_file(target, name), True
            return [], True
        candidates = [self.symbols[s] for s in self.by_name.get(name, [])]
        if recv not in ("", "self", "cls"):
            candidates = [c for c in candidates if c.kind in ("method", "function")] or candidates
        if len(candidates) == 1 and info.get("exact") and candidates[0].exact and recv in ("", "self", "cls"):
            return candidates, True
        return candidates[:12], False

    def find(self, wanted: str) -> List[Symbol]:
        """The symbols a user's or a model's wording points at: `name`, `Class.method`, `file.py:name`."""
        wanted = str(wanted or "").strip().strip("`").rstrip("()")
        file_hint = ""
        if ":" in wanted and not wanted.startswith(":"):
            file_hint, _, wanted = wanted.rpartition(":")
            if wanted.isdigit():   # file.py:120 — the symbol on that line
                line = int(wanted)
                rel = self._match_file(file_hint)
                inside = [self.symbols[s] for s in self.files.get(rel or "", {}).get("ids", [])
                          if self.symbols[s].kind != "module" and self.symbols[s].line <= line <= self.symbols[s].end]
                return sorted(inside, key=lambda s: s.end - s.line)[:1]
        name = wanted.split(".")[-1]
        found = [self.symbols[s] for s in self.by_name.get(name, [])]
        if "." in wanted:
            exact = [s for s in found if s.qual == wanted or s.qual.endswith("." + wanted)]
            found = exact or found
        if file_hint:
            rel = self._match_file(file_hint)
            found = [s for s in found if s.file == rel] or found
        return found

    def _match_file(self, hint: str) -> Optional[str]:
        hint = hint.replace("\\", "/").lstrip("./")
        if hint in self.files:
            return hint
        ends = [r for r in self.files if r.endswith("/" + hint) or os.path.basename(r) == hint]
        return ends[0] if len(ends) >= 1 else None

    # -------------------------------------------------------------- answers ---

    def callers(self, target: Symbol) -> List[Tuple[Symbol, int, bool, Tuple[int, Tuple[str, ...], bool]]]:
        """[(the caller, line, certain, (nargs, kwargs, starred))] for every call that may mean *target*."""
        out = []
        for sym in self.symbols.values():
            for name, recv, line, nargs, kwargs, starred in sym.calls:
                if name != target.name:
                    continue
                found, certain = self.targets(sym, name, recv)
                if target in found:
                    out.append((sym, line, certain and len(found) == 1, (nargs, kwargs, starred)))
        out.sort(key=lambda row: (not row[2], row[0].file, row[1]))
        return out

    def callees(self, source: Symbol) -> List[Tuple[Symbol, int, bool]]:
        out, seen = [], set()
        for name, recv, line, _n, _k, _s in source.calls:
            found, certain = self.targets(source, name, recv)
            for target in found:
                if (target.sid, certain) not in seen:
                    seen.add((target.sid, certain))
                    out.append((target, line, certain and len(found) == 1))
        return out

    def impact(self, target: Symbol, depth: int = 2) -> List[List[Tuple[Symbol, bool]]]:
        """Callers, then their callers, to *depth* levels. Each level is [(symbol, certain)]."""
        levels: List[List[Tuple[Symbol, bool]]] = []
        seen = {target.sid}
        frontier = [(target, True)]
        for _ in range(max(1, min(6, int(depth)))):
            nxt: Dict[int, Tuple[Symbol, bool]] = {}
            for sym, sure in frontier:
                for caller, _line, certain, _args in self.callers(sym):
                    if caller.sid in seen:
                        continue
                    both = sure and certain
                    if caller.sid not in nxt or (both and not nxt[caller.sid][1]):
                        nxt[caller.sid] = (caller, both)
            if not nxt:
                break
            seen.update(nxt)
            frontier = list(nxt.values())
            levels.append(sorted(frontier, key=lambda p: (not p[1], p[0].file, p[0].line)))
        return levels

    def search(self, query: str, kind: str = "", limit: int = 20) -> List[Symbol]:
        query = str(query or "").strip().strip("`").rstrip("()")
        low = query.lower()
        words = _parts(query)
        scored = []
        for sym in self.symbols.values():
            if sym.kind == "module" or (kind and sym.kind != kind and not (kind == "function" and sym.kind == "method")):
                continue
            name = sym.name.lower()
            if name == low or sym.qual.lower() == low:
                score = 100
            elif name.startswith(low) and low:
                score = 60
            elif low and low in sym.qual.lower():
                score = 40
            else:
                overlap = len(words & _parts(sym.qual))
                score = 10 * overlap + (2 * len(words & _parts(sym.doc)) if overlap or len(words) > 1 else 0)
            if score > 0:
                scored.append((score, sym))
        scored.sort(key=lambda p: (-p[0], p[1].file, p[1].line))
        return [s for _, s in scored[:limit]]

    def source(self, sym: Symbol, max_lines: int = 60) -> str:
        try:
            with open(os.path.join(self.project_dir, sym.file), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return ""
        chunk = lines[sym.line - 1:max(sym.end, sym.line)]
        if len(chunk) > max_lines:
            chunk = chunk[:max_lines] + [f"    ... ({len(chunk) - max_lines} more lines; read {sym.file} from line {sym.line + max_lines})"]
        return "\n".join(f"{sym.line + n:>5}  {text}" for n, text in enumerate(chunk))

    def stats(self) -> Dict[str, int]:
        return {"files": len(self.files), "symbols": sum(1 for s in self.symbols.values() if s.kind != "module"),
                "by_pattern": sum(1 for f in self.files.values() if not f.get("exact")), "skipped": self.skipped}


# -------------------------------------------------------------------- tools ---

_GRAPHS: Dict[str, CodeGraph] = {}
_GRAPHS_LOCK = threading.Lock()


def graph_for(project_dir: str, force: bool = False) -> CodeGraph:
    key = os.path.abspath(project_dir)
    with _GRAPHS_LOCK:
        graph = _GRAPHS.get(key)
        if graph is None:
            graph = _GRAPHS[key] = CodeGraph(key)
            if len(_GRAPHS) > 6:
                _GRAPHS.pop(next(iter(_GRAPHS)))
    return graph.refresh(force)


def _line(sym: Symbol) -> str:
    return f"{sym.where()}  {sym.sig or sym.qual}" + ("" if sym.exact else "  (read by pattern)")


def _one(graph: CodeGraph, wanted: str) -> Tuple[Optional[Symbol], str]:
    found = graph.find(wanted)
    if not found:
        near = graph.search(wanted, limit=5)
        hint = ("\nClosest: " + "; ".join(f"{s.qual} ({s.where()})" for s in near)) if near else ""
        return None, f"ERROR: nothing named {wanted!r} is defined in this project.{hint}"
    if len(found) > 1:
        return None, (f"{wanted!r} is defined {len(found)} times. Say which, as file.py:name or Class.method:\n"
                      + "\n".join("- " + _line(s) for s in found[:12]))
    return found[0], ""


def graph_tools(session) -> List[Any]:
    from tools.registry import Tool

    def graph() -> CodeGraph:
        return graph_for(session.project_dir)

    def code_search(query: str, kind: str = "") -> str:
        found = graph().search(query, kind if kind in ("function", "method", "class") else "")
        if not found:
            return "Nothing by that name. Try a shorter word, or grep for text that is not a name."
        return "\n".join(_line(s) + (f"\n      {s.doc}" if s.doc else "") for s in found)

    def code_node(symbol: str) -> str:
        g = graph()
        sym, problem = _one(g, symbol)
        if sym is None:
            return problem
        return f"{sym.kind} {sym.qual}  ({sym.where()}-{sym.end})\n{sym.sig}\n\n[File: {sym.file}]\n{g.source(sym, 120)}"

    def code_callers(symbol: str) -> str:
        g = graph()
        sym, problem = _one(g, symbol)
        if sym is None:
            return problem
        rows = g.callers(sym)
        if not rows:
            return (f"Nothing in this project calls {sym.qual} ({sym.where()}) by name. It may still be used "
                    "indirectly (passed as a value, named in a string or a config, called from outside).")
        sure = [r for r in rows if r[2]]
        maybe = [r for r in rows if not r[2]]
        out = [f"{sym.qual} ({sym.where()}) is called from:"]
        out += [f"- {c.file}:{line}  in {c.qual}" for c, line, _c, _a in sure[:40]]
        if maybe:
            out.append("Possibly (the same name, but it cannot be told which definition is meant):")
            out += [f"- {c.file}:{line}  in {c.qual}" for c, line, _c, _a in maybe[:20]]
        return "\n".join(out)

    def code_callees(symbol: str) -> str:
        g = graph()
        sym, problem = _one(g, symbol)
        if sym is None:
            return problem
        rows = g.callees(sym)
        if not rows:
            return f"{sym.qual} calls nothing that is defined in this project."
        out = [f"{sym.qual} ({sym.where()}) calls:"]
        out += [f"- {t.qual}  {t.where()}" + ("" if certain else "  (possibly)") for t, _line_no, certain in rows[:50]]
        return "\n".join(out)

    def code_impact(symbol: str, depth: int = 2) -> str:
        from .codegate import is_test_file

        g = graph()
        sym, problem = _one(g, symbol)
        if sym is None:
            return problem
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            depth = 2
        levels = g.impact(sym, depth)
        if not levels:
            return f"Nothing in this project depends on {sym.qual} ({sym.where()}) by a direct call."
        out = [f"If {sym.qual} ({sym.where()}) changes:"]
        files: Set[str] = set()
        tests: Set[str] = set()
        for n, level in enumerate(levels, 1):
            out.append(f"Level {n} ({'calls it directly' if n == 1 else 'reaches it through level ' + str(n - 1)}):")
            for s, certain in level[:25]:
                out.append(f"- {s.qual}  {s.where()}" + ("" if certain else "  (possibly)"))
                (tests if is_test_file(s.file) else files).add(s.file)
            if len(level) > 25:
                out.append(f"- … and {len(level) - 25} more")
        out.append("Files to look at: " + (", ".join(sorted(files)[:20]) or "none besides its own"))
        out.append("Tests that reach it: " + (", ".join(sorted(tests)[:20]) or "none found: a change here is not covered by a test that calls it"))
        return "\n".join(out)

    def code_explore(query: str) -> str:
        g = graph()
        found = g.search(query, limit=8)
        if not found:
            return "Nothing in the index matches. Try other words, or grep."
        out, used, by_file = [], 0, {}
        for s in found:
            by_file.setdefault(s.file, []).append(s)
        for rel, symbols in by_file.items():
            out.append(f"[File: {rel}]")
            for s in sorted(symbols, key=lambda x: x.line):
                text = g.source(s, 40)
                if used + len(text) > 7000:
                    out.append(f"{s.where()}  {s.sig}   (source left out to save room: use code_node)")
                    continue
                used += len(text)
                out.append(text)
                out.append("")
        return "\n".join(out).rstrip()

    text = {"type": "string"}
    symbol = {"type": "object", "properties": {"symbol": dict(text, description="A name, Class.method, or file.py:name.")},
              "required": ["symbol"]}
    return [
        Tool(name="code_search", func=code_search,
             description="Find where a function, method or class is defined, by name or part of a name. "
                         "Answers with file:line and signature, not source. Faster and surer than grep for names.",
             parameters={"type": "object", "properties": {"query": text, "kind": dict(text, description="function, method or class (optional).")},
                         "required": ["query"]}),
        Tool(name="code_explore", func=code_explore,
             description="Show the source of the functions and classes that bear on a question, grouped by file. "
                         "Use this first when you need to understand how something works.",
             parameters={"type": "object", "properties": {"query": dict(text, description="Words from the names involved, e.g. 'parse config'.")},
                         "required": ["query"]}),
        Tool(name="code_node", func=code_node, parameters=symbol,
             description="One function, method or class: where it is, its signature and its source."),
        Tool(name="code_callers", func=code_callers, parameters=symbol,
             description="Who calls this function or method. Check this before you change its name, arguments or behaviour."),
        Tool(name="code_callees", func=code_callees, parameters=symbol,
             description="What this function or method calls inside the project."),
        Tool(name="code_impact", func=code_impact,
             description="Everything that depends on a function, level by level, the files to look at and the tests "
                         "that reach it. Use before a change that alters what it returns or accepts.",
             parameters={"type": "object", "properties": {"symbol": dict(text, description="A name, Class.method, or file.py:name."),
                                                          "depth": {"type": "integer", "description": "Levels of callers to follow (default 2)."}},
                         "required": ["symbol"]}),
    ]
