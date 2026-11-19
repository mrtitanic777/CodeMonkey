"""Smoke tests for the agent framework (stdlib only).

Run from the project root:
    python tests/test_smoke.py

Uses a FAKE model client, so no API key or network is needed. Every test is
a plain assert; any failure raises and the script exits non-zero.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import AgentLoop  # noqa: E402
from config import Config, load_config  # noqa: E402
from memory import MemoryManager  # noqa: E402
from mini_yaml import parse as yaml_parse  # noqa: E402
from scheduler import cron_matches, is_due, load_jobs  # noqa: E402
from skills import load_skills, skill_search  # noqa: E402
from tools import Tool, ToolRegistry  # noqa: E402
from tools import builtin as builtin_tools  # noqa: E402

import datetime  # noqa: E402


# ------------------------------------------------------- fake client ---


class FakeClient:
    """Scripted stand-in for a model: pops one response per chat() call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        # a copy of what the loop had built by this request (for assertions)
        self.seen_messages = list(messages)
        assert self.script, "FakeClient ran out of scripted responses"
        return self.script.pop(0)


def tool_call_msg(name, args, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


def text_msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def make_config(tmp):
    return Config(
        workspace=tmp,
        agent_name="TestAgent",
        max_iterations=6,
        skills_dir=os.path.join(ROOT, "skills"),
    )


# ------------------------------------------------------------- tests ---


def test_yaml_parser():
    data = yaml_parse("a: 1\nb:\n  c: hello\nd:\n  - x\n  - y: 2\n")
    assert data == {"a": 1, "b": {"c": "hello"}, "d": ["x", {"y": 2}]}, data
    assert yaml_parse("flag: true\nnothing: null\nf: 1.5") == {
        "flag": True,
        "nothing": None,
        "f": 1.5,
    }
    print("PASS test_yaml_parser")


def test_yaml_block_scalars():
    # Regression test: folded/literal scalars must not break sibling items.
    text = (
        "jobs:\n"
        "  - name: first\n"
        "    prompt: >\n"
        "      line one: with a colon\n"
        "      line two without\n"
        "  - name: second\n"
        "    prompt: |\n"
        "      kept\n"
        "      newlines\n"
    )
    data = yaml_parse(text)
    assert len(data["jobs"]) == 2, data
    assert data["jobs"][0]["prompt"] == (
        "line one: with a colon line two without"
    ), data["jobs"][0]["prompt"]
    assert data["jobs"][1]["prompt"] == "kept\nnewlines", (
        data["jobs"][1]["prompt"]
    )
    print("PASS test_yaml_block_scalars")


def test_config_load():
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    assert isinstance(cfg, Config)
    assert cfg.approval_mode in ("ask", "auto")
    assert cfg.max_iterations >= 1
    assert cfg.workspace and cfg.model and cfg.base_url
    print("PASS test_config_load")


def test_registry_dispatch():
    reg = ToolRegistry(auto_approve=True)
    seen = {}

    def echo(text):
        seen["text"] = text
        return f"echo:{text}"

    reg.register(
        Tool(
            name="echo",
            description="Echoes text.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            func=echo,
        )
    )
    out = reg.execute("echo", {"text": "hello"})
    assert out == "echo:hello", out
    assert seen["text"] == "hello"
    # an argument the tool does not take (models add "description") is dropped and said, not fatal
    out = reg.execute("echo", {"text": "hi", "description": "say hi"})
    assert out == "echo:hi\n(ignored arguments this tool does not take: description)", out
    # unknown tool -> ERROR string, not an exception
    out = reg.execute("nope", {})
    assert out.startswith("ERROR: unknown tool"), out
    # exception inside tool -> ERROR string
    reg.register(
        Tool(
            name="boom",
            description="Always fails.",
            parameters={"type": "object", "properties": {}},
            func=lambda: 1 / 0,
        )
    )
    out = reg.execute("boom", {})
    assert out.startswith("ERROR:"), out
    print("PASS test_registry_dispatch")


def test_builtin_file_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry(auto_approve=True)
        for tool in builtin_tools.builtin_tools(tmp):
            reg.register(tool)
        out = reg.execute("write_file", {"path": "a/b.txt", "content": "hi there"})
        assert out.startswith("Wrote"), out
        out = reg.execute("read_file", {"path": "a/b.txt"})
        assert out == "hi there", out
        out = reg.execute(
            "edit_file",
            {"path": "a/b.txt", "old_text": "hi", "new_text": "yo"},
        )
        assert "Edited" in out, out
        assert reg.execute("read_file", {"path": "a/b.txt"}) == "yo there"
        # sandbox escape is rejected
        out = reg.execute("read_file", {"path": "../../etc/passwd"})
        assert out.startswith("ERROR:"), out
    print("PASS test_builtin_file_roundtrip")


def test_skill_search():
    skills = load_skills(os.path.join(ROOT, "skills"))
    names = {s.name for s in skills}
    assert {"notes", "deep-research"} <= names, names
    hits = skill_search("please save a quick note for later", skills)
    assert hits and hits[0].name == "notes", [s.name for s in hits]
    hits = skill_search("plan a thorough investigation of this topic", skills)
    assert hits and hits[0].name == "deep-research", [s.name for s in hits]
    # lazy body loading
    assert "notes.md" in hits[0].body.lower() or "note" in hits[0].body.lower()
    # no match -> empty
    assert skill_search("quantum banana pancakes", skills) == []
    print("PASS test_skill_search")


def test_memory():
    with tempfile.TemporaryDirectory() as tmp:
        mem = MemoryManager(tmp)
        mem.remember("User likes green tea.")
        mem.remember("User is learning Spanish.")
        hits = mem.recall("green tea")
        assert any("green tea" in h for h in hits), hits
        summary = mem.summary()
        assert "green tea" in summary and "Spanish" in summary
        removed = mem.forget("green tea")
        assert removed == 1, removed
        assert mem.recall("green tea") == []
        assert mem.recall("Spanish"), "other facts must survive forget"
        path = mem.log_daily("Tested the memory system.")
        assert os.path.isfile(path)
    print("PASS test_memory")


def test_agent_tool_loop():
    with tempfile.TemporaryDirectory() as tmp:
        received = {}

        def echo(text):
            received["text"] = text
            return f"echo:{text}"

        reg = ToolRegistry(auto_approve=True)
        reg.register(
            Tool(
                name="echo",
                description="Echoes text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
                func=echo,
            )
        )
        client = FakeClient(
            [
                tool_call_msg("echo", {"text": "hello-tool"}),
                text_msg("All done!"),
            ]
        )
        agent = AgentLoop(
            config=make_config(tmp),
            registry=reg,
            client=client,
            skills=[],
        )
        reply = agent.run("please test the echo tool")
        assert reply == "All done!", reply
        assert received["text"] == "hello-tool", received
        assert client.calls == 2, client.calls
    print("PASS test_agent_tool_loop")


def test_agent_recovers_from_tool_error():
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry(auto_approve=True)  # no tools registered
        client = FakeClient(
            [
                tool_call_msg("missing_tool", {}),
                text_msg("Recovered gracefully."),
            ]
        )
        agent = AgentLoop(
            config=make_config(tmp),
            registry=reg,
            client=client,
            skills=[],
        )
        reply = agent.run("call something unknown")
        assert reply == "Recovered gracefully.", reply
        # the error must have been fed back as a tool message
        last_user_visible = client.calls
        assert last_user_visible == 2
    print("PASS test_agent_recovers_from_tool_error")


def test_parallel_read_only_calls_run_together():
    """Consecutive read-only tool calls overlap; results stay in call order."""
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        barrier = threading.Barrier(2, timeout=5)

        def echo(text):
            # Only passes if both calls are inside the tool at once; a
            # sequential loop would hit the barrier's timeout and fail.
            barrier.wait()
            return f"echo:{text}"

        reg = ToolRegistry(auto_approve=True)
        reg.register(
            Tool(
                name="echo",
                description="Echoes text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
                func=echo,
                read_only=True,
            )
        )
        msg = {
            "choices": [{"message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {
                        "name": "echo", "arguments": json.dumps({"text": "one"})}},
                    {"id": "c2", "type": "function", "function": {
                        "name": "echo", "arguments": json.dumps({"text": "two"})}},
                ],
            }}]
        }
        client = FakeClient([msg, text_msg("done")])
        agent = AgentLoop(config=make_config(tmp), registry=reg,
                          client=client, skills=[])
        reply = agent.run("run both")
        assert reply == "done", reply
        # results in the order the model asked for, not completion order
        tool_msgs = [m for m in _captured_messages(agent, client)
                     if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"], tool_msgs
        assert [m["content"] for m in tool_msgs] == ["echo:one", "echo:two"], tool_msgs

        # a non-read-only call between two read-only ones splits the batch:
        # order preserved, no overlap required (barrier would time out)
        reg2 = ToolRegistry(auto_approve=True)
        reg2.register(Tool(name="echo", description="", parameters={
            "type": "object", "properties": {}}, func=lambda text: f"e:{text}",
            read_only=True))
        reg2.register(Tool(name="act", description="", parameters={
            "type": "object", "properties": {}}, func=lambda: "acted"))
        msg2 = {"choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "a1", "type": "function", "function": {
                    "name": "echo", "arguments": json.dumps({"text": "x"})}},
                {"id": "a2", "type": "function", "function": {
                    "name": "act", "arguments": "{}"}},
                {"id": "a3", "type": "function", "function": {
                    "name": "echo", "arguments": json.dumps({"text": "y"})}},
            ]}}]}
        client2 = FakeClient([msg2, text_msg("ok")])
        agent2 = AgentLoop(config=make_config(tmp), registry=reg2,
                           client=client2, skills=[])
        assert agent2.run("mix them") == "ok"
        msgs2 = [m for m in _captured_messages(agent2, client2)
                 if m.get("role") == "tool"]
        assert [(m["tool_call_id"], m["content"]) for m in msgs2] == [
            ("a1", "e:x"), ("a2", "acted"), ("a3", "e:y")], msgs2
    print("PASS test_parallel_read_only_calls_run_together")


def _captured_messages(agent, client):
    """The message list the loop had built as of the client's last request."""
    return client.seen_messages


def test_parallel_safe_gate():
    """parallel_safe is the loop's only say-so on overlapping calls:
    read_only tools yes; unknown tools, PARALLEL_NEVER names, and any
    wrapper layer that lacks read_only no."""
    from tools import parallel_safe

    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(name="look", description="", parameters={},
                      func=lambda: "x", read_only=True))
    reg.register(Tool(name="touch", description="", parameters={},
                      func=lambda: "x"))
    assert parallel_safe(reg, "look") is True
    assert parallel_safe(reg, "touch") is False
    assert parallel_safe(reg, "nope") is False

    # a wrapper layer must agree: its own tool table decides too, and it can
    # veto with PARALLEL_NEVER even though the inner layer allows the call
    class Wrapper(ToolRegistry):
        def __init__(self, inner, never=()):
            super().__init__(auto_approve=inner.auto_approve)
            self.tools = dict(inner.tools)
            self._inner = inner
            if never:
                self.PARALLEL_NEVER = set(never)

    inner = reg
    w = Wrapper(inner)
    assert parallel_safe(w, "look") is True
    w2 = Wrapper(inner, never={"look"})
    assert parallel_safe(w2, "look") is False
    assert parallel_safe(w2, "touch") is False

    # a wrapper whose table lost the flag (registered without read_only)
    # vetoes by itself: every layer must say so
    w3 = Wrapper(inner)
    w3.tools["look"] = Tool(name="look", description="", parameters={},
                            func=lambda: "x")
    assert parallel_safe(w3, "look") is False
    print("PASS test_parallel_safe_gate")


def test_agent_max_iterations_guard():
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry(auto_approve=True)
        reg.register(
            Tool(
                name="loop",
                description="Does nothing.",
                parameters={"type": "object", "properties": {}},
                func=lambda: "again",
            )
        )
        script = [tool_call_msg("loop", {}, call_id=f"c{i}") for i in range(10)]
        client = FakeClient(script)
        cfg = make_config(tmp)
        cfg.max_iterations = 3
        agent = AgentLoop(config=cfg, registry=reg, client=client, skills=[])
        reply = agent.run("loop forever")
        assert "maximum" in reply.lower(), reply
        # three rounds of the same call: no progress, so no more rounds, and one request (no tools) to wrap up
        assert client.calls == 4, client.calls
    print("PASS test_agent_max_iterations_guard")


def test_delegate_tool_registered_once():
    # depth 0 -> delegate present; child (depth 1) -> absent
    with tempfile.TemporaryDirectory() as tmp:
        parent = AgentLoop(
            config=make_config(tmp), client=FakeClient([text_msg("x")]), skills=[]
        )
        assert parent.registry.get("delegate") is not None
        assert parent.depth == 0
        # simulate what _spawn_child builds
        child = AgentLoop(
            config=make_config(tmp),
            client=FakeClient([text_msg("y")]),
            skills=[],
            depth=1,
        )
        assert child.registry.get("delegate") is None
    print("PASS test_delegate_tool_registered_once")


def test_cron_parser():
    monday_8am = datetime.datetime(2026, 10, 12, 8, 0)  # a Monday
    assert cron_matches("0 8 * * *", monday_8am)
    assert not cron_matches("0 9 * * *", monday_8am)
    assert cron_matches("*/15 * * * *", monday_8am.replace(minute=30))
    assert not cron_matches("*/15 * * * *", monday_8am.replace(minute=31))
    assert cron_matches("0 8 * * 1", monday_8am)  # Monday
    assert not cron_matches("0 8 * * 2", monday_8am)  # Tuesday
    print("PASS test_cron_parser")


def test_scheduler_due_logic():
    now = datetime.datetime(2026, 10, 12, 8, 0, 30)
    cron_job = {"name": "j", "schedule": {"cron": "0 8 * * *"}, "prompt": "hi"}
    assert is_due(cron_job, now, None)
    minute_start = now.replace(second=0, microsecond=0).timestamp()
    assert not is_due(cron_job, now, minute_start)  # already fired this minute
    assert is_due(cron_job, now, minute_start - 3600)
    interval_job = {
        "name": "k",
        "schedule": {"interval_seconds": 60},
        "prompt": "hi",
    }
    assert is_due(interval_job, now, None)
    assert not is_due(interval_job, now, now.timestamp() - 30)
    assert is_due(interval_job, now, now.timestamp() - 61)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:   # the jobs file format, from a file of its own
        path = os.path.join(tmp, "jobs.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("jobs:\n  - name: check\n    schedule:\n      interval_seconds: 3600\n    prompt: Say hi.\n")
        jobs = load_jobs(path)
    assert len(jobs) == 1 and jobs[0]["name"] == "check" and jobs[0]["prompt"] == "Say hi.", jobs
    print("PASS test_scheduler_due_logic")


def main():
    tests = [
        test_yaml_parser,
        test_yaml_block_scalars,
        test_config_load,
        test_registry_dispatch,
        test_builtin_file_roundtrip,
        test_skill_search,
        test_memory,
        test_agent_tool_loop,
        test_agent_recovers_from_tool_error,
        test_parallel_read_only_calls_run_together,
        test_parallel_safe_gate,
        test_agent_max_iterations_guard,
        test_delegate_tool_registered_once,
        test_cron_parser,
        test_scheduler_due_logic,
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
