"""Tests for the breadth pass: hooks system, new skills, new commands.

Run from the project root:
    python3 tests/test_breadth.py

Stdlib only. Hook commands are tiny inline shell snippets run via
subprocess; no network, no API key.
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coder.commands import (  # noqa: E402
    expand_command,
    find_command,
    load_commands,
)
from hooks import Hook, HookManager, wrap_registry  # noqa: E402
from skills import load_skills  # noqa: E402
from tools import Tool, ToolRegistry  # noqa: E402

ALLOW = "printf '%s' '{\"allow\": true}'"
DENY = "printf '%s' '{\"allow\": false, \"message\": \"nope\"}'"
MODIFY = "printf '%s' '{\"allow\": true, \"modified_args\": {\"text\": \"changed\"}}'"
NOTE = "printf '%s' '{\"note\": \"audited\"}'"
BAD_JSON = "printf '%s' 'not json'"
FAIL_EXIT = "exit 3"


def mgr(*hooks):
    return HookManager(list(hooks))


def test_load_example_yaml():
    manager = HookManager.load(os.path.join(ROOT, "hooks.yaml.example"))
    events = sorted(h.event for h in manager.hooks)
    assert events == ["post_tool", "pre_tool"], events
    assert all(h.timeout == 5 for h in manager.hooks), [
        (h.event, h.timeout) for h in manager.hooks
    ]
    print("PASS test_load_example_yaml")


def test_load_missing_file_is_empty():
    manager = HookManager.load("/nonexistent/hooks.yaml")
    assert manager.hooks == []
    assert manager.run("pre_tool", {})["allow"] is True
    print("PASS test_load_missing_file_is_empty")


def test_load_ignores_bad_entries():
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as fh:
        fh.write(
            "hooks:\n"
            "  - event: pre_tool\n"
            "    command: echo hi\n"
            "  - event: bogus_event\n"
            "    command: echo hi\n"
            "  - event: post_tool\n"
        )
        path = fh.name
    try:
        manager = HookManager.load(path)
        assert len(manager.hooks) == 1, [h.event for h in manager.hooks]
        assert manager.hooks[0].event == "pre_tool"
    finally:
        os.unlink(path)
    print("PASS test_load_ignores_bad_entries")


def test_pre_tool_allow():
    d = mgr(Hook("pre_tool", ALLOW)).run(
        "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}}
    )
    assert d["allow"] is True and d["message"] is None, d
    print("PASS test_pre_tool_allow")


def test_pre_tool_deny():
    d = mgr(Hook("pre_tool", DENY)).run(
        "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}}
    )
    assert d["allow"] is False, d
    assert d["message"] == "nope", d
    print("PASS test_pre_tool_deny")


def test_deny_wins_over_allow():
    d = mgr(Hook("pre_tool", ALLOW), Hook("pre_tool", DENY)).run(
        "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}}
    )
    assert d["allow"] is False, d
    print("PASS test_deny_wins_over_allow")


def test_modified_args_chain():
    d = mgr(
        Hook("pre_tool", MODIFY),
        Hook("pre_tool", "printf '%s' '{\"allow\": true}'"),
    ).run("pre_tool", {"event": "pre_tool", "tool": "echo", "args": {"text": "hi"}})
    assert d["allow"] is True, d
    assert d["modified_args"] == {"text": "changed"}, d
    print("PASS test_modified_args_chain")


def test_post_tool_note():
    d = mgr(Hook("post_tool", NOTE)).run(
        "post_tool",
        {"event": "post_tool", "tool": "echo", "args": {}, "result": "ok"},
    )
    assert d["allow"] is True, d
    assert d["notes"] == ["audited"], d
    print("PASS test_post_tool_note")


def test_hook_receives_payload():
    script = (
        "python3 -c 'import json,sys; p=json.load(sys.stdin); "
        "print(json.dumps({\"allow\": p[\"tool\"] != \"evil\"}))'"
    )
    d = mgr(Hook("pre_tool", script)).run(
        "pre_tool", {"event": "pre_tool", "tool": "evil", "args": {}}
    )
    assert d["allow"] is False, d
    d2 = mgr(Hook("pre_tool", script)).run(
        "pre_tool", {"event": "pre_tool", "tool": "good", "args": {}}
    )
    assert d2["allow"] is True, d2
    print("PASS test_hook_receives_payload")


def test_timeout_fails_open():
    t0 = time.monotonic()
    d = mgr(Hook("pre_tool", "sleep 30", timeout=0.2)).run(
        "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}}
    )
    elapsed = time.monotonic() - t0
    assert d["allow"] is True, d
    assert elapsed < 5, elapsed
    print("PASS test_timeout_fails_open")


def test_bad_exit_and_bad_json_fail_open():
    for cmd in (FAIL_EXIT, BAD_JSON):
        d = mgr(Hook("pre_tool", cmd)).run(
            "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}}
        )
        assert d["allow"] is True, (cmd, d)
    print("PASS test_bad_exit_and_bad_json_fail_open")


def _echo_registry():
    reg = ToolRegistry(auto_approve=True)
    reg.register(
        Tool(
            name="echo",
            description="Echoes text.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            func=lambda text: f"echo:{text}",
        )
    )
    return reg


def test_wrap_registry_blocked():
    wrapped = wrap_registry(_echo_registry(), mgr(Hook("pre_tool", DENY)))
    result = wrapped.execute("echo", {"text": "hi"})
    assert result.startswith("ERROR: blocked by hook: nope"), result
    print("PASS test_wrap_registry_blocked")


def test_wrap_registry_allowed():
    wrapped = wrap_registry(_echo_registry(), mgr(Hook("pre_tool", ALLOW)))
    assert wrapped.execute("echo", {"text": "hi"}) == "echo:hi"
    print("PASS test_wrap_registry_allowed")


def test_wrap_registry_modified_args():
    wrapped = wrap_registry(_echo_registry(), mgr(Hook("pre_tool", MODIFY)))
    assert wrapped.execute("echo", {"text": "hi"}) == "echo:changed"
    print("PASS test_wrap_registry_modified_args")


def test_wrap_registry_post_note():
    wrapped = wrap_registry(
        _echo_registry(),
        mgr(Hook("pre_tool", ALLOW), Hook("post_tool", NOTE)),
    )
    result = wrapped.execute("echo", {"text": "hi"})
    assert result.startswith("echo:hi"), result
    assert "[hook note] audited" in result, result
    print("PASS test_wrap_registry_post_note")


def test_wrap_registry_no_hooks_is_transparent():
    reg = _echo_registry()
    wrapped = wrap_registry(reg, HookManager([]))
    assert isinstance(wrapped, ToolRegistry)
    assert wrapped.names() == reg.names()
    assert wrapped.execute("echo", {"text": "hi"}) == "echo:hi"
    assert wrapped.execute("missing", {}) .startswith("ERROR: unknown tool")
    # original registry untouched
    assert type(reg) is ToolRegistry
    print("PASS test_wrap_registry_no_hooks_is_transparent")


def test_new_skills_load_with_use_when():
    skills = load_skills(os.path.join(ROOT, "skills"))
    by_name = {s.name: s for s in skills}
    for name in ("debugging", "tdd", "writing-plans", "documenting"):
        assert name in by_name, sorted(by_name)
        desc = by_name[name].description
        assert desc.lower().startswith("use when"), (name, desc)
        assert len(by_name[name].body) > 200, name
    print("PASS test_new_skills_load_with_use_when")


def test_new_commands_parse():
    commands = load_commands(os.path.join(ROOT, "commands"))
    for name in ("refactor", "debug", "document", "explain"):
        cmd = find_command(commands, name)
        assert cmd is not None, name
        assert cmd.name == name, cmd.name
        assert cmd.description, name
        assert "$ARGUMENTS" in cmd.body, name
    expanded = expand_command(find_command(commands, "explain"), "agent.py run()")
    assert "agent.py run()" in expanded, expanded
    assert "$ARGUMENTS" not in expanded, expanded
    print("PASS test_new_commands_parse")


def main():
    tests = [
        test_load_example_yaml,
        test_load_missing_file_is_empty,
        test_load_ignores_bad_entries,
        test_pre_tool_allow,
        test_pre_tool_deny,
        test_deny_wins_over_allow,
        test_modified_args_chain,
        test_post_tool_note,
        test_hook_receives_payload,
        test_timeout_fails_open,
        test_bad_exit_and_bad_json_fail_open,
        test_wrap_registry_blocked,
        test_wrap_registry_allowed,
        test_wrap_registry_modified_args,
        test_wrap_registry_post_note,
        test_wrap_registry_no_hooks_is_transparent,
        test_new_skills_load_with_use_when,
        test_new_commands_parse,
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
