"""Pictures from the web saved into the project, and shown in replies (webui/pictures.py)."""

import os
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui.pictures import kind_of, pictures_on_page, save_picture, size_of  # noqa: E402


def png(w=3, h=2) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PAGE = ('<html><head><meta property="og:image" content="/media/cat.png"></head><body>'
        '<img src="/static/logo.png" alt="logo"><img src="dog.png" alt="A dog" width="400">'
        '<img src="/pix.gif" width="1"></body></html>')


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body, kind = {
            "/media/cat.png": (png(8, 5), "image/png"),
            "/dog.png": (png(), "image/png"),
            "/page": (PAGE.encode(), "text/html"),
            "/fake.png": (b"<svg onload=alert(1)></svg>", "image/png"),   # a name and type that lie
        }.get(self.path, (b"", ""))
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", kind or "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_what_a_picture_is_is_read_from_the_file_itself():
    assert kind_of(png()) == ("PNG", ".png") and size_of(png(8, 5)) == (8, 5)
    assert kind_of(b"<svg></svg>") is None and kind_of(b"GIF89a....") == ("GIF", ".gif")


def test_a_page_names_its_main_picture_first_and_no_icons():
    found = pictures_on_page(PAGE, "https://example.com/a/page")
    assert [u for u, _ in found] == ["https://example.com/media/cat.png", "https://example.com/a/dog.png"]
    assert found[1][1] == "A dog"


def test_saving_a_picture_and_a_pages_main_picture():
    server, base = _site()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            said = save_picture(tmp, base + "/dog.png")
            assert "Saved pictures/dog.png (PNG, 3×2" in said and "![" in said, said
            assert open(os.path.join(tmp, "pictures", "dog.png"), "rb").read() == png()
            again = save_picture(tmp, base + "/dog.png")
            assert "pictures/dog-2.png" in again, "an existing file is never overwritten"
            page = save_picture(tmp, base + "/page", "art/hero")
            assert "Saved art/hero.png (PNG, 8×5" in page and "main picture" in page and "/dog.png (A dog)" in page, page
            assert save_picture(tmp, base + "/fake.png").startswith("ERROR"), "only real pictures are saved"
            assert save_picture(tmp, base + "/dog.png", "../outside.png").startswith("ERROR")
            assert not os.path.exists(os.path.join(os.path.dirname(tmp), "outside.png"))
            assert save_picture(tmp, "file:///etc/passwd").startswith("ERROR")
    finally:
        server.shutdown()


def test_a_reply_can_show_only_pictures_in_the_project():
    from test_webui import start_server
    from webui.webtools import is_network_tool

    assert is_network_tool("picture_save"), "it reaches the web, so it asks like the other web tools"
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pictures"))
        open(os.path.join(tmp, "pictures", "a.png"), "wb").write(png())
        open(os.path.join(tmp, "notes.png"), "wb").write(b"not a picture")
        app, server, base = start_server(tmp)
        try:
            s = app.create_session(tmp)
            assert "picture_save" in {t["function"]["name"] for t in s.loop.registry.schemas()} or \
                "picture_save" in str(s.loop.registry.execute("tools_load", {"group": "web"}))
            url = f"{base}/api/sessions/{s.id}/picture?path="
            with urllib.request.urlopen(url + "pictures/a.png") as resp:
                assert resp.headers["Content-Type"] == "image/png" and resp.read() == png()
            for bad in ("notes.png", "../x.png", "pictures/none.png", "config.yaml"):
                try:
                    urllib.request.urlopen(url + bad)
                    raise AssertionError(bad + " was served")
                except urllib.error.HTTPError as exc:
                    assert exc.code in (400, 403, 404), (bad, exc.code)
            s.close()
        finally:
            server.shutdown()
