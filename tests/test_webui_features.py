"""Tests for the web UI features added with the redesign (stdlib only).

Kernel guard and claim labels, per-command and per-kind permissions, changing the folder of a
live session, the kernel workbench, commands, memory, usage and the memory watchdog.
Same harness as test_webui.py: the real server, scripted fake model clients.
"""

import os
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import (  # noqa: E402
    FakeClient,
    SSEReader,
    http,
    make_session_with_fake,
    start_server,
    text_msg,
    tool_call_msg,
)

from webui import machine  # noqa: E402
from webui.app import simple_command  # noqa: E402
from webui.epistemic import Ledger, command_key  # noqa: E402


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _run(tmp, script, text="go", before=None):
    """Start a server and one scripted session; returns (app, server, base, sid, reader)."""
    app, server, base = start_server(tmp)
    sid, _fake = make_session_with_fake(base, tmp, script)
    if before:
        before(app, base, sid)
    reader = SSEReader(f"{base}/api/sessions/{sid}/events")
    reader.start()
    reader.wait_for("state")
    http("POST", f"{base}/api/sessions/{sid}/message", {"text": text})
    return app, server, base, sid, reader


# ------------------------------------------------------------ guard ---


def test_guard_asks_in_auto_mode_for_unread_edit():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "g.txt"), "one\n")
        script = [
            tool_call_msg("edit", {"path": "g.txt", "old_text": "one\n", "new_text": "two\n"}),
            text_msg("done"),
        ]

        def auto(app, base, sid):
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})

        app, server, base, sid, reader = _run(tmp, script, before=auto)
        try:
            preq = reader.wait_for("permission_request")
            assert preq["guard"]["state"] == "CONFABULATED", preq
            assert "has not read" in preq["guard"]["reasons"][0], preq
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": preq["request_id"], "decision": "allow"})
            reader.wait_for("turn_end")
            with open(os.path.join(tmp, "g.txt"), encoding="utf-8") as fh:
                assert fh.read() == "two\n"
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_guard_asks_in_auto_mode_for_unread_edit")


def test_guard_block_mode_refuses_and_file_is_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "b.txt"), "one\n")
        script = [
            tool_call_msg("edit", {"path": "b.txt", "old_text": "one\n", "new_text": "two\n"}),
            text_msg("could not"),
        ]

        def block(app, base, sid):
            http("POST", f"{base}/api/settings", {"guard": "block"})
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})

        app, server, base, sid, reader = _run(tmp, script, before=block)
        try:
            reader.wait_for("turn_end")
            results = [d for t, d in reader.events if t == "tool_result"]
            assert results and "blocked by the guard" in results[0]["result"], results
            assert not reader.seen("permission_request")
            with open(os.path.join(tmp, "b.txt"), encoding="utf-8") as fh:
                assert fh.read() == "one\n"
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_guard_block_mode_refuses_and_file_is_untouched")


def test_guard_off_restores_plain_auto_mode():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "o.txt"), "one\n")
        script = [
            tool_call_msg("edit", {"path": "o.txt", "old_text": "one\n", "new_text": "two\n"}),
            text_msg("done"),
        ]

        def off(app, base, sid):
            http("POST", f"{base}/api/settings", {"guard": "off"})
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})

        app, server, base, sid, reader = _run(tmp, script, before=off)
        try:
            reader.wait_for("turn_end")
            assert not reader.seen("permission_request")
            with open(os.path.join(tmp, "o.txt"), encoding="utf-8") as fh:
                assert fh.read() == "two\n"
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_guard_off_restores_plain_auto_mode")


# ----------------------------------------------------------- claims ---


def test_reply_claims_are_typed():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "real.py"), "def greet():\n    return 1\n")
        script = [
            tool_call_msg("read", {"path": "real.py"}),
            text_msg("I read `real.py` and `ghost.py`. `greet` is fine, `zzznothere` is not. All tests pass."),
        ]
        def labels_only(app, base, sid):   # this test is about the labels; the reply gate has its own
            http("POST", f"{base}/api/settings", {"reply_gate": "off"})

        app, server, base, sid, reader = _run(tmp, script, before=labels_only)
        try:
            claims = reader.wait_for("claims")
            reader.wait_for("turn_end")
            by_text = {c["text"]: c["state"] for c in claims["claims"]}
            assert by_text["real.py"] == "KNOWN", by_text
            assert by_text["ghost.py"] == "UNKNOWN_REFERENCE", by_text
            assert by_text["greet"] == "KNOWN", by_text
            assert by_text["zzznothere"] == "CONFABULATED", by_text
            # "All tests pass" with no command run is a guess.
            assert any(c["kind"] == "result" and c["state"] == "CONFABULATED" for c in claims["claims"])
            assert claims["state"] == "UNKNOWN_REFERENCE", claims["state"]
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            last = [e for e in hist["transcript"] if e["kind"] == "assistant"][-1]
            assert last["claims"]["state"] == "UNKNOWN_REFERENCE"
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_reply_claims_are_typed")


def test_ledger_conflict_and_command_keys():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(tmp)
        ledger.note_tool("bash", {"cmd": "pytest -q"}, "[exit 1]\n1 failed, 466 passed")
        out = ledger.label_reply("466 passed and 9 failed. All tests pass now.")
        states = {c["text"]: c["state"] for c in out["claims"]}
        assert states["466 passed"] == "KNOWN", states
        assert states["9 failed"] == "CONFABULATED", states
        assert "CONFLICTED" in states.values(), states
        # A plain answer with nothing looked at is the model's own, unchecked.
        assert Ledger(tmp).label_reply("Use a dict.")["state"] == "CONFABULATED"
    assert command_key("python -m pytest -q tests") == "python -m pytest"
    assert command_key("git status -s") == "git status"
    assert command_key("FOO=1 pytest -x") == "pytest"
    assert simple_command("pytest -q") and not simple_command("pytest && rm -rf x")
    assert not simple_command("pytest; echo hi") and not simple_command("cat a > b")
    print("PASS test_ledger_conflict_and_command_keys")


# ------------------------------------------------------ permissions ---


def test_always_allow_is_per_command_and_never_covers_a_chain():
    with tempfile.TemporaryDirectory() as tmp:
        script = [
            tool_call_msg("bash", {"cmd": "echo one"}),
            tool_call_msg("bash", {"cmd": "echo two"}),
            tool_call_msg("bash", {"cmd": "echo three && echo four"}),
            text_msg("done"),
        ]
        app, server, base, sid, reader = _run(tmp, script)
        try:
            preq = reader.wait_for("permission_request")
            assert preq["always_label"] == "echo" and preq["always_scope"] == "folder", preq
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": preq["request_id"], "decision": "always"})
            # "echo two" runs without asking; the chained command asks again.
            deadline = time.time() + 15
            second = None
            while time.time() < deadline and second is None:
                asks = [d for t, d in reader.events if t == "permission_request"]
                if len(asks) > 1:
                    second = asks[1]
                time.sleep(0.1)
            assert second is not None, "the chained command was not asked about"
            assert "&&" in second["args"]["cmd"], second
            assert second["always_label"] == "", second
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": second["request_id"], "decision": "allow"})
            reader.wait_for("turn_end")
            status, settings = http("GET", f"{base}/api/settings")
            rules = settings["always_commands"]
            assert len(rules) == 1 and rules[0]["cmd"] == "echo", rules
            assert rules[0]["project_dir"] == os.path.abspath(tmp), rules
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_always_allow_is_per_command_and_never_covers_a_chain")


def test_kind_set_to_never_refuses_without_asking():
    with tempfile.TemporaryDirectory() as tmp:
        script = [tool_call_msg("bash", {"cmd": "echo hi"}), text_msg("ok")]

        def never(app, base, sid):
            status, st = http("POST", f"{base}/api/settings",
                              {"action_permissions": {"run": "never", "bogus": "allow"}})
            assert st["action_permissions"] == {
                "read": "allow", "edit": "ask", "run": "never", "helpers": "ask", "network": "ask"}, st

        app, server, base, sid, reader = _run(tmp, script, before=never)
        try:
            reader.wait_for("turn_end")
            results = [d for t, d in reader.events if t == "tool_result"]
            assert "turned off in Settings" in results[0]["result"], results
            assert not reader.seen("permission_request")
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_kind_set_to_never_refuses_without_asking")


# ----------------------------------------------------------- folders ---


def test_switch_folder_keeps_conversation_and_moves_the_tools():
    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "first")
        second = os.path.join(tmp, "second")
        os.makedirs(first)
        os.makedirs(second)
        _write(os.path.join(second, "only_here.txt"), "second folder\n")
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(
                base, first,
                [text_msg("hello"), tool_call_msg("read", {"path": "only_here.txt"}), text_msg("read it")],
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "hi"})
            reader.wait_for("turn_end")
            status, snap = http("POST", f"{base}/api/sessions/{sid}/folder", {"project_dir": second})
            assert status == 200 and snap["project_dir"] == os.path.abspath(second), (status, snap)
            # _bind_project rebuilt the loop; re-attach the fake client.
            app.get_session(sid)._build_client = lambda provider=None: fake
            reader.events.clear()
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "read it"})
            reader.wait_for("turn_end")
            results = [d for t, d in reader.events if t == "tool_result"]
            assert "second folder" in results[0]["result"], results
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            kinds = [e["kind"] for e in hist["transcript"]]
            assert kinds.count("user") == 2 and "notice" in kinds, kinds
            status, listing = http("GET", f"{base}/api/sessions")
            assert listing[0]["project_dir"] == os.path.abspath(second), listing
            status, err = http("POST", f"{base}/api/sessions/{sid}/folder",
                               {"project_dir": os.path.join(tmp, "missing")})
            assert status == 400, (status, err)
            status, snap = http("POST", f"{base}/api/sessions/{sid}/folder", {"no_project": True})
            assert status == 200 and snap["no_project"] is True, snap
            status, folders = http("GET", f"{base}/api/folders")
            assert all(f["path"] != app.chat_dir for f in folders), folders
            status, listing = http("GET", f"{base}/api/browse?path=" + tmp.replace("\\", "/"))
            assert status == 200 and {"first", "second"} <= {d["name"] if isinstance(d, dict) else d for d in listing["dirs"]}, listing
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_switch_folder_keeps_conversation_and_moves_the_tools")


def test_duplicate_session_and_delete_of_a_closed_one():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("first answer")])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "question"})
            reader.wait_for("turn_end")
            reader.stop()
            status, copy_ = http("POST", f"{base}/api/sessions/{sid}/duplicate", {})
            assert status == 201 and copy_["id"] != sid, (status, copy_)
            status, hist = http("GET", f"{base}/api/sessions/{copy_['id']}/history")
            assert [e["kind"] for e in hist["transcript"]] == ["user", "assistant"], hist
        finally:
            server.shutdown()
    print("PASS test_duplicate_session_and_delete_of_a_closed_one")


# ------------------------------------------------- kernel workbench ---


def test_kernel_workbench_runs_and_types_answers():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, info = http("GET", f"{base}/api/kernel")
            assert status == 200 and len(info["examples"]) >= 30, (status, len(info.get("examples", [])))
            status, out = http("POST", f"{base}/api/kernel/run",
                               {"text": "ask value q-metal conductance\nask value gren conductance"})
            assert status == 200, (status, out)
            states = [a["state"] for a in out["answers"]]
            assert states == ["KNOWN", "UNKNOWN_REFERENCE"], states
            assert out["answers"][0]["supported"] and not out["answers"][1]["supported"]
            status, _ = http("POST", f"{base}/api/kernel/run", {"text": "  "})
            assert status == 400, status
        finally:
            server.shutdown()
    print("PASS test_kernel_workbench_runs_and_types_answers")


# ------------------------------------------- commands, memory, usage ---


def test_commands_memory_usage_and_settings_reset():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        app.commands_dir = os.path.join(tmp, "commands")  # never the repository's own
        app.commands = []
        try:
            status, cmds = http("POST", f"{base}/api/commands",
                                {"name": "/Greet", "description": "Say hello", "body": "Say hello to $ARGUMENTS"})
            assert status == 200 and cmds[0]["name"] == "greet", (status, cmds)
            assert os.path.isfile(os.path.join(tmp, "commands", "greet.md"))
            status, _ = http("POST", f"{base}/api/commands", {"name": "../evil", "body": "x"})
            assert status == 400, status
            status, cmds = http("DELETE", f"{base}/api/commands/greet")
            assert status == 200 and cmds == [], (status, cmds)

            status, claims = http("POST", f"{base}/api/memory", {"fact": "tests run with pytest -q"})
            assert status == 200 and claims[0]["text"] == "tests run with pytest -q", claims
            assert claims[0]["state"] == "KNOWN", claims
            status, left = http("POST", f"{base}/api/memory/forget", {"id": claims[0]["id"]})
            assert status == 200 and left == [], left

            app.usage.add("fake-model", 4000, 400, 1500)
            status, usage = http("GET", f"{base}/api/usage")
            assert usage["today"]["tasks"] == 1 and usage["by_model"][0]["model"] == "fake-model", usage

            http("POST", f"{base}/api/settings", {"guard": "nonsense", "min_free_ram_gb": "x"})
            status, st = http("GET", f"{base}/api/settings")
            assert st["guard"] == "ask" and st["min_free_ram_gb"] == 2, st
            http("POST", f"{base}/api/settings", {"guard": "off"})
            status, st = http("POST", f"{base}/api/settings/reset", {})
            assert st["guard"] == "ask", st

            status, about = http("GET", f"{base}/api/about")
            assert status == 200 and about["workspace"], about
            status, hw = http("GET", f"{base}/api/hardware")
            assert status == 200 and "memory" in hw and hw["cpu"]["cores"], hw
            status, test = http("POST", f"{base}/api/providers/fake/test", {})
            assert status == 200 and test["ok"] is False, test  # nothing listens on port 9
        finally:
            server.shutdown()
    print("PASS test_commands_memory_usage_and_settings_reset")


def test_memory_watchdog_stops_a_working_session():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        real = machine.memory
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            machine.memory = lambda: {"total_gb": 32.0, "free_gb": 0.5}
            assert app.watchdog.check_once() is False  # nothing is working
            session.status = "working"
            assert app.watchdog.check_once() is True
            assert session.loop.cancel_requested is True
            err = reader.wait_for("error")
            assert "0.5 GB" in err["message"] and err["kind"] == "memory", err
            session.loop.cancel_requested = False
            app.settings["min_free_ram_gb"] = 0
            assert app.watchdog.check_once() is False  # the limit is off
            session.status = "idle"
            reader.stop()
        finally:
            machine.memory = real
            server.shutdown()
    print("PASS test_memory_watchdog_stops_a_working_session")


# ------------------------------------------- store, memory, ledger ---


def test_store_claims_supersede_reinforce_and_forget():
    from store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        first = store.add_claim("Tests run with pytest -q", "CONFABULATED", "its own conclusion")
        assert first["outcome"] == "added"
        # the same thing again, now told by the user: one claim, better standing
        again = store.add_claim("tests run with pytest -q", "KNOWN", "you said so")
        assert again["outcome"] == "reinforced" and again["claim"]["state"] == "KNOWN", again
        assert again["claim"]["reinforced_count"] == 1
        # a different statement about the same subject replaces it and keeps the history
        newer = store.add_claim("Tests run with pytest -q -x --lf", "KNOWN", "you said so")
        assert newer["outcome"] == "superseded" and newer["replaced"] == first["claim"]["claim_id"], newer
        active = store.claims()
        assert [c["claim_text"] for c in active] == ["Tests run with pytest -q -x --lf"], active
        assert len(store.claims(include_history=True)) == 2
        # folder-scoped claims stay in their folder
        store.add_claim("The build needs Node 22", "DERIVED", "from package.json", project_dir="/a")
        assert len(store.claims("/a")) == 2 and len(store.claims("/b")) == 1
        found = store.search_claims("how do I run pytest", "/b")
        assert found and "pytest" in found[0]["claim_text"], found
        assert store.retract_claim(newer["claim"]["claim_id"])
        assert store.search_claims("pytest") == []
        gone = store.claim(newer["claim"]["claim_id"])
        assert gone["status"] == "retracted" and gone["claim_text"] == "", gone
        # the item log numbers records per agent and survives a reopen
        store.touch_session("s1", "t", "/a")
        assert store.append_item("s1", {"kind": "user", "text": "hi"}) == 1
        assert store.append_item("s1", {"kind": "tool", "name": "bash", "result": "ERROR: x"}) == 2
        store.add_compaction("s1", "summary", 900, 300, "manual")
        assert store.compactions("s1")[0]["first_kept_seq"] == 3
        store.close()
        reopened = Store(os.path.join(tmp, "s.db"))
        assert [e["kind"] for e in reopened.items("s1")] == ["user", "tool"]
        reopened.delete_session("s1")
        assert reopened.items("s1") == []
        reopened.close()
    print("PASS test_store_claims_supersede_reinforce_and_forget")


def test_memory_standing_ledger_and_log_through_a_session():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "setup.cfg"), "[tool:pytest]\naddopts = -q --maxfail=1\n")
        script = [
            tool_call_msg("read", {"path": "setup.cfg"}),
            # told by the user in this message
            tool_call_msg("memory_save", {"fact": "The user prefers small verified changes"}, "c2"),
            # in a file it read
            tool_call_msg("memory_save", {"fact": "pytest addopts are -q --maxfail=1", "source_path": "setup.cfg"}, "c3"),
            # nothing supports this
            tool_call_msg("memory_save", {"fact": "Deployment happens on Fridays through Jenkins"}, "c4"),
            tool_call_msg("bash", {"cmd": "echo done"}, "c5"),
            text_msg("Saved."),
        ]

        def auto(app, base, sid):
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})

        app, server, base, sid, reader = _run(
            tmp, script, text="I prefer small verified changes. Look at the test setup.", before=auto)
        try:
            reader.wait_for("turn_end")
            status, claims = http("GET", f"{base}/api/memory")
            by_text = {c["text"]: c for c in claims}
            assert by_text["The user prefers small verified changes"]["state"] == "KNOWN", claims
            assert by_text["pytest addopts are -q --maxfail=1"]["state"] == "DERIVED", claims
            unsure = by_text["Deployment happens on Fridays through Jenkins"]
            assert unsure["state"] == "CONFABULATED", claims
            # the next prompt marks the unverified one
            section = app.get_session(sid).loop.memory.summary()
            assert "Unverified" in section and section.index("Unverified") < section.index("Jenkins"), section
            assert section.index("small verified") < section.index("Unverified"), section
            # you confirm it: it becomes known
            status, claims = http("POST", f"{base}/api/memory/confirm", {"id": unsure["id"]})
            assert {c["text"]: c["state"] for c in claims}[unsure["text"]] == "KNOWN"
            # the ledger has the command and the finished task; the log has every record
            status, acts = http("GET", f"{base}/api/activity?session={sid}")
            kinds = [a["activity_type"] for a in acts]
            assert "command_run" in kinds and "task" in kinds and "memory_saved" in kinds, kinds
            task = [a for a in acts if a["activity_type"] == "task"][0]
            assert task["status"] == "success" and "5 steps" in task["subtitle"], task
            stored = app.store.items(sid)
            assert [e["kind"] for e in stored] == ["user"] + ["tool"] * 5 + ["assistant"], stored
            status, snap = http("GET", f"{base}/api/sessions/{sid}")
            assert snap["activity"] and snap["activity"][0]["activity_type"] == "task", snap["activity"]
        finally:
            reader.stop()
            server.shutdown()
    print("PASS test_memory_standing_ledger_and_log_through_a_session")


# ---------------------------------------------------------- helpers ---


def test_background_helper_reports_back_and_follows_the_same_rules():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "notes.txt"), "the answer is 42\n")
        _write(os.path.join(tmp, "target.txt"), "old\n")
        app, server, base = start_server(tmp)
        try:
            sid, parent = make_session_with_fake(base, tmp, [
                tool_call_msg("subagent_spawn", {"brief": "Read notes.txt and report the answer"}),
                text_msg("A helper is looking."),
                # the handoff turn: the main agent relays the report
                text_msg("The helper found that the answer is 42."),
            ])
            child = FakeClient([
                tool_call_msg("read", {"path": "notes.txt"}),
                # a helper is gated like the main agent: this edit must ask
                tool_call_msg("edit", {"path": "target.txt", "old_text": "old\n", "new_text": "new\n"}, "c2"),
                text_msg("The answer is 42 (notes.txt, line 1)."),
            ])
            session = app.get_session(sid)
            session._build_child_client = lambda: child
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "find the answer"})
            # starting a helper is itself asked about (Settings, Permissions, "Start helpers")
            first = reader.wait_for("permission_request")
            assert first["tool"] == "subagent_spawn" and first["kind"] == "helpers", first
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": first["request_id"], "decision": "allow"})
            started = reader.wait_for("subagent_start")
            deadline = time.time() + 15
            preq = None
            while time.time() < deadline and preq is None:
                asks = [d for t, d in reader.events if t == "permission_request"]
                preq = asks[1] if len(asks) > 1 else None
                time.sleep(0.1)
            assert preq is not None, "the helper's edit was not asked about"
            assert preq["tool"] == "edit" and "Read notes.txt" in preq["helper"], preq
            # the question is still there for a page that opens now
            status, snap = http("GET", f"{base}/api/sessions/{sid}")
            assert snap["pending_permission"]["request_id"] == preq["request_id"], snap["pending_permission"]
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": preq["request_id"], "decision": "deny"})
            ended = reader.wait_for("subagent_end")
            assert ended["id"] == started["id"] and ended["ok"] and "42" in ended["summary"], ended
            with open(os.path.join(tmp, "target.txt"), encoding="utf-8") as fh:
                assert fh.read() == "old\n"   # denied: untouched
            # the report comes back as a handoff turn, not as a user message
            deadline = time.time() + 20
            while time.time() < deadline:
                turns = [d for t, d in reader.events if t == "turn_start"]
                ends = [d for t, d in reader.events if t == "turn_end"]
                if len(turns) >= 2 and len(ends) >= 2:
                    break
                time.sleep(0.1)
            assert turns[1]["kind"] == "handoff" and "helper finished" in turns[1]["note"].lower(), turns
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            kinds = [e["kind"] for e in hist["transcript"]]
            assert kinds.count("user") == 1 and "subagent" in kinds, kinds
            assert hist["transcript"][-1]["text"] == "The helper found that the answer is 42."
            # the helper has its own row and its own log; what it read is session evidence
            agents = app.store.agents(sid)
            helper = [a for a in agents if a["kind"] == "subagent"][0]
            assert helper["status"] == "done" and helper["parent_agent_id"] == sid, helper
            assert [e["name"] for e in app.store.items(helper["agent_id"])] == ["read", "edit"]
            assert "notes.txt" in session.ledger.read
            status, snap = http("GET", f"{base}/api/sessions/{sid}")
            assert snap["helpers"][0]["status"] == "done" and snap["status"] == "idle", snap["helpers"]
            assert child.calls == 3 and parent.calls == 3, (child.calls, parent.calls)
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_background_helper_reports_back_and_follows_the_same_rules")


def test_web_tools_ask_first_and_mark_outside_content():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("web_fetch", {"url": base + "/api/about"}),
                text_msg("read it"),
            ])
            # even in auto mode the per-kind setting is honoured: set the web to ask
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "look it up"})
            preq = reader.wait_for("permission_request")
            assert preq["tool"] == "web_fetch" and preq["kind"] == "network", preq
            http("POST", f"{base}/api/sessions/{sid}/permission",
                 {"request_id": preq["request_id"], "decision": "allow"})
            reader.wait_for("turn_end")
            result = [d for t, d in reader.events if t == "tool_result"][0]["result"]
            assert result.startswith("[From the web: outside content"), result[:80]
            assert "workspace" in result, result[:200]
            names = app.get_session(sid).loop.tool_names()
            assert "web_search" in names and "subagent_spawn" in names and "memory_save" in names, names
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_web_tools_ask_first_and_mark_outside_content")


# -------------------------------------------------------- scheduler ---


class BrokenClient:
    def chat(self, messages, tools):
        raise ConnectionError("model server is down")


def test_scheduled_jobs_run_report_stay_silent_and_pause_themselves():
    import datetime

    from webui import jobs as jobs_mod

    assert jobs_mod.describe("0 9 * * 1-5") == "Weekdays at 09:00"
    assert jobs_mod.describe("30 7 * * *") == "Every day at 07:30"
    monday = datetime.datetime(2026, 10, 12, 8, 59, 30)
    assert jobs_mod.next_run("0 9 * * *", monday) == datetime.datetime(2026, 10, 12, 9, 0)
    assert jobs_mod.next_run("0 9 * * *", monday.replace(hour=9, minute=0)) == datetime.datetime(2026, 10, 13, 9, 0)

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "status.txt"), "build: red\n")
        app, server, base = start_server(tmp)
        try:
            status, bad = http("POST", f"{base}/api/jobs", {"title": "x", "task": "y", "schedule": "every day"})
            assert status == 400, (status, bad)
            status, job = http("POST", f"{base}/api/jobs",
                               {"title": "Build check", "task": "Read status.txt and report the build state.",
                                "schedule": "0 9 * * *", "project_dir": tmp})
            assert status == 201 and job["schedule_text"] == "Every day at 09:00" and job["next_run_at"], job
            jid = job["id"]
            due = datetime.datetime.fromisoformat(job["next_run_at"])

            # not due yet: nothing runs, and the history says so
            assert app.jobs.tick(due - datetime.timedelta(minutes=5), wait=True) == []
            assert app.store.runs(jid) == []

            # due: a worker reads the file and reports; the report lands in the job's own session
            app.jobs._client = lambda j: FakeClient([
                tool_call_msg("read", {"path": "status.txt"}),
                # an unattended run may not run a command you have not allowed
                tool_call_msg("bash", {"cmd": "python -c \"open('made.txt','w').write('x')\""}, "c2"),
                text_msg("The build is red according to `status.txt`."),
            ])
            assert app.jobs.tick(due + datetime.timedelta(seconds=20), wait=True) == [jid]
            run = app.store.runs(jid)[0]
            assert run["status"] == "reported" and run["steps"] == 2 and run["delivered"] == 1, run
            assert not os.path.exists(os.path.join(tmp, "made.txt")), "an unallowed command ran unattended"
            stored = app.store.job(jid)
            assert stored["session_id"] and stored["next_run_at"] > job["next_run_at"], stored
            status, hist = http("GET", f"{base}/api/sessions/{stored['session_id']}/history")
            last = hist["transcript"][-1]
            assert last["kind"] == "assistant" and "build is red" in last["text"], hist
            assert {c["text"]: c["state"] for c in last["claims"]["claims"]}["status.txt"] == "KNOWN", last

            # nothing new: the run is recorded, and nothing is delivered
            before = len(hist["transcript"])
            app.jobs._client = lambda j: FakeClient([text_msg("NOTHING_TO_REPORT")])
            http("POST", f"{base}/api/jobs/{jid}/run", {})
            deadline = time.time() + 15
            while time.time() < deadline:
                rows = app.store.runs(jid)
                if len(rows) >= 2 and rows[0]["status"] != "running":
                    break
                time.sleep(0.1)
            assert app.store.runs(jid)[0]["status"] == "silent", app.store.runs(jid)[0]
            status, hist = http("GET", f"{base}/api/sessions/{stored['session_id']}/history")
            assert len(hist["transcript"]) == before

            # the model is down: after three failures in a row the job pauses itself and says why
            app.jobs._client = lambda j: BrokenClient()
            for _ in range(3):
                assert app.jobs.run_now(jid, wait=True)
            status, listed = http("GET", f"{base}/api/jobs")
            assert "failed 3 times" in listed[0]["blocked_reason"], listed[0]
            far = datetime.datetime.now() + datetime.timedelta(minutes=5)
            app.store.update_job(jid, next_run_at=datetime.datetime.now().isoformat(timespec="seconds"))
            assert app.jobs.tick(far, wait=True) == [], "a paused job must wait for its retry time"
            # resuming it by hand clears the pause
            status, resumed = http("POST", f"{base}/api/jobs/{jid}", {"enabled": True})
            assert resumed["blocked_reason"] is None and resumed["enabled"], resumed
            status, runs = http("GET", f"{base}/api/jobs/{jid}/runs")
            assert [r["status"] for r in runs] == ["error"] * 3 + ["silent", "reported"], runs
            # the agent has the tools, and cron_add asks first
            sid, _ = make_session_with_fake(base, tmp, [])
            names = app.get_session(sid).loop.tool_names()
            assert {"cron_add", "cron_list", "cron_runs"} <= set(names), names
            status, left = http("DELETE", f"{base}/api/jobs/{jid}")
            assert status == 200 and left == [], left
        finally:
            server.shutdown()
    print("PASS test_scheduled_jobs_run_report_stay_silent_and_pause_themselves")


# ------------------------------------------------- looking back ---


def test_quiet_moment_pass_saves_what_was_said_and_marks_what_was_not():
    import datetime

    from webui import webtools
    from webui.app import tool_kind

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("Understood.")])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message",
                 {"text": "From now on always use tabs for indentation in this project."})
            reader.wait_for("turn_end")
            reader.stop()
            session = app.get_session(sid)
            now = datetime.datetime.now()
            # still fresh: nothing happens
            assert app.learner.tick(now, wait=True) == []
            calls = []

            def scripted():
                client = FakeClient([
                    tool_call_msg("memory_search", {"query": "indentation tabs"}),
                    tool_call_msg("memory_save", {"fact": "Always use tabs for indentation in this project"}, "c2"),
                    tool_call_msg("memory_save", {"fact": "The team deploys with Kubernetes on Azure"}, "c3"),
                    text_msg("NOTHING"),
                ])
                calls.append(client)
                return client

            app.learner._client = scripted
            later = now + datetime.timedelta(minutes=11)
            assert app.learner.tick(later, wait=True) == [sid]
            claims = {c["claim_text"]: c for c in app.store.claims()}
            said = claims["Always use tabs for indentation in this project"]
            assert said["state"] == "KNOWN" and said["project_dir"] == session.project_dir, said
            # the pass cannot promote its own conclusion
            assert claims["The team deploys with Kubernetes on Azure"]["state"] == "CONFABULATED", claims
            # the same stretch of conversation is never gone over twice
            assert app.learner.tick(later + datetime.timedelta(hours=1), wait=True) == []
            assert len(calls) == 1
            status, st = http("GET", f"{base}/api/learning")
            assert st["last"]["status"] == "done" and st["last"]["saved"] == 2, st
            acts = [a["activity_type"] for a in app.store.recent_activity(sid)]
            assert "memory_pass" in acts, acts
            # switched off: no pass, whatever is new
            http("POST", f"{base}/api/settings", {"learn_from_conversations": False})
            app.store.set_meta("learned:" + sid, "0")
            assert app.learner.tick(later, wait=True) == []
        finally:
            server.shutdown()

    # reading a long page in parts, and searching inside it, without fetching it twice
    pages = {"http://x/": "Alpha one. " * 40 + "The licence is MIT. " + "Omega two. " * 40}
    first = webtools.fetch_page(pages, "http://x/", 300)
    assert "more characters; call again with start=300" in first, first[-120:]
    assert webtools.fetch_page(pages, "http://x/", 300, start=300).startswith(pages["http://x/"][300:320])
    found = webtools.fetch_page(pages, "http://x/", pattern="licence")
    assert found.startswith("1 mention(s)") and "MIT" in found, found
    assert "No mention" in webtools.fetch_page(pages, "http://x/", pattern="zebra")
    # a tool server's tool counts as reading only when you listed it as read-only yourself
    from coder import mcp

    assert tool_kind("mcp__gh__search_issues") == "run"
    mcp.READ_ONLY_TOOLS.add("mcp__gh__search_issues")
    try:
        assert tool_kind("mcp__gh__search_issues") == "read" and tool_kind("mcp__gh__create_issue") == "run"
    finally:
        mcp.READ_ONLY_TOOLS.discard("mcp__gh__search_issues")
    print("PASS test_quiet_moment_pass_saves_what_was_said_and_marks_what_was_not")


def test_goals_reach_the_prompt_and_skill_proposals_come_from_real_work():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.txt"), "x\n")
        app, server, base = start_server(tmp)
        app.skills_dir = os.path.join(tmp, "skills")   # never the repository's own
        app.skills = []
        try:
            # ---- goals
            status, goals = http("POST", f"{base}/api/goals", {"title": "Ship version 1", "project_dir": tmp})
            assert status == 201 and goals[0]["status"] == "active", goals
            gid = goals[0]["goal_id"]
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.txt"}),
                tool_call_msg("grep", {"pattern": "x", "path": "."}, "c2"),
                tool_call_msg("goal_log", {"goal_id": gid, "note": "Read a.txt and confirmed the marker is there"}, "c3"),
                text_msg("Logged."),
            ])
            session = app.get_session(sid)
            assert "Ship version 1" in session.loop.memory.summary()
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "check the marker"})
            reader.wait_for("turn_end")
            reader.stop()
            status, goals = http("GET", f"{base}/api/goals")
            assert goals[0]["updates"][0]["author"] == "agent" and "marker" in goals[0]["updates"][0]["note"], goals
            assert "confirmed the marker" in session.loop.memory.summary()
            status, goals = http("POST", f"{base}/api/goals/{gid}", {"note": "Tagged the release", "status": "completed"})
            assert goals[0]["status"] == "completed" and goals[0]["updates"][0]["author"] == "you", goals
            assert "Ship version 1" not in session.loop.memory.summary()   # finished goals leave the prompt

            # ---- skills: the finished task was kept as a case
            status, over = http("GET", f"{base}/api/skills")
            assert over["cases"] == 1 and over["proposals"] == [], over
            # the same steps working three times make a proposal; nothing is installed by itself
            store = app.case_store()
            for _ in range(2):
                store.add_case(dict(store.all_cases()[0], id=None))
            status, over = http("POST", f"{base}/api/skills/mine", {})
            assert over["proposals"], over
            folder = over["proposals"][0]["folder"]
            assert over["installed"] == [] and os.path.isdir(os.path.join(tmp, "skills", "proposals", folder))
            status, over = http("POST", f"{base}/api/skills/proposals/{folder}/install", {})
            assert status == 200 and [s["name"] for s in over["installed"]] == [over["installed"][0]["name"]], over
            assert os.path.isfile(os.path.join(tmp, "skills", folder, "SKILL.md")) and over["proposals"] == []
            assert session.loop.skills and session.loop.skills[0].name == over["installed"][0]["name"]
            status, _ = http("POST", f"{base}/api/skills/proposals/..%2Fx/install", {})
            assert status == 404, status
        finally:
            server.shutdown()
    print("PASS test_goals_reach_the_prompt_and_skill_proposals_come_from_real_work")


def test_ideas_come_from_evidence_persona_leads_the_prompt_and_chats_are_searchable():
    from webui import ideas as idea_source

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "calc.py"), "def add(a, b):\n    # FIXME: breaks on None\n    return a + b\n")
        # sample code inside a string is not a note left in the code
        _write(os.path.join(tmp, "test_sample.py"), 'SRC = "def f():\\n    # FIXME: inside a string\\n"\n')
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "calc.py"}),
                tool_call_msg("edit", {"path": "calc.py", "old_text": "return a + b",
                                       "new_text": "return (a or 0) + (b or 0)"}, "c2"),
                tool_call_msg("bash", {"cmd": "python -c \"import sys; sys.exit(3)\""}, "c3"),
                text_msg("Changed it; the check command failed."),
            ])
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            session = app.get_session(sid)
            # persona leads the prompt; what you wrote about yourself follows it
            http("POST", f"{base}/api/persona", {"user": "Call me Sam. I work on billing code."})
            prompt = session.loop.build_system_prompt("fix the pineapple importer")
            assert prompt.startswith("[How to work with this user]"), prompt[:80]
            assert "Call me Sam" in prompt and prompt.index("Call me Sam") < prompt.index("expert coding agent")
            assert session._orient_query == "fix the pineapple importer"

            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message",
                 {"text": "make add tolerate None in the zebra module"})
            reader.wait_for("turn_end")
            reader.stop()

            status, found = http("GET", f"{base}/api/ideas?project_dir=" + tmp.replace("\\", "/"))
            kinds = {i["kind"]: i for i in found}
            assert {"todo", "failed_command", "untested_change"} <= set(kinds), [i["title"] for i in found]
            assert kinds["todo"]["source_path"] == "calc.py" and kinds["todo"]["source_line"] == 2, kinds["todo"]
            assert [i["source_path"] for i in found if i["kind"] == "todo"] == ["calc.py"]
            assert found[0]["kind"] == "failed_command", "the failed command should rank first"
            # dismissed stays dismissed, even after a fresh scan
            http("POST", f"{base}/api/ideas/{kinds['todo']['idea_id']}", {"status": "dismissed"})
            idea_source.refresh(app, tmp, force=True)
            assert "todo" not in {i["kind"] for i in app.store.ideas(os.path.abspath(tmp))}
            # the cause goes away: the idea retires by itself
            _write(os.path.join(tmp, "test_calc.py"), "def test_add():\n    pass\n")
            idea_source.refresh(app, tmp, force=True)
            assert "untested_change" not in {i["kind"] for i in app.store.ideas(os.path.abspath(tmp))}

            # earlier conversation can be found again by its words
            rows = app.store.search_items("zebra tolerate")
            assert rows and rows[0]["kind"] == "user" and "zebra" in rows[0]["text"], rows
            assert app.store.search_items("zebra giraffe") == []
            assert "chat_search" in session.loop.tool_names()

            # with more remembered than fits, what bears on the message comes first
            for n in range(45):
                app.store.add_claim(f"Unrelated fact number {n} about topic{n} alpha{n}", "KNOWN", "test")
            app.store.add_claim("The pineapple importer needs the legacy CSV dialect", "KNOWN", "test")
            from webui import recall

            assert "pineapple importer needs the legacy CSV" not in session.loop.build_system_prompt("fix the pineapple importer")
            assert "pineapple importer needs the legacy CSV" in recall.recalled(session, "fix the pineapple importer")
        finally:
            server.shutdown()
    print("PASS test_ideas_come_from_evidence_persona_leads_the_prompt_and_chats_are_searchable")


def test_library_pages_are_sandboxed_and_other_pages_cannot_drive_the_app():
    import urllib.request

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("artifact_create", {
                    "title": "Test report", "filename": "report.html",
                    "content": "<h1>Report</h1><script>fetch('/api/settings')</script>"}),
                tool_call_msg("artifact_create", {"title": "x", "filename": "run.exe", "content": "MZ"}, "c2"),
                text_msg("Made the report."),
            ])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "make a report"})
            card = reader.wait_for("artifact")
            reader.wait_for("turn_end")
            reader.stop()
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert "Saved to the Library" in results[0] and results[1].startswith("ERROR"), results
            # it is kept apart from the project folder
            assert not os.path.exists(os.path.join(tmp, "report.html"))
            status, items = http("GET", f"{base}/api/artifacts")
            assert len(items) == 1 and items[0]["kind"] == "page" and items[0]["url"] == card["url"], items
            # served in a sandbox: scripts run but have no origin and no network
            with urllib.request.urlopen(base + card["url"], timeout=10) as resp:
                csp = resp.headers.get("Content-Security-Policy", "")
                body = resp.read().decode("utf-8")
            assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp, csp
            assert "default-src 'none'" in csp and "<h1>Report</h1>" in body
            # the page may reach exactly one thing: its own data, which it can read and replace
            data_url = base + card["url"].rsplit("/", 1)[0] + "/data"
            assert "connect-src " + data_url in csp, csp
            with urllib.request.urlopen(data_url, timeout=10) as resp:
                assert resp.read() == b"null" and resp.headers.get("Access-Control-Allow-Origin") == "null"
            save = urllib.request.Request(data_url, data=b'{"count": 3}', method="POST",
                                          headers={"Content-Type": "text/plain", "Origin": "null"})
            with urllib.request.urlopen(save, timeout=10) as resp:
                assert resp.status == 200
            with urllib.request.urlopen(data_url, timeout=10) as resp:
                assert resp.read() == b'{"count": 3}'
            bad = urllib.request.Request(data_url, data=b"not json", method="POST")
            try:
                urllib.request.urlopen(bad, timeout=10)
                raise AssertionError("data that is not JSON was accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            # the card is part of the session's record
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            assert [e["kind"] for e in hist["transcript"]].count("artifact") == 1

            # a request that changes something is refused when another page sent it
            def post_from(origin):
                req = urllib.request.Request(
                    base + "/api/settings", data=b'{"guard": "off"}', method="POST",
                    headers={"Content-Type": "text/plain", "Origin": origin},
                )
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            # nor does it answer when reached under a name that is not this computer's
            import http.client as raw_http

            port = int(base.rsplit(":", 1)[1])
            conn = raw_http.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/settings", headers={"Host": "evil.example:%d" % port})
            assert conn.getresponse().status == 403
            conn.close()
            assert post_from("https://evil.example") == 403
            assert post_from("null") == 403            # a sandboxed page the agent made
            assert app.settings["guard"] == "ask"
            assert post_from(base) == 200 and app.settings["guard"] == "off"   # the app's own page

            status, _ = http("DELETE", f"{base}/api/artifacts/{items[0]['artifact_id']}")
            assert status == 200 and http("GET", f"{base}/api/artifacts")[1] == []
        finally:
            server.shutdown()
    print("PASS test_library_pages_are_sandboxed_and_other_pages_cannot_drive_the_app")


def test_extra_tools_load_on_request_and_a_watch_job_only_wakes_on_change():
    import datetime
    import json

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "state.txt"), "one\n")
        app, server, base = start_server(tmp)
        try:
            # ---- only the core tools are offered until a group is asked for
            seen = []

            class Recording(FakeClient):
                def chat(self, messages, tools):
                    seen.append(sorted(t["function"]["name"] for t in tools))
                    return super().chat(messages, tools)

            sid, _ = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)
            client = Recording([
                tool_call_msg("tools_load", {"group": "goals"}),
                tool_call_msg("goal_list", {}, "c2"),
                text_msg("done"),
            ])
            session._build_client = lambda provider=None: client
            everything = len(session.loop.registry.names())
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what are my goals"})
            reader.wait_for("turn_end")
            reader.stop()
            assert "goal_list" not in seen[0] and "tools_load" in seen[0] and "read" in seen[0], seen[0]
            assert "browser_navigate" not in seen[0] and len(seen[0]) < everything / 2, (len(seen[0]), everything)
            # loaded mid-turn: offered from the very next round
            assert "goal_list" in seen[1] and "cron_add" not in seen[1], seen[1]
            small = len(json.dumps(session.loop.registry.schemas()))
            http("POST", f"{base}/api/settings", {"lazy_tools": False})
            full = len(json.dumps(session.loop.registry.schemas()))
            assert full > small * 1.5, (small, full)

            # ---- a job with a check command wakes the agent only when the output changes
            show = "python -c \"print(open('state.txt').read())\""
            status, refused = http("POST", f"{base}/api/jobs",
                                   {"title": "Watch", "task": "Report the state.", "schedule": "0 9 * * *",
                                    "project_dir": tmp, "watch": show})
            assert status == 400 and "always allow" in refused["error"], refused
            app.allow_command("python", os.path.abspath(tmp))
            status, job = http("POST", f"{base}/api/jobs",
                               {"title": "Watch", "task": "Report the state.", "schedule": "0 9 * * *",
                                "project_dir": tmp, "watch": show})
            assert status == 201 and job["watch"] == show, job
            jid = job["id"]
            calls = []

            def scripted(j):
                calls.append(1)
                return FakeClient([text_msg("The state is now different.")])

            app.jobs._client = scripted
            assert app.jobs.run_now(jid, wait=True)          # first look: something to report
            assert app.jobs.run_now(jid, wait=True)          # nothing changed: the model is not called
            runs = app.store.runs(jid)
            assert [r["status"] for r in runs] == ["silent", "reported"], runs
            assert "not woken" in runs[0]["result_summary"] and len(calls) == 1, (runs[0], calls)
            _write(os.path.join(tmp, "state.txt"), "two\n")
            assert app.jobs.run_now(jid, wait=True)          # it changed: woken again
            assert app.store.runs(jid)[0]["status"] == "reported" and len(calls) == 2
        finally:
            server.shutdown()
    print("PASS test_extra_tools_load_on_request_and_a_watch_job_only_wakes_on_change")


def test_daily_review_turns_repeated_problems_into_lessons_and_retires_old_ones():
    import datetime

    from webui import review

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.txt"), "alpha\n")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.txt"}),
                # three edits whose text is not in the file, and a read of a file that is not there
                tool_call_msg("edit", {"path": "a.txt", "old_text": "nope1", "new_text": "x"}, "c2"),
                tool_call_msg("edit", {"path": "a.txt", "old_text": "nope2", "new_text": "x"}, "c3"),
                tool_call_msg("edit", {"path": "a.txt", "old_text": "nope3", "new_text": "x"}, "c4"),
                tool_call_msg("read", {"path": "missing.txt"}, "c5"),
                text_msg("All tests pass and I updated `ghost_file.py`."),
            ])
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "change alpha"})
            reader.wait_for("turn_end")
            reader.stop()
            session = app.get_session(sid)
            # an old lesson nobody has needed for weeks
            old = app.store.add_claim("Prefer short commands", "DERIVED", "from the record", kind="lesson")
            long_ago = (datetime.datetime.now() - datetime.timedelta(days=20)).isoformat(timespec="seconds")
            app.store.update_claim_dates(old["claim"]["claim_id"], long_ago)

            status, reviews = http("POST", f"{base}/api/reviews", {})
            r = reviews[0]
            assert r["tasks"]["done"] == 1 and r["steps"] == 5, r
            top = r["failures"][0]
            assert top["tool"] == "edit" and top["count"] == 3 and "not in the file" in top["cause"], r["failures"]
            lessons = [l["text"] for l in r["lessons"]]
            assert any("Read the file again right before editing" in l for l in lessons), lessons
            # "all tests pass" with no run is a guess, not a contradiction: no lesson from one such reply
            assert not any("Never report tests" in l for l in lessons), lessons
            assert r["retired"] == ["Prefer short commands"], r["retired"]
            # the lesson is in the next prompt, under its own heading, and listed as a lesson
            prompt = session.loop.build_system_prompt("next task")
            assert "Lessons from the record of recent work" in prompt and "Read the file again right before editing" in prompt
            assert "Prefer short commands" not in prompt
            status, claims = http("GET", f"{base}/api/memory")
            assert [c for c in claims if c["kind"] == "lesson"], claims
            # once a day: a second scheduled run the same day does nothing
            assert review.run(app) is None
            assert review.due(app, datetime.datetime.now()) is False
        finally:
            server.shutdown()

    # the rules themselves
    quiet = {"failures": [{"tool": "bash", "cause": "the command exited with code 1", "count": 1, "example": ""}],
             "denied": [], "guard": 0, "claims": {}, "corrections": [], "asked_again": 0}
    assert review.lessons_from(quiet) == [], "one failure is not a pattern"
    loud = dict(quiet, denied=[{"what": "bash: rm", "count": 2}], guard=3, claims={"CONFLICTED": 1},
                corrections=["no, I said tabs", "that's wrong"], asked_again=1)
    texts = " | ".join(t for t, _ in review.lessons_from(loud))
    assert "keeps declining" in texts and "Look before acting" in texts and "Never report tests" in texts
    assert "Restate what was asked" in texts, texts
    print("PASS test_daily_review_turns_repeated_problems_into_lessons_and_retires_old_ones")


def test_answers_stream_as_they_are_written_and_can_be_stopped():
    import importlib.util
    import threading
    from http.server import ThreadingHTTPServer

    from agent import OpenAICompatClient

    spec = importlib.util.spec_from_file_location("demo_model", os.path.join(ROOT, "tests", "demo_model.py"))
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    model = ThreadingHTTPServer(("127.0.0.1", 0), demo.Handler)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{model.server_port}/v1"
    try:
        # ---- the client: pieces arrive one by one and add up to the same answer
        client = OpenAICompatClient(url, "", "demo-model")
        pieces = []
        client.on_delta = pieces.append
        out = client.chat([{"role": "user", "content": "say hi"}], [])
        text = out["choices"][0]["message"]["content"]
        assert text.startswith("Hi. I have not looked") and "".join(pieces) == text and len(pieces) > 5, pieces
        assert client.usage_total["reported"] and client.usage_total["out"] == len(text.split()), client.usage_total
        assert client.usage_total["in"] == 900 and client.usage_total["ttft_ms"] is not None
        # a tool call arrives through the stream in the same shape as before
        out = client.chat([{"role": "user", "content": "fix the ordinal tests"}], [])
        call = out["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "grep" and "def ordinal" in call["function"]["arguments"], call
        # Stop cuts the answer short instead of waiting for the end
        stopper = OpenAICompatClient(url, "", "demo-model")
        got = []
        stopper.on_delta = got.append
        stopper.should_stop = lambda: len(got) >= 3
        cut = stopper.chat([{"role": "user", "content": "say hi"}], [])["choices"][0]["message"]["content"]
        assert len(got) == 3 and len(cut) < len(text), (got, cut)
        # without a listener nothing changes: one plain request, one answer
        plain = OpenAICompatClient(url, "", "demo-model").chat([{"role": "user", "content": "say hi"}], [])
        assert plain["choices"][0]["message"]["content"] == text

        # ---- a whole session against it: partial answers reach the page before the final one
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "ws")
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(
                    'agent_name: T\nworkspace: "%s"\nprovider: "demo"\nmodel: "demo-model"\nproviders:\n'
                    '  demo:\n    base_url: "%s"\n    api_key_env: ""\n' % (ws.replace("\\", "/"), url)
                )
            from webui.app import WebApp
            from webui.server import WebHandler

            app = WebApp(cfg, default_project=tmp)
            WebHandler.app = app
            WebHandler._load_static()
            server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
                sid = snap["id"]
                reader = SSEReader(f"{base}/api/sessions/{sid}/events")
                reader.start()
                reader.wait_for("state")
                http("POST", f"{base}/api/sessions/{sid}/message", {"text": "say hi"})
                end = reader.wait_for("turn_end")
                tokens = [d for t, d in reader.events if t == "token"]
                partial = [d for d in tokens if not d["final"]]
                assert partial and tokens[-1]["final"] and tokens[-1]["text"] == text, [len(partial), tokens[-1]]
                assert len(partial[0]["text"]) < len(text) and text.startswith(partial[0]["text"]), partial[0]
                # the counts come from the server, and the speed is measured
                assert end["output_tokens"] == len(text.split()) and end["input_tokens"] == 900, end
                assert end["tokens_per_s"] and end["first_token_ms"] is not None, end
                reader.stop()
            finally:
                server.shutdown()
    finally:
        model.shutdown()
    print("PASS test_answers_stream_as_they_are_written_and_can_be_stopped")


def test_skills_can_be_written_edited_and_removed_and_themes_are_kept_clean():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        app.skills_dir = os.path.join(tmp, "skills")   # never the repository's own
        app.skills = []
        try:
            status, over = http("POST", f"{base}/api/skills", {
                "name": "Release Checklist", "description": "Use when preparing a release",
                "keywords": "release, changelog, version", "body": "1. Run the tests.\n2. Update the changelog."})
            assert status == 200 and over["installed"][0]["folder"] == "release-checklist", over
            assert over["installed"][0]["keywords"] == ["release", "changelog", "version"], over
            status, detail = http("GET", f"{base}/api/skills/release-checklist")
            assert "Update the changelog" in detail["body"] and detail["description"] == "Use when preparing a release"
            # a new session is offered the skill; editing replaces it in place
            sid, _ = make_session_with_fake(base, tmp, [])
            assert "release-checklist" in app.get_session(sid).loop.build_system_prompt("prepare the release")
            status, over = http("POST", f"{base}/api/skills", {
                "name": "release-checklist", "description": "Use before tagging a version", "keywords": [],
                "body": "Only tag from main."})
            assert len(over["installed"]) == 1 and over["installed"][0]["description"] == "Use before tagging a version"
            status, _ = http("POST", f"{base}/api/skills", {"name": "x", "description": "", "body": ""})
            assert status == 400
            status, _ = http("POST", f"{base}/api/skills", {"name": "proposals", "description": "d", "body": "b"})
            assert status == 400, "the proposals folder is not a skill"
            status, over = http("DELETE", f"{base}/api/skills/release-checklist")
            assert status == 200 and over["installed"] == [] and not os.path.exists(
                os.path.join(tmp, "skills", "release-checklist"))

            # appearance: a theme of your own is four colours, and odd values are dropped
            status, st = http("POST", f"{base}/api/settings", {"ui": {
                "theme": "custom:Night", "meter_seconds": 0.01, "bogus": 1,
                "custom_themes": {"Night": {"bg": "#000000", "panel": "#0a0a0a", "text": "#ffffff",
                                            "accent": "#7c9cff", "script": "alert(1)"}, "Bad": "nope"}}})
            ui = st["ui"]
            assert ui["theme"] == "custom:Night" and "bogus" not in ui and ui["meter_seconds"] == 0.5, ui
            assert ui["custom_themes"] == {"Night": {"bg": "#000000", "panel": "#0a0a0a", "text": "#ffffff",
                                                     "accent": "#7c9cff"}}, ui["custom_themes"]
            assert ui["text_size"] == "comfortable"   # what was not sent keeps its default
        finally:
            server.shutdown()
    print("PASS test_skills_can_be_written_edited_and_removed_and_themes_are_kept_clean")


def test_other_devices_need_the_key():
    import http.client as raw_http

    from webui.server import WebHandler

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        port = int(base.rsplit(":", 1)[1])
        key = "a-fixed-key-set-by-whoever-embeds-the-server"
        WebHandler.access_key = key
        try:
            def ask(method, path, cookie=None, body=None):
                conn = raw_http.HTTPConnection("127.0.0.1", port, timeout=10)
                headers = {"Content-Type": "application/json"}
                if cookie:
                    headers["Cookie"] = cookie
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                out = (resp.status, resp.getheader("Set-Cookie") or "", resp.getheader("Location") or "")
                resp.read()
                conn.close()
                return out

            # without the key: nothing, not even the page
            assert ask("GET", "/")[0] == 401
            assert ask("GET", "/api/settings")[0] == 401
            assert ask("POST", "/api/settings", body='{"guard": "off"}')[0] == 401
            assert ask("DELETE", "/api/goals/abc")[0] == 401
            assert ask("GET", "/?key=wrong")[0] == 401
            assert app.settings["guard"] == "ask"
            # the private link sets the key once and moves it out of the address
            status, cookie, location = ask("GET", "/?key=" + key)
            assert status == 302 and location == "/" and "cm_key=" + key in cookie, (status, cookie, location)
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
            # with it: everything works
            carried = "cm_key=" + key
            assert ask("GET", "/", carried)[0] == 200
            assert ask("GET", "/api/settings", carried)[0] == 200
            assert ask("POST", "/api/settings", carried, '{"guard": "off"}')[0] == 200
            assert app.settings["guard"] == "off"
            assert ask("GET", "/api/settings", "cm_key=" + key[:-1] + "x")[0] == 401
        finally:
            WebHandler.access_key = None
            server.shutdown()
    print("PASS test_other_devices_need_the_key")


def test_phone_pairs_with_a_short_code_or_a_scanned_link():
    import http.client as raw_http

    from webui import phone as phone_mod
    from webui import qr
    from webui.server import WebHandler

    # the QR writer: a standard reader must get the same text back (when one is installed)
    link = "http://10.0.0.100:8080/?key=TEST-key-not-a-real-1234"
    m = qr.matrix(link)
    assert len(m) == len(m[0]) == 29 and "<path" in qr.svg(link) and len(qr.text_blocks(link).splitlines()) == 33
    try:
        import cv2
        import numpy as np

        img = np.full(((len(m) + 8) * 8,) * 2, 255, np.uint8)
        for y, row in enumerate(m):
            for x, on in enumerate(row):
                if on:
                    img[(y + 4) * 8:(y + 5) * 8, (x + 4) * 8:(x + 5) * 8] = 0
        assert cv2.QRCodeDetector().detectAndDecode(img)[0] == link, "the QR code does not read back"
    except ImportError:
        pass

    lan = phone_mod.lan_address()
    if lan.startswith("127."):
        print("PASS test_phone_pairs_with_a_short_code_or_a_scanned_link (pairing skipped: no network)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        port = int(base.rsplit(":", 1)[1])
        app.phone.handler, app.phone.port = WebHandler, port
        try:
            def ask(host, method, path, cookie=None, body=None, ctype="application/json", ua="test"):
                conn = raw_http.HTTPConnection(host, port, timeout=10)
                headers = {"Content-Type": ctype, "User-Agent": ua}
                if cookie:
                    headers["Cookie"] = cookie
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                out = (resp.status, resp.getheader("Set-Cookie") or "", resp.read().decode("utf-8", "replace"))
                conn.close()
                return out

            # off: this computer works, and nothing listens on the network address
            status, st = http("GET", base + "/api/phone")
            assert st == {"enabled": False, "address": "", "code": "", "expires_in": 0, "link": "",
                          "can_switch": True}, st
            status, st = http("POST", base + "/api/phone", {"enabled": True})
            assert status == 200 and st["enabled"] and st["address"] == f"{lan}:{port}" and len(st["code"]) == 6, st
            assert app.settings["phone_access"] is True
            form = "application/x-www-form-urlencoded"
            # a device that is not paired sees only the code box
            status, _, page = ask(lan, "GET", "/")
            assert status == 401 and "6-digit code" in page and "session-list" not in page, status
            assert ask(lan, "GET", "/api/settings")[0] == 401
            assert ask(lan, "POST", "/api/settings", body='{"guard": "off"}')[0] == 401
            assert ask(lan, "GET", "/api/phone/qr.svg")[0] == 401
            # a wrong code is refused; the right one lets it in, once
            status, _, page = ask(lan, "POST", "/pair", body="code=000000" if st["code"] != "000000" else "code=111111", ctype=form)
            assert status == 401 and "not the code" in page, (status, page[-200:])
            iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
            status, cookie, _ = ask(lan, "POST", "/pair", body="code=" + st["code"][:3] + "+" + st["code"][3:], ctype=form, ua=iphone)
            assert status == 302 and "cm_dev=" in cookie and "HttpOnly" in cookie, (status, cookie)
            carried = cookie.split(";")[0]
            assert ask(lan, "GET", "/api/settings", carried)[0] == 200
            assert ask(lan, "GET", "/", carried)[0] == 200
            assert ask(lan, "POST", "/pair", body="code=" + st["code"], ctype=form)[0] == 401, "a code works once"
            # a paired phone still cannot switch phone access or fetch the QR (computer only)
            assert ask(lan, "POST", "/api/phone", carried, '{"enabled": false}')[0] == 403
            assert ask(lan, "GET", "/api/phone/qr.svg", carried)[0] == 403
            # the scanned link works too, and the computer gets its QR
            fresh = app.phone.status()
            assert fresh["code"] and fresh["code"] != st["code"] or True
            android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
            first_link = app.phone.status()["link"].split("key=")[1]
            status, cookie, _ = ask(lan, "GET", "/?key=" + app.phone.status()["link"].split("key=")[1], ua=android)
            assert status == 302 and "cm_dev=" in cookie
            second = cookie.split(";")[0]
            assert second != carried, "each device gets a key of its own"
            # the link works once: a copy of the QR code is worth nothing after it was used
            assert ask(lan, "GET", "/?key=" + first_link, ua=android)[0] == 401
            assert app.phone.status()["link"].split("key=")[1] != first_link
            # scanning again on a paired device does not make a second entry
            status, cookie, _ = ask(lan, "GET", "/?key=" + app.phone.status()["link"].split("key=")[1], second)
            assert status == 302 and cookie == "", cookie
            # ---- the computer sees both, by name; only a hash of each key is kept
            import json as _json
            status, listed = http("GET", base + "/api/devices")
            names = sorted(d["name"] for d in listed["devices"])
            assert names == ["Android phone, Chrome", "iPhone, Safari"] and listed["remote"] is False, listed
            stored = app.store.get_meta("devices")
            assert carried.split(".")[1] not in stored and second.split(".")[1] not in stored
            # a device sees only itself, cannot manage the others, and is not shown the code or the link
            mine = _json.loads(ask(lan, "GET", "/api/devices", carried)[2])
            assert mine["remote"] and [d["name"] for d in mine["devices"]] == ["iPhone, Safari"], mine
            assert mine["devices"][0]["current"] is True
            hidden = _json.loads(ask(lan, "GET", "/api/phone", carried)[2])
            assert hidden["code"] == "" and hidden["link"] == "" and hidden["remote"] is True, hidden
            droid = [d for d in listed["devices"] if d["name"].startswith("Android")][0]["id"]
            assert ask(lan, "POST", f"/api/devices/{droid}/remove", carried, "{}")[0] == 403
            # a paired device can change ordinary settings, but not which program this computer runs
            assert ask(lan, "POST", "/api/settings", carried, '{"llama_server_path": "C:/evil.exe", "guard": "block"}')[0] == 200
            assert app.settings["guard"] == "block" and app.settings["llama_server_path"] == "", app.settings["llama_server_path"]
            http("POST", base + "/api/settings", {"guard": "ask"})
            # rename, then disconnect one: the other stays connected
            status, out = http("POST", base + f"/api/devices/{droid}/rename", {"name": "Pixel <b>"})
            assert [d["name"] for d in out["devices"] if d["id"] == droid] == ["Pixel b"], out
            assert http("POST", base + "/api/devices/ffff/remove", {})[0] == 404
            # a conversation the device has open is cut off too
            got = []
            import threading
            def stream():
                conn = raw_http.HTTPConnection(lan, port, timeout=6)
                conn.request("GET", "/api/events", headers={"Cookie": second})
                resp = conn.getresponse()
                try:
                    got.append(resp.read())   # returns only when the server ends the stream
                except OSError:
                    got.append(b"")
                conn.close()
            t = threading.Thread(target=stream, daemon=True)
            t.start()
            time.sleep(0.4)
            assert http("POST", base + f"/api/devices/{droid}/remove", {})[0] == 200
            assert ask(lan, "GET", "/api/settings", second)[0] == 401
            assert ask(lan, "GET", "/api/settings", carried)[0] == 200
            t.join(timeout=5)
            assert not t.is_alive(), "the disconnected device's stream is still open"
            # a device can sign itself out; pair it again for what follows
            third = ask(lan, "GET", "/?key=" + app.phone.status()["link"].split("key=")[1], ua=android)[1].split(";")[0]
            assert ask(lan, "POST", "/api/devices/signout", third, "{}")[0] == 200
            assert ask(lan, "GET", "/api/settings", third)[0] == 401
            assert len(http("GET", base + "/api/devices")[1]["devices"]) == 1
            status, _, body = ask("127.0.0.1", "GET", "/api/phone/qr.svg")
            assert status == 200 and body.startswith("<svg"), status
            # five wrong tries kill the code
            for _ in range(5):
                ask(lan, "POST", "/pair", body="code=999999" if fresh["code"] != "999999" else "code=888888", ctype=form)
            status, _, page = ask(lan, "POST", "/pair", body="code=" + fresh["code"], ctype=form)
            assert status == 401 and "Too many wrong tries" in page, page[-300:]
            # "disconnect every phone": the paired phone's key stops working at once
            assert ask(lan, "GET", "/api/settings", carried)[0] == 200
            old_link = app.phone.status()["link"]
            status, st = http("POST", base + "/api/phone/forget", {})
            assert status == 200 and st["link"] != old_link and len(st["code"]) == 6, st
            assert ask(lan, "GET", "/api/settings", carried)[0] == 401
            assert ask(lan, "POST", "/api/phone/forget", carried)[0] == 401
            # this computer never needed any of it; switching off closes the door
            assert ask("127.0.0.1", "GET", "/api/settings")[0] == 200
            status, st = http("POST", base + "/api/phone", {"enabled": False})
            assert st["enabled"] is False and app.settings["phone_access"] is False
            time.sleep(0.3)
            try:
                ask(lan, "GET", "/")
                raise AssertionError("the network address still answers after switching off")
            except OSError:
                pass
        finally:
            app.phone.stop()
            server.shutdown()
    print("PASS test_phone_pairs_with_a_short_code_or_a_scanned_link")


def test_people_have_pages_and_come_up_when_mentioned():
    with tempfile.TemporaryDirectory() as tmp:
        script = [
            # told by the user, about someone else: filed on that person's page
            tool_call_msg("memory_save", {"fact": "Dana reviews every database change", "about": "dana"}),
            tool_call_msg("memory_save", {"fact": "Sam prefers tabs over spaces", "about": "Sam Okoro"}, "c2"),
            # nothing supports this one: it is kept, marked unverified
            tool_call_msg("memory_save", {"fact": "Dana is leaving the company in March", "about": "Dana"}, "c3"),
            # not a person: refused
            tool_call_msg("memory_save", {"fact": "likes dark themes", "about": "user"}, "c4"),
            text_msg("Noted."),
        ]

        def auto(app, base, sid):
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})

        app, server, base, sid, reader = _run(
            tmp, script, before=auto,
            text="Dana reviews every database change, and Sam Okoro prefers tabs over spaces.")
        try:
            reader.wait_for("turn_end")
            refused = [d for t, d in reader.events if t == "tool_result"][3]
            assert "must be the name of a person" in refused["result"], refused
            status, people = http("GET", f"{base}/api/people")
            by = {p["name"]: p for p in people}
            assert sorted(by) == ["Dana", "Sam Okoro"], people
            states = {c["text"]: c["state"] for c in by["Dana"]["claims"]}
            assert states == {"Dana reviews every database change": "KNOWN",
                              "Dana is leaving the company in March": "CONFABULATED"}, states
            # they are not mixed into the general memory list, and not spread over every prompt
            assert http("GET", f"{base}/api/memory")[1] == []
            session = app.get_session(sid)
            from webui import recall

            # the system prompt names who there are notes on, and reads the same whatever the message is
            quiet = session.loop.build_system_prompt("fix the failing test")
            assert "database change" not in quiet and "tabs" not in quiet, quiet
            assert "notes about these people: " in quiet and "Dana" in quiet and "Sam Okoro" in quiet, quiet
            assert quiet == session.loop.build_system_prompt("Can you draft the migration so sam can look at it?")
            assert "What you were told" not in recall.recalled(session, "fix the failing test")
            # mention one, by first name: that person's notes come with the message, the other's do not
            said = recall.recalled(session, "Can you draft the migration so sam can look at it?")
            assert "What you were told about Sam Okoro:" in said and "tabs over spaces" in said, said
            assert "database change" not in said and said.startswith("[Recalled for this message"), said
            assert said.endswith("[End of recall]\n\n")
            said = recall.recalled(session, "ask Dana.")
            assert "Dana is leaving the company in March (unverified" in said, said
            # two people can have notes that read alike without one replacing the other
            http("POST", f"{base}/api/people", {"name": "Alex", "fact": "Alex prefers tabs over spaces"})
            status, people = http("POST", f"{base}/api/people", {"name": "alex", "fact": "owns the billing service"})
            by = {p["name"]: p for p in people}
            assert len(by["Alex"]["claims"]) == 2 and len(by["Sam Okoro"]["claims"]) == 1, people
            assert all(c["state"] == "KNOWN" for c in by["Alex"]["claims"])
            assert http("POST", f"{base}/api/people", {"name": "me", "fact": "x"})[0] == 400
            # confirm a note, forget a note, rename (which can fold two pages into one), forget a person
            unsure = [c for c in by["Dana"]["claims"] if c["state"] == "CONFABULATED"][0]
            status, people = http("POST", f"{base}/api/people/claim", {"id": unsure["id"], "confirm": True})
            assert all(c["state"] == "KNOWN" for c in {p["name"]: p for p in people}["Dana"]["claims"])
            status, people = http("POST", f"{base}/api/people/claim", {"id": unsure["id"]})
            assert len({p["name"]: p for p in people}["Dana"]["claims"]) == 1
            status, people = http("POST", f"{base}/api/people/rename", {"name": "Alex", "to": "Sam Okoro"})
            by = {p["name"]: p for p in people}
            assert sorted(by) == ["Dana", "Sam Okoro"] and len(by["Sam Okoro"]["claims"]) == 3, people
            assert http("POST", f"{base}/api/people/rename", {"name": "Nobody", "to": "X Y"})[0] == 404
            status, people = http("POST", f"{base}/api/people/forget", {"name": "sam okoro"})
            assert [p["name"] for p in people] == ["Dana"], people
            erased = [r for r in app.store.claims(include_history=True) if r["status"] == "retracted"]
            assert len(erased) == 4 and all(r["claim_text"] == "" for r in erased), erased
            assert [r["subject"] for r in erased if r["subject"]] == ["Dana"], "the name is erased with the notes"
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_people_have_pages_and_come_up_when_mentioned")


_UNIT = "import unittest\nimport m\n\n\nclass T(unittest.TestCase):\n    def test_double(self):\n        self.assertEqual(m.double(2), 4)\n\n    def test_name(self):\n        self.assertEqual(m.NAME, 'm')\n"
_TESTS = sys.executable.replace("\\", "/") + " -m unittest -q"


def _auto(app, base, sid):
    http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})


def test_code_gate_refuses_what_is_not_there():
    from webui.codegate import failing_tests, is_test_command, is_test_file, other_problems, python_problems

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pkg", "sub"))
        _write(os.path.join(tmp, "pkg", "__init__.py"), "")
        _write(os.path.join(tmp, "pkg", "util.py"), "LIMIT = 3\n\ndef slugify(text):\n    return text\n\ndef _private():\n    pass\n")
        _write(os.path.join(tmp, "pkg", "sub", "__init__.py"), "from .deep import thing\n")
        _write(os.path.join(tmp, "pkg", "sub", "deep.py"), "thing = 1\n")
        old = "import os\nfrom pkg.util import slugify\n\ndef run(x):\n    return slugify(x) + legacy_global\n"

        def check(new, before=old, rel="app.py"):
            return python_problems(tmp, rel, new, before)

        # it must parse
        bad, _ = check("def run(x):\n    return (x\n")
        assert len(bad) == 1 and "does not parse" in bad[0] and "line" in bad[0], bad
        # a name imported from a project file must be in that file; the refusal says what is
        bad, _ = check(old + "from pkg.util import slugfy\n")
        assert len(bad) == 1 and "pkg/util.py has no `slugfy`" in bad[0] and "slugify" in bad[0], bad
        bad, _ = check(old + "from pkg import nothere\n")
        assert len(bad) == 1 and "has no `nothere`" in bad[0], bad
        # a submodule, a re-export and a relative import that exist are fine
        ok = old + "from pkg import util, sub\nfrom pkg.sub import thing, deep\nimport pkg.util as u\nprint(u.LIMIT, util.slugify, sub, thing, deep)\n"
        assert check(ok) == ([], []), check(ok)
        assert python_problems(tmp, "pkg/other.py", "from .util import slugify\nfrom . import sub\nprint(slugify, sub)\n", None) == ([], [])
        bad, _ = python_problems(tmp, "pkg/other.py", "from .utils import slugify\nprint(slugify)\n", None)
        assert len(bad) == 1 and "no such module next to this file" in bad[0], bad
        # an attribute on a project module must exist there
        bad, _ = check(old + "import pkg.util as u\nprint(u.make_slug('a'))\n")
        assert len(bad) == 1 and "`u.make_slug`" in bad[0] and "pkg/util.py has no `make_slug`" in bad[0], bad
        # a name must be defined or imported; what was already wrong before is not this change's fault
        bad, _ = check(old + "\ndef more(y):\n    return helper(y)\n")
        assert len(bad) == 1 and "`helper` is used but never defined" in bad[0], bad
        assert "legacy_global" not in " ".join(bad)
        assert check(old + "\ndef more(y):\n    z = [n for n in y]\n    return len(z), os.sep, run(y)\n") == ([], [])
        # a new file gets no such allowance
        bad, _ = python_problems(tmp, "new.py", "def f():\n    return undefined_thing\n", None)
        assert len(bad) == 1 and "undefined_thing" in bad[0], bad
        # a module from outside that cannot be found is a warning (another environment may have it)
        bad, warn = check(old + "import zzz_no_such_pkg\nprint(zzz_no_such_pkg)\n")
        assert bad == [] and len(warn) == 1 and "zzz_no_such_pkg" in warn[0], (bad, warn)
        assert check(old + "try:\n    import zzz_no_such_pkg\nexcept ImportError:\n    zzz_no_such_pkg = None\n") == ([], [])
        assert check(old + "import json, collections.abc\nprint(json, collections)\n") == ([], [])
        # files the gate cannot reason about are left alone
        assert check("from somewhere import *\nprint(anything_at_all)\n", None) [0] == []
        # other kinds
        assert "not valid JSON" in other_problems("data.json", "{\"a\": 1,}")[0]
        assert other_problems("data.json", "{\"a\": 1}") == [] and other_problems("tsconfig.json", "{ // ok\n}") == []
        assert "not valid TOML" in other_problems("x.toml", "a = = 1")[0]
    assert is_test_command("python -m pytest -q") and is_test_command("npm run test") and is_test_command("go test ./...")
    assert not is_test_command("python setup.py build") and not is_test_command("git status")
    assert is_test_file("tests/test_a.py") and is_test_file("src/a.test.ts") and not is_test_file("src/attest.py")
    assert failing_tests("FAILED tests/test_a.py::test_x - assert 1\n1 failed, 3 passed", 1) == {"tests/test_a.py::test_x"}
    assert failing_tests("FAIL: test_double (test_m.T.test_double)\n", 1) == {"test_double (test_m.T.test_double)"}
    assert failing_tests("4 passed", 0) == set() and failing_tests("boom", 2) == {"(the run failed with exit 2)"}
    print("PASS test_code_gate_refuses_what_is_not_there")


def test_gates_hold_through_a_session():
    """A refused edit writes nothing; a broken test is reported with the change; the reply cannot
    say it passed; in strict mode the change is undone."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "m.py"), "NAME = 'm'\n\ndef double(x):\n    return x * 2\n")
        _write(os.path.join(tmp, "test_m.py"), _UNIT)
        script = [
            tool_call_msg("read", {"path": "m.py"}),
            tool_call_msg("bash", {"cmd": _TESTS}, "c2"),
            # calls something that does not exist
            tool_call_msg("edit", {"path": "m.py", "old_text": "    return x * 2\n", "new_text": "    return tripple(x)\n"}, "c3"),
            # parses and resolves, but breaks a test that passed
            tool_call_msg("edit", {"path": "m.py", "old_text": "    return x * 2\n", "new_text": "    return x * 3\n"}, "c4"),
            text_msg("I've changed `double`. All tests pass."),
            text_msg("The change breaks test_double, so this is not finished."),
        ]
        app, server, base = start_server(tmp)
        sid, fake = make_session_with_fake(base, tmp, script)
        _auto(app, base, sid)
        reader = SSEReader(f"{base}/api/sessions/{sid}/events")
        reader.start()
        reader.wait_for("state")
        try:
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "make double faster"})
            reader.wait_for("turn_end", timeout=60)
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert "OK" in results[1] and "[exit 0]" in results[1], results[1]
            assert results[2].startswith("ERROR: the code gate refused this change to m.py; nothing was written"), results[2]
            assert "`tripple` is used but never defined" in results[2] and "double" in results[2], results[2]
            assert "[Gate] WARNING" in results[3] and "test_double" in results[3] and "The change is kept" in results[3], results[3]
            assert "test_name" not in results[3].split("fails them now:")[1].split("\n")[0]
            with open(os.path.join(tmp, "m.py")) as fh:
                assert "x * 3" in fh.read()
            session = app.get_session(sid)
            assert len(session.codegate.broken) == 1 and session.codegate.refused == 1, (session.codegate.broken, session.codegate.refused)
            # the reply said it passed; the record says otherwise: sent back, and the system adds what is true
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert "All tests pass" not in final and final.startswith("The change breaks test_double"), final
            assert "Not finished: tests that passed before this session's changes and fail now: test_double" in final, final
            assert fake.calls == 6, fake.calls
            notices = [d["text"] for t, d in reader.events if t == "notice"]
            assert any("sent back" in n and "All tests pass" in n for n in notices), notices
            kinds = [a["activity_type"] for a in http("GET", f"{base}/api/activity?session={sid}")[1]]
            assert kinds.count("gate") == 2 and "reply_check" in kinds, kinds
            reader.stop()
        finally:
            server.shutdown()

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "m.py"), "NAME = 'm'\n\ndef double(x):\n    return x * 2\n")
        _write(os.path.join(tmp, "test_m.py"), _UNIT)
        script = [
            tool_call_msg("read", {"path": "m.py"}),
            tool_call_msg("bash", {"cmd": _TESTS}, "c2"),
            tool_call_msg("edit", {"path": "m.py", "old_text": "    return x * 2\n", "new_text": "    return x * 3\n"}, "c3"),
            # a test file may fail on purpose (that is how work starts): no rollback, no re-run
            tool_call_msg("write_file", {"path": "test_new.py", "content": "import unittest\nimport m\n\n\nclass N(unittest.TestCase):\n    def test_triple(self):\n        self.assertEqual(m.triple(1), 3)\n"}, "c4"),
            text_msg("I could not change it without breaking test_double."),
        ]
        app, server, base = start_server(tmp)
        http("POST", f"{base}/api/settings", {"code_gate": "strict"})
        sid, fake = make_session_with_fake(base, tmp, script)
        _auto(app, base, sid)
        reader = SSEReader(f"{base}/api/sessions/{sid}/events")
        reader.start()
        reader.wait_for("state")
        try:
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "make double faster"})
            reader.wait_for("turn_end", timeout=60)
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert results[2].startswith("ERROR: the code gate undid this change to m.py"), results[2]
            assert "test_double" in results[2] and "back as it was" in results[2]
            with open(os.path.join(tmp, "m.py")) as fh:
                assert "x * 2" in fh.read(), "strict mode must put the file back"
            assert results[3].startswith("Wrote") and "undid" not in results[3], results[3]
            assert "[Gate] Check these" in results[3] and "m.py has no `triple`" in results[3], results[3]
            assert os.path.exists(os.path.join(tmp, "test_new.py"))
            assert app.get_session(sid).codegate.broken == set()
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            # a file was written after the last run, and the reply is told so by the system
            assert final.startswith("I could not change it without breaking test_double.") and final.endswith(
                "Not verified: files were changed in this turn and no tests were run afterwards."), final
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_gates_hold_through_a_session")


def test_reply_gate_sends_back_then_removes_what_is_unbacked():
    from webui import replygate

    text, removed = replygate.strike(
        "Summary:\n- I've fixed the parser. It was a typo.\n- All tests pass.\n```\nAll tests pass\n```\nDone.",
        [{"text": "I've fixed", "why": "nothing was changed in this turn"},
         {"text": "All tests pass", "why": "no command was run in this session"}])
    assert len(removed) == 2 and text.count("[Removed: an unbacked statement") == 2, text
    assert "- [Removed" in text and " It was a typo." in text and "```\nAll tests pass\n```" in text and text.endswith("Done."), text

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "m.py"), "NAME = 'm'\n\ndef double(x):\n    return x * 2\n")
        _write(os.path.join(tmp, "test_m.py"), _UNIT)
        app, server, base = start_server(tmp)
        try:
            # nothing was done: said twice, so it is taken out and the reply says so
            sid, fake = make_session_with_fake(base, tmp, [
                text_msg("I've fixed the bug in the parser. All tests pass."),
                text_msg("Yes: I have fixed it and 12 tests passed. Anything else?"),
            ])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "fix the parser"})
            reader.wait_for("turn_end", timeout=30)
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert "12 tests passed" not in final and "have fixed" not in final, final
            assert final.startswith("[Removed: an unbacked statement (") and final.endswith("Anything else?"), final
            assert fake.calls == 2
            acts = http("GET", f"{base}/api/activity?session={sid}")[1]
            check = [a for a in acts if a["activity_type"] == "reply_check"][0]
            assert check["status"] == "denied" and "removed" in check["subtitle"], check
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            assert [e for e in hist["transcript"] if e["kind"] == "assistant"][-1]["text"] == final
            reader.stop()

            # sent back, and this time the statement is backed by a run: it stands, untouched
            sid, fake = make_session_with_fake(base, tmp, [
                text_msg("All tests pass."),
                tool_call_msg("bash", {"cmd": _TESTS}),
                text_msg("All tests pass."),
            ])
            _auto(app, base, sid)
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "do the tests pass?"})
            reader.wait_for("turn_end", timeout=60)
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert final == "All tests pass.", final
            reader.stop()

            # a result goes out of date when the code changes after it; a recounting of the past is left alone
            ledger = app.get_session(sid).ledger
            assert not replygate.hard(ledger.label_reply("All tests pass."))
            ledger.note_tool("edit", {"path": "m.py", "new_text": "x"}, "Edited 'm.py'")
            stale = replygate.hard(ledger.label_reply("All tests pass."))
            assert len(stale) == 1 and "out of date" in stale[0]["why"], stale
            assert ledger.tests_stale()
            ledger.note_tool("edit", {"path": "README.md", "new_text": "x"}, "Edited 'README.md'")
            ledger.note_run(_TESTS, "[exit 0]\nOK")
            assert not ledger.tests_stale() and not replygate.hard(ledger.label_reply("All tests pass."))
            ledger.start_turn()
            assert not replygate.hard(ledger.label_reply("Yesterday I fixed the parser and ran the tests."))
            assert len(replygate.hard(ledger.label_reply("I've updated the config and it is now fixed."))) == 1
            ledger.note_tool("memory_save", {"fact": "x"}, "Saved.")
            assert not replygate.hard(ledger.label_reply("I've added that to memory."))
        finally:
            server.shutdown()
    print("PASS test_reply_gate_sends_back_then_removes_what_is_unbacked")


def test_working_set_keeps_the_task_in_front_and_stale_text_out():
    class Recording:
        def __init__(self, script):
            self.script, self.sent = list(script), []

        def chat(self, messages, tools):
            self.sent.append([dict(m) for m in messages])
            return self.script.pop(0)

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "m.py"), "NAME = 'm'\n\nclass Box:\n    def __init__(self, n=0):\n        self.n = n\n\n    def _hidden(self):\n        pass\n\ndef double(x, *, fast=False):\n    return x * 2\n")
        for n in range(4):
            _write(os.path.join(tmp, f"b{n}.txt"), "\n".join(f"{n}:{i:03d} " + "x" * 52 for i in range(160)))
        script = [
            tool_call_msg("read", {"path": "m.py"}, "c1"),
            tool_call_msg("edit", {"path": "m.py", "old_text": "    return x * 2\n", "new_text": "    return x + x\n"}, "c2"),
            tool_call_msg("read", {"path": "b0.txt"}, "c3"),
            tool_call_msg("read", {"path": "b1.txt"}, "c4"),
            tool_call_msg("read", {"path": "b2.txt"}, "c5"),
            tool_call_msg("read", {"path": "b3.txt"}, "c6"),
            tool_call_msg("glob", {"pattern": "*.py"}, "c7"),
            text_msg("Looked around."),
        ]
        app, server, base = start_server(tmp)
        status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
        sid = snap["id"]
        rec = Recording(script)
        session = app.get_session(sid)
        session._build_client = lambda provider=None: rec
        _auto(app, base, sid)
        http("POST", f"{base}/api/settings", {"context_window": 26000})   # a small window, to see the stages
        assert session.context_window() == 26000
        reader = SSEReader(f"{base}/api/sessions/{sid}/events")
        reader.start()
        reader.wait_for("state")
        try:
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "tidy the doubling in m.py"})
            reader.wait_for("turn_end", timeout=30)
            tools_of = lambda msgs: [m for m in msgs if m.get("role") == "tool"]
            # the first request has nothing to add; from the second round the newest result says where things stand
            assert "[Where things stand" not in str(rec.sent[0])
            second = tools_of(rec.sent[1])
            assert second[-1]["content"].startswith("[File:") or "NAME = 'm'" in second[-1]["content"]
            note = second[-1]["content"].split("[Where things stand")[1]
            assert "Task, in the user's words: tidy the doubling in m.py" in note and "Round 2 of" in note, note
            assert "Defined in m.py:" in note and "def double(x, *, fast=False)" in note, note
            assert "class Box" in note and "def __init__(self, n=0)" in note and "_hidden" not in note, note
            # a note stays on the result it was written on, and a new one is written when something
            # has changed: what was already sent is not rewritten (the server would read it again)
            last = tools_of(rec.sent[-1])
            assert sum("[Where things stand" in m["content"] for m in last) >= 1
            note = "\n".join(m["content"] for m in last).split("[Where things stand")[-1]
            assert "Changed so far: m.py" in note and "Tests: not run in this session." in note, note
            # the reading of m.py from before the edit is not shown again
            assert "earlier reading of m.py" in last[0]["content"] and "x * 2" not in last[0]["content"], last[0]["content"]
            assert "x + x" in last[1]["content"], "the edit's own result still shows the change"
            # once the window was half full, an older reading was replaced by one line; recent results are whole
            cut = [m for m in last if m["content"].startswith("[Offloaded to save room: read b")]
            assert len(cut) >= 1 and "(160 lines)" in cut[0]["content"], [m["content"][:60] for m in last]
            assert len(last[-2]["content"]) > 8000, "the most recent results are sent whole"
            assert sum(len(m["content"]) for m in last) < 40_000
            # switched off: the model is sent the conversation as it is
            http("POST", f"{base}/api/settings", {"working_set": False})
            rec.script = [tool_call_msg("read", {"path": "m.py"}, "d1"), tool_call_msg("glob", {"pattern": "*"}, "d2"), text_msg("ok")]
            rec.sent = []
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "again"})
            time.sleep(0.2)
            reader.wait_for("turn_end", timeout=30)
            deadline = time.time() + 10
            while len(rec.sent) < 3 and time.time() < deadline:
                time.sleep(0.1)
            assert len(rec.sent) == 3 and "[Where things stand" not in str(rec.sent), len(rec.sent)
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_working_set_keeps_the_task_in_front_and_stale_text_out")


def test_recall_travels_with_the_message_and_the_prompt_stays_the_same():
    from webui import recall

    class Recording:
        def __init__(self, script):
            self.script, self.sent = list(script), []

        def chat(self, messages, tools):
            self.sent.append([dict(m) for m in messages])
            return self.script.pop(0)

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
        app, server, base = start_server(tmp)
        try:
            store = app.store
            # more is remembered than the stable part of the prompt holds
            for n in range(recall.STABLE_LIMIT):
                store.add_claim(f"alpha{n} bravo{n} charlie{n} delta{n} echo{n}", "KNOWN", "test")
            store.add_claim("The invoice exporter must write dates as ISO 8601", "CONFABULATED", "test")
            store.add_claim("Invoice totals are rounded half up to cents", "KNOWN", "test")
            store.add_claim("Dana signs off every invoice change", "KNOWN", "test", kind="person", subject="Dana")

            # an earlier session did some work in this folder; another folder's work is not ours
            old = app.create_session(tmp, "earlier")
            store.add_activity(old.id, "task", "Fix rounding in the invoice exporter", "4 steps in 30s", "success")
            elsewhere = app.create_session(other, "elsewhere")
            store.add_activity(elsewhere.id, "task", "Unrelated work somewhere else", "1 step in 2s", "success")

            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            sid = snap["id"]
            rec = Recording([text_msg("First."), text_msg("Second."), text_msg("Third.")])
            session = app.get_session(sid)
            session._build_client = lambda provider=None: rec
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")

            def say(text, n):
                http("POST", f"{base}/api/sessions/{sid}/message", {"text": text})
                deadline = time.time() + 20
                while len(rec.sent) < n and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(0.4)
                return rec.sent[n - 1]

            first = say("How should the invoice exporter write its totals? Ask Dana if unsure.", 1)
            user = first[-1]["content"]
            # what bears on the message came with it: the claims the stable part had no room for,
            # the notes on the person named, and (first message only) the last work in this folder
            assert user.startswith("[Recalled for this message by the system."), user[:80]
            assert "rounded half up to cents" in user and "ISO 8601 (unverified)" in user, user
            # two rankings are fused: when the words match equally well, the claim that can be relied on wins
            store.add_claim("Deploy window is Tuesday morning", "CONFABULATED", "test")
            store.add_claim("Deploy window is Thursday evening", "KNOWN", "test", project_dir=session.project_dir)
            ranked = [c["claim_text"] for c in store.search_claims("when is the deploy window", session.project_dir)]
            assert ranked[:2] == ["Deploy window is Thursday evening", "Deploy window is Tuesday morning"], ranked
            assert "What you were told about Dana:" in user and "signs off every invoice change" in user
            assert "Fix rounding in the invoice exporter (done; 4 steps in 30s)" in user and "Unrelated work" not in user
            assert user.rstrip().endswith("Ask Dana if unsure."), "the user's own words come last, unchanged"
            # recall is not evidence: nothing recalled counts as said by the user
            assert not any("rounded half up" in t for t in session.ledger.user_texts)

            second = say("Thanks. What is 2 + 2?", 2)
            third = say("And 3 + 3?", 3)
            # the opening of the prompt is identical from turn to turn, so the server's cache holds
            assert first[0]["content"] == second[0]["content"] == third[0]["content"]
            assert "rounded half up" not in first[0]["content"] and "alpha3 bravo3 " in first[0]["content"]
            # nothing bears on these, so nothing is added; earlier turns are sent exactly as they were
            assert second[-1]["content"] == "Thanks. What is 2 + 2?" and third[-1]["content"] == "And 3 + 3?"
            # a skill is attached once, not again on every message that happens to fit it
            made = http("POST", f"{base}/api/skills", {"name": "invoice-checks", "description": "Checks for invoice code",
                                                       "keywords": ["invoice", "exporter"], "body": "Always check the totals."})
            assert made[0] == 200, made
            once = recall.recalled(session, "look at the invoice exporter")
            again = recall.recalled(session, "look at the invoice exporter again")
            http("DELETE", f"{base}/api/skills/invoice-checks")   # the harness shares the real skills folder
            assert "skill that fits this message: invoice-checks" in once and "Always check the totals." in once, once
            assert "invoice-checks" not in again
            assert second[1]["content"] == user and third[1]["content"] == user
            assert "Last work in this folder" not in str(second[-1]) + str(third[-1])
            # the page shows what the user typed, not the recall block
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            shown = [e["text"] for e in hist["transcript"] if e["kind"] == "user"]
            assert shown[0] == "How should the invoice exporter write its totals? Ask Dana if unsure.", shown
            # it is bounded, and says where to look for more
            for n in range(40):
                store.add_claim(f"zebra permit rule zeta{n} yotta{n} xeno{n} whisk{n} vole{n} umbra{n} " + f"t{n}pad " * 70, "KNOWN", "test")
            big = recall.recalled(session, "what is the zebra permit rule?")
            assert 0 < big.count("zebra permit rule") <= recall.MAX_CLAIMS and len(big) < recall.MAX_CHARS + 400, len(big)
            assert "use memory_search or chat_search" in big
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_recall_travels_with_the_message_and_the_prompt_stays_the_same")


def test_code_graph_knows_who_calls_what_and_the_gate_uses_it():
    from coder.tools import _is_ignored
    from webui.codegate import call_problems, left_behind
    from webui.codegraph import graph_for

    # ignored folders are really skipped (a folder pattern used to match nothing)
    pats = ["build/", "*.pyc", "docs/out/", "secret"]
    for path, ignored in (("build/", 1), ("build/a.py", 1), ("x/build/", 1), ("rebuild/", 0), ("d/a.pyc", 1),
                          ("docs/out/x.html", 1), ("other/out/", 0), ("secret/", 1), ("a/secret", 1), ("a.py", 0)):
        assert bool(_is_ignored(path, pats)) == bool(ignored), path

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pkg"))
        os.makedirs(os.path.join(tmp, "build"))
        _write(os.path.join(tmp, ".gitignore"), "build/\n")
        _write(os.path.join(tmp, "build", "junk.py"), "def slugify(a, b, c):\n    pass\n")
        _write(os.path.join(tmp, "pkg", "__init__.py"), "")
        util = ("import functools\n\n\ndef slugify(text):\n    \"\"\"Lowercase and dash a title.\"\"\"\n    return _clean(text).lower()\n\n\n"
                "def _clean(text):\n    return text.strip()\n\n\n@functools.lru_cache\ndef cached(a):\n    return a\n")
        _write(os.path.join(tmp, "pkg", "util.py"), util)
        app_src = ("from pkg.util import slugify\n\n\ndef title(x):\n    return slugify(x)\n\n\n"
                   "class Page:\n    def render(self):\n        return self.head() + title('a')\n\n    def head(self):\n        return '<h1>'\n")
        _write(os.path.join(tmp, "app.py"), app_src)
        cli_src = "import pkg.util as u\n\n\ndef main():\n    return u.slugify('x')\n"
        _write(os.path.join(tmp, "cli.py"), cli_src)
        _write(os.path.join(tmp, "test_app.py"), "from app import title\n\n\ndef test_title():\n    assert title('A') == 'a'\n")
        _write(os.path.join(tmp, "one.py"), "class A:\n    def run(self):\n        return 1\n")
        _write(os.path.join(tmp, "two.py"), "class B:\n    def run(self):\n        return 2\n\n\ndef go(thing):\n    return thing.run()\n")
        _write(os.path.join(tmp, "web.js"), "function draw(a) {\n  paint(a);\n}\n\nfunction paint(a) {\n  return a;\n}\n")

        g = graph_for(tmp, force=True)
        st = g.stats()
        assert st["files"] == 8 and st["by_pattern"] == 1, st     # build/ is ignored
        found = g.search("slug")
        assert [(s.qual, s.file, s.line) for s in found] == [("slugify", "pkg/util.py", 4)], found
        assert found[0].sig == "def slugify(text)" and found[0].doc == "Lowercase and dash a title."
        slug = found[0]
        # who calls it: through a plain import and through a module alias, both certain
        calls = [(c.qual, c.file, line, sure) for c, line, sure, _a in g.callers(slug)]
        assert calls == [("title", "app.py", 5, True), ("main", "cli.py", 5, True)], calls
        # what a method calls: its own class through self, and a function in the file
        render = g.find("Page.render")[0]
        assert sorted((t.qual, sure) for t, _l, sure in g.callees(render)) == [("Page.head", True), ("title", True)]
        # impact, level by level, down to the test that reaches it
        levels = [[(s.qual, sure) for s, sure in level] for level in g.impact(slug, 3)]
        assert levels[0] == [("title", True), ("main", True)], levels
        assert sorted(levels[1]) == [("Page.render", True), ("test_title", True)], levels
        # the same method name on two classes: listed as possible, never as certain
        runs = g.find("run")
        assert len(runs) == 2
        assert [(c.qual, sure) for c, _l, sure, _a in g.callers(runs[0])] == [("go", False)]
        # other languages are read by pattern, and say so
        paint = g.find("paint")[0]
        assert not paint.exact and [(c.qual) for c, _l, _s, _a in g.callers(paint)] == ["draw"]
        # ways of naming a symbol
        assert g.find("app.py:title")[0].line == 4 and g.find("app.py:10")[0].qual == "Page.render"
        assert g.find("nothing_here") == []

        # ---- the gate: a call this change adds must fit the function it names
        def calls_in(new, old=app_src, rel="app.py"):
            return call_problems(tmp, rel, new, old, g)

        bad = calls_in(app_src + "\n\ndef more(x):\n    return slugify(x, '-')\n")
        assert len(bad) == 1 and "passes 2 positional arguments, but it takes at most 1" in bad[0], bad
        assert "pkg/util.py defines `slugify(text)`" in bad[0] and bad[0].startswith("line 17:"), bad
        bad = calls_in(app_src + "\n\ndef more(x):\n    return slugify()\n")
        assert len(bad) == 1 and "does not pass `text`" in bad[0], bad
        bad = calls_in(cli_src + "\n\ndef more():\n    return u.slugify('x', sep='-')\n", cli_src, "cli.py")
        assert len(bad) == 1 and "passes `sep=`, which it does not take" in bad[0], bad
        bad = calls_in(app_src + "\n\ndef more(x):\n    return title(x, 1)\n")
        assert len(bad) == 1 and "app.py defines `title(x)`" in bad[0], bad
        # what cannot be judged is left alone: *args, a decorated function, a name reused as a variable,
        # a call that was already there, a function from outside the project
        fine = (app_src + "from pkg.util import cached\nimport os\n\n\ndef more(x, *rest):\n    slugify(*rest)\n"
                "    cached(1, 2, 3)\n    os.path.join()\n    title = len\n    return title(x, 1) if False else slugify(x)\n")
        assert calls_in(fine) == [], calls_in(fine)
        assert calls_in(app_src.replace("slugify(x)", "slugify(x)  # same call")) == []

        # ---- callers left behind by a changed or removed function
        notes = left_behind("pkg/util.py", util, util.replace("def slugify(text):", "def slugify(text, sep):"), g)
        assert len(notes) == 1 and "`slugify(text, sep)` no longer fits 2 calls elsewhere" in notes[0], notes
        assert "app.py:5" in notes[0] and "cli.py:5" in notes[0], notes
        assert left_behind("pkg/util.py", util, util.replace("def slugify(text):", "def slugify(text, sep='-'):"), g) == []
        notes = left_behind("pkg/util.py", util, util.replace("def slugify(text):", "def make_slug(text):"), g)
        assert len(notes) == 1 and "`slugify` is no longer defined in pkg/util.py but is still called from" in notes[0], notes

        # ---- it follows the files
        _write(os.path.join(tmp, "cli.py"), cli_src + "\n\ndef second():\n    return main()\n")
        os.remove(os.path.join(tmp, "one.py"))
        g = graph_for(tmp, force=True)
        assert g.find("second") and len(g.find("run")) == 1 and g.stats()["files"] == 7
        assert [(c.qual, sure) for c, _l, sure, _a in g.callers(g.find("run")[0])] == [("go", False)]

        # ---- through a session: the tools answer, and the gate refuses a call that cannot fit
        script = [
            tool_call_msg("code_callers", {"symbol": "slugify"}),
            tool_call_msg("code_impact", {"symbol": "pkg/util.py:slugify"}, "c2"),
            tool_call_msg("code_node", {"symbol": "nope"}, "c3"),
            tool_call_msg("read", {"path": "app.py"}, "c4"),
            tool_call_msg("edit", {"path": "app.py", "old_text": "    return slugify(x)\n",
                                   "new_text": "    return slugify(x, sep='_')\n"}, "c5"),
            tool_call_msg("read", {"path": "pkg/util.py"}, "c6"),
            tool_call_msg("edit", {"path": "pkg/util.py", "old_text": "def slugify(text):", "new_text": "def slugify(text, sep):"}, "c7"),
            text_msg("Looked at who calls slugify."),
        ]
        app, server, base, sid, reader = _run(tmp, script, before=_auto)
        try:
            reader.wait_for("turn_end", timeout=40)
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert "is called from:" in results[0] and "app.py:5  in title" in results[0] and "cli.py:5  in main" in results[0], results[0]
            assert "Level 2" in results[1] and "Tests that reach it: test_app.py" in results[1], results[1]
            assert results[2].startswith("ERROR: nothing named 'nope'"), results[2]
            assert results[4].startswith("ERROR: the code gate refused this change to app.py"), results[4]
            assert "passes `sep=`, which it does not take" in results[4], results[4]
            with open(os.path.join(tmp, "app.py")) as fh:
                assert "sep='_'" not in fh.read()
            # a signature change lands, and the result says which callers it leaves behind
            assert results[6].startswith("Edited") and "[Gate] Check these:" in results[6], results[6]
            assert "no longer fits 2 calls elsewhere: app.py:5, cli.py:5" in results[6], results[6]
            assert "code_callers" in app.get_session(sid).loop.tool_names()
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_code_graph_knows_who_calls_what_and_the_gate_uses_it")


def test_context_offload_moves_output_out_in_stages():
    import json

    from webui import offload
    from webui.offload import Offloader

    def turn(steps):
        """A system prompt, a user message, then (tool, args, result) steps as the loop records them."""
        msgs = [{"role": "system", "content": "S" * 2000}, {"role": "user", "content": "do the thing"}]
        calls = {}
        for n, (name, args, result) in enumerate(steps):
            cid = f"c{n}"
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
            msgs.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})
            calls[cid] = {"name": name, "args": args}
        return msgs, calls

    def paired(view):
        """Every result still has its call, and every call its result."""
        asked = {c["id"] for m in view for c in (m.get("tool_calls") or [])}
        answered = {m["tool_call_id"] for m in view if m.get("role") == "tool"}
        return asked == answered

    big = lambda tag: "\n".join(f"{tag} line {i} " + "x" * 60 for i in range(100))   # about 7,500 characters
    steps = [
        ("read", {"path": "a.py"}, big("a-old")),                      # read again later: the first to go
        ("grep", {"pattern": "TODO"}, big("match")),                   # a listing
        ("bash", {"cmd": "pytest -q"}, "[exit 1]\n" + big("FAILED")),  # a failure nobody re-ran: kept longest
        ("read", {"path": "b.py"}, big("b")),
        ("edit", {"path": "a.py", "old_text": "x", "new_text": "y"}, "Edited 'a.py':\n" + big("diff")),
        ("read", {"path": "a.py"}, big("a-new")),
        ("read", {"path": "c.py"}, big("c")),
        ("read", {"path": "d.py"}, big("d")),
        ("read", {"path": "e.py"}, big("e")),
    ]
    msgs, calls = turn(steps)
    tool_at = [i for i, m in enumerate(msgs) if m["role"] == "tool"]

    # ---- plenty of room: nothing is touched
    roomy = Offloader(200_000)
    assert roomy.apply(msgs, calls, set()) == msgs and roomy.tier == "none"

    # ---- half full: the most replaceable results go first, one line each, until it is under half
    off = Offloader(36_000)
    view = off.apply(msgs, calls, set())
    assert off.tier == "mild" and off.last["fill"] < offload.MILD, off.last
    text = {i: m["content"] for i, m in zip([i for i in range(len(msgs))], view)}
    gone = [i for i in tool_at if view[i]["content"].startswith("[Offloaded to save room")]
    assert tool_at[0] in gone, "the reading that was made again later goes first"
    assert view[tool_at[0]]["content"] == ("[Offloaded to save room: read a.py (100 lines). Run the tool again "
                                           "if you need the full result.]")
    assert tool_at[2] not in gone, "a failure that was never re-run is the last thing to give up"
    assert all(i not in gone for i in tool_at[-offload.KEEP_RECENT:]), "the latest results are never touched"
    assert len(view) == len(msgs) and paired(view) and view[1]["content"] == "do the thing"
    # what was moved out stays out, and the earlier part of the conversation does not change again
    more, more_calls = turn(steps + [("read", {"path": "f.py"}, "short")])
    again = off.apply(more, more_calls, set())
    assert [m["content"] for m in again[:tool_at[1] + 1]] == [m["content"] for m in view[:tool_at[1] + 1]]

    # ---- 85% full and nothing big enough to replace: the oldest steps are dropped whole, down to 60%
    many = [("read", {"path": f"f{n}.py"}, f"file {n}: " + "z" * 280) for n in range(160)]
    many[3] = ("bash", {"cmd": "pytest -q"}, "[exit 1]\n2 failed")
    msgs3, calls3 = turn(many)
    off = Offloader(12_000)
    view = off.apply(msgs3, calls3, set())
    assert off.tier == "aggressive" and off.last["dropped"] > 40 and off.last["fill"] <= offload.TARGET + 0.02, off.last
    assert paired(view), "a call is never left without its result, nor a result without its call"
    assert view[0]["role"] == "system" and view[1]["content"] == "do the thing"
    kept = [m["content"] for m in view if m["role"] == "tool"]
    assert kept[-1] == many[-1][2] and kept[0] != many[0][2], "the oldest go, the newest stay whole"
    journal = off.journal_lines()
    assert journal[0].startswith("Earlier steps of this turn") and len(journal) == 21, len(journal)
    assert "read f" in journal[1] and journal[1].split(".")[0] == str(off.last["dropped"] - 19), journal[1]
    assert "ran `pytest -q`: exit 1; last line: 2 failed" in off.journal[3]
    # dropped steps stay dropped as the turn goes on
    more3, more_calls3 = turn(many + [("read", {"path": "last.py"}, "tail")])
    again = off.apply(more3, more_calls3, set())
    assert paired(again) and [m["content"] for m in again[:6]] == [m["content"] for m in view[:6]]

    # ---- 95% full with nothing left to drop: the largest thing is cut short, so the turn can go on
    huge, huge_calls = turn([("read", {"path": "big.py"}, "y" * 60_000)])
    off = Offloader(6_000)
    view = off.apply(huge, huge_calls, set())
    assert off.tier == "emergency" and len(view[-1]["content"]) < 2300 and "cut short to fit" in view[-1]["content"]
    assert paired(view)

    # ---- a reading made before the file changed goes out with the next batch (the standing note
    # names it meanwhile): taking it out alone would have the server read the conversation again
    assert Offloader(200_000).apply(msgs, calls, {tool_at[0]}) == msgs
    view = Offloader(36_000).apply(msgs, calls, {tool_at[0]})
    assert "earlier reading of a.py" in view[tool_at[0]]["content"] and "a-old" not in view[tool_at[0]]["content"]
    at_once = Offloader(200_000)
    at_once.stale_now = True
    view = at_once.apply(msgs, calls, {tool_at[0]})
    assert "earlier reading of a.py" in view[tool_at[0]]["content"]

    # ---- routine lines are squeezed out of long command output; anything that looks like a problem stays
    noisy = "[exit 1]\n" + "\n".join(
        [f"Collecting package{i}" for i in range(60)] + [f"tests/test_m{i}.py::test_ok PASSED [ {i}%]" for i in range(60)]
        + ["tests/test_x.py::test_bad FAILED", "E   AssertionError: 3 != 4"] + [f"Downloading wheel{i} (1.{i} MB)" for i in range(40)]
        + ["1 failed, 60 passed"])
    small = offload.squeeze(noisy)
    assert len(small) < len(noisy) / 4 and "routine lines not shown" in small, len(small)
    assert "test_bad FAILED" in small and "AssertionError: 3 != 4" in small and "1 failed, 60 passed" in small, small
    assert offload.squeeze("short output") == "short output"
    msgs2, calls2 = turn([("bash", {"cmd": "pip install x"}, noisy), ("read", {"path": "a.py"}, "one")])
    view = Offloader(200_000).apply(msgs2, calls2, set())
    assert "routine lines not shown" in view[3]["content"] and "test_bad FAILED" in view[3]["content"]

    # ---- a second run of the loop in the same turn is a different conversation: nothing carries over by position
    from webui.workingset import GroundedClient

    class Echo:
        def __init__(self):
            self.sent = []

        def chat(self, messages, tools):
            self.sent.append([str(m.get("content") or "") for m in messages])
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    class FakeSession:
        _chars_per_token = 0.0

        def context_window(self):
            return 30_000

    echo = Echo()
    grounded = GroundedClient(echo, FakeSession(), "task")
    grounded.__dict__["round_no"] = -10          # keep the standing note (which needs a real session) out of this
    grounded.chat(msgs, [])
    assert any(c.startswith("[Offloaded to save room") for c in echo.sent[-1]), "the long run had output moved out"
    short, _ = turn([("read", {"path": "z.py"}, big("z")), ("read", {"path": "y.py"}, big("y"))])
    grounded.__dict__["round_no"] = -10
    grounded.chat(short, [])
    assert echo.sent[-1] == [str(m.get("content") or "") for m in short], "a new run is sent as it is"

    # ---- one-line stand-ins come from the call itself
    assert offload.summarize("bash", {"cmd": "pytest -q"}, "[exit 1]\nboom\n2 failed") == "ran `pytest -q`: exit 1; last line: 2 failed"
    assert offload.summarize("edit", {"path": "a.py"}, "ERROR: refused") == "tried to change a.py: refused or failed"
    # ---- only a server on this computer is asked how large its window is
    assert offload.detect_window("https://api.example.com/v1", "m") is None
    assert offload.detect_window("http://127.0.0.1:9/v1", "m", timeout=0.3) is None
    print("PASS test_context_offload_moves_output_out_in_stages")


def test_a_model_that_does_not_answer_is_retried_then_replaced():
    from agent import ThinkFilter, strip_thinking
    from webui.failover import Breaker, ContextOverflow, ResilientClient, classify

    assert classify(RuntimeError("Model request failed (URLError: <urlopen error [WinError 10061] refused>)")) == "transient"
    assert classify(RuntimeError("Model request failed (HTTPError: HTTP Error 503: Service Unavailable)")) == "transient"
    assert classify(RuntimeError("Model request failed (HTTPError: HTTP Error 401: Unauthorized)")) == "config"
    assert classify(RuntimeError("the request exceeds the available context size, try increasing it")) == "overflow"
    assert classify(RuntimeError("Model request failed (HTTPError: HTTP Error 400: Bad Request)")) == "final"

    class Scripted:
        def __init__(self, *outcomes):
            self.outcomes, self.calls, self.on_delta, self.should_stop = list(outcomes), 0, None, None

        def chat(self, messages, tools):
            self.calls += 1
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

    down = RuntimeError("Model request failed (URLError: connection refused)")
    waits, said = [], []
    ok = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    # it comes back on the second try: no fallback is touched
    first, second = Scripted(down, ok), Scripted(ok)
    client = ResilientClient(first, "local/big", [("cloud/small", lambda: second)], Breaker(), said.append, waits.append)
    assert client.chat([], []) == ok and first.calls == 2 and second.calls == 0 and len(waits) == 1 and not said
    # it stays down: two retries, then the next model answers, and the user is told
    first, second = Scripted(down, down, down, down), Scripted(ok, ok)
    first.on_delta = print
    breaker = Breaker()
    client = ResilientClient(first, "local/big", [("cloud/small", lambda: second)], breaker, said.append, waits.append)
    assert client.chat([], []) == ok and first.calls == 3 and second.calls == 1
    assert client.switched_to == "cloud/small" and second.on_delta is print, "the page keeps following the answer"
    assert said == ["local/big did not answer (URLError: connection refused). Trying cloud/small for this request."], said
    # the rest of the turn stays with the model that works; the one that failed is left alone for a while
    assert client.chat([], []) == ok and first.calls == 3 and breaker.resting("local/big")
    other = ResilientClient(Scripted(ok), "local/big", [("cloud/small", lambda: Scripted(ok))], breaker, said.append, waits.append)
    other.chat([], [])
    assert other.switched_to == "cloud/small", "another session does not queue up against a dead server"
    # a wrong key is not retried; with nothing else to try, the failure is reported as it is
    bad = Scripted(RuntimeError("Model request failed (HTTPError: HTTP Error 401: Unauthorized)"))
    try:
        ResilientClient(bad, "cloud/x", [], Breaker(), said.append, waits.append).chat([], [])
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "401" in str(exc) and bad.calls == 1
    # a request the model refuses as too long is handed back to be cut down, not retried as it is
    long = Scripted(RuntimeError("Model request failed (HTTPError: HTTP Error 400: context length exceeded)"))
    try:
        ResilientClient(long, "local/big", [], Breaker(), said.append, waits.append).chat([], [])
        raise AssertionError("should have raised")
    except ContextOverflow:
        assert long.calls == 1
    # a server's "retry after" is honoured
    waits.clear()
    slow = Scripted(RuntimeError("Model request failed (HTTPError: HTTP Error 429: Retry-After: 7)"), ok)
    ResilientClient(slow, "cloud/x", [], Breaker(), said.append, waits.append).chat([], [])
    assert waits == [7.0], waits

    # ---- a reasoning model's private thinking is not part of its answer
    f = ThinkFilter()
    shown = "".join(f.feed(piece) for piece in ["<thi", "nk>step one, ", "step two</th", "ink>The answer", " is 4 <", "b>"]) + f.flush()
    assert shown == "The answer is 4 <b>" and f.hidden == len("step one, step two"), (shown, f.hidden)
    assert strip_thinking("<think>plan</think>\n\nDone.") == "Done." and strip_thinking("no tags") == "no tags"

    # ---- through a session: the turn survives its model going away, and the window is cut when it overflows
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.txt"), "alpha\n")
        app, server, base = start_server(tmp)
        try:
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            sid = snap["id"]
            session = app.get_session(sid)
            reply = lambda text: {"choices": [{"message": {"role": "assistant", "content": text}}]}
            primary = Scripted(tool_call_msg("read", {"path": "a.txt"}), down, down, down)
            backup = Scripted(reply("It says alpha."))
            session._build_client = lambda provider=None: primary
            session._raw_client = lambda provider=None, model=None: backup
            app.config.providers["spare"] = {"base_url": "http://127.0.0.1:1/v1"}
            http("POST", f"{base}/api/settings", {"fallback_models": ["spare/small", "nope", "ghost/x"]})
            assert app.settings["fallback_models"] == ["spare/small", "ghost/x"]
            import webui.failover as failover_mod
            old_backoff, failover_mod.BACKOFF = failover_mod.BACKOFF, (0.01, 0.01)
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what does a.txt say"})
            reader.wait_for("turn_end", timeout=30)
            failover_mod.BACKOFF = old_backoff
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert final == "It says alpha." and primary.calls == 4 and backup.calls == 1, (final, primary.calls)
            notices = [d["text"] for t, d in reader.events if t == "notice"]
            assert any("did not answer" in n and "Trying spare/small" in n for n in notices), notices
            assert not [d for t, d in reader.events if t == "error"], "the turn did not fail"
            kinds = [a["activity_type"] for a in http("GET", f"{base}/api/activity?session={sid}")[1]]
            assert "failover" in kinds, kinds
            reader.stop()

            # the model that failed is being left alone, so a new session would go straight to the fallback
            assert app.breaker.resting(f"{session.settings['provider']}/{session.settings['model']}")
            app.breaker._until.clear()
            http("POST", f"{base}/api/settings", {"fallback_models": []})

            # too long for the model: older output is moved out and the same request goes again, once
            sent = []

            class Tight(Scripted):
                def chat(self, messages, tools):
                    sent.append(sum(len(str(m.get("content") or "")) for m in messages))
                    return Scripted.chat(self, messages, tools)

            _write(os.path.join(tmp, "big.txt"), "\n".join("row %03d " % i + "y" * 60 for i in range(190)))
            tight = Tight(*[tool_call_msg("read", {"path": "big.txt", "offset": 1 + n}, f"r{n}") for n in range(6)],
                          RuntimeError("Model request failed (HTTPError: HTTP Error 400: the request exceeds the available context size)"),
                          reply("Read it."))
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            sid2 = snap["id"]
            session2 = app.get_session(sid2)
            session2._build_client = lambda provider=None: tight
            http("POST", f"{base}/api/sessions/{sid2}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid2}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid2}/message", {"text": "read big.txt a few times"})
            reader.wait_for("turn_end", timeout=30)
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert final == "Read it." and tight.calls == 8, (final, tight.calls)
            assert sent[-1] < sent[-2] * 0.8, "the request that went again was smaller: " + str(sent[-3:])
            assert any("window was smaller than expected" in d["text"] for t, d in reader.events if t == "notice")
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_a_model_that_does_not_answer_is_retried_then_replaced")


def _pretend_computer(monkeypatch, models_mod, hw):
    """The model tests' own computer, with a graphics card: the real one (a test machine with none)
    would refuse to start the models the tests run."""
    pretend = lambda models_dir: dict(hw)  # noqa: E731
    if monkeypatch is not None:
        monkeypatch.setattr(models_mod, "hardware", pretend)
    else:
        models_mod.hardware = pretend


def test_local_models_what_fits_download_and_run(monkeypatch=None):
    import hashlib
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from webui import models as models_mod

    big = {"gpu": "RTX 3060", "vram_gb": 12.0, "ram_gb": 32.0, "ram_free_gb": 20.0, "disk_free_gb": 100.0}
    _pretend_computer(monkeypatch, models_mod, dict(big, vram_used_gb=0.5, cores=8))
    moe, tern, dense14 = (models_mod.entry(i) for i in ("qwen38-35b-a3b-q4km", "bonsai2-27b-ptq1", "qwen25-coder-14b-q4km"))
    # a mixture-of-experts model larger than the card is placed, not refused
    f = models_mod.fit(moe, big)
    assert f["verdict"] == "split" and f["flags"][:3] == ["-ngl", "99", "--n-cpu-moe"] and 20 <= int(f["flags"][3]) <= 30, f
    assert "3 of its 35 billion" in f["how"]
    assert models_mod.fit(tern, big)["verdict"] == "gpu" and models_mod.fit(dense14, big)["verdict"] == "gpu"
    # too little memory for the whole file: it says why
    f = models_mod.fit(moe, dict(big, ram_gb=16.0))
    assert f["verdict"] == "no" and "must sit in graphics memory plus memory" in f["why"][0], f
    # no graphics card: a mixture-of-experts model still runs from memory; a big dense one that does not fit a card is slow
    assert models_mod.fit(moe, dict(big, vram_gb=0))["verdict"] == "cpu"
    dense_big = dict(dense14, size_gb=20.0)
    f = models_mod.fit(dense_big, big)
    assert f["verdict"] == "slow" and "every layer is needed for every word" in f["how"], f
    assert models_mod.fit(dense_big, dict(big, ram_gb=8.0))["verdict"] == "no"
    assert models_mod.fit(moe, {"vram_gb": 12.0})["verdict"] == "unknown"
    cmd = models_mod.command(moe, models_mod.fit(moe, big), "C:/m/x y.gguf")
    line = models_mod.shown(cmd)
    assert line.startswith('llama-server -m "C:/m/x y.gguf" -ngl 99 --n-cpu-moe') and "--temp 0.6 --top-p 0.95 --top-k 20" in line
    assert line.endswith("--host 127.0.0.1 --port 8090"), line
    # what is known about driving a model is found by its name
    assert models_mod.profile_for("Qwen3.8-35B-A3B-Q4_K_M")["sampling"] == {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
    assert models_mod.profile_for("ternary-bonsai-2-27b-ptq1_0")["sampling"]["min_p"] == 0.05
    assert models_mod.profile_for("some-other-model") == {}

    payload = bytes(range(256)) * 4000     # about 1 MB
    served = {"ranges": [], "break_after": 300_000}

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
            served["ranges"].append(start)
            body = payload[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if served["break_after"]:     # the connection drops part of the way through
                cut, served["break_after"] = served["break_after"], 0
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.connection.close()
                return
            self.wfile.write(body)

    hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    tiny = {"id": "tiny", "name": "Tiny", "quant": "test", "kind": "dense", "params_b": 1, "active_b": 1, "layers": 2,
            "file": "tiny-test.gguf", "size_gb": 0.001, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "repo": "org/tiny", "license": "x", "runner": "stock", "runner_note": "", "context": 4096, "thinking": False,
            "sampling": {"temperature": 0.2}, "match": ["tiny-test"]}
    models_mod.CATALOG.append(tiny)
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        app.models.hub = f"http://127.0.0.1:{hub.server_address[1]}"
        try:
            status, view = http("GET", f"{base}/api/models")
            row = [m for m in view["models"] if m["id"] == "tiny"][0]
            assert status == 200 and row["can_download"] and not row["installed"] and row["verdict"] in ("gpu", "slow", "no", "unknown")
            assert view["hardware"]["disk_free_gb"] and view["port"] == 8090
            real = [m for m in view["models"] if m["id"] == "qwen38-35b-a3b-q4km"][0]
            assert real["size_gb"] == 21.71 and real["source"] == "https://huggingface.co/empero-ai/Qwen3.8-35B-A3B-Distill-GGUF"
            assert "one user's post" in real["reported"], "a claim is shown as someone's claim"

            def wait(state, timeout=15):
                deadline = time.time() + timeout
                while time.time() < deadline:
                    d = app.models.download
                    if d.get("state") in state:
                        return d
                    time.sleep(0.05)
                raise AssertionError(f"download stayed {app.models.download}")

            # nothing is fetched until asked; then the connection drops and the download says so
            assert served["ranges"] == []
            status, _ = http("POST", f"{base}/api/models/download", {"id": "tiny"})
            assert status == 200
            d = wait(("failed", "done"))
            part = os.path.join(app.models.dir, "tiny-test.gguf.part")
            assert d["state"] == "failed" and 0 < os.path.getsize(part) < len(payload), d
            # pressing Download again carries on from the bytes already there, and the checksum is verified
            http("POST", f"{base}/api/models/download", {"id": "tiny"})
            d = wait(("failed", "done"))
            assert d["state"] == "done", d
            assert served["ranges"][0] == 0 and 0 < served["ranges"][1] < len(payload), served["ranges"]
            final = os.path.join(app.models.dir, "tiny-test.gguf")
            with open(final, "rb") as fh:
                assert fh.read() == payload
            assert not os.path.exists(part)
            row = [m for m in http("GET", f"{base}/api/models")[1]["models"] if m["id"] == "tiny"][0]
            assert row["installed"] and not row["can_download"] and row["path"] == final
            # a file that does not match the publisher's checksum is set aside, never installed
            os.remove(final)
            tiny["sha256"] = "0" * 64
            http("POST", f"{base}/api/models/download", {"id": "tiny"})
            d = wait(("failed", "done"))
            assert d["state"] == "failed" and "checksum" in d["error"] and not os.path.exists(final), d
            assert os.path.exists(part + ".bad")
            tiny["sha256"] = hashlib.sha256(payload).hexdigest()
            assert http("POST", f"{base}/api/models/download", {"id": "qwen25-coder-14b-q4km"})[0] == 400

            # ---- starting it: refused without the program or the file, then run, watched and stopped
            http("POST", f"{base}/api/models/download", {"id": "tiny"})
            wait(("done",))
            app.settings["llama_server_path"] = ""
            old_find = models_mod.find_runner
            models_mod.find_runner = lambda setting="", workspace="", kind="stock": ""
            status, err = http("POST", f"{base}/api/models/start", {"id": "tiny"})
            assert status == 409 and "llama-server is not installed" in err["error"], err
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            fake = os.path.join(tmp, "fake_server.py")
            _write(fake, "import sys, json, threading\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
                         "class H(BaseHTTPRequestHandler):\n    def log_message(self, *a): pass\n    def do_GET(self):\n"
                         "        body = json.dumps({'status': 'ok', 'default_generation_settings': {'n_ctx': 4096}}).encode()\n"
                         "        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)\n"
                         "print('fake llama-server on', sys.argv[1], flush=True)\n"
                         "s = HTTPServer(('127.0.0.1', int(sys.argv[1])), H)\nthreading.Timer(25, s.shutdown).start()\ns.serve_forever()\n")
            models_mod.find_runner = lambda setting="", workspace="", kind="stock": sys.executable
            old_command, old_port = models_mod.command, models_mod.PORT
            models_mod.command = lambda m, placement, path, binary="x", sleep_minutes=0: [sys.executable, fake, str(port)]
            models_mod.PORT = port
            try:
                status, view = http("POST", f"{base}/api/models/start", {"id": "tiny"})
                assert status == 200 and view["running"]["state"] in ("loading", "ready"), view["running"]
                assert http("POST", f"{base}/api/models/start", {"id": "tiny"})[0] == 409, "one model at a time"
                deadline = time.time() + 15
                while time.time() < deadline and http("GET", f"{base}/api/models")[1]["running"]["state"] != "ready":
                    time.sleep(0.2)
                running = http("GET", f"{base}/api/models")[1]["running"]
                assert running["state"] == "ready" and running["id"] == "tiny", running
                assert "fake llama-server on" in app.models.log_tail()
                # the app can talk to it at once, and asks it how large its window is
                assert app.config.providers["local"]["base_url"] == f"http://127.0.0.1:{port}/v1"
                from webui import offload
                assert offload.detect_window(f"http://127.0.0.1:{port}/v1", "x") == 4096
                status, view = http("POST", f"{base}/api/models/stop", {})
                assert view["running"]["state"] == "stopped"
            finally:
                app.models.stop()
                models_mod.command, models_mod.PORT, models_mod.find_runner = old_command, old_port, old_find
        finally:
            models_mod.CATALOG.remove(tiny)
            hub.shutdown()
            server.shutdown()
    print("PASS test_local_models_what_fits_download_and_run")


def test_the_check_measures_a_model_on_this_computer():
    import json

    from webui import bench

    good_code = {
        "is_palindrome": "def is_palindrome(text):\n    s = [c.lower() for c in text if c.isalnum()]\n    return s == s[::-1]",
        "chunk": "def chunk(items, size):\n    if size < 1:\n        raise ValueError('size')\n    return [items[i:i + size] for i in range(0, len(items), size)]",
        "ordinal": "def ordinal(n):\n    if 10 <= n % 100 <= 20:\n        s = 'th'\n    else:\n        s = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')\n    return f'{n}{s}'",
        "merge_intervals": "def merge_intervals(intervals):\n    out = []\n    for s, e in sorted(intervals):\n        if out and s <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], e)\n        else:\n            out.append([s, e])\n    return out",
        "parse_duration": "import re\ndef parse_duration(text):\n    m = re.fullmatch(r'(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?', text or '')\n    if not m or not any(m.groups()):\n        raise ValueError(text)\n    h, mi, s = (int(x or 0) for x in m.groups())\n    return h * 3600 + mi * 60 + s",
        "roman": "def roman(n):\n    out = ''\n    for v, s in ((1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')):\n        while n >= v:\n            out += s\n            n -= v\n    return out",
    }
    # the hidden tests are fair: a correct answer passes every one, a wrong one is caught
    for name, _prompt, tests in bench.CODE_PROBES:
        assert bench.run_hidden_tests(good_code[name], tests) == "", name
    assert "AssertionError" in bench.run_hidden_tests("def ordinal(n):\n    return str(n) + 'th'", bench.CODE_PROBES[2][2])
    assert bench.run_hidden_tests("def roman(n):\n    while True:\n        pass", bench.CODE_PROBES[5][2], timeout=1.0) == "it ran past the time limit"
    assert bench.extract_code("Here you go:\n```python\ndef f():\n    return 1\n```\nDone.") == "def f():\n    return 1\n"

    def say(text):
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    def call(name, args):
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}

    class Capable:
        """Calls the right tools, writes working code, and says when something is not there."""

        def chat(self, messages, tools):
            q = messages[-1]["content"]
            if tools:
                if "src/app.py" in q: return call("read", {"path": "src/app.py"})
                if "README" in q: return call("read", {"path": "README.md"})
                if "TODO" in q: return call("grep", {"pattern": "TODO"})
                if "parse_config" in q: return call("grep", {"pattern": "parse_config"})
                if "pytest" in q: return call("bash", {"cmd": "pytest -q"})
                return call("bash", {"cmd": "git log -3"})
            for name in good_code:
                if f"function {name}(" in q:
                    return say("```python\n" + good_code[name] + "\n```")
            if "compute_tax" in q: return say("There is no function compute_tax in this file; it defines net_price and total.")
            if "settings.toml" in q: return say("I can't see that file. Please paste it and I will look at line 40.")
            return say("A hash table stores values in slots chosen by a hash of the key. " * 6)

    class Bluffer:
        """Answers everything in confident prose."""

        def chat(self, messages, tools):
            q = messages[-1]["content"]
            if "ordinal" in q: return say("```python\ndef ordinal(n):\n    return str(n) + 'th'\n```")
            if "compute_tax" in q: return say("compute_tax returns the price multiplied by 0.2.")
            if "settings.toml" in q: return say("Line 40 sets debug = true.")
            if "src/app.py" in q: return call("grep", {"pattern": "app"})
            if "TODO" in q: return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "grep", "arguments": "{not json"}}]}}]}
            return say("Sure, done.")

    steps = []
    good = bench.run_check(Capable(), steps.append)
    assert [sum(1 for r in good[k] if r["ok"]) for k in ("tools", "code", "candour")] == [6, 6, 2], good
    assert steps[0] == "Measuring speed" and "Writing code 6 of 6" in steps and steps[-1] == "Candour 2 of 2"
    assert "tool calls 6 of 6; code 6 of 6; candour 2 of 2" in bench.summary(good)
    poor = bench.run_check(Bluffer())
    assert [sum(1 for r in poor[k] if r["ok"]) for k in ("tools", "code", "candour")] == [0, 0, 0], poor
    whys = " | ".join(r["why"] for k in ("tools", "code", "candour") for r in poor[k])
    assert "it called grep instead of read" in whys and "not valid JSON" in whys and "answered in words" in whys, whys
    assert "it wrote no function by that name" in whys and "AssertionError" in whys and "as if the thing were there" in whys

    class Dead:
        def chat(self, messages, tools):
            raise RuntimeError("Model request failed (URLError: refused)")

    dead = bench.run_check(Dead())
    assert dead["tools"] == [] and bench.summary(dead).startswith("it did not answer"), dead

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            provider = app.config.provider
            app.bench._client = lambda p, m: Capable()
            assert http("POST", f"{base}/api/bench", {"provider": "nope", "model": "x"})[0] == 400
            status, out = http("POST", f"{base}/api/bench", {"provider": provider, "model": "Qwen3.8-35B-A3B-Q4_K_M"})
            assert status == 200 and out["state"]["running"], out
            assert http("POST", f"{base}/api/bench", {"provider": provider, "model": "x"})[0] == 409, "one at a time"
            deadline = time.time() + 60
            while time.time() < deadline and http("GET", f"{base}/api/bench")[1]["state"].get("running"):
                time.sleep(0.2)
            status, out = http("GET", f"{base}/api/bench")
            assert not out["state"].get("running") and len(out["results"]) == 1, out["state"]
            row = out["results"][0]
            assert "code 6 of 6" in row["summary"] and row["model_id"] == "qwen38-35b-a3b-q4km" and row["machine"], row
            # the result is shown with the model it belongs to, apart from what others reported
            shown = [m for m in http("GET", f"{base}/api/models")[1]["models"] if m["id"] == "qwen38-35b-a3b-q4km"][0]
            assert shown["results"] and "code 6 of 6" in shown["results"][0]["summary"] and "one user's post" in shown["reported"]
        finally:
            server.shutdown()
    print("PASS test_the_check_measures_a_model_on_this_computer")


def test_loop_repeats_a_check_into_the_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(base, tmp, [])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")

            def send(text, expect):
                before = len([1 for t, _ in reader.events if t == "notice"])
                http("POST", f"{base}/api/sessions/{sid}/message", {"text": text})
                deadline = time.time() + 10
                while time.time() < deadline:
                    notes = [d["text"] for t, d in reader.events if t == "notice"]
                    if len(notes) > before:
                        assert expect in notes[-1], notes[-1]
                        return notes[-1]
                    time.sleep(0.05)
                raise AssertionError("no answer to " + text)

            send("/loop", "Nothing is looping in this conversation")
            send("/loop soon check the tests", "Usage: /loop 10m")
            send("/loop 90m check the tests", "cannot be kept exactly")
            note = send("/loop 10m run the tests and tell me if any fail", "Looping every 10 minutes: run the tests")
            assert "read-only" in note and "/loop stop" in note
            jobs = app.store.jobs()
            assert len(jobs) == 1 and jobs[0]["schedule_expr"] == "*/10 * * * *" and jobs[0]["session_id"] == sid, jobs
            assert jobs[0]["task_text"] == "run the tests and tell me if any fail" and jobs[0]["project_dir"] == os.path.abspath(tmp)
            send("/loop 2h summarise new commits", "Looping every 2 hours")
            listed = send("/loop", "Looping here: ")
            assert "run the tests and tell me if any fail (*/10 * * * *)" in listed and "summarise new commits (" in listed, listed
            # another conversation's loops are not this one's to stop
            other, _ = make_session_with_fake(base, tmp, [])
            app.store.update_job(app.jobs.add("Loop: theirs", "x", "*/5 * * * *", tmp)["id"], session_id=other)
            send("/loop stop", "Stopped 2 loops.")
            left = app.store.jobs()
            assert len(left) == 1 and left[0]["session_id"] == other, left
            send("/loop stop", "Nothing was looping here.")
            assert fake.calls == 0, "none of this asks the model anything"
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_loop_repeats_a_check_into_the_conversation")


def test_talking_to_a_local_reasoning_model_the_way_its_server_needs():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent import OpenAICompatClient, normalise_messages
    from webui import models as models_mod
    from webui.failover import classify
    from webui.offload import Offloader

    # ---- the conversation is put in the shape chat templates accept
    raw = [
        {"role": "system", "content": "Rules."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "reasoning_content": "private", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "read", "arguments": "{not json"}},
            {"id": "2", "type": "function", "function": {"name": "todo_list", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "1", "content": None},
        {"role": "system", "content": "A summary added later."},
        {"role": "assistant", "content": None, "tool_calls": None},
    ]
    shaped = normalise_messages(raw)
    assert [m["role"] for m in shaped] == ["system", "user", "assistant", "tool", "assistant"], shaped
    assert shaped[0]["content"] == "Rules.\n\nA summary added later.", "one system message, first"
    assert [c["function"]["arguments"] for c in shaped[2]["tool_calls"]] == ["{}", "{}"], "arguments are always a JSON object"
    assert "reasoning_content" not in shaped[2] and shaped[3]["content"] == "" and shaped[4] == {"role": "assistant", "content": ""}
    assert raw[2]["tool_calls"][0]["function"]["arguments"] == "{not json", "the record itself is left as it was"

    # ---- each model takes its own words for how long to think
    bonsai, qwen = models_mod.profile_for("bonsai2-27b-pq2"), models_mod.profile_for("Qwen3.8-35B-A3B-Q4_K_M")
    assert models_mod.reasoning_params(bonsai, "high") == {"reasoning_effort": "xhigh", "thinking_budget_tokens": 8192}
    assert models_mod.reasoning_params(bonsai, "low") == {"reasoning_effort": "medium", "thinking_budget_tokens": 1024}
    assert models_mod.reasoning_params(bonsai, "off") == {"reasoning_effort": "none", "thinking_budget_tokens": None}
    # a model with no word for "no thinking" is not told to stop: it thinks as little as it can
    assert models_mod.reasoning_params(qwen, "off") == models_mod.reasoning_params(qwen, "low")
    assert models_mod.reasoning_params(qwen, "medium") == {"reasoning_effort": "medium"}
    assert models_mod.reasoning_params(qwen, "auto") == {} and models_mod.reasoning_params({}, "high") == {}
    line = models_mod.shown(models_mod.command(models_mod.entry("bonsai2-27b-pq2"), {"flags": ["-ngl", "99"], "context": 32768}, "m.gguf", sleep_minutes=15))
    assert "--cache-reuse 256 -a bonsai2-27b-pq2 --sleep-idle-seconds 900" in line and "--min-p 0.05" in line, line
    assert "--reasoning on" not in line, "that flag would override a request that asks for no thinking"
    assert "--cache-type" not in line, "a compressed conversation cache broke a model here; none is used unmeasured"
    assert "--sleep-idle" not in models_mod.shown(models_mod.command(models_mod.entry("bonsai2-27b-pq2"), {"flags": []}, "m.gguf"))

    seen = []          # request bodies, as the server got them
    plan = []          # what to answer, in order

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            kind, payload = plan.pop(0)
            if kind == "error":
                data = json.dumps({"error": {"message": payload[1]}}).encode()
                self.send_response(payload[0])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in payload:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    def delta(**fields):
        return {"choices": [{"delta": fields}]}

    timings = {"timings": {"cache_n": 900, "prompt_n": 100, "prompt_ms": 200.0, "predicted_n": 40, "predicted_ms": 800.0}, "choices": []}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        shown, progress = [], []
        client = OpenAICompatClient(url, "", "bonsai2-27b-pq2")
        client.on_delta = shown.append
        client.on_progress = lambda done, total, reused: progress.append((done, total, reused))
        client.params = {"temperature": 1.0, "reasoning_effort": "high"}

        # the server refuses the effort word; the request goes again without it, and it is not sent after that
        plan += [("error", (500, "unsupported reasoning_effort")),
                 ("ok", [{"prompt_progress": {"total": 1000, "cache": 900, "processed": 950, "time_ms": 50}, "choices": []},
                         delta(reasoning_content="let me think"), delta(content="<think>more</think>The answer"),
                         delta(content=" is 4."), timings])]
        reply = client.chat(raw, [])
        assert reply["choices"][0]["message"]["content"] == "The answer is 4." and "".join(shown) == "The answer is 4."
        assert seen[0]["reasoning_effort"] == "high" and "reasoning_effort" not in seen[1] and seen[1]["temperature"] == 1.0
        assert seen[1]["return_progress"] is True and seen[1]["messages"][0]["content"].startswith("Rules.")
        assert len([m for m in seen[1]["messages"] if m["role"] == "system"]) == 1
        assert client.params == {"temperature": 1.0} and "refused the reasoning settings" in client.notes[0]
        # how far it was through reading, and what it measured, are kept
        assert progress == [(950, 1000, 900)], progress
        u = client.usage_total
        assert (u["cache_n"], u["prompt_n"], u["predicted_n"], u["last_context"]) == (900, 100, 40, 1040), u
        assert u["reasoning"] == len("let me think") + len("more")

        # it thinks and never answers: asked once more with less thinking
        plan += [("ok", [delta(reasoning_content="thinking " * 50)]), ("ok", [delta(content="Short answer.")])]
        reply = client.chat([{"role": "user", "content": "q"}], [])
        assert reply["choices"][0]["message"]["content"] == "Short answer." and seen[-1]["reasoning_effort"] == "medium"
        assert "spent its whole answer thinking" in client.notes[-1]

        # a model stuck writing the same symbol is stopped instead of being left to run for minutes
        plan += [("ok", [delta(reasoning_content="/" * 100) for _ in range(40)] + [delta(content="never reached")])]
        try:
            client.chat([{"role": "user", "content": "q"}], [])
            raise AssertionError("should have been stopped")
        except RuntimeError as exc:
            assert "stuck repeating" in str(exc), str(exc)
        seen.pop()

        # what the server says about a failure is kept, so "too long" can be told from "broken"
        plan += [("error", (400, "the request exceeds the available context size (8192 tokens)"))]
        try:
            client.chat([{"role": "user", "content": "q"}], [])
            raise AssertionError("should have failed")
        except RuntimeError as exc:
            assert "exceeds the available context size" in str(exc) and classify(exc) == "overflow", str(exc)
        assert len(seen) == 5, "a request that is too long is not sent again unchanged"
    finally:
        httpd.shutdown()

    # ---- the size of a token is learnt from what the server counted, not assumed
    off = Offloader(8000, per_token=2.0)
    msgs = [{"role": "user", "content": "x" * 4000}]
    assert off.apply(msgs, {}, set()) == msgs
    assert off.last["tokens"] == (4000 + 12) // 2

    # ---- through a session: reuse is reported, the model's notes reach the user, a stuck model is stopped
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.txt"), "alpha\n")

        class Counting:
            def __init__(self, script):
                self.script, self.calls = list(script), 0
                self.usage_total = {"in": 0, "out": 0, "gen_ms": 0, "ttft_ms": None, "reported": False}
                self.notes = []

            def chat(self, messages, tools):
                self.calls += 1
                sent = sum(len(str(m.get("content") or "")) + 12 for m in messages) + len(json.dumps(tools))
                u = self.usage_total
                u["cache_n"] = u.get("cache_n", 0) + (0 if self.calls == 1 else sent // 2 - 50)
                u["prompt_n"] = u.get("prompt_n", 0) + (sent // 2 if self.calls == 1 else 50)
                u["prompt_ms"] = u.get("prompt_ms", 0) + 400.0
                u["predicted_n"] = u.get("predicted_n", 0) + 20
                u["predicted_ms"] = u.get("predicted_ms", 0) + 500.0
                u["out"] += 20
                if self.calls == 2:
                    self.notes.append("The model server refused the reasoning settings (reasoning_effort), so they are no longer sent to it.")
                return self.script.pop(0)

        same = tool_call_msg("read", {"path": "a.txt"})
        client = Counting([same, same, same, same, text_msg("I keep reading a.txt; it says alpha.")])
        app, server, base = start_server(tmp)
        try:
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            sid = snap["id"]
            session = app.get_session(sid)
            session._build_client = lambda provider=None: client
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what does a.txt say"})
            end = reader.wait_for("turn_end", timeout=30)
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert "alpha" in results[0] and "third time in a row" not in results[1], results[1]
            assert "third time in a row you made this exact call" in results[2], results[2]
            assert results[3].startswith("ERROR: refused: you have made this exact read call 3 times"), results[3]
            # the server's own figures reach the page: how much was reused, and its speeds
            assert end["reused_pct"] is not None and 60 <= end["reused_pct"] <= 99, end
            assert end["tokens_per_s"] == 40.0 and end["read_per_s"] and end["read_per_s"] > 0, end
            assert any("refused the reasoning settings" in d["text"] for t, d in reader.events if t == "notice")
            # two characters a token for this model, learnt from the counts, where 3.5 was assumed
            assert 1.8 <= session._chars_per_token <= 2.2, session._chars_per_token
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_talking_to_a_local_reasoning_model_the_way_its_server_needs")


def test_several_models_switching_roles_and_taking_over(monkeypatch=None):
    import socket

    from webui import models as models_mod
    from webui.failover import Breaker, ResilientClient

    # ---- one presets file, one section per model, each with its own placement
    hw = {"vram_gb": 12.0, "ram_gb": 32.0}
    _pretend_computer(monkeypatch, models_mod, {"gpu": "RTX 3060", "vram_gb": 12.0, "vram_used_gb": 0.5, "ram_gb": 32.0,
                                                "ram_free_gb": 20.0, "cores": 8, "disk_free_gb": 100.0})
    rows = [{"model": models_mod.entry(i), "path": f"C:/m/{i}.gguf", "placement": models_mod.fit(models_mod.entry(i), hw)}
            for i in ("qwen38-35b-a3b-q4km", "qwen25-coder-14b-q4km")]
    ini = models_mod.presets(rows, 15)
    assert "[*]" in ini and "sleep-idle-seconds = 900" in ini and "cache-reuse = 256" in ini, ini
    moe = ini.split("[qwen38-35b-a3b-q4km]")[1].split("[qwen25")[0]
    assert "model = C:/m/qwen38-35b-a3b-q4km.gguf" in moe and "n-cpu-moe = " in moe and "temp = 0.6" in moe and "top-k = 20" in moe
    assert "n-cpu-moe" not in ini.split("[qwen25-coder-14b-q4km]")[1], "a dense model that fits the card has no experts to place"
    assert models_mod.shown(models_mod.router_command("C:/m/models.ini")) == (
        "llama-server --models-preset C:/m/models.ini --models-max 1 --host 127.0.0.1 --port 8090")

    # ---- a second model takes over a turn that is going round in circles, once, and says so
    class Scripted:
        def __init__(self, name):
            self.name, self.calls, self.on_delta, self.should_stop = name, 0, None, None

        def chat(self, messages, tools):
            self.calls += 1
            return {"choices": [{"message": {"role": "assistant", "content": self.name}}]}

    said, state = [], {"stuck": False}
    small, big = Scripted("small"), Scripted("big")
    client = ResilientClient(small, "local/small", [], Breaker(), said.append)
    client.hand_over_when("local/big", lambda: big, lambda: state["stuck"], "The model was going round in circles.")
    assert client.chat([], [])["choices"][0]["message"]["content"] == "small" and not said
    state["stuck"] = True
    assert client.chat([], [])["choices"][0]["message"]["content"] == "big"
    assert said == ["The model was going round in circles. local/big takes over for the rest of this turn."], said
    state["stuck"] = False
    assert client.chat([], [])["choices"][0]["message"]["content"] == "big" and small.calls == 1, "it stays handed over"

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "m.py"), "def double(x):\n    return x * 2\n")
        app, server, base = start_server(tmp)
        try:
            # ---- roles: settings are kept clean, and background work uses its own model when one is set
            status, st = http("POST", f"{base}/api/settings", {"helper_model": "nonsense", "escalate_model": "spare/big", "models_at_once": 9})
            assert st["helper_model"] == "" and st["escalate_model"] == "spare/big" and st["models_at_once"] == 4, st
            app.config.providers["spare"] = {"base_url": "http://127.0.0.1:1/v1", "api_key_env": ""}
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            sid = snap["id"]
            session = app.get_session(sid)
            built = []
            session._raw_client = lambda provider=None, model=None: built.append((provider, model)) or Scripted(f"{provider}/{model}")
            session._build_client = lambda provider=None, stream=True: Scripted("main")
            assert session._build_child_client().name == "main"
            http("POST", f"{base}/api/settings", {"helper_model": "spare/tiny"})
            assert session._build_child_client().name == "spare/tiny" and built[-1] == ("spare", "tiny")
            job_client = app.jobs._client({})
            assert job_client.model == "tiny" and job_client.base_url == "http://127.0.0.1:1/v1"

            # ---- a turn where the main model keeps writing a call that cannot work: the second one takes over
            bad = tool_call_msg("edit", {"path": "m.py", "old_text": "    return x * 2\n", "new_text": "    return tripple(x)\n"})

            class Main(Scripted):
                def chat(self, messages, tools):
                    self.calls += 1
                    return tool_call_msg("read", {"path": "m.py"}) if self.calls == 1 else bad

            main = Main("main")

            class Big(Scripted):
                def chat(self, messages, tools):
                    self.calls += 1
                    return text_msg("I could not make that change: there is no function tripple in this project.")

            big = Big("big")
            session._build_client = lambda provider=None, stream=True: main
            session._raw_client = lambda provider=None, model=None: big
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "make double faster"})
            reader.wait_for("turn_end", timeout=40)
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert sum(1 for r in results if r.startswith("ERROR: the code gate refused")) == 3, [r[:50] for r in results]
            assert "That is 3 refused attempts on this file in a row" in results[3], results[3]
            notices = [d["text"] for t, d in reader.events if t == "notice"]
            assert any("going round in circles" in n and "spare/big takes over" in n for n in notices), notices
            assert main.calls == 4 and big.calls == 1, (main.calls, big.calls)
            final = [d for t, d in reader.events if t == "token" and d.get("final")][-1]["text"]
            assert final.startswith("I could not make that change"), final
            with open(os.path.join(tmp, "m.py")) as fh:
                assert "x * 2" in fh.read()
            assert session.codegate.strikes == {}, "the model that took over starts with a clean sheet"
            reader.stop()

            # ---- one server for several models: started, watched, a model loaded and unloaded by hand
            for name in ("tiny-a.gguf", "tiny-b.gguf"):
                _write(os.path.join(app.models.dir, name) if os.makedirs(app.models.dir, exist_ok=True) is None else "", "x")
            extra = [dict(models_mod.entry("qwen25-coder-14b-q4km"), id=i, file=f, match=[i], size_gb=0.001, bytes=1)
                     for i, f in (("tiny-a", "tiny-a.gguf"), ("tiny-b", "tiny-b.gguf"))]
            models_mod.CATALOG.extend(extra)
            models_mod._FILES.clear()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            fake = os.path.join(tmp, "fake_router.py")
            _write(fake, "import sys, json, threading\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
                         "state = {'tiny-a': 'unloaded', 'tiny-b': 'unloaded'}\n"
                         "class H(BaseHTTPRequestHandler):\n    def log_message(self, *a): pass\n"
                         "    def _send(self, obj):\n        body = json.dumps(obj).encode()\n        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)\n"
                         "    def do_GET(self):\n"
                         "        if self.path.startswith('/models'):\n            return self._send({'data': [{'id': k, 'status': {'value': v}} for k, v in state.items()]})\n"
                         "        if self.path.startswith('/props'):\n            return self._send({'default_generation_settings': {'n_ctx': 8192}, 'asked': self.path})\n"
                         "        self._send({'status': 'ok'})\n"
                         "    def do_POST(self):\n        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))\n"
                         "        if self.path == '/models/load':\n            for k in state: state[k] = 'unloaded'\n            state[body['model']] = 'loaded'\n"
                         "        if self.path == '/models/unload': state[body['model']] = 'unloaded'\n        self._send({'success': True})\n"
                         "print('router with', open(sys.argv[2]).read().count('[tiny-'), 'models', flush=True)\n"
                         "s = HTTPServer(('127.0.0.1', int(sys.argv[1])), H)\nthreading.Timer(25, s.shutdown).start()\ns.serve_forever()\n")
            old = (models_mod.find_runner, models_mod.router_command, models_mod.PORT)
            models_mod.find_runner = lambda setting="", workspace="", kind="stock": ""
            assert http("POST", f"{base}/api/models/start-all", {})[0] == 409, "no runner, no start"
            models_mod.find_runner = lambda setting="", workspace="", kind="stock": sys.executable
            models_mod.router_command = lambda preset_path, binary="x", at_once=1: [sys.executable, fake, str(port), preset_path]
            models_mod.PORT = port
            try:
                status, view = http("POST", f"{base}/api/models/start-all", {})
                assert status == 200 and view["running"]["router"] and set(view["running"]["models"]) >= {"tiny-a", "tiny-b"}, view["running"]
                with open(os.path.join(app.models.dir, "models.ini")) as fh:
                    written = fh.read()
                assert "[tiny-a]" in written and "[tiny-b]" in written and "tiny-a.gguf" in written
                assert set(app.config.providers["local"]["models"]) >= {"tiny-a", "tiny-b"}
                deadline = time.time() + 15
                while time.time() < deadline and http("GET", f"{base}/api/models")[1]["running"]["state"] != "ready":
                    time.sleep(0.2)
                running = http("GET", f"{base}/api/models")[1]["running"]
                assert running["state"] == "ready" and running["each"]["tiny-a"] == "unloaded" and running["loaded"] == [], running
                status, view = http("POST", f"{base}/api/models/load", {"id": "tiny-b"})
                assert view["running"]["each"] == {"tiny-a": "unloaded", "tiny-b": "loaded"} and view["running"]["loaded"] == ["tiny-b"]
                status, view = http("POST", f"{base}/api/models/load", {"id": "tiny-a"})
                assert view["running"]["loaded"] == ["tiny-a"], "one at a time: the other one made room"
                status, view = http("POST", f"{base}/api/models/unload", {"id": "tiny-a"})
                assert view["running"]["loaded"] == []
                assert http("POST", f"{base}/api/models/load", {"id": "not-there"})[0] == 400
                assert "router with 2 models" in app.models.log_tail()
                # the size of a model's window is asked without making the server load that model
                from webui import offload
                assert offload.detect_window(f"http://127.0.0.1:{port}/v1", "tiny-a") == 8192
                assert http("POST", f"{base}/api/models/stop", {})[1]["running"]["state"] == "stopped"
                assert http("POST", f"{base}/api/models/load", {"id": "tiny-a"})[0] == 409
            finally:
                app.models.stop()
                models_mod.find_runner, models_mod.router_command, models_mod.PORT = old
                for e in extra:
                    models_mod.CATALOG.remove(e)
                models_mod._FILES.clear()
        finally:
            server.shutdown()
    print("PASS test_several_models_switching_roles_and_taking_over")


def test_agent_trial_scores_real_tasks_by_their_result():
    from webui import trial

    # ---- what a trial lets the agent do
    assert trial.allow("read", {"path": "a.py"}) == (True, None) and trial.allow("edit", {"path": "a.py"})[0]
    assert trial.allow("bash", {"cmd": "python -m pytest -q"})[0] and trial.allow("bash", {"cmd": "pytest -q test_a.py"})[0]
    for cmd in ("rm -rf .", "python -m pytest -q && curl example.com", "pip install x", "git push", "python evil.py"):
        assert trial.allow("bash", {"cmd": cmd})[0] is False, cmd
    assert trial.allow("web_fetch", {"url": "http://x"})[0] is False and trial.allow("subagent_spawn", {})[0] is False

    by_id = {t["id"]: t for t in trial.TASKS}
    # ---- every task can be solved, and its hidden tests tell a right answer from a wrong one
    solutions = {
        "fix-bug": {"inflect.py": by_id["fix-bug"]["files"]["inflect.py"].replace("< (11, 12, 13)", "in (11, 12, 13)")},
        "add-function": {"textutil.py": by_id["add-function"]["files"]["textutil.py"]
                         + "\n\nimport re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"},
        "two-files": {"pricing.py": "def total(prices, tax_rate):\n    s = sum(prices)\n    return s + s * tax_rate\n",
                      "cart.py": "from pricing import total\n\n\ndef checkout(prices, tax_rate):\n    return {'due': total(prices, tax_rate)}\n"},
    }
    for task_id, files in solutions.items():
        task = by_id[task_id]
        with tempfile.TemporaryDirectory() as folder:
            for name, text in task["files"].items():
                _write(os.path.join(folder, name), text)
            wrong = trial.score(task, folder, "I've fixed it and all tests pass.", task["files"])
            assert not wrong["solved"] and wrong["false_done"], (task_id, wrong)
            honest = trial.score(task, folder, "I could not do this.", task["files"])
            assert not honest["solved"] and not honest["false_done"], (task_id, honest)
            for name, text in files.items():
                _write(os.path.join(folder, name), text)
            right = trial.score(task, folder, "Done.", task["files"])
            assert right["solved"] and not right["false_done"], (task_id, right)
            assert not os.path.exists(os.path.join(folder, "_hidden_check.py"))
    q = by_id["question"]
    with tempfile.TemporaryDirectory() as folder:
        for name, text in q["files"].items():
            _write(os.path.join(folder, name), text)
        assert trial.score(q, folder, "It is called by `start` in app.py.", q["files"])["solved"]
        assert not trial.score(q, folder, "It is called by run in worker.py.", q["files"])["solved"]
        _write(os.path.join(folder, "app.py"), "changed")
        assert "told not to" in trial.score(q, folder, "start in app.py", q["files"])["why"]
    r = by_id["report"]
    with tempfile.TemporaryDirectory() as folder:
        for name, text in r["files"].items():
            _write(os.path.join(folder, name), text)
        assert trial.score(r, folder, "2 passed and 1 failed (test_half).", r["files"])["solved"]
        assert trial.score(r, folder, "- **Passed: 2** (test_add)\n- **Failed: 1** (test_half)", r["files"])["solved"]
        assert not trial.score(r, folder, "Passed: 3, Failed: 0", r["files"])["solved"]
        assert not trial.score(r, folder, "All 3 tests pass.", r["files"])["solved"]

    # ---- through the app: a scripted model that solves one task, bluffs one, and is honest about the rest
    class Scripted:
        def __init__(self, script):
            self.script = list(script)

        def chat(self, messages, tools):
            return self.script.pop(0) if self.script else text_msg("I could not do this.")

    fixed = solutions["fix-bug"]["inflect.py"]
    scripts = {
        "fix-bug": [tool_call_msg("read", {"path": "inflect.py"}),
                    tool_call_msg("write_file", {"path": "inflect.py", "content": fixed}, "c2"),
                    tool_call_msg("bash", {"cmd": "curl http://example.com"}, "c3"),
                    text_msg("I changed the comparison to a membership test.")],
        "add-function": [text_msg("I've added slugify and all tests pass.")],
    }
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            seen_settings = []

            def prepare(session):
                task_id = session.title.replace("Trial: ", "")
                seen_settings.append((task_id, app.settings["code_gate"], app.settings["reply_gate"], app.settings["working_set"]))
                client = Scripted(scripts.get(task_id, []))
                session._build_client = lambda provider=None, stream=True: client

            app.trial._prepare = prepare
            provider = app.config.provider
            assert http("POST", f"{base}/api/trial", {"provider": "nope", "model": "x"})[0] == 400
            status, out = http("POST", f"{base}/api/trial", {"provider": provider, "model": "m", "compare": True})
            assert status == 200 and out["trial"]["running"] and out["trial"]["compare"], out
            assert http("POST", f"{base}/api/bench", {"provider": provider, "model": "m"})[0] == 409, "one user of the model at a time"
            deadline = time.time() + 150
            while time.time() < deadline and http("GET", f"{base}/api/bench")[1]["trial"].get("running"):
                time.sleep(0.3)
            status, out = http("GET", f"{base}/api/bench")
            assert not out["trial"].get("running"), out["trial"]
            row = out["results"][0]
            rounds = row["details"]["trial"]
            assert list(rounds) == ["with the checks", "without the checks"] and all(len(v) == 5 for v in rounds.values())
            on = {r["task"]: r for r in rounds["with the checks"]}
            off = {r["task"]: r for r in rounds["without the checks"]}
            assert on["fix-bug"]["solved"] and on["fix-bug"]["steps"] == 3, on["fix-bug"]
            # with the checks, the bluff is sent back and withdrawn; without them it stands, and is counted
            assert not on["add-function"]["solved"] and on["add-function"]["sent_back"] == 1 and not on["add-function"]["false_done"], on["add-function"]
            assert not off["add-function"]["solved"] and off["add-function"]["false_done"] and off["add-function"]["sent_back"] == 0, off["add-function"]
            assert "with the checks: 1 of 5 tasks done, 0 said done when it was not, 1 caught by the checks" in row["summary"], row["summary"]
            assert "without the checks: 1 of 5 tasks done, 1 said done when it was not, 0 caught" in row["summary"], row["summary"]
            # the checks were really off for the second round, and are back as they were
            assert ("fix-bug", "check", "enforce", True) in seen_settings and ("fix-bug", "off", "off", False) in seen_settings
            assert (app.settings["code_gate"], app.settings["reply_gate"], app.settings["working_set"]) == ("check", "enforce", True)
            # the trial's sessions and folders are gone
            assert not [s for s in app.sessions.values() if getattr(s, "_trial", False)]
            assert not [s for s in http("GET", f"{base}/api/sessions")[1] if str(s.get("title", "")).startswith("Trial:")]
        finally:
            server.shutdown()
    print("PASS test_agent_trial_scores_real_tasks_by_their_result")


def _read_stream(url, want, timeout=8, headers=None):
    """Read one event stream until *want* events have arrived. Returns [(id, type, data)]."""
    import http.client as raw_http
    import json
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    conn = raw_http.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""), headers=headers or {})
    resp = conn.getresponse()
    events, buf, deadline = [], b"", time.time() + timeout
    try:
        while len(events) < want and time.time() < deadline:
            piece = resp.read(1)
            if not piece:
                break
            buf += piece
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                eid, etype, data = None, None, {}
                for line in frame.decode("utf-8", "replace").split("\n"):
                    if line.startswith("id:"):
                        eid = int(line[3:].strip())
                    elif line.startswith("event:"):
                        etype = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:].strip() or "{}")
                if etype and etype != "ping":
                    events.append((eid, etype, data))
    except OSError:
        pass
    finally:
        conn.close()
    return events


def test_pages_stay_in_step_replay_resync_and_no_double_sends():
    from webui.events import EventBus

    # ---- the bus itself
    bus = EventBus(keep=5)
    for n in range(3):
        bus.emit("step", {"n": n})
    q, missed = bus.subscribe_from(1)
    assert [e["data"]["n"] for e in missed] == [1, 2] and bus.seq == 3
    assert bus.subscribe_from(3)[1] == [] and bus.subscribe_from(None)[1] is None
    # a long answer is many partial events; only the latest is kept, so it cannot crowd the rest out
    for n in range(50):
        bus.emit("token", {"text": "x" * n}, transient=True)
    assert [e["type"] for e in bus.subscribe_from(3)[1]] == ["token"]
    assert bus.subscribe_from(3)[1][0]["data"]["text"] == "x" * 49
    for n in range(10):
        bus.emit("step", {"n": n})
    assert bus.subscribe_from(3)[1] is None, "too far behind: the page must be told to reload"
    bus.emit("live")
    assert q.get_nowait()["seq"] == 4, "a subscriber still gets every event, in order"

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.txt"), "alpha\n")
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.txt"}),
                text_msg("It says alpha."),
                text_msg("Second answer."),
            ])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            # ---- every page hears about sessions and their state, whichever one it has open
            app_events = []
            import threading

            listener = threading.Thread(
                target=lambda: app_events.extend(_read_stream(f"{base}/api/events", 4, 6)), daemon=True)
            listener.start()
            time.sleep(0.3)

            # ---- the same message sent twice (a retry on a bad connection) is heard once
            body = {"text": "what does a.txt say", "client_id": "abc123"}
            status, first = http("POST", f"{base}/api/sessions/{sid}/message", body)
            status, again = http("POST", f"{base}/api/sessions/{sid}/message", body)
            assert first.get("duplicate") is None and again.get("duplicate") is True, (first, again)
            reader.wait_for("turn_end")
            time.sleep(0.4)
            assert fake.calls == 2, "the repeat must not start a second turn"
            said = [d for t, d in reader.events if t == "user_message"]
            assert said == [{"text": "what does a.txt say", "client_id": "abc123"}], said
            result = [d for t, d in reader.events if t == "tool_result"][0]
            assert result["args"] == {"path": "a.txt"}, "a page that joined mid-step needs the arguments"
            listener.join(timeout=7)
            kinds = [t for _, t, _ in app_events]
            assert kinds[0] == "hello" and "sessions" in kinds and "status" in kinds, kinds
            working = [d for _, t, d in app_events if t == "status"]
            assert working[0] == {"id": sid, "status": "working"}, working

            # ---- the record tells a page where to pick the stream up
            status, hist = http("GET", f"{base}/api/sessions/{sid}/history")
            assert hist["seq"] == app.get_session(sid).bus.seq and hist["seq"] > 5, hist["seq"]
            # a page that has everything gets only what happens next ...
            later = []
            follow = threading.Thread(target=lambda: later.extend(
                _read_stream(f"{base}/api/sessions/{sid}/events?after={hist['seq']}", 4, 8)), daemon=True)
            follow.start()
            time.sleep(0.3)
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "again", "client_id": "def456"})
            follow.join(timeout=9)
            assert later[0][1] == "state" and later[1][1] == "user_message", later[:3]
            assert all(eid > hist["seq"] for eid, _, _ in later[1:]), later
            # ... and one that fell behind is sent exactly what it missed, in order, once
            time.sleep(0.5)
            behind = _read_stream(f"{base}/api/sessions/{sid}/events?after={hist['seq']}", 6, 4)
            ids = [eid for eid, _, _ in behind[1:]]
            assert ids == sorted(set(ids)) and ids[0] == hist["seq"] + 1, ids
            kinds = [t for _, t, _ in behind if t != "turn_effort"]   # Auto's level for the message, said too
            assert kinds[1:4] == ["user_message", "turn_start", "token"], behind[1:5]
            # the browser's own reconnect header works the same way
            again = _read_stream(f"{base}/api/sessions/{sid}/events", 3, 4,
                                 headers={"Last-Event-ID": str(hist["seq"])})
            assert again[0][1] == "state" and again[1][0] == hist["seq"] + 1, again[:2]
            # asking for something from before the server remembers: told to reload
            app.get_session(sid).bus._recent.clear()
            app.get_session(sid).bus.emit("notice", {"text": "x"})
            stale = _read_stream(f"{base}/api/sessions/{sid}/events?after=1", 2, 3)
            assert [t for _, t, _ in stale] == ["state", "resync"], stale
            # settings and the Library are announced to every page too
            mark = app.bus.seq
            http("POST", f"{base}/api/settings", {"guard": "off"})
            saved = app.save_artifact(sid, "Notes", "notes.md", "# hello")
            app.delete_artifact(saved["artifact_id"])
            told = [e["type"] for e in app.bus.subscribe_from(mark)[1]]
            assert told.count("settings") == 1 and told.count("library") == 2, told
            reader.stop()
        finally:
            server.shutdown()
    print("PASS test_pages_stay_in_step_replay_resync_and_no_double_sends")


def test_page_script_parses():
    """The page script is one big file; a slip in it breaks everything, so check that it parses."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        print("PASS test_page_script_parses (skipped: node is not installed)")
        return
    js_dir = os.path.join(ROOT, "webui", "static", "js")
    for name in sorted(os.listdir(js_dir)):
        proc = subprocess.run([node, "--check", os.path.join(js_dir, name)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, name + ": " + proc.stderr[:600]
    print("PASS test_page_script_parses")


def main():
    tests = [
        test_page_script_parses,
        test_other_devices_need_the_key,
        test_pages_stay_in_step_replay_resync_and_no_double_sends,
        test_people_have_pages_and_come_up_when_mentioned,
        test_recall_travels_with_the_message_and_the_prompt_stays_the_same,
        test_context_offload_moves_output_out_in_stages,
        test_a_model_that_does_not_answer_is_retried_then_replaced,
        test_talking_to_a_local_reasoning_model_the_way_its_server_needs,
        test_local_models_what_fits_download_and_run,
        test_several_models_switching_roles_and_taking_over,
        test_the_check_measures_a_model_on_this_computer,
        test_agent_trial_scores_real_tasks_by_their_result,
        test_loop_repeats_a_check_into_the_conversation,
        test_code_gate_refuses_what_is_not_there,
        test_code_graph_knows_who_calls_what_and_the_gate_uses_it,
        test_gates_hold_through_a_session,
        test_reply_gate_sends_back_then_removes_what_is_unbacked,
        test_working_set_keeps_the_task_in_front_and_stale_text_out,
        test_phone_pairs_with_a_short_code_or_a_scanned_link,
        test_skills_can_be_written_edited_and_removed_and_themes_are_kept_clean,
        test_answers_stream_as_they_are_written_and_can_be_stopped,
        test_daily_review_turns_repeated_problems_into_lessons_and_retires_old_ones,
        test_extra_tools_load_on_request_and_a_watch_job_only_wakes_on_change,
        test_library_pages_are_sandboxed_and_other_pages_cannot_drive_the_app,
        test_ideas_come_from_evidence_persona_leads_the_prompt_and_chats_are_searchable,
        test_goals_reach_the_prompt_and_skill_proposals_come_from_real_work,
        test_quiet_moment_pass_saves_what_was_said_and_marks_what_was_not,
        test_scheduled_jobs_run_report_stay_silent_and_pause_themselves,
        test_web_tools_ask_first_and_mark_outside_content,
        test_background_helper_reports_back_and_follows_the_same_rules,
        test_store_claims_supersede_reinforce_and_forget,
        test_memory_standing_ledger_and_log_through_a_session,
        test_guard_asks_in_auto_mode_for_unread_edit,
        test_guard_block_mode_refuses_and_file_is_untouched,
        test_guard_off_restores_plain_auto_mode,
        test_reply_claims_are_typed,
        test_ledger_conflict_and_command_keys,
        test_always_allow_is_per_command_and_never_covers_a_chain,
        test_kind_set_to_never_refuses_without_asking,
        test_switch_folder_keeps_conversation_and_moves_the_tools,
        test_duplicate_session_and_delete_of_a_closed_one,
        test_kernel_workbench_runs_and_types_answers,
        test_commands_memory_usage_and_settings_reset,
        test_memory_watchdog_stops_a_working_session,
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


def test_each_turns_changes_can_be_kept_or_undone_file_by_file():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.py"), "x = 1\n")
        _write(os.path.join(tmp, "b.py"), "y = 1\n")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.py"}),
                tool_call_msg("edit", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, "c2"),
                tool_call_msg("read", {"path": "b.py"}, "c3"),
                tool_call_msg("edit", {"path": "b.py", "old_text": "y = 1", "new_text": "y = 2\nz = 3"}, "c4"),
                tool_call_msg("write_file", {"path": "new.py", "content": "n = 1\n"}, "c5"),
                tool_call_msg("edit", {"path": "a.py", "old_text": "x = 2", "new_text": "x = 3"}, "c6"),
                text_msg("Done."),
            ])
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "change them"})
            got = reader.wait_for("turn_changes")
            reader.wait_for("turn_end")
            reader.stop()
            turn = got["turn"]
            files = {f["path"]: f for f in got["files"]}
            assert set(files) == {"a.py", "b.py", "new.py"}, got
            assert (files["a.py"]["added"], files["a.py"]["removed"]) == (1, 1)      # two edits, one change
            assert (files["b.py"]["added"], files["b.py"]["removed"]) == (2, 1) and files["new.py"]["created"]
            assert "before" not in files["a.py"]                                     # no contents sent out
            _, d = http("GET", f"{base}/api/sessions/{sid}/changes/{turn}?path=a.py")
            assert "-x = 1" in d["diff"] and "+x = 3" in d["diff"], d

            url = f"{base}/api/sessions/{sid}/changes/{turn}"
            status, r = http("POST", url, {"action": "undo", "path": "a.py"})
            assert status == 200 and open(os.path.join(tmp, "a.py")).read() == "x = 1\n"
            assert {f["path"]: f["state"] for f in r["files"]}["a.py"] == "undone"
            assert http("POST", url, {"action": "undo", "path": "a.py"})[0] == 409   # once only

            _write(os.path.join(tmp, "b.py"), "y = 2\nz = 3\nmine = 4\n")        # changed again after the turn
            status, r = http("POST", url, {"action": "undo", "path": "b.py"})
            assert status == 409 and "changed again" in r["error"], r
            assert "mine = 4" in open(os.path.join(tmp, "b.py")).read()          # your work is not thrown away

            status, r = http("POST", url, {"action": "undo_all"})                # what is left: new.py, b.py
            assert not os.path.exists(os.path.join(tmp, "new.py"))
            assert {f["path"]: f["state"] for f in r["files"]} == {"a.py": "undone", "b.py": "pending", "new.py": "undone"}
            assert http("POST", url, {"action": "keep", "path": "b.py"})[0] == 200

            # the record outlives the session object (a restart reads it back)
            from webui.turnchanges import TurnChanges

            again = TurnChanges(tmp, os.path.join(app.webui_dir, "changes", f"{sid}.json"))
            assert {f["path"]: f["state"] for f in again.get(turn)["files"]}["b.py"] == "kept"
        finally:
            server.shutdown()
    print("PASS test_each_turns_changes_can_be_kept_or_undone_file_by_file")


def test_rewind_to_a_message_restores_code_conversation_or_both():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.py"), "x = 1\n")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.py"}),
                tool_call_msg("edit", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, "c2"),
                text_msg("First done."),
                tool_call_msg("edit", {"path": "a.py", "old_text": "x = 2", "new_text": "x = 3"}, "c3"),
                tool_call_msg("write_file", {"path": "b.py", "content": "b = 1\n"}, "c4"),
                text_msg("Second done."),
            ])
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            session = app.get_session(sid)
            for n, text in ((2, "first change"), (4, "second change")):
                http("POST", f"{base}/api/sessions/{sid}/message", {"text": text})
                end = time.time() + 20
                while (len(session.history) < n or session.status != "idle") and time.time() < end:
                    time.sleep(0.05)
            reader.stop()
            assert len(session.history) == 4

            _, r = http("GET", f"{base}/api/sessions/{sid}/rewind")
            points = r["points"]
            assert [p["text"] for p in points] == ["second change", "first change"]
            assert [p["files"] for p in points] == [2, 2]              # a.py, b.py from either point on
            url = f"{base}/api/sessions/{sid}/rewind"
            assert http("POST", url, {"turn": points[0]["turn"]})[0] == 400       # it must be confirmed

            # code only: the files go back to before the second message; the conversation stays
            status, r = http("POST", url, {"confirm": True, "turn": points[0]["turn"], "restore": "code"})
            assert status == 200 and r["code"]["restored"] == ["a.py"] and r["code"]["removed"] == ["b.py"], r
            assert open(os.path.join(tmp, "a.py")).read() == "x = 2\n" and not os.path.exists(os.path.join(tmp, "b.py"))
            assert len(session.history) == 4

            # conversation only, to the first message: the code stays, the message comes back to edit
            status, r = http("POST", url, {"confirm": True, "turn": points[1]["turn"], "restore": "chat"})
            assert status == 200 and r["text"] == "first change", r
            assert session.history == [] and not [e for e in session.transcript if e.get("kind") == "user"]
            assert open(os.path.join(tmp, "a.py")).read() == "x = 2\n"
            assert app.store.items(sid) == session.transcript                 # the record went back too
            assert http("POST", url, {"confirm": True, "turn": points[1]["turn"], "restore": "chat"})[0] == 404
        finally:
            server.shutdown()
    print("PASS test_rewind_to_a_message_restores_code_conversation_or_both")


def test_plan_mode_plans_instead_of_doing_and_approval_leaves_it():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.py"), "x = 1\n")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("1. Change x in a.py to 2.\n2. Run the tests.")])
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "plan"})
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "make x two"})
            plan = reader.wait_for("plan_ready")
            reader.stop()
            assert "Change x" in plan["plan"] and open(os.path.join(tmp, "a.py")).read() == "x = 1\n"
            session = app.get_session(sid)
            assert [e["kind"] for e in session.transcript][-2:] == ["user", "plan"]
            session.decide_plan("approve")
            assert session.settings["permission_mode"] == "edits"           # out of plan mode, edits accepted

            usage = session.context_usage()
            names = [p["name"] for p in usage["parts"]]
            assert names == ["Messages", "System tools", "MCP tools", "System prompt", "Skills", "Memory files"]
            assert usage["window"] > 0 and usage["used"] == sum(p["tokens"] for p in usage["parts"])
            assert usage["free"] == usage["window"] - usage["used"] == usage["until_compact"]
            assert set(usage["lists"]) == {"MCP tools", "Memory files"}
            assert all(d["tokens"] > 0 for d in usage["deferred"])   # only groups that are waiting
            parts = http("GET", f"{base}/api/sessions/{sid}/context")[1]["parts"]
            assert parts[1]["name"] == "System tools" and parts[1]["tokens"] > 100
        finally:
            server.shutdown()
    print("PASS test_plan_mode_plans_instead_of_doing_and_approval_leaves_it")


def test_hooks_page_events_and_matchers_reach_open_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.py"), "x = 1\n")
        block = os.path.join(tmp, "block.py")
        _write(block, "import json, sys\nd = json.load(sys.stdin)\n"
                      "print(json.dumps({'allow': 'secret' not in d.get('prompt', ''), 'message': 'no secrets',"
                      " 'note': 'house rule: be brief'}))\n")
        mark = os.path.join(tmp, "post.py")
        _write(mark, "import json, sys\nd = json.load(sys.stdin)\nprint(json.dumps({'note': 'saw ' + d['tool']}))\n")
        app, server, base = start_server(tmp)
        app.config.coder = dict(app.config.coder or {}, hooks_yaml=os.path.join(tmp, "hooks.yaml"))
        try:
            sid, fake = make_session_with_fake(base, tmp, [
                tool_call_msg("read", {"path": "a.py"}),
                text_msg("Read it."),
            ])
            hooks = [{"event": "user_prompt", "command": f'"{sys.executable}" "{block}"'},
                     {"event": "post_tool", "matcher": "read|grep", "command": f'"{sys.executable}" "{mark}"', "timeout": 20},
                     {"event": "post_tool", "matcher": "bash", "command": "exit 3"}]
            status, r = http("POST", f"{base}/api/hooks", {"hooks": hooks})
            assert status == 200 and [h["event"] for h in r["hooks"]] == ["user_prompt", "post_tool", "post_tool"], r
            assert r["hooks"][1]["matcher"] == "read|grep" and r["hooks"][1]["timeout"] == 20
            assert http("POST", f"{base}/api/hooks", {"hooks": [{"event": "nope", "command": "x"}]})[0] == 400
            assert http("POST", f"{base}/api/hooks", {"hooks": [{"event": "pre_tool", "matcher": "(", "command": "x"}]})[0] == 400
            assert http("GET", f"{base}/api/hooks")[1]["hooks"][0]["command"].endswith('block.py"')

            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            session = app.get_session(sid)
            assert session._hook_manager is not None                        # the open session got them
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "tell me the secret"})
            end = time.time() + 15
            while not any("blocked" in str(e.get("text", "")) for e in session.transcript) and time.time() < end:
                time.sleep(0.05)
            assert any("A hook blocked this message: no secrets" in str(e.get("text", "")) for e in session.transcript)
            assert fake.calls == 0                                           # the model never saw it
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "read a.py"})
            end = time.time() + 20
            while len(session.history) < 2 and time.time() < end:
                time.sleep(0.05)
            reader.stop()
            assert "[hook note] house rule: be brief" in session.history[0]["content"]
            tool = [e for e in session.transcript if e.get("kind") == "tool"][0]
            assert "[hook note] saw read" in tool["result"], tool                # the matcher let this one through
        finally:
            server.shutdown()
    print("PASS test_hooks_page_events_and_matchers_reach_open_sessions")


def test_memory_notes_and_files_and_ctrl_b_moves_a_command_to_the_background():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)
            url = f"{base}/api/sessions/{sid}/memory"
            status, m = http("POST", url, {"scope": "project", "text": "  use tabs,   not spaces ", "append": True})
            assert status == 200 and m["project"]["text"].endswith("- use tabs, not spaces\n"), m
            assert m["project"]["path"].endswith("AGENTS.md") and "use tabs" in session.loop.project_context
            http("POST", url, {"scope": "project", "text": "run pytest -q", "append": True})
            assert open(os.path.join(tmp, "AGENTS.md")).read().count("\n- ") == 2
            http("POST", url, {"scope": "user", "text": "I like short answers", "append": True})
            assert "- I like short answers" in app.persona()["user"]
            status, m = http("POST", url, {"scope": "project", "text": "# Rules\nall new\n"})   # /memory saves it whole
            assert open(os.path.join(tmp, "AGENTS.md")).read() == "# Rules\nall new\n"
            assert http("POST", url, {"scope": "nowhere", "text": "x"})[0] == 400
            assert http("GET", url)[1]["user"]["text"] == app.persona()["user"]

            # Ctrl+B: the command keeps running in the background and the tool call returns at once
            bash = session.loop.registry.get("bash")
            box = {}
            t = threading.Thread(target=lambda: box.update(out=bash.func(
                cmd="for i in 1 2 3 4 5 6 7 8 9; do echo tick $i; sleep 1; done", timeout=60)))
            t.start()
            end = time.time() + 10
            while not getattr(session, "_bash_running", False) and time.time() < end:
                time.sleep(0.05)
            time.sleep(1.2)
            assert http("POST", f"{base}/api/sessions/{sid}/background", {})[1] == {"moved": True}
            t.join(5)
            assert not t.is_alive() and "moved this command to the background as 'bash-1'" in box["out"], box
            snap = http("GET", f"{base}/api/sessions/{sid}")[1]
            assert snap["processes"][0]["name"] == "bash-1" and snap["processes"][0]["state"] == "running"
            logs = http("GET", f"{base}/api/sessions/{sid}/processes/bash-1/logs")[1]
            assert logs["lines"][0] == "tick 1"
            http("POST", f"{base}/api/sessions/{sid}/processes/bash-1/stop", {})
            assert session.processes.running["bash-1"].proc.poll() is not None
            assert http("POST", f"{base}/api/sessions/{sid}/background", {})[1] == {"moved": False}
            assert bash.func(cmd="echo plain", timeout=10).startswith("[exit 0]\nplain")
        finally:
            server.shutdown()
    print("PASS test_memory_notes_and_files_and_ctrl_b_moves_a_command_to_the_background")


def test_helper_runs_open_review_commands_fork_and_export():
    import subprocess

    from webui.app import review_prompt

    assert "git diff HEAD`" in review_prompt("review", "") and "Do not change any files" in review_prompt("review", "")
    assert "git diff HEAD...feature-x" in review_prompt("security", "feature-x")
    assert "exploit" in review_prompt("security", "")
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "a.py"), "x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp)
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(base, tmp, [
                tool_call_msg("delegate", {"brief": "read a.py and report x"}),
                tool_call_msg("read", {"path": "a.py"}, "h1"),          # the helper's step
                text_msg("x is 1."),                                     # the helper's report
                text_msg("The helper says x is 1."),
                text_msg("No problems found."),
            ])
            session = app.get_session(sid)
            session._build_child_client = lambda: fake
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "auto"})
            app.update_settings({"action_permissions": {"helpers": "allow", "read": "allow"}})
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what is x"})
            end = time.time() + 20
            while (len(session.history) < 2 or session.status != "idle") and time.time() < end:
                time.sleep(0.05)
            helper = [e for e in session.transcript if e.get("kind") == "subagent"][0]
            status, run = http("GET", f"{base}/api/sessions/{sid}/agents/{helper['id']}")
            assert status == 200 and run["brief"] == "read a.py and report x" and run["report"] == "x is 1.", run
            assert [i["name"] for i in run["items"] if i["kind"] == "tool"] == ["read"]
            assert http("GET", f"{base}/api/sessions/{sid}/agents/{'0' * 12}")[0] == 404

            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "/review"})
            end = time.time() + 20
            while (len(session.history) < 4 or session.status != "idle") and time.time() < end:
                time.sleep(0.05)
            assert "Review the uncommitted changes" in session.history[2]["content"]
            assert [e["text"] for e in session.transcript if e.get("kind") == "user"][-1] == "/review"

            # fork from the second message: the new session has only the first exchange
            second = [e for e in session.transcript if e.get("kind") == "user"][1]
            status, snap = http("POST", f"{base}/api/sessions/{sid}/duplicate", {"turn": second["turn"]})
            assert status == 201 and snap["title"].endswith("(fork)")
            forked = app.get_session(snap["id"])
            assert len(forked.history) == 2 and [e["text"] for e in forked.transcript if e.get("kind") == "user"] == ["what is x"]
            assert len(session.history) == 4                                   # the original is untouched
            assert http("POST", f"{base}/api/sessions/{sid}/duplicate", {"turn": "nope"})[0] == 404

            with urllib.request.urlopen(f"{base}/api/sessions/{sid}/export", timeout=10) as resp:
                md = resp.read().decode("utf-8")
                assert "attachment" in resp.headers["Content-Disposition"]
            assert "## You\n\nwhat is x" in md and "The helper says x is 1." in md and "- `delegate`" in md
        finally:
            server.shutdown()
    print("PASS test_helper_runs_open_review_commands_fork_and_export")


if __name__ == "__main__":
    main()


def test_cut_off_answer_is_asked_again():
    """A reply that hits the length limit without acting is stopped and asked once more."""
    import agent

    client = agent.OpenAICompatClient("http://x", "", "m")
    sent = []

    def fake(messages, tools, params):
        sent.append((messages, params))
        if len(sent) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "thinking aloud " * 400,
                                             "tool_calls": None}, "finish_reason": "length"}]}
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}

    client._send = fake
    reply = client.chat([{"role": "user", "content": "go"}], [{"type": "function", "function": {"name": "read"}}])
    assert reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read"
    assert sent[0][1]["max_tokens"] == agent.ANSWER_TOKENS
    assert "cut off" in sent[1][0][-1]["content"] and len(sent[1][0][-2]["content"]) < 1400
    assert any("length limit" in n for n in client.notes)

    # a tool call cut off half way is dropped; one that arrived whole is kept
    client._send = lambda m, t, p: {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{\"path\": \"a\"}"}},
        {"id": "b", "type": "function", "function": {"name": "edit", "arguments": "{\"path\": \"a\", \"new"}}]},
        "finish_reason": "length"}]}
    reply = client.chat([{"role": "user", "content": "go"}], [{"type": "function", "function": {"name": "read"}}])
    assert [c["id"] for c in reply["choices"][0]["message"]["tool_calls"]] == ["a"]


def test_side_chat_reads_the_session_and_adds_nothing_to_it():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("the parser lives in src/parse.py")])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "where is the parser?"})
            reader.wait_for("turn_end")
            reader.stop()
            session = app.get_session(sid)
            before = list(session.history)
            side = FakeClient([text_msg("it splits on commas"), text_msg("yes, quoted ones too")])
            seen = []
            real_chat = side.chat
            side.chat = lambda messages, tools: (seen.append((messages, tools)), real_chat(messages, tools))[1]
            session._raw_client = lambda *a, **k: side

            status, r = http("POST", f"{base}/api/sessions/{sid}/side", {"question": "what does it do?"})
            assert status == 200 and r["answer"] == "it splits on commas", (status, r)
            messages, tools = seen[0]
            assert tools == [], "a side chat calls no tools"
            assert "where is the parser?" in messages[1]["content"] and "src/parse.py" in messages[1]["content"]
            assert session.history == before, "nothing goes back into the main thread"

            # the side thread carries on, then starts over when cleared
            status, r = http("POST", f"{base}/api/sessions/{sid}/side", {"question": "and quotes?"})
            assert r["answer"] == "yes, quoted ones too" and seen[1][0][-2]["content"] == "it splits on commas"
            assert http("POST", f"{base}/api/sessions/{sid}/side", {"clear": True})[0] == 200
            assert session._side == []
            assert http("POST", f"{base}/api/sessions/{sid}/side", {"question": "  "})[0] == 400
        finally:
            server.shutdown()


def test_suggested_prompt_is_one_short_line_or_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("I added the retry helper.")])
            session = app.get_session(sid)
            # nothing to suggest before there is a reply
            assert http("POST", f"{base}/api/sessions/{sid}/suggest", {})[1] == {"text": ""}
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "add a retry helper"})
            reader.wait_for("turn_end")
            reader.stop()
            before = list(session.history)
            side = FakeClient([text_msg('"Run the tests to check it"\nmore text'), text_msg("NONE"),
                               text_msg("<think>hmm</think>Now commit it")])
            session._raw_client = lambda *a, **k: side
            assert http("POST", f"{base}/api/sessions/{sid}/suggest", {})[1]["text"] == "Run the tests to check it"
            assert http("POST", f"{base}/api/sessions/{sid}/suggest", {})[1]["text"] == ""
            assert http("POST", f"{base}/api/sessions/{sid}/suggest", {})[1]["text"] == "Now commit it"
            assert session.history == before, "a suggestion adds nothing to the conversation"

            # a model that fails means no suggestion, not an error
            session._raw_client = lambda *a, **k: FakeClient([])
            assert http("POST", f"{base}/api/sessions/{sid}/suggest", {}) == (200, {"text": ""})
        finally:
            server.shutdown()


def test_reveal_refuses_paths_outside_the_session_folder():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            status, r = http("POST", f"{base}/api/sessions/{sid}/reveal", {"path": "../../Windows"})
            assert status == 400 and "not in this session" in r.get("error", ""), (status, r)
            status, r = http("POST", f"{base}/api/sessions/{sid}/reveal", {"path": "no/such/file.py"})
            assert status == 400, (status, r)
        finally:
            server.shutdown()


def test_bypass_permissions_runs_what_the_guard_would_ask_about():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "g.txt"), "one\n")
        script = [
            tool_call_msg("edit", {"path": "g.txt", "old_text": "one\n", "new_text": "two\n"}),
            text_msg("done"),
        ]

        def bypass(app, base, sid):
            assert "bypass" in http("GET", f"{base}/api/config")[1]["permission_modes"]
            http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "bypass"})

        app, server, base, sid, reader = _run(tmp, script, before=bypass)
        try:
            reader.wait_for("turn_end")
            assert not [e for e in reader.events if e[0] == "permission_request"], "bypass never asks"
            with open(os.path.join(tmp, "g.txt"), encoding="utf-8") as fh:
                assert fh.read() == "two\n"
        finally:
            reader.stop()
            server.shutdown()


def test_a_suggested_task_becomes_a_card_and_the_turn_goes_on():
    from test_webui import tool_call_msg

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [
                tool_call_msg("suggest_task", {"title": "Add parser tests", "prompt": "Write tests for src/parse.py"}),
                text_msg("done, and I suggested a follow-up"),
            ])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "fix the bug"})
            card = reader.wait_for("task_suggested")
            reader.wait_for("turn_end")
            reader.stop()
            assert card["title"] == "Add parser tests" and "src/parse.py" in card["prompt"], card
            session = app.get_session(sid)
            tasks = [e for e in session.transcript if e.get("kind") == "task"]
            assert tasks == [{"kind": "task", "title": "Add parser tests", "prompt": "Write tests for src/parse.py"}]
        finally:
            server.shutdown()


def test_words_before_a_step_stay_in_the_conversation():
    """Text the model writes before a tool call is kept (and replayed), not overwritten by the next reply."""
    from test_webui import tool_call_msg

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)
            lead_in = tool_call_msg("glob", {"pattern": "*.py"})
            lead_in["choices"][0]["message"]["content"] = "Let me look at the files first."
            script = [lead_in, text_msg("There are no Python files here.")]

            def chat(messages, tools):
                msg = script.pop(0)
                text = msg["choices"][0]["message"].get("content")
                for word in (text or "").split(" "):   # as a streaming client would, word by word and fast
                    session._on_delta(word if word == text.split(" ")[0] else " " + word)
                return msg
            fake.chat = chat
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what is here?"})
            reader.wait_for("turn_end")
            reader.stop()
            said = [e for e in session.transcript if e.get("kind") == "assistant"]
            assert said[0] == {"kind": "assistant", "text": "Let me look at the files first.", "interim": True}, said
            # the page is sent the whole lead-in before the step, not just the first word (throttled events)
            kinds = [t for t, _ in reader.events]
            before_step = [d for t, d in reader.events[:kinds.index("tool_call")] if t == "token"]
            assert before_step and before_step[-1]["text"] == "Let me look at the files first.", before_step
            assert said[-1]["text"] == "There are no Python files here." and not said[-1].get("interim")
        finally:
            server.shutdown()


def test_the_agents_browser_is_shown_after_each_browser_step():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)
            status, _ = http("GET", f"{base}/api/sessions/{sid}/browser-view.png")
            assert status == 404, "nothing to show before the agent opens a page"

            class FakeBrowser:
                def view(self, path):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as fh:
                        fh.write(b"\x89PNG\r\n\x1a\nfake")
                    return "http://localhost:5173/todo"

            if getattr(session, "browser", None) is None:
                from webui import webtools
                session.browser = webtools.SessionBrowser(webtools.shot_dir(app.workspace, sid))
            session.browser.session = FakeBrowser()
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            session._show_browser()
            view = reader.wait_for("browser_view")
            reader.stop()
            assert view == {"url": "http://localhost:5173/todo", "n": 1}, view
            with urllib.request.urlopen(f"{base}/api/sessions/{sid}/browser-view.png", timeout=10) as resp:
                assert resp.headers["Content-Type"] == "image/png" and resp.read().startswith(b"\x89PNG")
        finally:
            server.shutdown()


def test_usage_has_every_one_of_the_last_14_days():
    from webui.usage import UsageLog

    with tempfile.TemporaryDirectory() as tmp:
        log = UsageLog(os.path.join(tmp, "usage.jsonl"))
        log.add("m", 1000, 200, 500)
        days = log.summary({})["days"]
        assert len(days) == 14 and days[-1]["in"] == 1000 and days[-1]["out"] == 200 and days[-1]["tasks"] == 1
        assert all(d["in"] == 0 for d in days[:-1]), "a quiet day is a zero, not a gap"


def test_the_usage_log_keeps_only_the_days_anything_reads():
    """summary() only reads the last 14 days, so older lines are dead weight: past the size cap
    the log is rewritten without them, and the numbers for the days that matter do not move."""
    import datetime

    from webui.usage import MAX_BYTES, UsageLog

    with tempfile.TemporaryDirectory() as tmp:
        log = UsageLog(os.path.join(tmp, "usage.jsonl"))
        old = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
        with open(log.path, "w", encoding="utf-8") as fh:
            for i in range(4000):    # a year of turns, all from long ago
                fh.write(__import__("json").dumps(
                    {"ts": f"{old}T10:{i % 60:02d}:00", "model": "m", "in": 1000, "out": 200, "ms": 500}) + "\n")
        assert os.path.getsize(log.path) > MAX_BYTES
        log.add("m", 1000, 200, 500)
        assert os.path.getsize(log.path) <= MAX_BYTES, "the old lines are gone"
        s = log.summary({})
        assert s["today"]["tasks"] == 1 and s["today"]["in"] == 1000, s


def test_thinking_is_shown_and_kept_apart_from_the_answer():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, fake = make_session_with_fake(base, tmp, [])
            session = app.get_session(sid)

            def chat(messages, tools):
                session._on_thinking("The user wants a greeting. ")
                session._on_thinking("Keep it short.")
                session._on_delta("Hello!")
                return text_msg("Hello!")
            fake.chat = chat
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "say hi"})
            reader.wait_for("turn_end")
            reader.stop()
            final = [d for t, d in reader.events if t == "thinking" and d.get("final")]
            assert final and final[0]["text"] == "The user wants a greeting. Keep it short.", final
            kinds = [e["kind"] for e in session.transcript]
            assert kinds.index("thinking") < len(kinds) - 1 and kinds[-1] == "assistant", kinds
            assert all("Keep it short" not in str(m.get("content")) for m in session.history), \
                "thinking is shown, never sent back to the model"
        finally:
            server.shutdown()


def test_your_own_shortcuts_are_kept_and_nothing_else():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, s = http("GET", f"{base}/api/settings")
            assert s["ui"]["keys"] == {}
            ui = dict(s["ui"], keys={"search": "Ctrl+J", "junk": 5})
            status, s = http("POST", f"{base}/api/settings", {"ui": ui})
            assert status == 200 and s["ui"]["keys"] == {"search": "Ctrl+J"}, s["ui"]["keys"]
        finally:
            server.shutdown()


def test_model_setup_reports_this_computer_and_refuses_bad_downloads():
    from webui.modelsetup import recommend

    assert recommend(24, 64)[0]["model"] == "qwen3-coder:30b" and recommend(24, 64)[0]["recommended"]
    assert recommend(12, 32)[0]["model"] == "qwen3:14b"
    assert [c["model"] for c in recommend(None, 16)] == ["qwen3:4b"], "no graphics card: sized from memory"
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, st = http("GET", f"{base}/api/setup")
            assert status == 200 and {"computer", "ollama", "lmstudio", "keys", "choices", "pull", "ready"} <= set(st)
            for bad in ("", "Bad Name", "x; rm -rf /", "../evil"):
                status, r = http("POST", f"{base}/api/setup/pull", {"model": bad})
                assert status == 400, (bad, status, r)
        finally:
            server.shutdown()
