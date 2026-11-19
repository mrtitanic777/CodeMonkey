"""Working with a model on this computer: effort fitted to each message, lean mode, the server's live state."""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui.effort import choose  # noqa: E402
from webui.models import profile_for, reasoning_params  # noqa: E402


def test_auto_effort_fits_the_message():
    assert choose("Hi!")[0] == "off" and choose("thanks")[0] == "off"
    assert choose("What does this function do?")[0] == "low"
    assert choose("Fix the crash in game.py")[0] == "high"
    assert choose("Traceback (most recent call last):\n  File 'x.py'\nKeyError: 'a'")[0] == "high"
    assert choose("Think hard about how the renderer should be split up")[0] == "high"
    assert choose("quickly, which port does the server use?")[0] == "low"
    assert choose("Add a settings page")[0] == "medium"


def test_strata_flash_next_thinks_more_or_less_without_changing_the_start_of_its_prompt():
    # its template writes low and xhigh into the start of the prompt (the whole conversation would be
    # read again at every change); medium and none write nothing there, and a budget sets the length
    profile = profile_for("qwen3.8-flash-next-iq3_s")
    assert profile.get("thinking") and profile["efforts"]["off"] == "none"
    levels = {e: reasoning_params(profile, e) for e in ("off", "low", "medium", "high", "auto")}
    assert levels["off"] == {"reasoning_effort": "none", "reasoning_budget_tokens": None}
    assert {p["reasoning_effort"] for e, p in levels.items() if e != "off"} == {"medium"}
    budgets = [levels[e]["reasoning_budget_tokens"] for e in ("low", "medium", "high")]
    assert budgets == sorted(budgets) and budgets[0] >= 1024
    assert levels["auto"] == levels["medium"], "nothing sent would be the server's own default, with its own start"


def test_a_step_after_tool_results_that_went_fine_thinks_less():
    import agent

    class Echo(agent.OpenAICompatClient):
        def _send(self, messages, tools, params):
            self.sent = dict(params)
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    c = Echo("http://127.0.0.1:9/v1", "", "m")
    c.params = {"reasoning_effort": "medium", "reasoning_budget_tokens": 16384}
    first = [{"role": "user", "content": "build it"}]
    fine = first + [{"role": "assistant", "content": "", "tool_calls": []},
                    {"role": "tool", "tool_call_id": "1", "content": "[exit 0]\nbuilt"}]
    failed = first + [{"role": "assistant", "content": "", "tool_calls": []},
                      {"role": "tool", "tool_call_id": "1", "content": "[exit 1]\nerror: missing ;"}]
    c.chat(first, [])
    assert c.sent["reasoning_budget_tokens"] == 16384, "the turn's first step plans with the whole budget"
    c.chat(fine, [])
    assert c.sent["reasoning_budget_tokens"] == 4096, "carrying on after it worked: a quarter"
    c.chat(failed, [])
    assert c.sent["reasoning_budget_tokens"] == 16384, "after a failure: the whole budget again"
    c.params["reasoning_budget_tokens"] = 1024
    c.chat(fine, [])
    assert c.sent["reasoning_budget_tokens"] == 1024, "never below the floor"
    assert c.params["reasoning_budget_tokens"] == 1024, "the client's own setting is not changed"
    # a model with no word for "none" thinks as little as it can instead
    assert reasoning_params({"thinking": True, "efforts": {"low": "low"}}, "off") == {"reasoning_effort": "low"}


def test_lean_mode_offers_the_core_tools_and_loads_the_rest_on_request():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers["here"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "", "models": ["m"]}
            app.config.providers["away"] = {"base_url": "https://api.example.com/v1", "api_key_env": "", "models": ["m"]}
            s = app.create_session(tmp)
            names = lambda: {t["function"]["name"] for t in s.loop.registry.schemas()}  # noqa: E731
            s.settings["provider"] = "away"
            assert "code_callers" in names() and "memory_save" in names()      # a hosted model: everything
            s.settings["provider"] = "here"
            lean = names()
            assert {"read", "edit", "bash", "grep", "tools_load"} <= lean and "code_callers" not in lean, lean
            assert "Loaded" in s.loop.registry.execute("tools_load", {"group": "graph"})
            assert "code_callers" in names()
            app.settings["lean_local"] = False
            assert "memory_save" in names()
            s.close()
        finally:
            server.shutdown()


def test_one_off_requests_ask_a_local_server_not_to_keep_them():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers["here"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "", "models": ["m"]}
            app.config.providers["away"] = {"base_url": "https://api.example.com/v1", "api_key_env": "", "models": ["m"]}
            s = app.create_session(tmp)
            s.settings["provider"], s.settings["model"] = "here", "m"
            assert s._raw_client(one_shot=True).params.get("strata_checkpoint") is False
            assert "strata_checkpoint" not in s._raw_client().params     # the conversation itself is kept
            s.settings["provider"] = "away"
            assert "strata_checkpoint" not in s._raw_client(one_shot=True).params
            s.close()
        finally:
            server.shutdown()


def test_kessie_sessions_keep_one_conversation_and_their_own_memory():
    from kessie_client import KeSSieClient
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers["kessie"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "",
                                              "client": "kessie", "models": ["m"]}
            s = app.create_session(tmp)
            s.settings["provider"], s.settings["model"] = "kessie", "m"
            turn = s._build_client()
            assert isinstance(turn, KeSSieClient), "KeSSie keeps the history itself: no summarizing in front"
            assert turn.conversation_id == "cm-" + s.id == s._raw_client().conversation_id
            assert s._raw_client(one_shot=True).conversation_id != turn.conversation_id
            s.close()
        finally:
            server.shutdown()


def test_kessie_tool_calls_sent_already_parsed_are_used():
    from kessie_client import KeSSieClient

    class _Reply(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\"path\": \"a\"}"}}]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Reply)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        reply = KeSSieClient(f"http://127.0.0.1:{server.server_port}/v1", "", "m").chat([{"role": "user", "content": "x"}], [])
        calls = reply["choices"][0]["message"]["tool_calls"]
        assert [(c["function"]["name"], c["function"]["arguments"]) for c in calls] == [("read", "{\"path\": \"a\"}")]
    finally:
        server.shutdown()


def test_a_kessie_provider_stays_kessie_when_saved():
    from webui.providers import CLIENTS

    assert "kessie" in CLIENTS


class _Metrics(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"engine": {"vram_free_mib": 2048},
                           "live": {"state": "reading", "phase": "reading the prompt", "prompt_read": 1500,
                                    "prompt_total": 6000, "tok_s": None},
                           "requests": [{"duration_s": 10.0, "output_tokens": 300, "prompt_tokens": 6000, "reused": 4500}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_local_server_says_what_it_is_doing():
    from webui.modellive import live

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Metrics)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state = live(f"http://127.0.0.1:{server.server_port}/v1")
        assert state["state"] == "reading" and state["read"] == 1500 and state["total"] == 6000, state
        assert state["last_tok_s"] == 30.0 and state["last_reused"] == 4500 and state["vram_free_mib"] == 2048
        assert live("https://api.example.com/v1") is None   # only servers on this computer are asked
    finally:
        server.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("PASS")
