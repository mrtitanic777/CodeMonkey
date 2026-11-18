"""The session list: unused sessions leave no row, and opening a session is not activity.
Also the page's files: compressed and cached by ETag."""

import gzip
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_webui import start_server  # noqa: E402


def test_unused_sessions_drop_out_and_opening_keeps_the_place():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        server.shutdown()
        used = app.create_session(tmp)
        app.store.append_item(used.id, {"kind": "user", "text": "hello"})
        named = app.create_session(tmp, title="kept by its name")
        empty = app.create_session(tmp)
        ids = [e["id"] for e in app.list_sessions()]
        assert empty.id in ids   # open: still listed

        # an old time for the used one, then it is closed and opened again
        index = app._load_index()
        for e in index:
            if e["id"] == used.id:
                e["last_active"] = "2026-01-01T10:00:00"
        app._save_index(index)
        app.sessions.clear()
        assert [e["id"] for e in app.list_sessions()] == [named.id, used.id]   # the empty one is gone
        app.reopen_session(used.id)
        row = next(e for e in app._load_index() if e["id"] == used.id)
        assert row["last_active"] == "2026-01-01T10:00:00", row   # opening is not activity

        # a restart removes the empty row for good
        from webui.app import WebApp

        again = WebApp(app.config_path, default_project=tmp)
        assert empty.id not in [e["id"] for e in again._load_index()]
    print("PASS test_unused_sessions_drop_out_and_opening_keeps_the_place")


def test_a_session_nobody_watches_is_let_go_and_comes_back():
    """A session holds a worker thread and the project's language servers. Once no page is
    watching it and its work is done, the app lets those go; the conversation is on disk, so the
    next request for it brings the session back."""
    from test_webui import http   # noqa: WPS433 - same folder

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            s1 = app.create_session(tmp)
            s2 = app.create_session(tmp)
            app.store.append_item(s1.id, {"kind": "user", "text": "hello"})
            app.store.append_item(s2.id, {"kind": "user", "text": "hello"})
            workers = [t for t in threading.enumerate() if t.name.startswith("websession")]
            assert len(workers) >= 2

            # one of them has a page listening: it must be left alone
            q, _ = s1.bus.subscribe_from(None)
            retired = app.close_idle_sessions(idle_seconds=0)
            assert retired == [s2.id], retired
            assert app.get_session(s1.id) is s1 and app.get_session(s2.id) is None
            s2._thread.join(5)
            assert not s2._thread.is_alive(), "its worker stopped with it"
            assert not s2.alive

            # working, or holding a plan, is never retired
            s3 = app.create_session(tmp)
            s3.status = "working"
            assert app.close_idle_sessions(idle_seconds=0) == []
            s3.status = "idle"
            s3._pending_plan = {"plan": "later"}
            assert app.close_idle_sessions(idle_seconds=0) == []
            s3._pending_plan = None

            # not idle long enough: kept
            s4 = app.create_session(tmp)
            assert app.close_idle_sessions(idle_seconds=3600) == []
            assert app.get_session(s4.id) is s4

            # the retired one answers as if it had never gone: the page just asks for it
            status, snap = http("GET", f"{base}/api/sessions/{s2.id}")
            assert status == 200 and snap["id"] == s2.id, (status, snap)
            assert app.get_session(s2.id) is not None
            app.bus.emit("sessions", {})
        finally:
            server.shutdown()
    print("PASS test_a_session_nobody_watches_is_let_go_and_comes_back")


def test_page_files_are_compressed_and_cached():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            req = urllib.request.Request(base + "/js/core.js", headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req) as r:
                body, etag = r.read(), r.headers["ETag"]
                assert r.headers["Content-Encoding"] == "gzip" and r.headers["Cache-Control"] == "no-cache"
            assert b"function" in gzip.decompress(body)
            try:
                urllib.request.urlopen(urllib.request.Request(base + "/js/core.js", headers={"If-None-Match": etag}))
                raise AssertionError("an unchanged file must answer 304")
            except urllib.error.HTTPError as exc:
                assert exc.code == 304
            with urllib.request.urlopen(base + "/") as r:
                assert b"fonts.css" not in r.read()   # the system's own fonts: nothing to download
        finally:
            server.shutdown()
    print("PASS test_page_files_are_compressed_and_cached")


def test_a_long_answer_is_sent_compressed():
    """A conversation's record is the biggest thing the API sends, and text like it packs down
    about four times. A page that can unpack it gets the small version; one that cannot, and any
    small answer, get it plain."""
    from test_webui import http   # noqa: WPS433 - same folder

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid = http("POST", f"{base}/api/sessions", {"project_dir": tmp})[1]["id"]
            session = app.get_session(sid)
            for n in range(80):   # a day of conversation, the case that matters
                session.transcript.append({"kind": "user", "text": f"question {n} " + "word " * 80})
                session.transcript.append({"kind": "assistant", "text": f"answer {n} " + "word " * 160})
            path = f"{base}/api/sessions/{sid}/history"

            def fetch(encoding):
                req = urllib.request.Request(path, headers={"Accept-Encoding": encoding})
                with urllib.request.urlopen(req) as r:
                    raw, headers = r.read(), r.headers
                body = gzip.decompress(raw) if headers.get("Content-Encoding") == "gzip" else raw
                return len(raw), headers, body   # the bytes actually sent

            plain, _, _ = fetch("identity")
            packed, headers, body = fetch("gzip")
            assert headers["Content-Encoding"] == "gzip", "a page that can unpack gets the small version"
            assert headers["Vary"] == "Accept-Encoding", "so a cache keeps the two apart"
            assert packed < plain / 2, (plain, packed)
            assert len(json.loads(body)["transcript"]) == 160, "and it still says the same thing"

            # a small answer is not worth packing, whatever the page will accept
            with urllib.request.urlopen(f"{base}/api/sessions") as r:
                assert r.headers.get("Content-Encoding") is None
        finally:
            server.shutdown()
    print("PASS test_a_long_answer_is_sent_compressed")


if __name__ == "__main__":
    test_unused_sessions_drop_out_and_opening_keeps_the_place()
    test_a_session_nobody_watches_is_let_go_and_comes_back()
    test_page_files_are_compressed_and_cached()
    test_a_long_answer_is_sent_compressed()
