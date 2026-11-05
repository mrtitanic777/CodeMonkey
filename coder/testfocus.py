"""Run the tests that cover what changed, instead of the whole suite.

The changed files come from the caller or from git (changed since the last commit, plus new
files). Python: a changed test file runs itself; a changed module runs the tests named after it
(test_<name>.py, <name>_test.py) and the test files that import it. JavaScript/TypeScript with Jest
or Vitest: their own "related tests" mode. When nothing can be matched the answer says so and the
full suite is left to the agent to run.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import List, Tuple

import procutil

TIMEOUT = 300
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}
_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _changed(project_dir: str) -> Tuple[List[str], str]:
    from coder.tools import _run_git

    code, out = _run_git(project_dir, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return [], "not a git repository: pass paths to say what changed"
    files = []
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path and os.path.isfile(os.path.join(project_dir, path)):
            files.append(path.replace("\\", "/"))
    return files, ""


def _is_test(path: str) -> bool:
    base = os.path.basename(path)
    return bool(re.match(r"test_.*\.py$|.*_test\.py$|.*\.(test|spec)\.[jt]sx?$", base))


def _python_tests(project_dir: str) -> List[str]:
    found = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for name in files:
            if name.endswith(".py") and _is_test(name):
                found.append(os.path.relpath(os.path.join(root, name), project_dir).replace("\\", "/"))
    return sorted(found)


def _import_pattern(dotted: str) -> str:
    """An import of the module *dotted* ("pkg.mod" or "mod") by its full name, or from its package.
    A bare "from tools import x" is some other top-level "tools", not pkg/tools.py."""
    full = re.escape(dotted)
    forms = [rf"from\s+{full}(\.\w+)*\s+import\b", rf"import\s+{full}\b"]
    if "." in dotted:
        # from the package itself: its __init__ often re-exports what the module defines
        forms.append(rf"from\s+{re.escape(dotted.rsplit('.', 1)[0])}\s+import\b")
    return r"^\s*(" + "|".join(forms) + ")"


def pick_python(project_dir: str, changed: List[str]) -> List[Tuple[str, str]]:
    """(test file, why) for the changed Python files."""
    picked: dict = {}
    modules = []
    for path in changed:
        if not path.endswith(".py"):
            continue
        if _is_test(path):
            picked.setdefault(path, "it changed")
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        dotted = os.path.splitext(path)[0].replace("/", ".")
        if stem == "__init__":
            stem, dotted = os.path.basename(os.path.dirname(path)), os.path.dirname(path).replace("/", ".")
        if stem:
            modules.append((path, stem, dotted))
    if not modules:
        return list(picked.items())
    for test in _python_tests(project_dir):
        base = os.path.basename(test)
        try:
            with open(os.path.join(project_dir, test), "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for path, stem, dotted in modules:
            if base in (f"test_{stem}.py", f"{stem}_test.py"):
                picked.setdefault(test, f"named after {path}")
            elif re.search(_import_pattern(dotted), text, re.M):
                picked.setdefault(test, f"imports {path}")
    return sorted(picked.items())


def _js_runner(project_dir: str) -> str:
    try:
        with open(os.path.join(project_dir, "package.json"), "r", encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return ""
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return "vitest" if "vitest" in deps else "jest" if "jest" in deps else ""


def run(project_dir: str, paths: str = "") -> str:
    if paths:
        changed = [p.strip().replace("\\", "/") for p in re.split(r"[,\n]", str(paths)) if p.strip()]
    else:
        changed, why = _changed(project_dir)
        if why:
            return "ERROR: " + why
    if not changed:
        return "Nothing has changed since the last commit, so there is nothing to focus on."
    out: List[str] = []
    ran = False
    py = pick_python(project_dir, changed)
    if py:
        files = [t for t, _ in py]
        out.append("Python tests chosen:\n" + "\n".join(f"  {t} ({why})" for t, why in py))
        cmd = f'"{sys.executable}" -m pytest -q ' + " ".join(f'"{f}"' for f in files)
        done = procutil.run_shell(cmd, cwd=project_dir, timeout=TIMEOUT, sandbox=_level())
        if done.code is not None and "No module named pytest" in done.output:
            done = None
            for f in files:   # no pytest: each file as a script
                one = procutil.run_shell(f'"{sys.executable}" "{f}"', cwd=project_dir, timeout=TIMEOUT, sandbox=_level())
                out.append(f"$ python {f} -> exit {one.code}\n" + _tail(one.output, 25))
        if done is not None:
            out.append(f"$ pytest ({len(files)} files) -> " + ("timed out" if done.timed_out else f"exit {done.code}"))
            out.append(_tail(done.output, 60))
        ran = True
    js = [p for p in changed if p.endswith(_JS)]
    runner = _js_runner(project_dir) if js else ""
    if runner:
        quoted = " ".join(f'"{p}"' for p in js)
        cmd = f"npx vitest related --run {quoted}" if runner == "vitest" else f"npx jest --findRelatedTests {quoted}"
        done = procutil.run_shell(cmd, cwd=project_dir, timeout=TIMEOUT, sandbox=_level())
        out.append(f"$ {cmd} -> " + ("timed out" if done.timed_out else f"exit {done.code}"))
        out.append(_tail(done.output, 60))
        ran = True
    if not ran:
        return ("No tests could be matched to the changed files (" + ", ".join(changed[:8]) +
                "). Run the project's full test suite instead.")
    other = [p for p in changed if not p.endswith(".py") and not (runner and p.endswith(_JS))]
    if other:
        out.append("Not covered by this run (no matching tests): " + ", ".join(other[:10]))
    return "\n".join(out)


def _level() -> str:
    from coder import sandbox

    return sandbox.level()


def _tail(text: str, lines: int) -> str:
    rows = (text or "").rstrip().splitlines()
    return "\n".join(rows[-lines:]) if rows else "(no output)"
