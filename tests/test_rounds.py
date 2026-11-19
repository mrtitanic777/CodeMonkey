"""The tool-round limit: a turn that is getting somewhere goes on; one going round in circles stops
and says where it got to (agent.py)."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import AgentLoop, ROUND_EXTENSIONS  # noqa: E402
from test_smoke import FakeClient, make_config, text_msg, tool_call_msg  # noqa: E402
from tools.registry import Tool, ToolRegistry  # noqa: E402


def _registry(results):
    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(name="edit", description="Change a file.", parameters={"type": "object", "properties": {
        "n": {"type": "string"}}}, func=lambda n="": results.get("edit", "Edited.")))
    reg.register(Tool(name="look", description="Look.", parameters={"type": "object", "properties": {}},
                      func=lambda: "the same thing"))
    return reg


def test_a_turn_that_keeps_changing_things_goes_past_the_limit():
    with tempfile.TemporaryDirectory() as tmp:
        steps = [tool_call_msg("edit", {"n": str(i)}, call_id=f"c{i}") for i in range(7)] + [text_msg("All built.")]
        client = FakeClient(steps)
        cfg = make_config(tmp)
        cfg.max_iterations = 3
        loop = AgentLoop(config=cfg, registry=_registry({}), client=client, skills=[])
        seen = []
        real = client.chat
        client.chat = lambda messages, tools: (seen.append(list(messages)), real(messages, tools))[1]
        assert loop.run("build the app") == "All built."
        assert client.calls == 8, "seven edits and the answer: 3 rounds, then 3 more, then 3 more"
        checks = [m for m in seen[-1] if m.get("role") == "user" and "Progress check" in str(m.get("content"))]
        assert len(checks) == 2 <= ROUND_EXTENSIONS


def test_extensions_run_out_and_the_model_says_where_it_got_to():
    with tempfile.TemporaryDirectory() as tmp:
        steps = [tool_call_msg("edit", {"n": str(i)}, call_id=f"c{i}") for i in range(20)]
        steps.insert(9, text_msg("Done: the board and the scores. Left: sound. Next I'd add the sound effects."))
        client = FakeClient(steps)
        cfg = make_config(tmp)
        cfg.max_iterations = 3
        reply = AgentLoop(config=cfg, registry=_registry({}), client=client, skills=[]).run("build the app")
        assert client.calls == 3 * (1 + ROUND_EXTENSIONS) + 1
        assert reply.startswith("Done: the board and the scores.") and "Say continue" in reply, reply


def test_failing_changes_are_not_progress():
    with tempfile.TemporaryDirectory() as tmp:
        steps = [tool_call_msg("edit", {"n": "x"}, call_id=f"c{i}") for i in range(3)] + [text_msg("I'm stuck on it.")]
        client = FakeClient(steps)
        cfg = make_config(tmp)
        cfg.max_iterations = 3
        reg = _registry({"edit": "ERROR: old_text not found"})
        reply = AgentLoop(config=cfg, registry=reg, client=client, skills=[]).run("fix it")
        assert client.calls == 4 and reply.startswith("I'm stuck on it."), (client.calls, reply)
