"""The three things that keep a slow local model from wasting its turn: the brief, thinking that
rises only with trouble, and moving old output out in batches."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui import brief, offload, workingset  # noqa: E402
from webui.epistemic import Ledger  # noqa: E402


def _project(tmp_path):
    (tmp_path / "shapes.py").write_text(
        "SIDES = {'square': 4}\n\n\n"
        "def area(width, height):\n    \"\"\"The area of a rectangle.\"\"\"\n    raise NotImplementedError\n\n\n"
        "def square(side):\n    return area(side, side)\n", encoding="utf-8")
    (tmp_path / "test_shapes.py").write_text(
        "import pytest\nimport shapes\n\nCASES = (\n    (2, 3, 6),\n    (4, 5, 20),\n)\n\n\n"
        "@pytest.mark.parametrize(('w', 'h', 'want'), CASES)\ndef test_area(w, h, want):\n"
        "    assert shapes.area(w, h) == want\n\n\ndef test_other(area):\n    assert area\n", encoding="utf-8")
    return types.SimpleNamespace(project_dir=str(tmp_path))


def test_brief_brings_the_definition_its_callers_and_its_tests(tmp_path):
    session = _project(tmp_path)
    text, files = brief.brief(session, "The function area in shapes.py is not implemented yet. Implement it.")
    assert files == ["shapes.py"]
    assert "area is defined in shapes.py, lines 4-6" in text and "raise NotImplementedError" in text
    assert "shapes.py:10 in square" in text                       # who calls it
    assert "test_shapes.py: test_area" in text and "(4, 5, 20)" in text   # its test and the cases it runs over
    assert "test_other" not in text                                # a parameter of the same name is not a use
    assert "SIDES" in text                                         # what the file defines beside it
    assert len(text) <= brief.MAX_CHARS + 400


def test_brief_says_nothing_when_no_code_is_named(tmp_path):
    session = _project(tmp_path)
    assert brief.brief(session, "What is the area of this project, roughly? Is it square?") == ("", [])
    assert brief.brief(session, "Please fix `no_such_thing` for me") == ("", [])


def _session(effort="medium"):
    ledger = Ledger(os.getcwd())
    ledger.start_turn()
    return types.SimpleNamespace(settings={"effort": effort}, ledger=ledger, codegate=types.SimpleNamespace(refused=0))


def test_thinking_is_off_until_there_is_trouble():
    session = _session("high")
    assert workingset.thinking_level(session, 0) == "off"
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one - assert 1 == 2")
    assert workingset.thinking_level(session, 0) == "medium"     # the tests fail on the code as it is
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one - assert 1 == 2")
    assert workingset.thinking_level(session, 0) == "high"       # twice running
    session.ledger.note_run("python -m pytest -q", "[exit 0]\n3 passed")
    assert workingset.thinking_level(session, 0) == "off"        # and back down once they pass


def test_thinking_never_passes_the_effort_you_set():
    session = _session("medium")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one")
    assert workingset.thinking_level(session, 0) == "off"        # failing before any change: that is the task
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one")
    assert workingset.thinking_level(session, 0) == "medium"
    calm = _session("medium")
    calm.codegate.refused = 1
    assert workingset.thinking_level(calm, 0) == "medium"        # a refused change is trouble too
    assert workingset.thinking_level(calm, 1) == "off"           # but only the round after it


def test_old_output_is_moved_out_in_one_batch_then_left_alone():
    """Once the window is over the line, one request clears well below it; the next rounds change
    nothing that was already sent, so the server's cache of the conversation holds."""
    loader = offload.Offloader(8192, per_token=4.0)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    calls = {}

    def add(n, lines=400):
        cid = f"c{n}"
        calls[cid] = {"name": "read", "args": {"path": f"f{n}.py"}}
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": cid, "content": f"file {n}\n" + "x = 1\n" * lines})

    for n in range(8):
        add(n)
    first = loader.apply(messages, calls, set())
    assert loader.tier == "mild"
    assert loader.last["tokens"] < loader.usable * offload.MILD        # cleared with room to spare
    views = [first]
    for n in range(8, 11):                                             # three more rounds
        add(n, 120)
        views.append(loader.apply(messages, calls, set()))
    for before, after in zip(views, views[1:]):
        assert after[:len(before)] == before, "an earlier message changed: the cache would be lost"


def test_thinking_is_set_on_the_client_that_sends_not_on_a_wrapper():
    from context.compactor import ContextManagedClient
    from webui.failover import ResilientClient

    real = types.SimpleNamespace(params={"temperature": 1.0, "thinking_budget_tokens": 2048})
    wrapped = ResilientClient(ContextManagedClient(real), "local/m")
    session = _session("medium")
    session.settings["model"] = "bonsai2-27b-pq2"
    session.app = types.SimpleNamespace(settings={})
    session.context_window = lambda: 32768
    grounded = workingset.GroundedClient(wrapped, session, "task")
    grounded._set_thinking()
    assert real.params["reasoning_effort"] == "none" and real.params["thinking_budget_tokens"] is None, real.params
    assert real.params["temperature"] == 1.0


def test_a_command_that_changes_into_its_own_folder_is_just_the_command():
    from procutil import strip_own_cd

    here = "C:\\Users\\me\\AppData\\Local\\Temp\\work" if os.name == "nt" else "/tmp/work"
    unix = "/c/Users/me/AppData/Local/Temp/work" if os.name == "nt" else "/tmp/work"
    assert strip_own_cd(f"cd {here} && python -m pytest -q", here) == "python -m pytest -q"
    assert strip_own_cd(f'cd "{unix}" && python -m pytest -q', here) == "python -m pytest -q"
    assert strip_own_cd("cd . && pytest", here) == "pytest"
    assert strip_own_cd("cd /tmp && ls", here) == "cd /tmp && ls"
    assert strip_own_cd("python -m pytest -q", here) == "python -m pytest -q"


def test_the_standing_note_says_what_each_failure_said(tmp_path):
    session = _session("medium")
    session.project_dir = str(tmp_path)
    session.loop = types.SimpleNamespace(config=types.SimpleNamespace(max_iterations=30))
    session.todo_state = types.SimpleNamespace(items=[])
    session.codegate = types.SimpleNamespace(refused=0, broken=set())
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\n..F\n=== short test summary info ===\n"
                            "FAILED test_a.py::test_u[HTMLTidy-html_tidy] - AssertionError: assert 'html_tidy' == 'htmltidy'\n"
                            "1 failed, 2 passed")
    note = workingset.standing(session, "do it", 3)
    assert "What failed (from that run):" in note and "assert 'html_tidy' == 'htmltidy'" in note, note


def test_what_was_sent_is_never_rewritten_from_round_to_round(tmp_path):
    """Each request is the one before it plus what was added: a note stays where it was written,
    and a stale reading is named in the next note instead of being taken out of an old message."""
    import json

    session = _session("medium")
    session.project_dir = str(tmp_path)
    session.loop = types.SimpleNamespace(config=types.SimpleNamespace(max_iterations=30))
    session.todo_state = types.SimpleNamespace(items=[])
    session.codegate = types.SimpleNamespace(refused=0, broken=set())
    session.app = types.SimpleNamespace(settings={"adaptive_thinking": False})
    session.context_window = lambda: 200_000
    session._chars_per_token = 0.0
    sent = []

    class Echo:
        def chat(self, messages, tools):
            sent.append([dict(m) for m in messages])
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    grounded = workingset.GroundedClient(Echo(), session, "fix a.py")
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "fix a.py"}]

    def step(n, name, args, result):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": result})
        session.ledger.note_tool(name, args, result)
        grounded.chat(messages, [])

    grounded.chat(messages, [])
    step(1, "read", {"path": "a.py"}, "x = 1")
    step(2, "read", {"path": "b.py"}, "y = 2")
    step(3, "edit", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, "Edited a.py")
    step(4, "read", {"path": "b.py"}, "y = 2")
    step(5, "read", {"path": "c.py"}, "z = 3")
    for before, after in zip(sent, sent[1:]):
        assert after[:len(before)] == before, "an earlier message was rewritten"
    text = "\n".join(str(m.get("content")) for m in sent[-1])
    assert "x = 1" in text, "the old reading is left where it is while there is room"
    assert "Changed since you last read it: a.py" in text
    assert text.count("[Where things stand") == 2, "one when work began, one when the file changed; none for a plain read"


def test_a_refused_command_is_not_a_test_run_and_a_guessed_folder_is_dropped():
    from procutil import strip_own_cd

    assert strip_own_cd("cd /workspace/project && python -m pytest -q", os.getcwd()) == "python -m pytest -q"
    assert strip_own_cd("cd /tmp && ls", os.getcwd()) == "cd /tmp && ls"
    assert strip_own_cd("cd src && ls", os.getcwd()) == "cd src && ls"
    session = _session("medium")
    session.ledger.note_tool("bash", {"cmd": "python -m pytest -q"}, "ERROR: the user denied approval for tool 'bash'.")
    assert session.ledger.runs == [], "nothing ran, so there is no test result"
    session.ledger.note_tool("bash", {"cmd": "python -m pytest -q"}, "[exit 0]\n3 passed")
    assert len(session.ledger.runs) == 1 and session.ledger.runs[0]["exit"] == 0


def test_when_only_part_of_the_tests_were_run_the_whole_suite_is(tmp_path):
    from webui.codegate import CodeGate

    (tmp_path / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    (tmp_path / "test_b.py").write_text("def test_b():\n    assert 1 == 2\n", encoding="utf-8")
    session = _session("medium")
    session.project_dir = str(tmp_path)
    session.app = types.SimpleNamespace(settings={})
    gate = CodeGate(session)
    assert gate.whole_suite() is None, "nothing was changed: nothing to check"
    # The interpreter running this test is the one that has pytest, and the widened run really
    # runs it: a bare `python` would be whatever the machine calls python, which may not.
    py = sys.executable.replace(os.sep, "/")
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_tool("bash", {"cmd": f"{py} -m pytest test_a.py -q"}, "[exit 0]\n1 passed")
    found = gate.whole_suite()
    assert found and found["cmd"] == f"{py} -m pytest -q" and "test_a.py" in found["narrow"], found
    assert any("test_b.py::test_b" in line for line in found["failed"]), found
    assert session.ledger.runs[-1]["gate"] and session.ledger.runs[-1]["exit"] == 1
    # the whole suite was what was run: there is nothing to widen
    session.ledger.note_tool("bash", {"cmd": "python -m pytest -q"}, "[exit 0]\n2 passed")
    assert gate.whole_suite() is None


def test_a_widened_run_that_never_reached_pytest_is_not_reported(tmp_path):
    """The widened command is the model's own interpreter line, and that interpreter may not have
    pytest. A run that never got as far as the tests says nothing about the suite."""
    import procutil
    from webui.codegate import CodeGate

    (tmp_path / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    session = _session("medium")
    session.project_dir = str(tmp_path)
    session.app = types.SimpleNamespace(settings={})
    gate = CodeGate(session)
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_tool("bash", {"cmd": "python -m pytest test_a.py -q"}, "[exit 0]\n1 passed")
    real = procutil.run_shell
    try:
        # it fails, but only because pytest is not there at all: nothing was learned
        procutil.run_shell = lambda *a, **k: procutil.Done(1, "", "No module named pytest", False)
        assert gate.whole_suite() is None, "not a failing suite: it never started"
        # a run that did reach the tests is still reported
        session.ledger.runs.pop()
        procutil.run_shell = lambda *a, **k: procutil.Done(
            1, "= FAILURES =\nFAILED test_b.py::test_b - assert 1 == 2", "", False)
        found = gate.whole_suite()
        assert found and any("test_b.py::test_b" in line for line in found["failed"]), found
    finally:
        procutil.run_shell = real


def test_thinking_is_spent_on_a_change_to_code_not_on_looking(tmp_path):
    """After a failed test the model is still asked without thinking; only when it comes back with
    a change to code is the same request asked again with thinking."""
    import json

    session = _session("medium")
    session.settings["model"] = "bonsai2-27b-pq2"
    session.project_dir = str(tmp_path)
    session.loop = types.SimpleNamespace(config=types.SimpleNamespace(max_iterations=30))
    session.todo_state = types.SimpleNamespace(items=[])
    session.codegate = types.SimpleNamespace(refused=0, broken=set())
    session.app = types.SimpleNamespace(settings={})
    session.context_window = lambda: 200_000
    session._chars_per_token = 0.0
    asked = []

    def call(name, **args):
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}

    class Model:
        def __init__(self):
            self.params = {"temperature": 1.0}
            self.answers = []

        def chat(self, messages, tools):
            asked.append(self.params.get("reasoning_effort"))
            return self.answers.pop(0)

    model = Model()
    grounded = workingset.GroundedClient(model, session, "fix a.py")
    session.ledger.note_tool("edit", {"path": "a.py", "new_text": "x"}, "ok")
    session.ledger.note_run("python -m pytest -q", "[exit 1]\nFAILED test_a.py::test_one - assert 1 == 2")
    model.answers = [call("read", path="a.py")]
    grounded.chat([{"role": "user", "content": "go"}], [])
    assert asked == ["none"], "a look at a file is not thought about"
    model.answers = [call("edit", path="a.py", old_text="x", new_text="y"), call("edit", path="a.py", old_text="x", new_text="z")]
    reply = grounded.chat([{"role": "user", "content": "go"}], [])
    assert asked == ["none", "none", "medium"], asked
    assert '"z"' in reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "the thought-about change is the one used"
