"""Tests for the web UI (stdlib only).

Starts the real HTTP server in a thread on an ephemeral port and drives it
over HTTP with scripted fake model clients. No browser, no network beyond
localhost.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from http.server import ThreadingHTTPServer  # noqa: E402

from webui.app import WebApp, WebSession  # noqa: E402
from webui.server import WebHandler  # noqa: E402


# ------------------------------------------------------------- fakes ---


class FakeClient:
    """Scripted stand-in for a model client."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        assert self.script, "FakeClient ran out of scripted responses"
        return self.script.pop(0)


def text_msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def tool_call_msg(name, args, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


class SSEReader(threading.Thread):
    """Reads an SSE stream in the background, collecting (type, data).

    Uses http.client directly: urllib's response wrapper blocks on
    read(n) for streaming bodies, while http.client de-chunks
    incrementally and returns available bytes.
    """

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.events = []
        self._cond = threading.Condition()
        self._stopping = False
        self._conn = None

    def run(self):
        import http.client
        import urllib.parse

        parts = urllib.parse.urlsplit(self.url)
        try:
            conn = http.client.HTTPConnection(
                parts.hostname, parts.port, timeout=30
            )
            conn.request(
                "GET",
                parts.path + (("?" + parts.query) if parts.query else ""),
                headers={"Accept": "text/event-stream"},
            )
            resp = conn.getresponse()
            if resp.status != 200:
                return
        except Exception:
            return
        self._conn = conn
        buf = b""
        while not self._stopping:
            try:
                piece = resp.read(1)
            except Exception:
                break
            if not piece:
                break
            buf += piece
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                self._parse(frame.decode("utf-8", "replace"))

    def _parse(self, frame):
        etype, data = None, {}
        for line in frame.split("\n"):
            if line.startswith("event:"):
                etype = line[len("event:"):].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[len("data:"):].strip())
                except (json.JSONDecodeError, ValueError):
                    data = {}
        if etype and etype != "ping":
            with self._cond:
                self.events.append((etype, data))
                self._cond.notify_all()

    def wait_for(self, etype, timeout=15):
        deadline = time.time() + timeout
        with self._cond:
            while time.time() < deadline:
                for t, d in self.events:
                    if t == etype:
                        return d
                self._cond.wait(timeout=0.2)
        raise AssertionError(
            f"timed out waiting for SSE event {etype!r}; got "
            f"{[t for t, _ in self.events]}"
        )

    def seen(self, etype):
        with self._cond:
            return any(t == etype for t, _ in self.events)

    def stop(self):
        self._stopping = True
        # Closing the connection from here would wait for the reader thread's blocked read.
        # The test server sends a keep-alive twice a second (see start_server), so the thread
        # sees the flag almost at once and the connection can then be closed freely.
        self.join(timeout=3)
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass


# ------------------------------------------------------------ harness ---


def write_config(path, workspace):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "agent_name: TestAgent\n"
            f'workspace: "{workspace}"\n'
            'provider: "fake"\n'
            "providers:\n"
            '  fake:\n'
            '    base_url: "http://127.0.0.1:9/v1"\n'
            '    api_key_env: ""\n'
        )


def start_server(tmp):
    ws = os.path.join(tmp, "ws")
    cfg_path = os.path.join(tmp, "config.yaml")
    write_config(cfg_path, ws)
    app = WebApp(cfg_path, default_project=tmp)
    WebHandler.app = app
    WebHandler._load_static()
    WebHandler.ping_seconds = 0.5   # so a test's stream reader can stop promptly
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    return app, server, base


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except (json.JSONDecodeError, ValueError):
            payload = {}
        return exc.code, payload


def make_session_with_fake(base, project_dir, script):
    status, snap = http("POST", base + "/api/sessions", {"project_dir": project_dir})
    assert status == 201, (status, snap)
    session_id = snap["id"]
    fake = FakeClient(script)
    # Patch the session's client factory (WebApp keeps the live object).
    app = WebHandler.app
    session = app.get_session(session_id)
    session._build_client = lambda provider=None: fake
    return session_id, fake


# --------------------------------------------------------------- tests ---


def test_static_pages():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            for path in ("/", "/style.css", "/js/core.js", "/js/boot.js"):
                req = urllib.request.Request(base + path)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    assert resp.status == 200, path
            # re-fetch index for the id checks
            with urllib.request.urlopen(base + "/", timeout=10) as resp:
                html = resp.read().decode("utf-8")
            for element_id in (
                'id="chat"', 'id="input"', 'id="perm-modal"',
                'id="model-menu"', 'id="folder-menu"', 'id="plan-body"',
                'id="tasks-body"', 'id="checkpoints"', 'id="sessions-drawer"',
                'id="session-list"', 'id="composer"', 'id="ctx-meter"',
                'id="inspector"', 'id="send-btn"', 'id="settings-modal"',
                'id="kernel-modal"',
            ):
                assert element_id in html, f"missing {element_id}"
            with urllib.request.urlopen(base + "/js/events.js", timeout=10) as resp:
                js = resp.read().decode("utf-8")
            assert "EventSource" in js and "handleEvent" in js
        finally:
            server.shutdown()
    print("PASS test_static_pages")


def test_session_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            status, snap = http("POST", base + "/api/sessions", {"project_dir": tmp})
            assert status == 201, (status, snap)
            sid = snap["id"]
            assert snap["status"] == "idle"
            status, snap2 = http("GET", f"{base}/api/sessions/{sid}")
            assert status == 200 and snap2["id"] == sid, (status, snap2)
            status, _ = http("DELETE", f"{base}/api/sessions/{sid}")
            assert status == 200, status
            status, _ = http("GET", f"{base}/api/sessions/{sid}")
            assert status == 404, status
        finally:
            server.shutdown()
    print("PASS test_session_lifecycle")


def test_message_turn_events():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(base, tmp, [text_msg("hello there")])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            status, _ = http(
                "POST", f"{base}/api/sessions/{sid}/message", {"text": "hi"}
            )
            assert status == 200, status
            reader.wait_for("turn_start")
            token = reader.wait_for("token")
            assert token["text"] == "hello there", token
            reader.wait_for("turn_end")
            assert fake.calls == 1, fake.calls
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_message_turn_events")


def test_permission_allow_flow():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "note.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("AAA\n")
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(
                base, tmp,
                [
                    tool_call_msg(
                        "edit",
                        {"path": "note.txt", "old_text": "AAA\n", "new_text": "BBB\n"},
                    ),
                    text_msg("edited it"),
                ],
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "edit the file"})
            reader.wait_for("tool_call")
            preq = reader.wait_for("permission_request")
            assert preq["tool"] == "edit", preq
            assert "BBB" in preq["preview"] or "AAA" in preq["preview"], preq
            status, _ = http(
                "POST",
                f"{base}/api/sessions/{sid}/permission",
                {"request_id": preq["request_id"], "decision": "allow"},
            )
            assert status == 200, status
            reader.wait_for("permission_resolved")
            reader.wait_for("tool_result")
            token = reader.wait_for("token")
            assert token["text"] == "edited it", token
            reader.wait_for("turn_end")
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "BBB\n"
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_permission_allow_flow")


def test_permission_deny_flow():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "note.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("AAA\n")
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(
                base, tmp,
                [
                    tool_call_msg(
                        "edit",
                        {"path": "note.txt", "old_text": "AAA\n", "new_text": "BBB\n"},
                    ),
                    text_msg("understood, not editing"),
                ],
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "edit it"})
            preq = reader.wait_for("permission_request")
            status, _ = http(
                "POST",
                f"{base}/api/sessions/{sid}/permission",
                {
                    "request_id": preq["request_id"],
                    "decision": "deny",
                    "message": "not now",
                },
            )
            assert status == 200, status
            reader.wait_for("turn_end")
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "AAA\n", "file must be unchanged after deny"
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_permission_deny_flow")


def test_plan_flow():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(
                base, tmp, [text_msg("1. Do the thing\n2. Verify it"), text_msg("built!")]
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http(
                "POST",
                f"{base}/api/sessions/{sid}/message",
                {"text": "/plan add a feature"},
            )
            plan = reader.wait_for("plan_ready")
            assert "Do the thing" in plan["plan"], plan
            assert os.path.isfile(os.path.join(tmp, "plan.md"))
            status, _ = http(
                "POST", f"{base}/api/sessions/{sid}/plan", {"action": "approve"}
            )
            assert status == 200, status
            token = reader.wait_for("token")
            assert token["text"] == "built!", token
            # the plan turn ended earlier; wait for the build turn to end too, so nothing is still
            # writing into the folder when it is removed
            deadline = time.time() + 20
            while time.time() < deadline and sum(1 for t, _ in reader.events if t == "turn_end") < 2:
                time.sleep(0.05)
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_plan_flow")


def test_tab_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        readers = []
        try:
            sid_a, _ = make_session_with_fake(base, tmp, [text_msg("A here")])
            sid_b, _ = make_session_with_fake(base, tmp, [text_msg("B here")])
            for sid in (sid_a, sid_b):
                reader = SSEReader(f"{base}/api/sessions/{sid}/events")
                reader.start()
                reader.wait_for("state")
                readers.append(reader)
            http("POST", f"{base}/api/sessions/{sid_a}/message", {"text": "ping A"})
            readers[0].wait_for("turn_end")
            time.sleep(1.0)
            assert not readers[1].seen("turn_start"), "session B saw A's turn"
            assert not readers[1].seen("token"), "session B saw A's token"
        finally:
            for reader in readers:
                reader.stop()
            server.shutdown()
    print("PASS test_tab_isolation")


def test_rewind_requires_confirmation():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            status, body = http("POST", f"{base}/api/sessions/{sid}/rewind", {})
            assert status == 400, (status, body)
            status, body = http(
                "POST", f"{base}/api/sessions/{sid}/rewind", {"confirm": True}
            )
            assert status == 200, (status, body)
            done = reader.wait_for("rewind_done")
            assert "Nothing to rewind" in done["report"], done
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_rewind_requires_confirmation")


def test_files_endpoint_and_gitignore_skips():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".git"))
        os.makedirs(os.path.join(tmp, "src"))
        with open(os.path.join(tmp, "src", "main.py"), "w") as fh:
            fh.write("x = 1\n")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            status, files = http(
                "GET", f"{base}/api/sessions/{sid}/files?prefix=src"
            )
            assert status == 200, status
            assert "src/main.py" in files, files
            assert not any(f.startswith(".git") for f in files), files
        finally:
            server.shutdown()
    print("PASS test_files_endpoint_and_gitignore_skips")


def test_settings_update_and_auto_permission_mode():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "auto.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("AAA\n")
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(
                base, tmp,
                [
                    # Read first: the kernel guard asks about an edit to a file
                    # the agent has not looked at, even in auto mode.
                    tool_call_msg("read", {"path": "auto.txt"}),
                    tool_call_msg(
                        "edit",
                        {"path": "auto.txt", "old_text": "AAA\n", "new_text": "BBB\n"},
                    ),
                    text_msg("auto-edited"),
                ],
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            status, settings = http(
                "POST",
                f"{base}/api/sessions/{sid}/settings",
                {"permission_mode": "auto", "effort": "low"},
            )
            assert status == 200, status
            assert settings["settings"]["permission_mode"] == "auto"
            assert settings["settings"]["effort"] == "low"
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "go"})
            reader.wait_for("turn_end")
            time.sleep(0.5)
            assert not reader.seen("permission_request"), "auto mode must not prompt"
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "BBB\n"
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_settings_update_and_auto_permission_mode")


def test_always_allow_rule():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "n.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("1\n")
        app, server, base = start_server(tmp)
        reader = None
        try:
            sid, fake = make_session_with_fake(
                base, tmp,
                [
                    tool_call_msg(
                        "edit",
                        {"path": "n.txt", "old_text": "1\n", "new_text": "2\n"},
                    ),
                    tool_call_msg(
                        "edit",
                        {"path": "n.txt", "old_text": "2\n", "new_text": "3\n"},
                    ),
                    text_msg("done twice"),
                ],
            )
            reader = SSEReader(f"{base}/api/sessions/{sid}/events")
            reader.start()
            reader.wait_for("state")
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "edit twice"})
            preq = reader.wait_for("permission_request")
            status, _ = http(
                "POST",
                f"{base}/api/sessions/{sid}/permission",
                {"request_id": preq["request_id"], "decision": "always"},
            )
            assert status == 200, status
            reader.wait_for("turn_end")
            time.sleep(0.5)
            # second edit must not have prompted again
            count = sum(
                1 for t, _ in reader.events if t == "permission_request"
            )
            assert count == 1, f"expected 1 permission prompt, got {count}"
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "3\n"
        finally:
            if reader:
                reader.stop()
            server.shutdown()
    print("PASS test_always_allow_rule")


def main():
    tests = [
        test_static_pages,
        test_session_lifecycle,
        test_message_turn_events,
        test_permission_allow_flow,
        test_permission_deny_flow,
        test_plan_flow,
        test_tab_isolation,
        test_rewind_requires_confirmation,
        test_files_endpoint_and_gitignore_skips,
        test_settings_update_and_auto_permission_mode,
        test_always_allow_rule,
        test_dom_id_references_resolve,
        test_sse_error_handler_ignores_connection_drops,
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


def page_js(root=None):
    """The page's script as the browser runs it: webui/static/js/*.js in the order index.html loads them."""
    import re

    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static = os.path.join(root, "webui", "static")
    html = open(os.path.join(static, "index.html"), encoding="utf-8").read()
    names = re.findall(r'<script src="/(js/[\w-]+\.js)"></script>', html)
    assert names, "index.html loads no scripts from js/"
    return "\n".join(open(os.path.join(static, *n.split("/")), encoding="utf-8").read() for n in names)


def test_dom_id_references_resolve():
    """Every $("...") / getElementById("...") in app.js must resolve to an id
    present in index.html or created dynamically in JS.

    Regression test: $("sessions-list") (plural) vs id="session-list"
    (singular) threw on every page load and the suite never noticed.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "webui", "static", "index.html"), encoding="utf-8").read()
    js = page_js(root)
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    refs = set(re.findall(r'\$\("([^"]+)"\)', js))
    refs |= set(re.findall(r'getElementById\("([^"]+)"\)', js))
    dynamic = set(re.findall(r'\.id\s*=\s*"([^"]+)"', js))
    dynamic |= set(re.findall(r'id="([^"]+)"', js))  # ids in JS-built HTML
    missing = sorted(r for r in refs if r not in html_ids and r not in dynamic)
    assert not missing, f"JS references missing DOM ids: {missing}"
    print("PASS test_dom_id_references_resolve")


def test_sse_error_handler_ignores_connection_drops():
    """The SSE 'error' listener must ignore DOM connection-error events.

    Regression test: addEventListener("error") received both server-sent
    error payloads (MessageEvent) and connection drops (plain Event); the
    latter rendered a scary 'Something went wrong' banner for a condition
    the browser heals by reconnecting.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = page_js(root)
    assert "instanceof MessageEvent" in js, "SSE error handler must gate on MessageEvent"
    print("PASS test_sse_error_handler_ignores_connection_drops")


if __name__ == "__main__":
    main()
