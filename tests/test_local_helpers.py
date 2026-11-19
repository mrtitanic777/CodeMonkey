"""Helps for a model on this computer: tool calls repaired, edits that tell it where to look,
files checked as they are written, output that keeps its end, reads not sent twice, a smaller
project pack, and a checklist for bigger requests."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import _lenient_json  # noqa: E402
from coder import checks  # noqa: E402
from coder.tools import _closest, _make_edit, _make_read, _make_write_file, _truncate  # noqa: E402
from tools.registry import Tool, ToolRegistry  # noqa: E402


def test_nearly_json_arguments_are_read():
    assert _lenient_json('{"path": "a.py"}') == {"path": "a.py"}
    assert _lenient_json('{"path": "a.py",}') == {"path": "a.py"}
    assert _lenient_json("```json\n{\"path\": \"a.py\"}\n```") == {"path": "a.py"}
    assert _lenient_json('{"content": "line one\nline two"}') == {"content": "line one\nline two"}
    assert _lenient_json("{'path': 'a.py', 'force': True}") == {"path": "a.py", "force": True}
    try:
        _lenient_json("not json at all")
        raise AssertionError("should not read")
    except ValueError:
        pass


def test_other_agents_tool_and_argument_names_run_the_right_tool():
    reg = ToolRegistry(auto_approve=True)
    got = {}
    reg.register(Tool(name="read", description="", parameters={"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "string"}}}, func=lambda path, offset=1: f"read {path} {offset}"))
    reg.register(Tool(name="edit", description="", parameters={"type": "object", "properties": {
        "path": {}, "old_text": {}, "new_text": {}}},
        func=lambda path, old_text, new_text: got.update(p=path, o=old_text, n=new_text) or "Edited."))
    reg.register(Tool(name="bash", description="", parameters={"type": "object", "properties": {"cmd": {}}},
                      func=lambda cmd: f"ran {cmd}"))
    assert reg.execute("read_file", {"file_path": "a.py"}) == "read a.py 1"
    assert reg.execute("functions.read", {"path": "a.py", "start_line": 5}) == "read a.py 5"
    assert reg.execute("str_replace", {"path": "a.py", "old_string": "x", "new_string": "y"}) == "Edited."
    assert got == {"p": "a.py", "o": "x", "n": "y"}
    assert reg.execute("run_command", {"command": "ls"}) == "ran ls"
    assert reg.execute("Bash", {"arguments": {"cmd": "pwd"}}) == "ran pwd"
    assert reg.execute("teleport", {}).startswith("ERROR: unknown tool")


def test_an_edit_that_does_not_apply_shows_the_closest_lines():
    with tempfile.TemporaryDirectory() as tmp:
        body = "".join(f"line {i}\n" for i in range(1, 40)) + "def total(items):\n    return sum(items)\n"
        open(os.path.join(tmp, "m.py"), "w").write(body)
        out = _make_edit(tmp, None)("m.py", "def total(things):\n    return sum(thing)\n", "x")
        assert out.startswith("ERROR: SEARCH block not found") and "40\tdef total(items):" in out, out
        assert _make_edit(tmp, None)("m.py", "", "x").startswith("ERROR: old_text is empty")
        assert "Nothing in the file looks like" in _closest(body, "zzzzqqqq")


def test_a_file_that_no_longer_parses_says_so_at_once():
    with tempfile.TemporaryDirectory() as tmp:
        write = _make_write_file(tmp, None)
        assert "CHECK FAILED" not in write("ok.py", "x = 1\n")
        out = write("bad.py", "def f(:\n    pass\n")
        assert "CHECK FAILED" in out and "line 1" in out, out
        assert "CHECK FAILED" in write("bad.json", '{"a": 1,}')
        assert "CHECK FAILED" not in write("notes.txt", "def f(:")
        out = _make_edit(tmp, None)("ok.py", "x = 1", "x = (1")
        assert out.startswith("Edited") and "CHECK FAILED" in out, out
        if checks.shutil.which("node"):
            assert "CHECK FAILED" in write("bad.js", "function f( {\n")
            assert "CHECK FAILED" not in write("good.js", "function f() { return 1; }\n")


def test_long_output_keeps_its_end():
    text = "\n".join(f"compiling unit {i}" for i in range(2000)) + "\nerror: missing ; at main.c:42\nFAILED"
    out = _truncate(text, 3000)
    assert len(out) < 3200 and out.endswith("FAILED") and "main.c:42" in out and out.startswith("compiling unit 0")
    assert _truncate("short") == "short"


def test_a_file_read_again_unchanged_is_not_sent_twice():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.py")
        open(path, "w").write("x = 1\n")
        read = _make_read(tmp)
        assert "x = 1" in read("a.py")
        assert read("a.py").startswith("(unchanged:")
        assert "x = 1" in read("a.py"), "asked again right after the note: sent in full"
        assert "x = 1" in read("a.py", 1, 50), "other lines: sent"
        open(path, "w").write("x = 2  # changed\n")
        assert "x = 2" in read("a.py"), "changed since: sent"


def test_a_bigger_request_to_a_local_model_asks_for_a_checklist():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers["here"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "", "models": ["m"]}
            app.config.providers["away"] = {"base_url": "https://api.example.com/v1", "api_key_env": "", "models": ["m"]}
            s = app.create_session(tmp)
            big = ("Build a snake game with a board, a score, levels that speed up, a high score table saved "
                   "to a file, sound effects, and a pause menu, and write tests for the scoring and the levels.")
            s.settings["provider"] = "here"
            assert s._wants_checklist(big)
            assert not s._wants_checklist("What does this function do?")
            s.settings["provider"] = "away"
            assert not s._wants_checklist(big), "a hosted model plans well enough on its own"
            s.settings["provider"] = "here"
            s.todo_state.write([{"title": "board", "status": "in_progress"}])
            assert not s._wants_checklist(big), "a checklist is already going"
            s.close()
        finally:
            server.shutdown()


def test_a_local_model_gets_a_smaller_project_pack():
    from test_webui import start_server
    from webui.app import LEAN_PACK_CHARS

    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "big")
        os.makedirs(proj)
        for i in range(150):
            with open(os.path.join(proj, f"module_{i:03d}.py"), "w") as fh:
                fh.write(f'"""Module number {i}, which does a fairly long list of useful things for the app."""\n'
                         + "".join(f"def function_{i}_{k}(x):\n    return x\n" for k in range(8)))
        app, server, base = start_server(tmp)
        try:
            s = app.create_session(proj)   # the test provider is on this computer
            assert s.lean() and s.loop.pack_prefix and len(s.loop.pack_prefix) <= LEAN_PACK_CHARS
            assert "module_000.py" in s.loop.pack_prefix
            s.close()
        finally:
            server.shutdown()


def test_a_reply_sent_back_carries_on_from_the_whole_turn():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            s = app.create_session(tmp)
            s.history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "done before"}]
            assert s._turn_so_far("fix it", "Fixed.")[-2:] == [{"role": "user", "content": "fix it"},
                                                               {"role": "assistant", "content": "Fixed."}]
            turn = [{"role": "system", "content": "sys"}] + s.history + [
                {"role": "user", "content": "fix it"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "edit"}}]},
                {"role": "tool", "tool_call_id": "1", "content": "Edited."},
                {"role": "assistant", "content": "Fixed.", "tool_calls": None}]
            s.loop.last_messages = turn
            kept = s._turn_so_far("fix it", "Fixed.")
            assert kept == turn[1:], "the tool steps stay, nothing is rewritten, the reply is not added twice"
            s.close()
        finally:
            server.shutdown()


def test_tool_calls_written_into_the_answer_are_run():
    from agent import text_tool_calls

    said = ("Saving that.\n<tool_call>\n<function=memory_save>\n<parameter=content>\nsettings.py parses INI\n"
            "</parameter>\n<parameter=limit>\n5\n</parameter>\n</function>\n</tool_call>")
    rest, calls = text_tool_calls(said)
    assert rest == "Saving that." and len(calls) == 1
    import json
    assert calls[0]["function"]["name"] == "memory_save"
    assert json.loads(calls[0]["function"]["arguments"]) == {"content": "settings.py parses INI", "limit": 5}
    rest, calls = text_tool_calls('<tool_call>{"name": "read", "arguments": {"path": "a.py"}}</tool_call>')
    assert rest == "" and json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}
    assert text_tool_calls("plain answer") == ("plain answer", [])


def test_the_loop_runs_a_tool_call_written_as_text():
    from agent import AgentLoop
    from test_smoke import FakeClient, make_config, text_msg

    with tempfile.TemporaryDirectory() as tmp:
        ran = []
        reg = ToolRegistry(auto_approve=True)
        reg.register(Tool(name="memory_save", description="", parameters={"type": "object", "properties": {
            "content": {"type": "string"}}}, func=lambda content: ran.append(content) or "Saved."))
        written = text_msg("<tool_call>\n<function=memory_save>\n<parameter=content>\nnote\n</parameter>\n"
                           "</function>\n</tool_call>")
        client = FakeClient([written, text_msg("All done.")])
        reply = AgentLoop(config=make_config(tmp), registry=reg, client=client, skills=[]).run("go")
        assert ran == ["note"] and reply == "All done."
