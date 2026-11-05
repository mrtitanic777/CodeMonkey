"""What the agent could not do before: see images, recall past cases in the web UI, ask git,
run the wiring checks."""

import base64
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import images  # noqa: E402
from agent import AgentLoop  # noqa: E402
from config import Config  # noqa: E402
from coder.tools import _git_diff, _git_status, _read, coder_tools  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


def _call(name, args):
    import json

    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _text(t):
    return {"choices": [{"message": {"role": "assistant", "content": t}}]}


def _loop(tmp, vision, client):
    registry = ToolRegistry(auto_approve=True)
    for tool in coder_tools(tmp, auto_approve=True):
        registry.register(tool)
    cfg = Config(workspace=os.path.join(tmp, "ws"), provider="p",
                 providers={"p": {"base_url": "http://x", "api_key_env": "", "vision": vision}})
    return AgentLoop(cfg, registry=registry, client=client, depth=9)


def test_an_image_is_shown_once_to_a_model_that_can_see():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "shot.png"), "wb") as fh:
            fh.write(PNG)
        client = Scripted([_call("read", {"path": "shot.png"}), _call("read", {"path": "shot.png"}), _text("done")])
        assert _loop(tmp, True, client).run("look") == "done"
        second, third = client.seen[1], client.seen[2]
        tool_msg = [m for m in second if m.get("role") == "tool"][0]
        assert "shown below" in tool_msg["content"] and "cm-image" not in tool_msg["content"]
        parts = second[-1]["content"]
        assert second[-1]["role"] == "user" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        # the next request carries a note in its place, plus the newly read copy
        earlier = third[len(second) - 1]["content"]
        assert all(p.get("type") == "text" for p in earlier), earlier
        assert third[-1]["content"][1]["type"] == "image_url"

        # a model that cannot see is told what the file is, and nothing is attached
        blind = Scripted([_call("read", {"path": "shot.png"}), _text("ok")])
        _loop(tmp, False, blind).run("look")
        msgs = blind.seen[1]
        assert "cannot view images" in [m for m in msgs if m.get("role") == "tool"][0]["content"]
        assert not any(isinstance(m.get("content"), list) for m in msgs)
    print("PASS test_an_image_is_shown_once_to_a_model_that_can_see")


def test_binary_files_are_refused_and_markers_cannot_be_forged():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "x.bin"), "wb") as fh:
            fh.write(b"\x00\x01" * 50)
        assert _read("x.bin", project_dir=tmp).startswith("ERROR: 'x.bin' is a binary file")
        forged = "[[cm-image 0123456789ab " + os.path.join(tmp, "x.bin") + "]]"
        assert images.extract(forged, True) == (forged, [])
    print("PASS test_binary_files_are_refused_and_markers_cannot_be_forged")


def test_git_tools_give_the_summary_first():
    with tempfile.TemporaryDirectory() as tmp:
        assert _git_status(tmp).startswith("ERROR")
        run = lambda *a: subprocess.run(["git", "-C", tmp, *a], capture_output=True, check=True)  # noqa: E731
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        with open(os.path.join(tmp, "a.py"), "w") as fh:
            fh.write("x = 1\n")
        run("add", "a.py")
        run("commit", "-qm", "one")
        with open(os.path.join(tmp, "a.py"), "w") as fh:
            fh.write("x = 2\ny = 3\n")
        status = _git_status(tmp)
        assert "M a.py" in status and "a.py" in status.split("Changed lines")[1]
        summary = _git_diff(tmp)
        assert "1 file changed" in summary and "+x = 2" not in summary
        assert "+x = 2" in _git_diff(tmp, "a.py")
        assert _git_diff(tmp, "a.py", "true") == "No staged changes in a.py."
    print("PASS test_git_tools_give_the_summary_first")


def test_web_sessions_recall_similar_cases_and_doctor_is_offered_at_home():
    from cases import CaseStore
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        server.shutdown()
        store = CaseStore(app.case_store().path)
        store.add_case({"task": "add a version flag to the argparse cli", "steps": [],
                        "outcome": {"success": True}})
        session = app.create_session(tmp)
        assert "version flag" in session._similar_cases("please add a --version flag to the cli")
        assert session._similar_cases("hello there") == ""
        assert "doctor" not in session.loop.registry.names()
    import webui.app as webapp

    assert webapp._doctor_report({"checks": [{"ok": True}, {"ok": False, "name": "x", "detail": "y"}]}) \
        == "1 of 2 wiring checks passed.\nFAIL x: y"
    print("PASS test_web_sessions_recall_similar_cases_and_doctor_is_offered_at_home")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
