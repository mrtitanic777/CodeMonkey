"""Using CodeMonkey from anywhere, through Tailscale (webui/anywhere.py): requests passed on from the
Tailscale address must pair like any other device, and the Home Screen pieces are there."""

import http.client as raw_http
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_webui import start_server  # noqa: E402

HOST = "my-pc.tail1234.ts.net"


def _ask(port, method, path, headers=None, body=None):
    conn = raw_http.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    resp = conn.getresponse()
    out = (resp.status, resp.getheader("Set-Cookie") or "", resp.getheader("Content-Type") or "",
           resp.read().decode("utf-8", "replace"))
    conn.close()
    return out


def test_requests_passed_on_from_tailscale_must_pair():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        port = int(base.rsplit(":", 1)[1])
        try:
            far = {"Host": HOST, "X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "me@example.com"}
            # this computer's own browser: as always
            assert _ask(port, "GET", "/api/settings")[0] == 200
            # switched off: the Tailscale name is just another name pointed at this computer, refused
            assert _ask(port, "GET", "/", far)[0] == 403
            # anything a proxy on this computer passes on is another device, even addressed to localhost
            assert _ask(port, "GET", "/api/settings", {"X-Forwarded-For": "100.64.0.7"})[0] == 401
            assert _ask(port, "GET", "/api/settings", {"Forwarded": "for=100.64.0.7"})[0] == 401
            app.anywhere.on, app.anywhere.host = True, HOST
            status, _, _, page = _ask(port, "GET", "/", far)
            assert status == 401 and "6-digit code" in page and "apple-touch-icon" in page
            assert _ask(port, "GET", "/api/settings", far)[0] == 401
            assert _ask(port, "GET", "/api/anywhere", far)[0] == 401
            st = app.phone.status()
            assert st["enabled"] and st["anywhere"] == "https://" + HOST and st["link"].startswith(f"https://{HOST}/?key=")
            form = dict(far, **{"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://" + HOST,
                                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
            status, cookie, _, _ = _ask(port, "POST", "/pair", form, "code=" + st["code"])
            assert status == 302 and "cm_dev=" in cookie, (status, cookie)
            paired = dict(far, Cookie=cookie.split(";")[0])
            assert _ask(port, "GET", "/api/settings", paired)[0] == 200
            assert _ask(port, "GET", "/", paired)[0] == 200
            # paired from afar is still not the computer: no switching remote access, no pairing codes
            assert _ask(port, "GET", "/api/anywhere", paired)[0] == 403
            assert _ask(port, "POST", "/api/anywhere", dict(paired, Origin="https://" + HOST), '{"enabled": false}')[0] == 403
            rows = app.devices.listing("")
            assert rows and rows[0]["address"] == "100.64.0.7" and "iPhone" in rows[0]["name"], rows
        finally:
            server.shutdown()


def test_the_home_screen_pieces_open_before_pairing():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        port = int(base.rsplit(":", 1)[1])
        try:
            far = {"X-Forwarded-For": "100.64.0.7"}
            status, _, ctype, body = _ask(port, "GET", "/manifest.webmanifest", far)
            assert status == 200 and "manifest" in ctype and '"display": "standalone"' in body
            for icon in ("/icon-180.png", "/icon-512.png", "/icon-maskable.png"):
                status, _, ctype, _ = _ask(port, "GET", icon, far)
                assert status == 200 and ctype == "image/png", (icon, status, ctype)
            page = _ask(port, "GET", "/")[3]
            assert "apple-mobile-web-app-capable" in page and "viewport-fit=cover" in page and "/manifest.webmanifest" in page
        finally:
            server.shutdown()


def test_starting_waits_for_https_to_be_allowed_and_stops_cleanly(monkeypatch):
    import subprocess

    from webui import anywhere as mod

    real_popen = subprocess.Popen
    said = "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n  https://login.tailscale.com/f/serve?node=abc\n"
    monkeypatch.setattr(mod.Anywhere, "look", lambda self: {"installed": True, "running": True, "host": HOST})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args, **kw: real_popen(
        [sys.executable, "-c", f"import sys,time; sys.stdout.write({said!r}); sys.stdout.flush(); time.sleep(60)"], **kw))
    place = mod.Anywhere()
    place.port = 8080
    st = place.start(wait=10)
    assert st["state"] == "enable" and st["enable_link"] == "https://login.tailscale.com/f/serve?node=abc", st
    assert not st["on"] and not place.allows(HOST)
    proc = place._proc
    place.stop()
    assert place.state == "off" and proc.wait(timeout=10) is not None

    # allowed already: the command finishes at once and the address is on
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args, **kw: real_popen(
        [sys.executable, "-c", "print('Available within your tailnet')"], **kw))
    st = place.start(wait=10)
    assert place.wait_until(("on",), 10) == "on"
    assert place.status()["address"] == "https://" + HOST and place.allows(HOST + ":443") and not place.allows("evil.example")
