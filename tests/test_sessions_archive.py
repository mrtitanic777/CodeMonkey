"""Archiving a session, and the findings block /review asks for."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import SSEReader, http, make_session_with_fake, start_server, text_msg  # noqa: E402
from webui.app import review_prompt  # noqa: E402


def test_archive_hides_nothing_on_disk_and_comes_back():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [text_msg("done")])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "hello"})
            reader.wait_for("turn_end")
            reader.stop()

            assert http("POST", f"{base}/api/sessions/{sid}/archive", {})[0] == 200
            row = next(e for e in http("GET", f"{base}/api/sessions")[1] if e["id"] == sid)
            assert row.get("archived") is True
            # still a session on disk: its history is there
            assert http("GET", f"{base}/api/sessions/{sid}/history")[0] == 200

            assert http("POST", f"{base}/api/sessions/{sid}/archive", {"archived": False})[0] == 200
            row = next(e for e in http("GET", f"{base}/api/sessions")[1] if e["id"] == sid)
            assert "archived" not in row
            assert http("POST", f"{base}/api/sessions/feedface/archive", {})[0] == 404
        finally:
            server.shutdown()


def test_review_prompts_ask_for_a_findings_block():
    for kind in ("review", "security"):
        text = review_prompt(kind, "")
        assert "```findings" in text and '"severity"' in text and "Do not change any files" in text
    assert "git diff HEAD...feature-x" in review_prompt("review", "feature-x")
