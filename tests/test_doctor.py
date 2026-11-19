"""Tests for doctor.py — the latent-bug catcher.

Each check must FAIL on a crafted buggy fixture (proving it catches the
bug class) and PASS on the real codebase (proving it doesn't cry wolf).

Run from the project root:
    python3 tests/test_doctor.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doctor  # noqa: E402


def _write_tmp(suffix, content):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ------------------------------------------------- 1. wrapper-delegation ---

_MIRRORING_WRAPPER = '''
from tools.registry import ToolRegistry

class MirroringRegistry(ToolRegistry):
    """Buggy: mirrors execute instead of delegating (the old condense bug)."""
    def __init__(self, registry):
        super().__init__(auto_approve=registry.auto_approve)
        self.tools = dict(registry.tools)

    def execute(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            return "ERROR: unknown tool"
        return str(tool.func(**arguments))
'''

_DELEGATING_WRAPPER = '''
from tools.registry import ToolRegistry

class GoodRegistry(ToolRegistry):
    def __init__(self, registry):
        super().__init__(auto_approve=registry.auto_approve)
        self.tools = dict(registry.tools)
        self._inner = registry

    def execute(self, name, arguments):
        return self._inner.execute(name, arguments)
'''


def test_wrapper_delegation_catches_mirroring():
    path = _write_tmp(".py", _MIRRORING_WRAPPER)
    try:
        ok, detail = doctor.check_wrapper_delegation(py_files=[path])
        assert not ok, "must flag a mirroring execute()"
        assert "MirroringRegistry" in detail, detail
    finally:
        os.unlink(path)
    print("PASS test_wrapper_delegation_catches_mirroring")


def test_wrapper_delegation_passes_delegating():
    path = _write_tmp(".py", _DELEGATING_WRAPPER)
    try:
        ok, detail = doctor.check_wrapper_delegation(py_files=[path])
        assert ok, f"must pass a delegating wrapper: {detail}"
    finally:
        os.unlink(path)
    print("PASS test_wrapper_delegation_passes_delegating")


def test_wrapper_delegation_passes_real_code():
    ok, detail = doctor.check_wrapper_delegation()
    assert ok, f"real codebase must pass: {detail}"
    print("PASS test_wrapper_delegation_passes_real_code")


# -------------------------------------------------- 4. config-documented ---

def _config_fixture(tmpdir, py_src, yaml_src):
    py_path = os.path.join(tmpdir, "mod.py")
    with open(py_path, "w", encoding="utf-8") as fh:
        fh.write(py_src)
    yaml_path = os.path.join(tmpdir, "config.yaml")
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write(yaml_src)
    return py_path, yaml_path


def test_config_documented_catches_undocumented():
    with tempfile.TemporaryDirectory() as tmp:
        py_path, yaml_path = _config_fixture(
            tmp,
            'def f(coder_cfg):\n    return coder_cfg.get("mystery_key", 1)\n',
            'other_key: 1\n',
        )
        ok, detail = doctor.check_config_documented(
            py_files=[py_path], yaml_path=yaml_path)
        assert not ok, "must flag mystery_key"
        assert "mystery_key" in detail, detail
    print("PASS test_config_documented_catches_undocumented")


def test_config_documented_passes_commented():
    with tempfile.TemporaryDirectory() as tmp:
        py_path, yaml_path = _config_fixture(
            tmp,
            'def f(coder_cfg):\n    return coder_cfg.get("mystery_key", 1)\n',
            '# mystery_key: 1\n',
        )
        ok, detail = doctor.check_config_documented(
            py_files=[py_path], yaml_path=yaml_path)
        assert ok, f"commented-out key counts as documented: {detail}"
    print("PASS test_config_documented_passes_commented")


def test_config_documented_real_code_state():
    # Known state (2026-10-12): zero undocumented keys — the hooks_yaml gap
    # (webui/app.py read it, config.yaml never documented it) was fixed by
    # documenting it under `coder:`. Any NEW undocumented key fails loudly.
    ok, detail = doctor.check_config_documented()
    assert ok, f"unexpected undocumented config keys: {detail}"
    print("PASS test_config_documented_real_code_state")


# ------------------------------------------------ 5. module-test-coverage ---

def test_module_test_coverage_catches_lonely_module():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "lonely.py"), "w") as fh:
            fh.write("X = 1\n")
        tests_dir = os.path.join(tmp, "tests")
        os.mkdir(tests_dir)
        with open(os.path.join(tests_dir, "test_x.py"), "w") as fh:
            fh.write("import os\n")
        ok, detail = doctor.check_module_test_coverage(root=tmp)
        assert not ok, "must flag lonely.py"
        assert "lonely" in detail, detail
    print("PASS test_module_test_coverage_catches_lonely_module")


def test_module_test_coverage_passes_real_code():
    ok, detail = doctor.check_module_test_coverage()
    assert ok, f"real codebase must pass: {detail}"
    print("PASS test_module_test_coverage_passes_real_code")


# ------------------------------------------------------- dynamic checks ---

def test_stack_composition_passes():
    ok, detail = doctor.check_stack_composition()
    assert ok, f"stack composition must pass: {detail}"
    print("PASS test_stack_composition_passes")


def test_policy_liveness_passes():
    ok, detail = doctor.check_policy_liveness()
    assert ok, f"policy liveness must pass: {detail}"
    print("PASS test_policy_liveness_passes")


def test_policy_liveness_catches_dead_policy():
    # The historical bug: an unwrapped base registry truncates at 8000,
    # so a 20000-char bash output never reaches the summarize policy.
    from tools.registry import Tool, ToolRegistry
    base = ToolRegistry(auto_approve=True)
    base.register(Tool(name="bash", description="d", parameters={},
                       func=lambda: "X" * 20000))
    out = base.execute("bash", {})
    assert out == "X" * 8000, "bare base must blind-truncate"
    assert not out.startswith("SUMMARY"), \
        "without the condense wrap the summarize policy cannot engage"
    print("PASS test_policy_liveness_catches_dead_policy")


def test_approver_forwarding_passes():
    ok, detail = doctor.check_approver_forwarding()
    assert ok, f"approver forwarding must pass: {detail}"
    print("PASS test_approver_forwarding_passes")


# ------------------------------------------------------- import smoke ---

def test_import_smoke():
    import doctor as _doctor  # noqa: F401
    assert hasattr(_doctor, "CHECKS"), "doctor.py must expose CHECKS"
    print("PASS test_import_smoke")


def main():
    tests = [
        test_wrapper_delegation_catches_mirroring,
        test_wrapper_delegation_passes_delegating,
        test_wrapper_delegation_passes_real_code,
        test_config_documented_catches_undocumented,
        test_config_documented_passes_commented,
        test_config_documented_real_code_state,
        test_module_test_coverage_catches_lonely_module,
        test_module_test_coverage_passes_real_code,
        test_stack_composition_passes,
        test_policy_liveness_passes,
        test_policy_liveness_catches_dead_policy,
        test_approver_forwarding_passes,
        test_import_smoke,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
