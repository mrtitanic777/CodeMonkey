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


def test_strata_flash_next_gets_its_own_words_for_effort():
    profile = profile_for("qwen3.8-flash-next-iq3_s")
    assert profile.get("thinking") and profile["efforts"]["off"] == "none"
    assert reasoning_params(profile, "off") == {"reasoning_effort": "none"}
    assert reasoning_params(profile, "high") == {"reasoning_effort": "high"}
    assert reasoning_params(profile, "auto") == {}
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
