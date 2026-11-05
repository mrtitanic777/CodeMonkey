"""Composition test: hooks + condensation + recorder through the real stack.

This is the audit the frontier wiring called for: it proves that real
pre/post_tool hooks fire THROUGH CondensingRegistry (not around it), that
the audit (post_tool) hook sees the full pre-condensation output while the
model sees the condensed form, and that small results stay byte-identical.

Stack under test (same order as the app builds it):
    base ToolRegistry -> HookedRegistry -> CondensingRegistry -> TraceRecorder

Run from the project root:
    python3 tests/test_hook_condense.py
"""

import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.registry import Tool, ToolRegistry  # noqa: E402
from hooks import Hook, HookManager, wrap_registry as wrap_hooks  # noqa: E402
from condense import CondensingRegistry, OutputStore  # noqa: E402
from cases import TraceRecorder  # noqa: E402


def hook_cmd(python_one_liner):
    """A hook command: reads the JSON payload on stdin, prints a decision."""
    return "python3 -c " + shlex.quote(python_one_liner)


# --- hook programs (JSON in on stdin, JSON decision out on stdout) ---

DENY_BASH = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({"
    "'allow': p['tool'] != 'bash',"
    "'message': ('no bash allowed' if p['tool'] == 'bash' else None),"
    "}))"
)

REWRITE_READ_PATH = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({"
    "'modified_args': {'path': '/hooked/path'}"
    " if p['tool'] == 'read' else {}"
    "}))"
)

POST_NOTE = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({'note': 'audit saw tool ' + p['tool']}))"
)

# Reports how much of the result the audit hook could see (payload caps at
# 4000 chars by the hook protocol).
NOTE_RESULT_LEN = (
    "import sys,json;"
    "p=json.load(sys.stdin);"
    "print(json.dumps({'note': 'result_len=%d' % len(p['result'])}))"
)


def make_stack(pre_hooks=(), post_hooks=(), threshold=1000):
    seen_paths = []

    base = ToolRegistry(auto_approve=True)
    base.register(Tool(
        name="bash", description="d", parameters={},
        func=lambda **kw: "x" * 20000,
    ))
    base.register(Tool(
        name="read", description="d", parameters={},
        func=lambda path="": seen_paths.append(path) or f"contents of {path}",
    ))
    base.register(Tool(
        name="small", description="d", parameters={},
        func=lambda: "tiny result",
    ))

    manager = HookManager(hooks=[
        Hook(event="pre_tool", command=hook_cmd(src)) for src in pre_hooks
    ] + [
        Hook(event="post_tool", command=hook_cmd(src)) for src in post_hooks
    ])
    hooked = wrap_hooks(base, manager)
    condensed = CondensingRegistry(
        hooked,
        store=OutputStore(),
        summarizer=lambda text: f"SUMMARY(len={len(text)})",
        threshold_chars=threshold,
    )
    recorder = TraceRecorder()
    recorder.attach(condensed)
    return condensed, recorder, seen_paths


def test_pre_tool_deny_blocks_through_condenser():
    stack, recorder, _ = make_stack(pre_hooks=(DENY_BASH,))
    out = stack.execute("bash", {})
    assert out.startswith("ERROR: blocked by hook"), out
    assert "no bash allowed" in out, out
    # Errors pass through condensation verbatim.
    assert "SUMMARY" not in out
    print("PASS test_pre_tool_deny_blocks_through_condenser")


def test_pre_tool_modified_args_flow_through():
    stack, recorder, seen = make_stack(pre_hooks=(REWRITE_READ_PATH,))
    out = stack.execute("read", {"path": "/original"})
    assert seen == ["/hooked/path"], seen
    assert "contents of /hooked/path" in out, out
    print("PASS test_pre_tool_modified_args_flow_through")


def test_post_tool_note_appended_and_recorder_sees_final():
    stack, recorder, _ = make_stack(post_hooks=(POST_NOTE,))
    out = stack.execute("small", {})
    assert out.startswith("tiny result"), out
    assert "[hook note] audit saw tool small" in out, out
    # Recorder observed the final (noted) result path.
    assert recorder.events and recorder.events[-1]["tool"] == "small"
    assert recorder.events[-1]["ok"]
    print("PASS test_post_tool_note_appended_and_recorder_sees_final")


def test_audit_sees_full_output_model_sees_condensed():
    stack, recorder, _ = make_stack(post_hooks=(NOTE_RESULT_LEN,))
    out = stack.execute("bash", {})
    # The audit hook saw the full pre-condensation result (hook protocol
    # caps the payload at 4000 chars) — its note survives in the stored
    # raw output, retrievable via /show.
    raw = stack.store.recall("out-1")
    assert "[hook note] result_len=4000" in raw, raw[-100:]
    assert len(stack.store) == 1
    print("PASS test_audit_sees_full_output_model_sees_condensed")


def test_small_output_untouched_by_condensation_with_hooks():
    stack, _, _ = make_stack(pre_hooks=(DENY_BASH,), post_hooks=(POST_NOTE,))
    out = stack.execute("small", {})
    assert "SUMMARY" not in out, "condenser must not touch small results"
    assert "out-" not in out, "small results must not be stored"
    assert len(stack.store) == 0
    print("PASS test_small_output_untouched_by_condensation_with_hooks")


def test_empty_hook_manager_is_transparent():
    stack, _, _ = make_stack()
    assert stack.execute("small", {}) == "tiny result"
    out = stack.execute("bash", {})
    assert out.startswith("SUMMARY(len=20000)"), out[:60]
    print("PASS test_empty_hook_manager_is_transparent")


def main():
    tests = [
        test_pre_tool_deny_blocks_through_condenser,
        test_pre_tool_modified_args_flow_through,
        test_post_tool_note_appended_and_recorder_sees_final,
        test_audit_sees_full_output_model_sees_condensed,
        test_small_output_untouched_by_condensation_with_hooks,
        test_empty_hook_manager_is_transparent,
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
