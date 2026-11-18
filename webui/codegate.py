"""The code gate: a change that cannot be right is refused before it is written.

A small model will happily call a function that does not exist, import a name a module never
had, or leave a file that does not parse. None of that needs judgement to catch, so none of it
is left to the model. Every edit and every new file goes through here first:

    it must parse                              (Python, JSON, TOML; JavaScript when node is there)
    names it imports from this project must exist in the file they are imported from
    attributes it uses on this project's modules must exist there
    names it uses must be defined or imported somewhere in the file

A refusal writes nothing and tells the model exactly what is wrong, with the names that do
exist. Only what the change itself introduces is checked: a file's older problems are not this
edit's fault. Modules from outside the project that cannot be found are a warning, not a
refusal, because the project may run in another environment than this one.

After a change lands, the tests are run again when the session already has a test command the
user allowed and it is quick. A test that passed before and fails now is reported with the
change; in strict mode the change is undone.

This is not a proof that code is correct. It removes the class of mistakes that are simply
false: things that are not there.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

MODES = ("off", "check", "strict")
QUICK_SECONDS = 30.0      # a test command slower than this is not re-run by the gate
MAX_STRIKES = 3           # refusals in a row on one file before the model is told to stop

_TEST_CMD = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|cargo\s+test|dotnet\s+test|ctest|"
    r"make\s+(?:test|check)|gradle(?:w)?\s+test|mvn\s+test|rspec|phpunit)\b"
)
_TEST_FILE = re.compile(r"(^|[/\\])(tests?|__tests__|spec)[/\\]|(^|[/\\])(test_[^/\\]*|[^/\\]*_test|conftest)\.py$"
                        r"|\.(test|spec)\.[cm]?[jt]sx?$", re.I)
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+|\S+\.py\S*)", re.M)   # pytest
_UNITTEST_FAIL = re.compile(r"^(?:FAIL|ERROR):\s+(\S+(?:\s+\([^)]*\))?)", re.M)  # unittest
_GO_FAIL = re.compile(r"^--- FAIL:\s+(\S+)", re.M)
_JS_FAIL = re.compile(r"^\s*(?:✕|×|✗|\d+\)\s)\s*(.+)$", re.M)
_JSON_WITH_COMMENTS = re.compile(r"(^|[/\\])(tsconfig[^/\\]*|jsconfig[^/\\]*|\.eslintrc|settings|launch|tasks|"
                                 r"extensions|devcontainer)\.json$|\.code-workspace$|\.jsonc$", re.I)
_MODULE_NAMES = frozenset(
    "__file__ __name__ __doc__ __package__ __spec__ __loader__ __builtins__ __debug__ __path__ "
    "__class__ __annotations__ __dict__ __all__ __version__ __qualname__ __module__".split()
)
_BUILTINS = frozenset(dir(builtins)) | _MODULE_NAMES


# a pytest run: the runner itself, then whatever narrowed it. The interpreter may be named the
# way a model writes it: plain `python`, or the full path to it, quoted or not (a Windows path
# with spaces comes quoted). Only the runner is kept for the widened command.
_INTERP = (r"python[0-9.]*|py"
           r'|"[^"]*[\\/](?:python[0-9.]*|py)(?:\.exe)?"'
           r"|[:\w.~+@%$\\/-]*[\\/](?:python[0-9.]*|py)(?:\.exe)?")
_PYTEST_RUN = re.compile(rf"^((?:{_INTERP})\s+-m\s+pytest|pytest|py\.test)\b(.*)$", re.S)

#: what it looks like when the widened run never reached the tests at all: the interpreter has no
#: pytest, the program is not on the path, or the shell could not run the line
_RUNNER_MISSING = ("no module named pytest", "command not found", "not recognized as an internal",
                   "can't find module", "couldn't find", "no such file or directory",
                   "is not a valid python", "modulenotfounderror", "nameerror: no module named")


def is_test_command(cmd: str) -> bool:
    return bool(_TEST_CMD.search(str(cmd or "")))


def is_test_file(rel_path: str) -> bool:
    return bool(_TEST_FILE.search(str(rel_path or "").replace("\\", "/")))


def failing_tests(output: str, exit_code: Optional[int]) -> Set[str]:
    """The tests a run reports as failing. A failed run nothing can be read from is one "failure"."""
    text = str(output or "")
    found: Set[str] = set()
    for pattern in (_FAILED_LINE, _UNITTEST_FAIL, _GO_FAIL):
        found.update(m.group(1).strip() for m in pattern.finditer(text))
    if not found and exit_code not in (0, None):
        found.update(m.group(1).strip()[:120] for m in _JS_FAIL.finditer(text))
    if not found and exit_code not in (0, None):
        found.add(f"(the run failed with exit {exit_code})")
    return found


# ---------------------------------------------------------------- python ---


class _Names(ast.NodeVisitor):
    """Every name a file binds anywhere, and every name it reads. Scope is ignored on purpose:
    that can only hide a problem, never invent one."""

    def __init__(self) -> None:
        self.bound: Set[str] = set()
        self.used: Dict[str, int] = {}
        self.star = False
        self.dynamic = False

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.used.setdefault(node.id, node.lineno)
        else:
            self.bound.add(node.id)

    def _args(self, args: ast.arguments) -> None:
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            self.bound.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a is not None:
                self.bound.add(a.arg)

    def visit_FunctionDef(self, node: Any) -> None:
        self.bound.add(node.name)
        self._args(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._args(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bound.add((alias.asname or alias.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.star = True
            else:
                self.bound.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.bound.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_MatchAs(self, node: Any) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: Any) -> None:
        if node.name:
            self.bound.add(node.name)

    def visit_MatchMapping(self, node: Any) -> None:
        if node.rest:
            self.bound.add(node.rest)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "globals", "locals", "vars"):
            self.dynamic = True
        self.generic_visit(node)


def _undefined(tree: ast.AST) -> Dict[str, int]:
    names = _Names()
    names.visit(tree)
    if names.star or names.dynamic:
        return {}
    return {n: line for n, line in names.used.items() if n not in names.bound and n not in _BUILTINS}


def _module_names(path: str) -> Optional[Set[str]]:
    """Top-level names a project module offers, or None when that cannot be told."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return None
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__" and node in tree.body:
            return None   # the module answers for any name
    for node in tree.body:
        for sub in ast.walk(node) if isinstance(node, (ast.If, ast.Try, ast.With)) else [node]:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, ast.Import):
                names.update((a.asname or a.name).split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.ImportFrom):
                if any(a.name == "*" for a in sub.names):
                    return None
                names.update(a.asname or a.name for a in sub.names)
            elif isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _find_module(project_dir: str, file_dir: str, module: str, level: int) -> Tuple[Optional[str], Optional[str]]:
    """(file, folder) of a module inside the project. Either may be None; both None: not ours."""
    parts = [p for p in (module or "").split(".") if p]
    roots: List[str] = []
    if level:
        base = file_dir
        for _ in range(level - 1):
            base = os.path.dirname(base)
        roots = [base]
    else:
        roots = [project_dir, os.path.join(project_dir, "src"), file_dir]
    for root in roots:
        target = os.path.join(root, *parts) if parts else root
        if os.path.isfile(target + ".py"):
            return target + ".py", (target if os.path.isdir(target) else None)
        if os.path.isdir(target) and (parts or level):
            init = os.path.join(target, "__init__.py")
            return (init if os.path.isfile(init) else None), target
    return None, None


def _installed(project_dir: str, top: str) -> bool:
    try:
        if importlib.util.find_spec(top) is not None:
            return True
    except (ImportError, ValueError, AttributeError):
        pass
    for env in (".venv", "venv", "env"):   # the project's own environment, when it has one
        for site in (os.path.join(project_dir, env, "Lib", "site-packages"),):
            if os.path.isdir(os.path.join(site, top)) or os.path.isfile(os.path.join(site, top + ".py")):
                return True
        lib = os.path.join(project_dir, env, "lib")
        if os.path.isdir(lib):
            for py in os.listdir(lib):
                site = os.path.join(lib, py, "site-packages")
                if os.path.isdir(os.path.join(site, top)) or os.path.isfile(os.path.join(site, top + ".py")):
                    return True
    return False


def _close(name: str, pool: Set[str]) -> str:
    public = sorted(n for n in pool if not n.startswith("_"))
    near = difflib.get_close_matches(name, public, n=3, cutoff=0.5)
    if near:
        return " Closest there: " + ", ".join(near) + "."
    if public:
        return " It has: " + ", ".join(public[:12]) + ("…" if len(public) > 12 else "") + "."
    return ""


def _guarded(tree: ast.AST) -> Set[int]:
    """ids of import nodes inside try: or `if TYPE_CHECKING:` (optional by design)."""
    out: Set[int] = set()
    for node in ast.walk(tree):
        optional = isinstance(node, ast.Try) or (
            isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test))
        if optional:
            out.update(id(n) for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom)))
    return out


def python_problems(project_dir: str, rel_path: str, new_src: str, old_src: Optional[str]) -> Tuple[List[str], List[str]]:
    """(problems that refuse the change, warnings that go with it) for a Python file."""
    try:
        tree = ast.parse(new_src)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        return [f"it does not parse: line {exc.lineno}: {exc.msg}" + (f": {line[:120]}" if line else "")], []
    except ValueError as exc:
        return [f"it does not parse: {exc}"], []
    old_tree = None
    if old_src is not None:
        try:
            old_tree = ast.parse(old_src)
        except (SyntaxError, ValueError):
            old_tree = None
    old_text = old_src or ""
    old_imports: Set[Tuple[str, int, str]] = set()
    if old_tree is not None:
        for node in ast.walk(old_tree):
            if isinstance(node, ast.ImportFrom):
                old_imports.update((node.module or "", node.level, a.name) for a in node.names)
            elif isinstance(node, ast.Import):
                old_imports.update((a.name, 0, "") for a in node.names)
    problems: List[str] = []
    warnings: List[str] = []
    file_dir = os.path.dirname(os.path.join(project_dir, rel_path))
    guarded = _guarded(tree)
    aliases: Dict[str, Tuple[str, Optional[str]]] = {}   # local name -> (module file, module folder)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            file, folder = _find_module(project_dir, file_dir, node.module or "", node.level)
            shown = "." * node.level + (node.module or "")
            if file is None and folder is None:
                if node.level:
                    if (shown, node.level, "") not in old_imports and id(node) not in guarded and not any(
                            (node.module or "", node.level, a.name) in old_imports for a in node.names):
                        problems.append(f"line {node.lineno}: `from {shown} import …`: there is no such module next to this file")
                    continue
                top = (node.module or "").split(".")[0]
                fresh = any((node.module or "", 0, a.name) not in old_imports for a in node.names)
                if top and fresh and id(node) not in guarded and not _installed(project_dir, top):
                    warnings.append(f"line {node.lineno}: no module named `{top}` was found in this project or installed here")
                continue
            offered = _module_names(file) if file else set()
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub_file, sub_folder = (None, None)
                if folder:
                    sub = os.path.join(folder, alias.name)
                    if os.path.isfile(sub + ".py"):
                        sub_file = sub + ".py"
                    if os.path.isdir(sub):
                        sub_folder = sub
                        init = os.path.join(sub, "__init__.py")
                        sub_file = sub_file or (init if os.path.isfile(init) else None)
                if sub_file or sub_folder:
                    aliases[alias.asname or alias.name] = (sub_file or "", sub_folder)
                    continue
                if offered is None or alias.name in offered:
                    continue
                if (node.module or "", node.level, alias.name) in old_imports or id(node) in guarded:
                    continue
                where = os.path.relpath(file, project_dir).replace("\\", "/") if file else shown
                problems.append(f"line {node.lineno}: `from {shown} import {alias.name}`: {where} has no `{alias.name}`."
                                + _close(alias.name, offered))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                file, folder = _find_module(project_dir, file_dir, alias.name, 0)
                if file is None and folder is None:
                    top = alias.name.split(".")[0]
                    if (alias.name, 0, "") not in old_imports and id(node) not in guarded and not _installed(project_dir, top):
                        warnings.append(f"line {node.lineno}: no module named `{top}` was found in this project or installed here")
                    continue
                if alias.asname or "." not in alias.name:
                    aliases[alias.asname or alias.name] = (file or "", folder)

    rebound = _Names()
    rebound.visit(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases):
            continue
        name, attr = node.value.id, node.attr
        if f"{name}.{attr}" in old_text:
            continue   # it was already like that before this change
        file, folder = aliases[name]
        offered = _module_names(file) if file else set()
        if offered is None or attr in offered or attr.startswith("__"):
            continue
        if folder and (os.path.isfile(os.path.join(folder, attr + ".py")) or os.path.isdir(os.path.join(folder, attr))):
            continue
        where = os.path.relpath(file, project_dir).replace("\\", "/") if file else name
        text = f"line {node.lineno}: `{name}.{attr}`: {where} has no `{attr}`." + _close(attr, offered)
        if text not in problems:
            problems.append(text)

    before = _undefined(old_tree) if old_tree is not None else {}
    after = _undefined(tree)
    bound = rebound.bound
    for name, line in sorted(after.items(), key=lambda kv: kv[1]):
        if name in before:
            continue   # it was already like that before this change
        problems.append(f"line {line}: `{name}` is used but never defined or imported in this file."
                        + _close(name, bound))
    return problems[:8], warnings[:4]


def _own_specs(tree: ast.AST) -> Dict[str, Any]:
    """Signatures of the plain top-level functions a file defines itself."""
    from .codegraph import arg_spec

    out: Dict[str, Any] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.decorator_list:
            try:
                sig = f"{node.name}({ast.unparse(node.args)})"
            except Exception:  # noqa: BLE001
                sig = node.name + "(...)"
            out[node.name] = (arg_spec(node.args), sig)
    return out


def call_problems(project_dir: str, rel_path: str, new_src: str, old_src: Optional[str], graph: Any) -> List[str]:
    """Calls this change introduces that cannot fit the function they name.

    Only where the function is certain: defined in this file, or imported by name from a project
    file, plain and undecorated. A call with *args or **kwargs is never judged.
    """
    from .codegraph import misfit

    try:
        tree = ast.parse(new_src)
    except (SyntaxError, ValueError):
        return []
    old_text = old_src or ""
    own = _own_specs(tree)
    rebound: Set[str] = set()   # names a function reuses for something else: not judged
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            rebound.add(node.id)
        elif isinstance(node, ast.arg):
            rebound.add(node.arg)
    imported: Dict[str, Tuple[str, int, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = (node.module or "", node.level, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname or "." not in a.name:
                    imported[a.asname or a.name] = (a.name, 0, "")

    def spec_for(func: ast.AST) -> Optional[Tuple[Dict[str, Any], str, str]]:
        if isinstance(func, ast.Name):
            if func.id in rebound:
                return None
            if func.id in own:
                return own[func.id][0], own[func.id][1], rel_path
            if func.id in imported:
                module, level, original = imported[func.id]
                target = graph._module_file(rel_path, module, level)
                found = [s for s in graph._in_file(target, original or func.id)] if target else []
                if len(found) == 1 and found[0].plain and found[0].params:
                    return found[0].params, found[0].sig.replace("def ", ""), found[0].file
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in imported \
                and func.value.id not in rebound:
            module, level, original = imported[func.value.id]
            target = graph._module_file(rel_path, module + ("." + original if original else ""), level) \
                or (graph._module_file(rel_path, module, level) if not original else None)
            found = graph._in_file(target, func.attr) if target else []
            if len(found) == 1 and found[0].plain and found[0].params:
                return found[0].params, found[0].sig.replace("def ", ""), found[0].file
        return None

    problems: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        known = spec_for(node.func)
        if known is None:
            continue
        segment = ast.get_source_segment(new_src, node) or ""
        if segment and segment in old_text:
            continue   # it was already like that before this change
        starred = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        why = misfit(known[0], len(node.args), tuple(k.arg for k in node.keywords if k.arg), starred)
        if why:
            shown = " ".join(segment.split())[:70] or "the call"
            problems.append(f"line {node.lineno}: `{shown}` {why}. {known[2]} defines `{known[1]}`.")
    return problems[:6]


def left_behind(rel_path: str, old_src: Optional[str], new_src: str, graph: Any) -> List[str]:
    """Callers elsewhere that a changed or removed function leaves broken."""
    from .codegraph import misfit

    if not old_src:
        return []
    try:
        old, new = _own_specs(ast.parse(old_src)), _own_specs(ast.parse(new_src))
    except (SyntaxError, ValueError):
        return []
    notes: List[str] = []
    for name, (spec, _sig) in old.items():
        targets = [s for s in graph._in_file(rel_path, name) if s.plain]
        if len(targets) != 1 or name in new and new[name][0] == spec:
            continue
        calls = [(c, line, args) for c, line, certain, args in graph.callers(targets[0]) if certain and c.file != rel_path]
        if not calls:
            continue
        if name not in new:
            where = ", ".join(f"{c.file}:{line}" for c, line, _a in calls[:5])
            notes.append(f"`{name}` is no longer defined in {rel_path} but is still called from {where}")
            continue
        broken = [(c, line) for c, line, args in calls if misfit(new[name][0], *args)]
        if broken:
            where = ", ".join(f"{c.file}:{line}" for c, line in broken[:5])
            notes.append(f"`{new[name][1]}` no longer fits {len(broken)} call{'s' if len(broken) != 1 else ''} "
                         f"elsewhere: {where}. Change them too")
    return notes[:4]


# ------------------------------------------------------------ other kinds ---

_NODE: Optional[str] = None
_NODE_LOOKED = False


def _node() -> Optional[str]:
    global _NODE, _NODE_LOOKED
    if not _NODE_LOOKED:
        _NODE, _NODE_LOOKED = shutil.which("node"), True
    return _NODE


def other_problems(rel_path: str, new_src: str) -> List[str]:
    lower = rel_path.lower()
    if lower.endswith(".json") and not _JSON_WITH_COMMENTS.search(rel_path):
        try:
            json.loads(new_src)
        except ValueError as exc:
            return [f"it is not valid JSON: {exc}"]
    elif lower.endswith(".toml"):
        try:
            import tomllib

            tomllib.loads(new_src)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - tomllib raises its own error type
            return [f"it is not valid TOML: {exc}"]
    elif lower.endswith((".js", ".mjs", ".cjs")) and _node():
        ext = os.path.splitext(lower)[1]
        fd, tmp = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_src)
            proc = subprocess.run([_node(), "--check", tmp], capture_output=True, text=True, timeout=15)
            if proc.returncode != 0:
                lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
                where = next((ln for ln in lines if tmp in ln), "")
                number = where.rsplit(":", 1)[-1] if where else "?"
                what = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "syntax error")
                return [f"it does not parse: line {number}: {what.strip()[:160]}"]
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return []


# ------------------------------------------------------------------ gate ---


class CodeGate:
    """One per session. `before` is asked ahead of an edit, `after` once it has been written."""

    def __init__(self, session):
        self.session = session
        self.strikes: Dict[str, int] = {}
        self.refused = 0           # this turn
        self.broken: Set[str] = set()   # tests this session's changes broke and have not mended
        self._old: Dict[str, Optional[str]] = {}

    def _words(self) -> str:
        """What was asked lately, where a target may be named ("for the original Xbox", "in C89")."""
        texts = getattr(self.session.ledger, "user_texts", None) or []
        return " ".join(t[-1500:] for t in texts[-2:])

    def mode(self) -> str:
        mode = self.session.app.settings.get("code_gate", "check")
        return mode if mode in MODES else "check"

    def new_turn(self) -> None:
        self.refused = 0

    # ----------------------------------------------------------- before ---

    def _proposed(self, name: str, args: Dict[str, Any]) -> Optional[Tuple[str, Optional[str], str]]:
        """(relative path, old text or None, new text) for an edit, or None when there is nothing to check."""
        from coder.tools import _compute_edit, _resolve_project

        path = str((args or {}).get("path") or "")
        if not path:
            return None
        try:
            target = _resolve_project(self.session.project_dir, path)
        except Exception:  # noqa: BLE001 - the tool itself reports a bad path
            return None
        rel = os.path.relpath(target, self.session.project_dir).replace("\\", "/")
        old: Optional[str] = None
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as fh:
                    old = fh.read()
            except OSError:
                return None
        if name == "write_file":
            return rel, old, str(args.get("content") or "")
        if old is None:
            return None
        computed = _compute_edit(old, str(args.get("old_text") or ""), str(args.get("new_text") or ""))
        if computed is None:
            return None   # the tool will say the block was not found
        return rel, old, computed[0]

    def before(self, name: str, args: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
        """(a refusal for the model, or None; warnings to add to the tool's result)."""
        if name not in ("edit", "write_file") or self.mode() == "off":
            return None, []
        proposed = self._proposed(name, args)
        if proposed is None:
            return None, []
        rel, old, new = proposed
        self._old.pop(rel, None)
        self._old[rel] = old
        while len(self._old) > 12:   # only the latest changes can still be undone
            self._old.pop(next(iter(self._old)))
        problems: List[str] = []
        warnings: List[str] = []
        try:
            if rel.lower().endswith((".py", ".pyi")):
                problems, warnings = python_problems(self.session.project_dir, rel, new, old)
                if not any("does not parse" in p for p in problems):
                    from .codegraph import graph_for

                    graph = graph_for(self.session.project_dir)   # as the project is before this change
                    problems = problems + call_problems(self.session.project_dir, rel, new, old, graph)
                    warnings = warnings + left_behind(rel, old, new, graph)
                if is_test_file(rel):
                    # a test may be written before the thing it tests: a missing name there is a
                    # note, not a refusal
                    ahead = [p for p in problems if " has no `" in p]
                    problems = [p for p in problems if p not in ahead]
                    warnings = warnings + [p + " (fine if you are about to add it)" for p in ahead]
            else:
                problems = other_problems(rel, new)
            knowledge = getattr(self.session.app, "codeknow", None)
            if knowledge is not None and knowledge.wants(rel):
                # C, C++ and assembly: the SDKs and the project are the only ground truth. A change that
                # uses a name neither has is refused; what is merely worth knowing is a note.
                problems = problems + knowledge.refusals(self.session.project_dir, rel, new, old, self._words())
                warnings = warnings + knowledge.notes(self.session.project_dir, rel, new, old, self._words())
        except RecursionError:
            problems = []
        except Exception:  # noqa: BLE001 - a fault in the gate must never block real work
            problems = []
        if not problems:
            self.strikes.pop(rel, None)
            return None, warnings
        self.refused += 1
        strikes = self.strikes[rel] = self.strikes.get(rel, 0) + 1
        text = (f"ERROR: the code gate refused this change to {rel}; nothing was written.\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\nUse only names that exist. Read the file they come from if you are not sure.")
        if strikes >= MAX_STRIKES:
            text += (f"\nThat is {strikes} refused attempts on this file in a row. Stop changing it: tell the user "
                     "what you tried and what stopped you.")
        try:
            self.session._activity("gate", f"Refused a change to {rel}", problems[0][:200], "denied")
        except Exception:  # noqa: BLE001
            pass
        return text, []

    # ------------------------------------------------------------ after ---

    def _test_run(self) -> Optional[Dict[str, Any]]:
        """The last test command this session ran, when it is quick enough to run again."""
        for run in reversed(self.session.ledger.runs):
            if run.get("tests") and not run.get("gate") and run.get("exit") is not None:
                return run if 0 <= float(run.get("seconds") or 0) <= QUICK_SECONDS else None
        return None

    def whole_suite(self) -> Optional[Dict[str, Any]]:
        """Before a reply goes out: when only part of the tests were run, run them all.

        A model changes a function, runs the one test file it was looking at, and reports that the
        tests pass. The rest of the suite (doctests, another module's tests) was never run. When
        files were changed this turn and the last test run named a file, a test or a -k filter and
        passed, the same runner is run once without the narrowing. None when there was nothing to
        widen or the whole suite passes; otherwise {"cmd", "narrow", "failed": [lines]}.
        """
        session = self.session
        ledger = session.ledger
        if self.mode() == "off" or not session.app.settings.get("gate_tests", True):
            return None
        if ledger.last_change_at <= ledger.turn_mark:
            return None
        last = next((r for r in reversed(ledger.runs) if r.get("tests")), None)
        if last is None or last.get("exit") != 0 or last["at"] < ledger.last_change_at:
            return None     # nothing passed after the change: the reply gate already speaks to that
        m = _PYTEST_RUN.match(str(last["cmd"]).strip())
        if not m:
            return None
        rest = m.group(2).split("|")[0].replace("2>&1", " ").split()
        narrowed = any(not t.startswith("-") for t in rest) or "-k" in rest
        if not narrowed:
            return None
        cmd = m.group(1) + " -q"
        from procutil import run_shell

        started = time.monotonic()
        try:
            done = run_shell(cmd, cwd=session.project_dir, timeout=QUICK_SECONDS * 2)
        except OSError:
            return None
        if done.timed_out:
            return None
        from .app import _ANSI_RE

        output = _ANSI_RE.sub("", done.output.strip() or "(no output)")[-6000:]
        ledger.note_run(cmd, f"[exit {done.code}]\n{output}", time.monotonic() - started, gate=True)
        if done.code == 0:
            return None
        # A run that never got as far as the tests is not a failing suite. The widened command is
        # the model's own interpreter line, which may not be the one that has pytest, or the shell
        # may not find it at all. Saying "the whole suite failed" then would be a claim we cannot
        # support: it is simply unknown, so the reply gate stays quiet.
        low = output.lower()
        if any(word in low for word in _RUNNER_MISSING):
            return None
        failed = [" ".join(line.split())[:230] for line in output.splitlines() if line.startswith(("FAILED ", "ERROR "))]
        return {"cmd": cmd, "narrow": str(last["cmd"])[:120],
                "failed": failed[:8] or [output.strip().splitlines()[-1][:200]]}

    def after(self, name: str, args: Dict[str, Any], result: str) -> str:
        """Text to add to an edit's result: what the tests say now. May undo the edit (strict)."""
        mode = self.mode()
        if name not in ("edit", "write_file") or mode == "off" or str(result).lstrip().startswith("ERROR"):
            return ""
        from coder.tools import _resolve_project

        session = self.session
        try:
            target = _resolve_project(session.project_dir, str(args.get("path") or ""))
        except Exception:  # noqa: BLE001
            return ""
        rel = os.path.relpath(target, session.project_dir).replace("\\", "/")
        compiled = ""
        knowledge = getattr(session.app, "codeknow", None)
        if knowledge is not None and knowledge.wants(rel):
            try:   # the target's own compiler, syntax only: the final word on C and C++
                compiled = knowledge.compiled(session.project_dir, rel, self._words())
            except Exception:  # noqa: BLE001 - a broken toolchain must never block real work
                compiled = ""
        return compiled + self._after_tests(mode, session, rel, target)

    def _after_tests(self, mode: str, session: Any, rel: str, target: str) -> str:
        if is_test_file(rel) or not session.app.settings.get("gate_tests", True):
            return ""   # a new failing test is how work often starts
        base = self._test_run()
        if base is None:
            return ""
        cmd = base["cmd"]
        started = time.monotonic()
        from procutil import run_shell

        import shutil
        import tempfile

        # Python keeps compiled copies of each file, checked by time (to the second) and size. An
        # edit in the same second that keeps the size (x * 2 -> x * 3) would run the old code, so
        # the run gets a cache folder of its own.
        fresh = tempfile.mkdtemp(prefix="cm-pycache-")
        try:   # the same shell the agent's own run used, so the command means the same thing
            done = run_shell(cmd, cwd=session.project_dir, timeout=max(20.0, float(base.get("seconds") or 0) * 3),
                             extra_env={"PYTHONPYCACHEPREFIX": fresh})
        except OSError:
            return ""
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
        if done.timed_out:
            return ""
        code: Optional[int] = done.code
        output = done.output.strip() or "(no output)"
        seconds = time.monotonic() - started
        from .app import _ANSI_RE

        output = _ANSI_RE.sub("", output)[-6000:]
        text = f"[exit {code}]\n{output}"
        now_failing = failing_tests(output, code)
        was_failing = set(base.get("failing") or ())
        # a real run, with a result the reply can stand on
        session.ledger.note_run(cmd, text, seconds, gate=True)
        newly = sorted(now_failing - was_failing - self.broken)
        self.broken = (self.broken | set(newly)) & now_failing
        if not newly:
            tail = output.strip().splitlines()[-1][:160] if output.strip() else ""
            mended = sorted(was_failing - now_failing)
            note = f"\n[Gate] Ran `{cmd}` again after this change: {tail}"
            if mended:
                note += f" Now passing: {', '.join(mended[:4])}."
            return note
        detail = "\n".join(output.splitlines()[-25:])
        if mode == "strict":
            old = self._old.get(rel)
            try:
                if old is None:
                    os.remove(target)
                else:
                    with open(target, "w", encoding="utf-8") as fh:
                        fh.write(old)
                self.broken -= set(newly)
                session.ledger.note_run(cmd, f"[exit {base.get('exit')}]\n(restored after an undone change)",
                                        0.0, gate=True, failing=was_failing)
            except OSError:
                pass
            else:
                session._activity("gate", f"Undid a change to {rel}", f"it broke {len(newly)} test(s)", "denied")
                return (f"\nERROR: the code gate undid this change to {rel}: `{cmd}` passed these before and "
                        f"fails them now: {', '.join(newly[:6])}.\n{detail}\nThe file is back as it was. "
                        "Change it in a way that keeps them passing.")
        session._activity("gate", f"A change to {rel} broke {len(newly)} test(s)", ", ".join(newly[:4])[:200], "error")
        return (f"\n[Gate] WARNING: `{cmd}` passed these before this change and fails them now: "
                f"{', '.join(newly[:6])}.\n{detail}\nThe change is kept. Mend it before you say the work is done.")
