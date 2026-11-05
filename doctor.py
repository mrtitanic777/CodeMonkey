#!/usr/bin/env python3
"""doctor.py — latent-bug catcher for CodeMonkey.

Some bugs slip past the test suite: hooks silently bypassed by a wrapper
that mirrored instead of delegating, a condensation policy made dead by an
inner truncation, an approver dropped by wrapping. Each incident becomes an executable check
here, so we never develop over that bug class again.

Usage:
    python3 doctor.py              run every check
    python3 doctor.py --list       list available checks
    python3 doctor.py --check NAME run a single check

Every check is a function returning ``(ok: bool, detail: str)``.
Exit code is 0 when all checks pass, 1 otherwise.

Static checks use the ``ast`` module (no imports of project code, so a
broken module can't break the doctor). Dynamic checks build the real
production stack with scripted fakes (no network, no model).
"""

import ast
import io
import os
import re
import shlex
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------- helpers --

def _rel(path):
    """*path* relative to the project, or as it is when it is on another drive."""
    try:
        return os.path.relpath(path, PROJECT_ROOT)
    except ValueError:
        return path


def _project_py_files():
    """All project .py files, excluding tests and caches."""
    out = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # kernel is its own project kept alongside; desktop holds the builds (and their bundled Python).
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", "tests", "kernel", "desktop", "node_modules",
                                    "agent-workspace", ".git")
                       and not dirpath.endswith("webui/static")]
        if "webui/static" in dirpath:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _parse(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return None


# ------------------------------------------------------- 1. wrapper-delegation
# Incident: CondensingRegistry mirrored ToolRegistry.execute line-for-line
# instead of delegating, so pre/post_tool hooks never fired and
# attach_tracing was silently dropped. The wrapper *looked* right.

def _registry_subclass_executes(tree):
    """Yield (class_name, execute_node) for ToolRegistry subclasses."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_sub = any(
            (isinstance(b, ast.Name) and b.id == "ToolRegistry")
            or (isinstance(b, ast.Attribute) and b.attr == "ToolRegistry")
            for b in node.bases
        )
        if not is_sub:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and item.name == "execute":
                yield node.name, item


def _execute_delegates(execute_node):
    """True if execute() reaches self._inner.execute( or super().execute(."""
    for node in ast.walk(execute_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "execute":
            continue
        recv = func.value
        # self._inner.execute(
        if (isinstance(recv, ast.Attribute) and recv.attr == "_inner"
                and isinstance(recv.value, ast.Name)
                and recv.value.id == "self"):
            return True
        # super().execute(
        if (isinstance(recv, ast.Call)
                and isinstance(recv.func, ast.Name)
                and recv.func.id == "super"):
            return True
    return False


def check_wrapper_delegation(py_files=None):
    """Every ToolRegistry.execute override must delegate inward."""
    bad = []
    for path in py_files if py_files is not None else _project_py_files():
        tree = _parse(path)
        if tree is None:
            continue
        for cls_name, exec_node in _registry_subclass_executes(tree):
            if not _execute_delegates(exec_node):
                bad.append(
                    f"{_rel(path)}:{cls_name}")
    if bad:
        return (False,
                "execute() mirrors instead of delegating in: "
                + ", ".join(bad)
                + " — hooks/approvers/tracing on inner layers are bypassed")
    return (True,
            "all ToolRegistry.execute overrides delegate via "
            "self._inner.execute() or super().execute()")


# -------------------------------------------------------- 4. config-documented
# Incident class: a config key read in code but absent from config.yaml
# (even as a comment) is invisible configuration — e.g. webui's
# `coder_cfg.get("hooks_yaml", ...)` was never documented.

_CONFIG_RECEIVERS = {"coder_cfg", "config", "spec_cfg"}


def _config_keys(py_files):
    """Yield (key, relpath, lineno) for .get("key") on config receivers."""
    for path in py_files:
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            recv = func.value
            if not isinstance(recv, ast.Name) \
                    or recv.id not in _CONFIG_RECEIVERS:
                continue
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                yield (node.args[0].value,
                       _rel(path), node.lineno)


def _key_in_yaml(key, yaml_text):
    # A commented-out key counts as documented.
    return re.search(r"(?m)^\s*#?\s*" + re.escape(key) + r"\s*:", yaml_text)


def check_config_documented(py_files=None, yaml_path=None):
    """Every config key read in code must appear in config.yaml."""
    files = py_files if py_files is not None else _project_py_files()
    yaml_path = yaml_path or os.path.join(PROJECT_ROOT, "config.yaml")
    try:
        with open(yaml_path, encoding="utf-8") as fh:
            yaml_text = fh.read()
    except OSError as exc:
        return False, f"cannot read config.yaml: {exc}"
    missing = []
    for key, relpath, lineno in _config_keys(files):
        if not _key_in_yaml(key, yaml_text):
            missing.append(f"{key!r} ({relpath}:{lineno})")
    if missing:
        return (False,
                "config keys read in code but absent from config.yaml: "
                + ", ".join(sorted(set(missing))))
    return True, "every config key read in code is documented in config.yaml"


# ----------------------------------------------------- 5. module-test-coverage
# Incident class: untested modules accumulate latent bugs (two modules
# once had zero test imports). Every top-level module/package must be
# imported by at least one tests/test_*.py file.

def _top_level_modules(root):
    mods = set()
    for entry in os.listdir(root):
        p = os.path.join(root, entry)
        if entry.endswith(".py") and os.path.isfile(p):
            mods.add(entry[:-3])
        elif os.path.isdir(p) and entry != "tests" \
                and os.path.isfile(os.path.join(p, "__init__.py")):
            mods.add(entry)
    return mods


def _test_imports(root):
    imported = set()
    tests_dir = os.path.join(root, "tests")
    if not os.path.isdir(tests_dir):
        return imported
    for fn in os.listdir(tests_dir):
        if not (fn.startswith("test_") and fn.endswith(".py")):
            continue
        tree = _parse(os.path.join(tests_dir, fn))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    return imported


def check_module_test_coverage(root=None):
    """Every top-level module/package is imported by at least one test."""
    root = root or PROJECT_ROOT
    modules = _top_level_modules(root)
    imported = _test_imports(root)
    missing = sorted(m for m in modules if m not in imported)
    if missing:
        return (False,
                "modules with no test import: " + ", ".join(missing))
    return True, f"{len(modules)} top-level modules, all imported by tests"


# ------------------------------------------------------- dynamic check helpers

def _hook_cmd(python_one_liner):
    from procutil import shell_name

    if shell_name() == "cmd.exe":
        # cmd.exe does not understand POSIX single quotes
        import subprocess
        return subprocess.list2cmdline([sys.executable, "-c", python_one_liner])
    # this interpreter, by a path any POSIX shell can read (there may be no python3 on the PATH)
    return shlex.quote(sys.executable.replace("\\", "/")) + " -c " + shlex.quote(python_one_liner)


_DENY_HOOK = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({"
    "'allow': p['tool'] != 'blocked',"
    "'message': ('denied by doctor hook' if p['tool'] == 'blocked' else None),"
    "}))"
)

_MODIFY_HOOK = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "a=dict(p['args']); a['text']='hooked';"
    "print(json.dumps({'modified_args': a} if p['tool']=='echo' else {}))"
)

_NOTE_HOOK = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({'note': 'doctor-audit:' + p['tool']}))"
)


def _build_production_stack(approver=None):
    """Mirror coder.py's registry wiring: base -> hooks -> condense -> speculation.

    Returns (outermost_registry, recorder, seen) where seen collects
    tool-call observations for assertions.
    """
    from tools.registry import Tool, ToolRegistry
    from hooks import Hook, HookManager, wrap_registry as wrap_hooks
    from condense import CondensingRegistry, OutputStore
    from speculation import SpeculativeRegistry
    from cases import TraceRecorder

    seen = {}

    def _echo(text=""):
        seen["echo_text"] = text
        return f"echo:{text}"

    base = ToolRegistry(auto_approve=False)
    base.register(Tool(name="small", description="d", parameters={},
                       func=lambda: "tiny result"))
    base.register(Tool(name="big", description="d", parameters={},
                       func=lambda: "X" * 20000))
    base.register(Tool(name="gated", description="d", parameters={},
                       func=lambda: "gated result", needs_approval=True))
    base.register(Tool(name="echo", description="d", parameters={},
                       func=_echo))
    base.register(Tool(name="blocked", description="d", parameters={},
                       func=lambda: "should never run"))

    manager = HookManager(hooks=[
        Hook(event="pre_tool", command=_hook_cmd(_DENY_HOOK)),
        Hook(event="pre_tool", command=_hook_cmd(_MODIFY_HOOK)),
        Hook(event="post_tool", command=_hook_cmd(_NOTE_HOOK)),
    ])
    registry = wrap_hooks(base, manager)
    registry = CondensingRegistry(
        registry, store=OutputStore(),
        summarizer=lambda t: f"SUMMARY(len={len(t)})",
        threshold_chars=8000,
    )
    # Speculation disabled (production default): must be transparent.
    registry = SpeculativeRegistry(registry, enabled=False)
    if approver is not None:
        registry.approver = approver  # outermost only — must forward inward
    recorder = TraceRecorder()
    recorder.attach(registry)
    return registry, recorder, seen


class _StdinGuard:
    """Make input() raise EOFError so a dropped approver can't hang the run."""

    def __enter__(self):
        self._old = sys.stdin
        sys.stdin = io.StringIO("")
        return self

    def __exit__(self, *exc):
        sys.stdin = self._old
        return False


# -------------------------------------------------------- 6. stack-composition
# Incident: the intended stack order (hooks -> condense -> recorder) was
# silently broken by non-delegating wrappers. This rebuilds the true
# production order and asserts every layer does its job end to end.

def check_stack_composition():
    """Production stack: deny/modify/note hooks, approver, condense, recorder."""
    approver_calls = []

    def approver(name, args, preview):
        approver_calls.append(name)
        return True, None

    with _StdinGuard():
        registry, recorder, seen = _build_production_stack(approver=approver)

        out = registry.execute("blocked", {})
        if not out.startswith("ERROR: blocked by hook"):
            return False, f"deny hook did not block: {out[:80]!r}"

        out = registry.execute("echo", {"text": "orig"})
        if seen.get("echo_text") != "hooked":
            return False, \
                f"modified args did not flow through: {seen.get('echo_text')!r}"

        out = registry.execute("gated", {})
        if not approver_calls:
            return False, \
                "approver set on outermost wrapper never fired " \
                "(terminal fallback would have hung or denied)"
        if not out.startswith("gated result"):
            return False, f"approved tool did not run: {out[:80]!r}"

        out = registry.execute("big", {})
        if not out.startswith("SUMMARY(len="):
            return False, \
                f"large output not condensed: {out[:80]!r}"

        out = registry.execute("small", {})
        # Byte-identical modulo hook notes: condensation must not touch
        # small results (no summary, no out-N id, no truncation markers).
        if not out.startswith("tiny result"):
            return False, f"small output corrupted: {out[:80]!r}"
        for marker in ("SUMMARY(", "[full output", "[output masked:",
                       "[... output truncated", "out-"):
            if marker in out:
                return False, \
                    f"condenser touched a small output ({marker!r}): " \
                    f"{out[:80]!r}"

        if not recorder.events:
            return False, "case recorder saw no events"
        if recorder.events[-1].get("tool") != "small":
            return False, \
                f"recorder last event mismatch: {recorder.events[-1]!r}"

    return True, ("deny blocks, modified args flow, approver fires through "
                  "all layers, large outputs condensed, small byte-identical, "
                  "recorder sees final results")


# ----------------------------------------------------------- 7. policy-liveness
# Incident: base ToolRegistry truncated results at 8000 chars, so the
# bash-summarize (8000) and read-truncate (12000) policies could never
# engage — dead policies in production. Every numeric-threshold policy
# must demonstrably fire through the real stack.

def _policy_engages(tool_name, size_chars, mode):
    """Push size_chars through base->condense; True if the mode engages."""
    from tools.registry import Tool, ToolRegistry
    from condense import CondensingRegistry, OutputStore

    lines = max(1, size_chars // 20)
    base = ToolRegistry(auto_approve=True)
    base.register(Tool(name=tool_name, description="d", parameters={},
                       func=lambda: "output line\n" * lines))
    registry = CondensingRegistry(
        base, store=OutputStore(),
        summarizer=lambda t: "SUMMARY",
        threshold_chars=8000,
    )
    out = registry.execute(tool_name, {})
    if mode == "summarize":
        return out.startswith("SUMMARY") and "[full output" in out
    if mode == "mask":
        return "[output masked:" in out
    if mode == "truncate":
        return "[... output truncated" in out
    return True  # passthrough has nothing to engage


def check_policy_liveness():
    """Every numeric-threshold condense policy fires through the real stack."""
    from condense import DEFAULT_POLICIES

    dead = []
    live = 0
    for policy in DEFAULT_POLICIES:
        threshold = policy.get("threshold_chars")
        if threshold is None:
            continue
        mode = policy.get("mode", "summarize")
        tools = policy.get("tools") or ["*"]
        name = tools[0] if tools[0] != "*" else "custom_tool"
        size = int(threshold * 2.5) + 1000
        try:
            engaged = _policy_engages(name, size, mode)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return False, f"policy {tools} raised {type(exc).__name__}: {exc}"
        if not engaged:
            dead.append(f"{tools} (mode={mode}, threshold={threshold})")
        else:
            live += 1
    if dead:
        return (False,
                "policies that never engage (inner layer pre-truncates?): "
                + ", ".join(dead))
    return True, f"{live} threshold policies engage through the full stack"


# ------------------------------------------------------- 8. approver-forwarding
# Incident: webui set registry.approver AFTER wrapping; the old mirrored
# execute honored the outer approver, delegation bypassed it, and approval
# fell back to the terminal prompt. The approver property must forward to
# the innermost registry from any layer.

def _approver_fires(stack_fn):
    """Build stack via stack_fn(base)->wrapped, set approver outermost only."""
    from tools.registry import Tool, ToolRegistry

    fired = []

    def approver(name, args, preview):
        fired.append(name)
        return True, None

    base = ToolRegistry(auto_approve=False)
    base.register(Tool(name="gated", description="d", parameters={},
                       func=lambda: "gated result", needs_approval=True))
    registry = stack_fn(base)
    registry.approver = approver  # outermost only
    with _StdinGuard():
        out = registry.execute("gated", {})
    return bool(fired) and out == "gated result", fired, out


def check_approver_forwarding():
    """Approver set on the outermost wrapper must fire at innermost execution."""
    from hooks import HookManager, wrap_registry as wrap_hooks
    from condense import CondensingRegistry, OutputStore
    from speculation import SpeculativeRegistry

    empty_hooks = HookManager(hooks=[])
    cases = {
        "base": lambda b: b,
        "hooked": lambda b: wrap_hooks(b, empty_hooks),
        "condensed": lambda b: CondensingRegistry(
            b, store=OutputStore(), summarizer=lambda t: "S"),
        "hooked+condensed": lambda b: CondensingRegistry(
            wrap_hooks(b, empty_hooks),
            store=OutputStore(), summarizer=lambda t: "S"),
        "full-stack": lambda b: SpeculativeRegistry(
            CondensingRegistry(wrap_hooks(b, empty_hooks),
                               store=OutputStore(),
                               summarizer=lambda t: "S"),
            enabled=False),
    }
    broken = []
    for name, fn in cases.items():
        try:
            ok, fired, out = _approver_fires(fn)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name} raised {type(exc).__name__}")
            continue
        if not ok:
            broken.append(f"{name} (approver fired: {bool(fired)})")
    if broken:
        return (False,
                "approver did not forward through: " + ", ".join(broken))
    return True, \
        f"approver forwards through {len(cases)} wrapper combinations"


# ------------------------------------------------------------------ registry --

CHECKS = [
    ("wrapper-delegation",
     check_wrapper_delegation,
     "ToolRegistry.execute overrides must delegate inward"),
    ("config-documented",
     check_config_documented,
     "every config key read in code is in config.yaml"),
    ("module-test-coverage",
     check_module_test_coverage,
     "every top-level module is imported by a test"),
    ("stack-composition",
     check_stack_composition,
     "production stack: hooks, approver, condense, recorder end to end"),
    ("policy-liveness",
     check_policy_liveness,
     "every condense threshold policy fires through the real stack"),
    ("approver-forwarding",
     check_approver_forwarding,
     "approver set outermost fires at innermost execution"),
]


def run_check(name):
    for cname, func, _desc in CHECKS:
        if cname == name:
            try:
                return func()
            except Exception as exc:  # noqa: BLE001 - a check must not crash
                return (False,
                        f"check raised {type(exc).__name__}: {exc}")
    return (False, f"unknown check: {name}")


def main(argv):
    if "--list" in argv:
        for cname, _func, desc in CHECKS:
            print(f"  {cname:22s} {desc}")
        return 0
    if "--check" in argv:
        idx = argv.index("--check")
        names = [argv[idx + 1]] if idx + 1 < len(argv) else []
    else:
        names = [c[0] for c in CHECKS]
    failed = 0
    for name in names:
        ok, detail = run_check(name)
        status = "PASS" if ok else "FAIL"
        print(f"{status} {name}: {detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
