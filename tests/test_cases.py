"""Tests for the case-based reasoning subsystem (cases/).

Run from the project root:
    python tests/test_cases.py

Stdlib only; no network, no API key.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cases import CaseStore, TraceRecorder, format_cases  # noqa: E402
from tools.registry import Tool, ToolRegistry  # noqa: E402


def make_store(tmp):
    return CaseStore(os.path.join(tmp, "cases.jsonl"))


def sample_case(task, success=True, plan_summary=""):
    return {
        "task": task,
        "plan_summary": plan_summary,
        "trace": [{"tool": "read", "args_summary": "path='x'", "ok": True}],
        "outcome": {"success": success, "tests_passed": success,
                    "turns": 3, "latency_s": 10.0},
        "project_kind": "python-cli",
    }


# ---------------------------------------------------------------- store ---

def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        stored = store.add_case(sample_case("add --version flag"))
        assert stored["id"].startswith("case-"), stored["id"]
        assert stored["timestamp"], "timestamp assigned"
        assert stored["embedding"] is None, "embedding field reserved"
        again = CaseStore(os.path.join(tmp, "cases.jsonl"))
        cases = again.all_cases()
        assert len(cases) == 1
        assert cases[0]["task"] == "add --version flag"
        assert cases[0]["id"] == stored["id"]
    print("PASS test_store_roundtrip")


def test_store_empty_and_corrupt_lines():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        assert store.all_cases() == [], "missing file -> []"
        assert store.find_similar("anything") == []
        path = os.path.join(tmp, "cases.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("this is not json\n")
            fh.write(json.dumps(sample_case("real case")) + "\n")
        assert len(store.all_cases()) == 1, "corrupt line skipped"
    print("PASS test_store_empty_and_corrupt_lines")


def test_find_similar_ranks_success_above_failure():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        store.add_case(sample_case(
            "add version flag to the coder script", success=False))
        store.add_case(sample_case("version flag", success=True))
        store.add_case(sample_case(
            "fix database migration rollback", success=True))
        hits = store.find_similar("add version flag", k=3)
        assert len(hits) == 2, [c["task"] for c in hits]
        assert hits[0]["task"] == "version flag", (
            "success outranks higher-scoring failure",
            [(c["task"], c["outcome"]["success"]) for c in hits],
        )
        assert hits[1]["outcome"]["success"] is False
    print("PASS test_find_similar_ranks_success_above_failure")


def test_find_similar_task_weighted_and_zero_overlap_excluded():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        store.add_case(sample_case(
            "add --version flag to coder.py",
            plan_summary="read argparse setup then add the flag"))
        hits = store.find_similar("--version argparse flag")
        assert len(hits) == 1
        assert store.find_similar("quantum banana hammock") == [], \
            "zero overlap -> []"
    print("PASS test_find_similar_task_weighted_and_zero_overlap_excluded")


# -------------------------------------------------------------- recorder ---

def make_registry():
    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(
        name="read", description="read a file",
        parameters={}, func=lambda path: f"contents of {path}",
    ))
    reg.register(Tool(
        name="boom", description="always fails",
        parameters={}, func=lambda: 1 / 0,
    ))
    return reg


def test_recorder_captures_calls_including_failures():
    reg = make_registry()
    rec = TraceRecorder().attach(reg)
    assert reg.execute("read", {"path": "coder.py"}).startswith("contents")
    assert reg.execute("boom", {}).startswith("ERROR")
    assert len(rec.events) == 2, rec.events
    assert rec.events[0] == {
        "tool": "read", "args_summary": "path='coder.py'", "ok": True,
    }
    assert rec.events[1]["tool"] == "boom"
    assert rec.events[1]["ok"] is False
    print("PASS test_recorder_captures_calls_including_failures")


def test_recorder_truncates_long_args_and_idempotent_attach():
    reg = make_registry()
    rec = TraceRecorder(arg_limit=200).attach(reg)
    rec.attach(reg)  # second attach must be a no-op
    reg.execute("read", {"path": "x" * 500})
    assert len(rec.events) == 1, "double attach would record twice"
    summary = rec.events[0]["args_summary"]
    assert len(summary) < 500 and "(+300 chars)" in summary, summary
    print("PASS test_recorder_truncates_long_args_and_idempotent_attach")


def test_recorder_to_case_schema():
    rec = TraceRecorder()
    rec.record("edit", {"path": "a.py"}, True)
    rec.record("bash", {"command": "false"}, False)
    case = rec.to_case(
        "fix the thing",
        {"success": False, "turns": 7},
        plan_summary="try stuff",
        project_kind="python-cli",
    )
    assert case["task"] == "fix the thing"
    assert case["plan_summary"] == "try stuff"
    assert len(case["trace"]) == 2
    assert case["outcome"]["success"] is False
    assert case["outcome"]["tests_passed"] is False  # defaulted
    assert case["outcome"]["turns"] == 7
    assert "id" not in case, "store assigns id"
    print("PASS test_recorder_to_case_schema")


# ----------------------------------------------------------------- recall ---

def test_format_cases_renders_sections():
    cases = [sample_case("add --version flag", plan_summary="read then edit")]
    text = format_cases(cases)
    assert "similar past tasks that SUCCEEDED" in text
    assert "Task: add --version flag" in text
    assert "Plan: read then edit" in text
    assert "What worked:" in text and "read(" in text
    assert "Outcome: success" in text
    assert format_cases([]) == "", "empty -> empty string"
    print("PASS test_format_cases_renders_sections")


def test_format_cases_respects_char_cap():
    cases = [sample_case("task number %d with padding x" % i + "y" * 300)
             for i in range(5)]
    text = format_cases(cases, max_chars=500)
    marker = "\n… [reference truncated]"
    assert len(text) <= 500 + len(marker), len(text)
    assert text.endswith(marker) and "truncated" in text
    print("PASS test_format_cases_respects_char_cap")


# -------------------------------------------------------- dual memory ---


def test_distill_strategies_from_success():
    rec = TraceRecorder()
    rec.record("read", {"path": "x.py"}, True)
    rec.record("grep", {"pattern": "foo"}, True)
    rec.record("edit", {"path": "x.py"}, True)
    rec.record("bash", {"command": "pytest"}, True)
    case = rec.to_case(
        "add thing", {"success": True, "turns": 4},
        plan_summary="read, edit, test",
    )
    strategies = case["strategies"]
    assert 1 <= len(strategies) <= 3, strategies
    assert any("before making edits" in s for s in strategies), strategies
    assert any("running tests" in s for s in strategies), strategies
    assert case["pitfalls"] == [], "successes distill no pitfalls"
    print("PASS test_distill_strategies_from_success")


def test_distill_pitfalls_from_failure():
    rec = TraceRecorder()
    rec.record("edit", {"path": "y.py"}, False)
    rec.record("edit", {"path": "z.py"}, False)  # same tool: deduped
    rec.record("bash", {"command": "nope"}, False)
    case = rec.to_case("fix thing", {"success": False})
    assert case["strategies"] == [], "failures distill no strategies"
    pitfalls = case["pitfalls"]
    assert len(pitfalls) == 2, pitfalls
    assert any("'edit'" in p and "old_text" in p for p in pitfalls), pitfalls
    assert any("'bash'" in p for p in pitfalls), pitfalls
    print("PASS test_distill_pitfalls_from_failure")


def test_store_backward_compat_dual_memory():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        stored = store.add_case(sample_case("old task"))
        assert stored["strategies"] == [], stored
        assert stored["pitfalls"] == [], stored
        assert store.all_cases()[0]["strategies"] == []
    print("PASS test_store_backward_compat_dual_memory")


def test_format_cases_renders_dual_memory_sections():
    rec = TraceRecorder()
    rec.record("read", {"path": "a"}, True)
    rec.record("edit", {"path": "a"}, True)
    ok_case = rec.to_case("t1", {"success": True})
    rec2 = TraceRecorder()
    rec2.record("bash", {"command": "x"}, False)
    fail_case = rec2.to_case("t2", {"success": False})
    text = format_cases([ok_case, fail_case])
    assert "Strategies that worked" in text, text
    assert "Pitfalls to avoid" in text, text
    # legacy cases without the keys render without the sections
    text2 = format_cases([sample_case("legacy")])
    assert "Strategies that worked" not in text2, text2
    assert "Pitfalls to avoid" not in text2, text2
    print("PASS test_format_cases_renders_dual_memory_sections")


def main():
    tests = [
        test_store_roundtrip,
        test_store_empty_and_corrupt_lines,
        test_find_similar_ranks_success_above_failure,
        test_find_similar_task_weighted_and_zero_overlap_excluded,
        test_recorder_captures_calls_including_failures,
        test_recorder_truncates_long_args_and_idempotent_attach,
        test_recorder_to_case_schema,
        test_format_cases_renders_sections,
        test_format_cases_respects_char_cap,
        test_distill_strategies_from_success,
        test_distill_pitfalls_from_failure,
        test_store_backward_compat_dual_memory,
        test_format_cases_renders_dual_memory_sections,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
