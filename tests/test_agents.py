"""Custom agents: definitions, where they come from, and a helper running as one."""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coder.agents import AgentDef, load_all, parse, render  # noqa: E402


def test_definitions_and_sources():
    a = parse("---\nname: checker\ndescription: Checks.\ntools: read-only, bash\nmodel: p/m\nmax_steps: 7\n---\nDo it well.")
    assert a.name == "checker" and a.model == "p/m" and a.max_steps == 7 and a.prompt == "Do it well."
    assert a.allows("read") and a.allows("code_callers") and a.allows("bash") and not a.allows("edit")
    assert not AgentDef("x", "", "p").allows("delegate") and AgentDef("x", "", "p").allows("edit")
    assert parse("---\nname: Bad Name\n---\nbody") is None and parse("---\nname: ok\n---\n") is None
    assert parse(render(a)).as_dict() == dict(a.as_dict(), origin="yours", path="")
    with tempfile.TemporaryDirectory() as tmp:
        mine, proj = os.path.join(tmp, "agents"), os.path.join(tmp, "proj")
        os.makedirs(mine)
        os.makedirs(os.path.join(proj, ".codemonkey", "agents"))
        with open(os.path.join(mine, "a.md"), "w") as fh:
            fh.write("---\nname: a\ndescription: mine\n---\nmine")
        with open(os.path.join(proj, ".codemonkey", "agents", "a.md"), "w") as fh:
            fh.write("---\nname: a\ndescription: theirs\n---\nproject's")
        found = load_all(mine, proj)
        assert found["a"].description == "theirs" and found["a"].origin == "project"   # the project's wins
    built = load_all(os.path.join(ROOT, "agents"))
    assert {"reviewer", "tester", "researcher"} <= set(built) and built["reviewer"].origin == "built in"
    assert not built["reviewer"].allows("edit") and built["tester"].allows("test_focus")
    print("PASS test_definitions_and_sources")


def test_a_helper_runs_as_a_named_agent():
    from test_webui import FakeClient, start_server, text_msg

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        server.shutdown()
        saved = app.save_agent({"name": "auditor", "description": "Audits.", "tools": "read, grep",
                                "prompt": "Audit carefully.", "max_steps": 3, "scope": "project",
                                "project_dir": tmp})
        assert os.path.isfile(os.path.join(tmp, ".codemonkey", "agents", "auditor.md")) and saved["max_steps"] == 3
        session = app.create_session(tmp)
        session.settings["permission_mode"] = "auto"   # starting a helper asks by default
        assert "auditor: Audits." in session.loop.registry.get("subagent_spawn").description
        assert "agent" in session.loop.registry.get("delegate").parameters["properties"]
        client = FakeClient([text_msg("audited: all good")])
        seen = {}

        def build():
            return client
        session._build_child_client = build
        real_chat = client.chat

        def chat(messages, tools):
            seen["tools"] = sorted(t["function"]["name"] for t in tools)
            seen["prompt"] = messages[-1]["content"]
            return real_chat(messages, tools)
        client.chat = chat
        out = session.loop.registry.execute("delegate", {"brief": "check a.py", "agent": "auditor"})
        assert out == "audited: all good", out
        assert seen["tools"] == ["grep", "read"], seen["tools"]
        assert seen["prompt"].startswith("You are the auditor agent.\nAudit carefully.") and "check a.py" in seen["prompt"]
        assert "no agent named 'nobody'" in session.loop.registry.execute("delegate", {"brief": "x", "agent": "nobody"})
        assert app.delete_agent("auditor", tmp) and "auditor" not in app.agents(tmp)
        session.close()
    print("PASS test_a_helper_runs_as_a_named_agent")


if __name__ == "__main__":
    test_definitions_and_sources()
    test_a_helper_runs_as_a_named_agent()
