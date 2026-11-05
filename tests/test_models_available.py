"""The model menu offers only models that are there: servers that are off, and providers with no key, are left out."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import http, start_server  # noqa: E402


def test_available_models_lists_only_what_answers_and_caches_it():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers = {"local": {"base_url": "http://x"}, "off": {"base_url": "http://y"},
                                    "keyless": {"base_url": "http://z"}}
            asked = []

            def fake(name):
                asked.append(name)
                if name == "local":
                    return {"ok": True, "base_url": "http://x", "models": ["coder-7b", "coder-32b"]}
                if name == "off":
                    return {"ok": False, "base_url": "http://y", "error": "refused", "models": []}
                raise RuntimeError("no API key yet")

            app.test_provider = fake
            status, live = http("GET", f"{base}/api/models/available?fresh=1")
            assert status == 200 and live == [{"provider": "local", "models": ["coder-7b", "coder-32b"], "base_url": "http://x"}], live
            assert sorted(asked) == ["keyless", "local", "off"]

            asked.clear()
            assert http("GET", f"{base}/api/models/available")[1] == live and asked == [], "kept for a few seconds"
        finally:
            server.shutdown()
