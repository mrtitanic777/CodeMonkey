"""Tests for retrievable tool-output condensation (condense.py).

Run from the project root:
    python tests/test_condense.py

Uses fake tools and fake summarizers; no network, no API key.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from condense import (  # noqa: E402
    CondensingRegistry,
    OutputStore,
    model_summarizer,
    recall_output,
    wrap_registry,
)
from tools.registry import Tool, ToolRegistry  # noqa: E402


def make_registry():
    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(
        name="small", description="d", parameters={},
        func=lambda: "tiny result",
    ))
    reg.register(Tool(
        name="big", description="d", parameters={},
        func=lambda: "X" * 20000,
    ))
    reg.register(Tool(
        name="num", description="d", parameters={},
        func=lambda: 12345,
    ))
    reg.register(Tool(
        name="boom", description="d", parameters={},
        func=lambda: 1 / 0,
    ))
    reg.register(Tool(
        name="gated", description="d", parameters={},
        func=lambda: "secret",
        needs_approval=True,
    ))
    return reg


def fake_summarizer(text):
    return f"SUMMARY(len={len(text)})"


def test_small_output_passthrough_byte_identical():
    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    out = wrapped.execute("small", {})
    assert out == "tiny result", repr(out)
    assert len(wrapped.store) == 0, "small results must not touch the store"
    print("PASS test_small_output_passthrough_byte_identical")


def test_large_output_condensed_and_recallable():
    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    out = wrapped.execute("big", {})
    assert "SUMMARY(len=20000)" in out, out[:200]
    assert "out-1" in out, out[-200:]
    assert "/show out-1" in out, out[-200:]
    assert len(out) < 20000, "condensed form must be far smaller"
    raw = wrapped.store.recall("out-1")
    assert raw == "X" * 20000, "raw output must be stored verbatim"
    print("PASS test_large_output_condensed_and_recallable")


def test_summarizer_failure_falls_back_to_truncation():
    def bad_summarizer(text):
        raise RuntimeError("model is down")

    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=bad_summarizer)
    out = wrapped.execute("big", {})
    assert out.startswith("X" * 8000), "head must be preserved"
    assert "truncated" in out, out[-200:]
    assert "out-1" in out, out[-200:]
    assert wrapped.store.recall("out-1") == "X" * 20000, "raw still stored"
    print("PASS test_summarizer_failure_falls_back_to_truncation")


def test_no_summarizer_falls_back_to_truncation():
    reg = make_registry()
    wrapped = wrap_registry(reg)  # summarizer=None
    out = wrapped.execute("big", {})
    assert out.startswith("X" * 8000)
    assert "out-1" in out
    assert wrapped.store.recall("out-1") == "X" * 20000
    print("PASS test_no_summarizer_falls_back_to_truncation")


def test_blank_summary_falls_back_to_truncation():
    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=lambda text: "   \n ")
    out = wrapped.execute("big", {})
    assert "truncated" in out and "out-1" in out
    print("PASS test_blank_summary_falls_back_to_truncation")


def test_threshold_configurable_and_boundary():
    reg = make_registry()
    wrapped = wrap_registry(
        reg, summarizer=fake_summarizer, threshold_chars=100
    )
    out = wrapped.execute("big", {})
    assert "SUMMARY(len=20000)" in out, "20000 chars > 100 threshold"
    # exactly at the threshold passes through untouched
    wrapped2 = wrap_registry(
        reg, summarizer=fake_summarizer, threshold_chars=11
    )
    assert wrapped2.execute("small", {}) == "tiny result"
    print("PASS test_threshold_configurable_and_boundary")


def test_store_eviction_oldest_first_ids_monotonic():
    store = OutputStore(max_bytes=100)
    assert store.save("a" * 60) == "out-1"
    assert store.save("b" * 60) == "out-2"  # evicts out-1
    assert store.ids() == ["out-2"], store.ids()
    try:
        store.recall("out-1")
    except KeyError:
        pass
    else:
        raise AssertionError("evicted id must raise KeyError")
    assert store.save("c" * 60) == "out-3", "ids never reused"
    assert store.recall("out-3") == "c" * 60
    print("PASS test_store_eviction_oldest_first_ids_monotonic")


def test_single_oversized_entry_kept():
    store = OutputStore(max_bytes=10)
    oid = store.save("z" * 100)
    assert store.recall(oid) == "z" * 100, "newest entry kept despite cap"
    print("PASS test_single_oversized_entry_kept")


def test_non_string_results_handled():
    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    assert wrapped.execute("num", {}) == "12345"
    store = OutputStore()
    oid = store.save(12345)
    assert store.recall(oid) == "12345"
    oid2 = store.save(None)
    assert store.recall(oid2) == "None"
    print("PASS test_non_string_results_handled")


def test_errors_pass_through_uncondensed():
    reg = make_registry()
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    unknown = wrapped.execute("nope", {})
    assert unknown.startswith("ERROR: unknown tool"), unknown
    boom = wrapped.execute("boom", {})
    assert boom.startswith("ERROR: tool"), boom
    assert len(wrapped.store) == 0, "errors must not be stored"
    print("PASS test_errors_pass_through_uncondensed")


def test_recall_output_helper_never_raises():
    store = OutputStore()
    assert "ERROR: unknown output id" in recall_output(store, "out-999")
    oid = store.save("hello")
    assert recall_output(store, oid) == "hello"
    print("PASS test_recall_output_helper_never_raises")


def test_approval_gate_and_approver_preserved():
    reg = make_registry()
    reg.auto_approve = False  # the gate only fires when not auto-approving
    seen = []

    def approver(name, args, preview):
        seen.append(name)
        return (False, "not today")

    reg.approver = approver
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    assert wrapped.approver is approver, "approver delegate must survive wrapping"
    out = wrapped.execute("gated", {})
    assert "denied approval" in out and "not today" in out, out
    assert seen == ["gated"], seen
    print("PASS test_approval_gate_and_approver_preserved")


def test_original_registry_untouched():
    reg = make_registry()
    wrap_registry(reg, summarizer=fake_summarizer)
    # Wrapping takes over size policy: the inner blind-truncation backstop
    # is raised to the documented _INNER_BACKSTOP (so policies can fire);
    # everything else about the base registry is intact.
    from condense import _INNER_BACKSTOP

    assert reg.max_result_chars == _INNER_BACKSTOP
    assert reg.execute("small", {}) == "tiny result"
    print("PASS test_original_registry_untouched")


def test_jsonl_audit_log():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "outputs.jsonl")
        store = OutputStore(file_path=path)
        oid = store.save("audit me")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["id"] == oid and entry["output"] == "audit me", entry
    print("PASS test_jsonl_audit_log")


def test_model_summarizer_factory():
    calls = []

    class FakeClient:
        def chat(self, messages, tools):
            calls.append(messages)
            assert tools == []
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "the brief",
                        "tool_calls": [],
                    }
                }]
            }

    summarize = model_summarizer(FakeClient())
    assert summarize("some output") == "the brief"
    system_text = calls[0][0]["content"]
    assert "condenser" in system_text and "15 lines" in system_text, system_text

    # long inputs are capped before reaching the model
    summarize2 = model_summarizer(FakeClient(), max_input_chars=100)
    summarize2("Q" * 5000)
    assert len(calls[1][1]["content"]) == 100, len(calls[1][1]["content"])
    print("PASS test_model_summarizer_factory")


# ------------------------------------------------------------- policies ---


def make_policy_registry():
    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(
        name="bash", description="d", parameters={},
        func=lambda: "log line\n" * 3000,  # ~27k chars of build log
    ))
    reg.register(Tool(
        name="grep", description="d", parameters={},
        func=lambda: "\n".join(f"file{i}.py: match {i}" for i in range(200)),
    ))
    reg.register(Tool(
        name="read", description="d", parameters={},
        func=lambda: "content line\n" * 2000,  # ~26k chars of file
    ))
    reg.register(Tool(
        name="delegate", description="d", parameters={},
        func=lambda: "subagent report\n" * 2000,
    ))
    return reg


def test_long_error_passes_through_verbatim():
    reg = ToolRegistry(auto_approve=True)
    long_error = "ERROR: " + "x" * 20000
    reg.register(Tool(
        name="bash", description="d", parameters={},
        func=lambda: long_error,
    ))
    calls = []
    wrapped = wrap_registry(
        reg, summarizer=lambda text: calls.append(text) or "SUMMARY"
    )
    out = wrapped.execute("bash", {})
    assert out == long_error, "long errors must surface verbatim"
    assert calls == [], "summarizer must never see an error"
    assert len(wrapped.store) == 0, "errors must not be stored"
    print("PASS test_long_error_passes_through_verbatim")


def test_delegate_exempt_from_condensation():
    reg = make_policy_registry()
    calls = []
    wrapped = wrap_registry(
        reg, summarizer=lambda text: calls.append(text) or "SUMMARY"
    )
    out = wrapped.execute("delegate", {})
    assert out == "subagent report\n" * 2000, "delegate output must be intact"
    assert calls == [], "no summarizer call for exempt tools"
    assert len(wrapped.store) == 0
    print("PASS test_delegate_exempt_from_condensation")


def test_grep_uses_mask_mode():
    reg = make_policy_registry()
    calls = []
    wrapped = wrap_registry(
        reg, summarizer=lambda text: calls.append(text) or "SUMMARY"
    )
    out = wrapped.execute("grep", {})
    assert "output masked" in out, out[:200]
    assert "file0.py: match 0" in out, "preview keeps the first lines"
    assert "file199.py: match 199" not in out.split("[output masked")[0], \
        "tail beyond preview is masked"
    assert "out-1" in out, "id note present"
    assert calls == [], "mask mode makes no model call"
    assert wrapped.store.recall("out-1").count("match") == 200, \
        "full output retrievable"
    print("PASS test_grep_uses_mask_mode")


def test_read_uses_truncate_mode():
    reg = make_policy_registry()
    calls = []
    wrapped = wrap_registry(
        reg, summarizer=lambda text: calls.append(text) or "SUMMARY"
    )
    out = wrapped.execute("read", {})
    assert "output truncated" in out, out[-200:]
    assert out.startswith("content line\n"), "head slice preserved"
    assert calls == [], "truncate mode makes no model call"
    print("PASS test_read_uses_truncate_mode")


def test_bash_uses_summarize_mode():
    reg = make_policy_registry()
    wrapped = wrap_registry(reg, summarizer=fake_summarizer)
    out = wrapped.execute("bash", {})
    assert out.startswith("SUMMARY("), out[:60]
    assert "out-1" in out
    print("PASS test_bash_uses_summarize_mode")


def test_custom_policies_and_validation():
    reg = make_registry()
    wrapped = wrap_registry(
        reg,
        summarizer=fake_summarizer,
        policies=[
            {"tools": ["big"], "mode": "passthrough", "threshold_chars": None},
            {"tools": ["*"], "mode": "truncate", "threshold_chars": 100},
        ],
    )
    assert wrapped.execute("big", {}) == "X" * 20000, "custom passthrough"
    out = wrapped.execute("small", {})  # 11 chars < 100: passthrough
    assert out == "tiny result", out
    try:
        wrap_registry(
            reg, policies=[{"tools": ["x"], "mode": "explode"}]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid mode must raise ValueError")
    try:
        wrap_registry(reg, policies=[{"mode": "summarize"}])
    except ValueError:
        pass
    else:
        raise AssertionError("missing tools list must raise ValueError")
    print("PASS test_custom_policies_and_validation")


def main():
    tests = [
        test_small_output_passthrough_byte_identical,
        test_large_output_condensed_and_recallable,
        test_summarizer_failure_falls_back_to_truncation,
        test_no_summarizer_falls_back_to_truncation,
        test_blank_summary_falls_back_to_truncation,
        test_threshold_configurable_and_boundary,
        test_store_eviction_oldest_first_ids_monotonic,
        test_single_oversized_entry_kept,
        test_non_string_results_handled,
        test_errors_pass_through_uncondensed,
        test_recall_output_helper_never_raises,
        test_approval_gate_and_approver_preserved,
        test_original_registry_untouched,
        test_jsonl_audit_log,
        test_model_summarizer_factory,
        test_long_error_passes_through_verbatim,
        test_delegate_exempt_from_condensation,
        test_grep_uses_mask_mode,
        test_read_uses_truncate_mode,
        test_bash_uses_summarize_mode,
        test_custom_policies_and_validation,
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
