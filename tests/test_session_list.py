"""The session list: unused sessions leave no row, and opening a session is not activity.
Also the page's files: compressed and cached by ETag."""

import gzip
import json
import os
import sys
import tempfile
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


if __name__ == "__main__":
    test_unused_sessions_drop_out_and_opening_keeps_the_place()
    test_page_files_are_compressed_and_cached()
