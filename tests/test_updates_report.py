"""The update check (webui/updates.py) and Report a problem (webui/report.py), with the real server."""

import io
import json
import os
import sys
import tempfile
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import http, start_server  # noqa: E402

from webui import report, updates  # noqa: E402
from webui.app import app_version  # noqa: E402


def test_share_links_become_the_file_and_versions_compare():
    assert updates.direct_link("https://drive.google.com/file/d/AbC_12-x/view?usp=sharing") == \
        "https://drive.google.com/uc?export=download&id=AbC_12-x"
    assert updates.direct_link("https://drive.google.com/open?id=XYZ") == "https://drive.google.com/uc?export=download&id=XYZ"
    assert updates.direct_link("https://www.dropbox.com/s/abc/latest.json?dl=0").endswith("latest.json?dl=1")
    assert updates.direct_link("https://example.com/latest.json") == "https://example.com/latest.json"
    assert updates.version_tuple("0.4.10") > updates.version_tuple("0.4.9")
    assert updates.parse("0.5.0\n")["version"] == "0.5.0"
    assert updates.parse('{"version": "1.2.3", "notes": "x"}')["notes"] == "x"
    for bad in ("", "{}", '{"version": "soon"}'):
        try:
            updates.parse(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


class _Release(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


def test_check_finds_a_newer_version_and_says_where_to_get_it():
    release = ThreadingHTTPServer(("127.0.0.1", 0), _Release)
    threading.Thread(target=release.serve_forever, daemon=True).start()
    link = f"http://127.0.0.1:{release.server_port}/latest.json"
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, u = http("GET", base + "/api/updates")
            assert status == 200 and u["current"] == app_version() and not u["newer"]
            http("POST", base + "/api/settings", {"update_url": link})
            mine = updates.this_system()
            _Release.body = json.dumps({"version": "99.0.0", "notes": "Faster", "page": "https://example.com/all",
                                        "downloads": {mine: "https://example.com/mine"}}).encode()
            status, u = http("POST", base + "/api/updates/check")
            assert status == 200 and u["newer"] and u["latest"] == "99.0.0" and u["download"] == "https://example.com/mine", u
            assert http("GET", base + "/api/updates")[1]["latest"] == "99.0.0"   # kept for the page
            _Release.body = app_version().encode()   # the same version: nothing to do
            u = http("POST", base + "/api/updates/check")[1]
            assert not u["newer"] and not u["error"], u
            _Release.body = b"<!DOCTYPE html><html>sign in</html>"   # a link that is not shared
            u = http("POST", base + "/api/updates/check")[1]
            assert "web page" in u["error"] and not u["newer"], u
        finally:
            server.shutdown()
            release.shutdown()


def test_report_is_one_zip_with_no_secrets():
    report.capture_output(tempfile.gettempdir())   # as serve() does
    print("model said: api_key=sk-ant-abcdefghijklmnop and Bearer abcdefghijklmnopqrstuv")
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            req = urllib.request.Request(base + "/api/report", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"note": "It froze. My password: hunter22", "conversation": True,
                                                          "session_id": snap["id"],
                                                          "page": {"agent": "test", "errors": ["TypeError: x is null"]}}).encode())
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.headers["Content-Type"] == "application/zip"
                assert "CodeMonkey-report-" in resp.headers["Content-Disposition"]
                data = resp.read()
        finally:
            server.shutdown()
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"report.txt", "settings.json", "server.log", "page-errors.txt", "conversation.md"} <= names, names
    everything = "".join(zf.read(n).decode("utf-8") for n in names)
    assert "It froze" in everything and "TypeError: x is null" in everything and "Version: " in everything
    assert "model said" in zf.read("server.log").decode()
    for secret in ("sk-ant-abcdefghijklmnop", "abcdefghijklmnopqrstuv", "hunter22"):
        assert secret not in everything, secret


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
