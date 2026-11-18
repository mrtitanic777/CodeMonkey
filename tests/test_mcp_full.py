"""MCP: local and remote servers, resources, prompts, images, live tool changes, timeouts, and the
app-wide hub behind Settings > MCP servers."""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import keystore  # noqa: E402

keystore.use_memory()

from coder.mcp import MCPClient, MCPError  # noqa: E402
from fake_mcp_full import Fake  # noqa: E402

SERVER = os.path.join(TESTS, "fake_mcp_full.py")


def _wait(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_local_server_everything():
    client = MCPClient("full", sys.executable, [SERVER], timeout=1)
    changes = []
    client.on_change = changes.append
    try:
        client.connect()
        assert client.server_info["name"] == "fullfake"
        names = [t["name"] for t in client.list_tools()]
        assert names == ["echo", "picture", "slow", "grow", "pinged", "ask_model", "ask_user"], names   # both pages
        assert client.call_tool("echo", {"text": "hi"}) == "echo:hi"
        pic = client.call_tool("picture", {})
        assert pic.startswith("here it is") and "[[cm-image " in pic
        import images

        _text, parts = images.extract(pic, True)
        assert parts and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert client.list_resources()[0]["uri"] == "mem://notes"
        assert client.list_resource_templates()[0]["uriTemplate"] == "mem://item/{id}"
        assert "hello notes" in client.read_resource("mem://notes")
        assert client.get_prompt("review", {"focus": "speed"}) == "Please review with a focus on speed"
        assert client.call_tool("pinged", {}) == "pong"                               # we answered its ping
        assert client.call_tool("grow", {}) == "grown"
        assert _wait(lambda: "tools" in changes)
        assert "extra" in [t["name"] for t in client.list_tools()]
        try:
            client.call_tool("slow", {})
            raise AssertionError("a call past the timeout must fail")
        except MCPError as exc:
            assert "no answer to tools/call in 1s" in str(exc)
        assert _wait(lambda: any("cancelled" in line for line in client.log))         # and was cancelled there
    finally:
        client.close()
    print("PASS test_local_server_everything")


class _HttpFake(BaseHTTPRequestHandler):
    fake = None
    deleted = []
    stream_calls = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = type(self).fake.handle(msg)
        if reply is None:
            self.send_response(202)
            self.end_headers()
            return
        self.send_response(200)
        if msg.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", "sess-1")
        elif self.headers.get("Mcp-Session-Id") != "sess-1":
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -1, "message": "no session id"}}
        if msg.get("method") == "tools/call" and type(self).stream_calls:
            body = ("event: message\ndata: " + json.dumps(reply) + "\n\n").encode()
            self.send_header("Content-Type", "text/event-stream")
        else:
            body = json.dumps(reply).encode()
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        type(self).deleted.append(self.headers.get("Mcp-Session-Id"))
        self.send_response(200)
        self.end_headers()


def test_remote_server_over_http_with_its_key():
    _HttpFake.fake = Fake(lambda m: None)
    _HttpFake.deleted = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HttpFake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    seen = {}
    real_post = _HttpFake.do_POST

    def spy(self):
        seen["auth"] = self.headers.get("Authorization")
        real_post(self)
    _HttpFake.do_POST = spy
    try:
        from coder.mcp import client_for

        keystore.set("mcp:remote", "tok-123")
        client = client_for("remote", {"url": f"http://127.0.0.1:{httpd.server_port}/mcp"})
        client.connect()
        assert len(client.list_tools()) == 7
        assert client.call_tool("echo", {"text": "far"}) == "echo:far"   # answered as an event stream
        assert seen["auth"] == "Bearer tok-123"
        client.close()
        assert _HttpFake.deleted == ["sess-1"]                           # the session was ended
    finally:
        _HttpFake.do_POST = real_post
        httpd.shutdown()
        keystore.delete("mcp:remote")
    print("PASS test_remote_server_over_http_with_its_key")


class _LegacySse(BaseHTTPRequestHandler):
    """The older transport: GET opens an event stream that names where to POST; replies come on it."""
    stream = None
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: endpoint\ndata: /messages?session=7\n\n")
        self.wfile.flush()
        type(self).stream = self.wfile
        while type(self).stream is self.wfile:
            time.sleep(0.05)

    def do_POST(self):
        if not self.path.startswith("/messages"):
            self.send_response(405)   # not a Streamable HTTP server
            self.end_headers()
            return
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(202)
        self.end_headers()
        reply = _LegacySse.fake.handle(msg)
        if reply is not None:
            with _LegacySse.lock:
                _LegacySse.stream.write(("event: message\ndata: " + json.dumps(reply) + "\n\n").encode())
                _LegacySse.stream.flush()


def test_an_sse_only_server_is_reached_through_the_fallback():
    _LegacySse.fake = Fake(lambda m: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LegacySse)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        client = MCPClient("old", url=f"http://127.0.0.1:{httpd.server_port}/sse")
        client.connect()
        assert client.transport.post_url.endswith("/messages?session=7")
        assert client.call_tool("echo", {"text": "old"}) == "echo:old"
        client.close()
    finally:
        _LegacySse.stream = None
        httpd.shutdown()
    print("PASS test_an_sse_only_server_is_reached_through_the_fallback")


def test_hub_shares_servers_and_keeps_sessions_current():
    from test_webui import start_server

    from webui.mcphub import MCPHub

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mcp.json")
        with open(path, "w") as fh:
            json.dump({"servers": {"full": {"command": sys.executable, "args": [SERVER],
                                            "read_only_tools": ["echo"]}}, "note": "kept"}, fh)
        app, server, base = start_server(tmp)
        try:
            app.mcp = MCPHub(path)
            app.mcp.start_all()
            assert _wait(lambda: app.mcp.servers["full"].status == "connected"), app.mcp.status()
            session = app.create_session(tmp)
            names = session.loop.registry.names()
            assert "mcp__full__echo" in names and "mcp_resources" in names and "mcp_read" in names
            from coder.mcp import READ_ONLY_TOOLS

            assert "mcp__full__echo" in READ_ONLY_TOOLS and "mcp__full__grow" not in READ_ONLY_TOOLS
            assert "hello notes" in session.loop.registry.execute("mcp_read", {"server": "full", "uri": "mem://notes"})
            assert app.mcp.run_prompt("full", "review", "speed") == "Please review with a focus on speed"
            try:
                app.mcp.run_prompt("full", "review", "")
                raise AssertionError("a required argument must be asked for")
            except ValueError as exc:
                assert "focus" in str(exc)

            # the server adds a tool: the open session is offered it without being reopened
            app.mcp.servers["full"].client.call_tool("grow", {})
            assert _wait(lambda: "mcp__full__extra" in session.loop.registry.names())

            # off: the tools go, and the file says so (and keeps what it does not know)
            app.mcp.set_enabled("full", False)
            assert "mcp__full__echo" not in session.loop.registry.names()
            saved = json.load(open(path))
            assert saved["servers"]["full"]["disabled"] is True and saved["note"] == "kept"

            # a remote one: the key goes to Credential Manager, never the file
            app.mcp.save({"name": "far", "url": "http://127.0.0.1:9/mcp", "key": "secret-xyz"})
            assert keystore.get("mcp:far") == "secret-xyz" and "secret-xyz" not in open(path).read()
            assert _wait(lambda: app.mcp.servers["far"].status == "failed")   # nothing listens there
            app.mcp.remove("far")
            assert keystore.get("mcp:far") is None and "far" not in json.load(open(path))["servers"]
            session.close()
        finally:
            app.mcp.close_all()
            server.shutdown()
    print("PASS test_hub_shares_servers_and_keeps_sessions_current")


def test_phones_cannot_change_mcp_servers():
    from test_webui import http, start_server

    from webui.server import WebHandler

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        real = WebHandler._remote, WebHandler._has_key
        WebHandler._remote = lambda self: True
        WebHandler._has_key = lambda self: True
        try:
            assert http("POST", base + "/api/mcp", {"name": "x", "command": "calc.exe"})[0] == 403
            assert http("DELETE", base + "/api/mcp/x")[0] == 403
            assert http("GET", base + "/api/mcp")[0] == 200
        finally:
            WebHandler._remote, WebHandler._has_key = real
            server.shutdown()
    print("PASS test_phones_cannot_change_mcp_servers")



class _Model:
    """A stand-in model client: says what it was asked."""
    model = "stand-in"

    def __init__(self):
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append(messages)
        return {"choices": [{"message": {"content": "model says: " + messages[-1]["content"]}}]}


def test_servers_can_ask_for_the_model_and_for_answers():
    from coder.mcp import check_elicited, elicit_fields, sample_with

    model = _Model()

    class Host:
        def sample(self, client, params):
            return sample_with(model, params)

        def elicit(self, client, params):
            fields = elicit_fields(params)
            assert [f["name"] for f in fields] == ["name", "age", "tea", "size"] and fields[0]["required"]
            return {"action": "accept", "content": check_elicited(fields, {"name": "Ada", "age": "36", "tea": "yes"})}

    client = MCPClient("full", sys.executable, [SERVER], timeout=10)
    client.host = Host()
    try:
        client.connect()
        assert client.call_tool("ask_model", {"text": "say hi"}) == "model says: say hi"
        assert model.seen[0][0] == {"role": "system", "content": "Be brief."}           # its system prompt too
        answer = json.loads(client.call_tool("ask_user", {}))
        assert answer == {"action": "accept", "content": {"name": "Ada", "age": 36, "tea": True}}, answer
    finally:
        client.close()

    plain = MCPClient("full", sys.executable, [SERVER], timeout=10)   # no host: nothing is offered
    try:
        plain.connect()
        assert plain._capabilities() == {"roots": {"listChanged": False}}
        assert plain.call_tool("ask_model", {}).startswith("refused: sampling/createMessage is not supported")
    finally:
        plain.close()

    fields = elicit_fields({"requestedSchema": {"properties": {"n": {"type": "number"}, "c": {"enum": ["a"]}},
                                                "required": ["n"]}})
    for bad, why in (({}, "required"), ({"n": "x"}, "number"), ({"n": 1, "c": "z"}, "one of")):
        try:
            check_elicited(fields, bad)
            raise AssertionError(f"{bad} must be refused")
        except ValueError as exc:
            assert why in str(exc), exc
    print("PASS test_servers_can_ask_for_the_model_and_for_answers")


def test_a_web_session_answers_for_its_own_tool_calls():
    from test_webui import http, start_server

    from coder.mcp import CALLER
    from webui.app import _McpHost
    from webui.mcphub import MCPHub

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mcp.json")
        with open(path, "w") as fh:
            json.dump({"servers": {"full": {"command": sys.executable, "args": [SERVER]}}}, fh)
        app, server, base = start_server(tmp)
        try:
            app.mcp = MCPHub(path)
            app.mcp.host = _McpHost()
            app.mcp.start_all()
            assert _wait(lambda: app.mcp.servers["full"].status == "connected"), app.mcp.status()
            assert app.mcp.status()["servers"][0]["offers"] == ["elicitation", "sampling"]
            session = app.create_session(tmp)
            session.settings["permission_mode"] = "auto"
            model = _Model()
            session._raw_client = lambda *a, **kw: model   # sampling asks for a one-shot client
            CALLER.value = session          # this thread plays the session's worker
            out = session.loop.registry.execute("mcp__full__ask_model", {"text": "hello"})
            assert out.startswith("model says: hello"), out

            # a form: shown to the page, checked when answered, and the server gets the typed values
            box = {}
            def call():
                CALLER.value = session
                box["out"] = session.loop.registry.execute("mcp__full__ask_user", {})
            t = threading.Thread(target=call)
            t.start()
            assert _wait(lambda: session._elicits)
            rid = next(iter(session._elicits))
            assert session.snapshot()["pending_elicitation"]["server"] == "full"
            url = f"{base}/api/sessions/{session.id}/elicitation"
            assert http("POST", url, {"request_id": rid, "action": "accept", "content": {"age": "3"}})[0] == 400
            assert http("POST", url, {"request_id": rid, "action": "accept",
                                      "content": {"name": "Bo", "size": "L"}})[0] == 200
            t.join(10)
            assert json.loads(box["out"].split("\n")[0]) == {"action": "accept", "content": {"name": "Bo", "size": "L"}}

            CALLER.value = None             # a request nobody's tool call is waiting on is refused
            assert "no CodeMonkey session" in session.loop.registry.execute("mcp__full__ask_model", {})
            session.close()
        finally:
            CALLER.value = None
            app.mcp.close_all()
            server.shutdown()
    print("PASS test_a_web_session_answers_for_its_own_tool_calls")


class _OAuthFake(BaseHTTPRequestHandler):
    """An MCP server behind OAuth, and its authorization server, at one address."""
    tokens = set()
    codes = {}
    registered = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/.well-known/oauth-protected-resource/mcp":
            return self._send(200, {"resource": base + "/mcp", "authorization_servers": [base]})
        if self.path == "/.well-known/oauth-authorization-server":
            return self._send(200, {"authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                                    "registration_endpoint": base + "/register",
                                    "code_challenge_methods_supported": ["S256"]})
        self._send(405, {})

    def do_POST(self):
        import base64
        import hashlib
        import urllib.parse

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/register":
            body = json.loads(raw)
            type(self).registered.append(body["redirect_uris"])
            return self._send(201, {"client_id": "cid-1"})
        if self.path == "/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            if form["grant_type"] == "authorization_code":
                want = type(self).codes.pop(form["code"], None)
                proof = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if want != proof or form["client_id"] != "cid-1":
                    return self._send(400, {"error": "invalid_grant"})
                type(self).tokens.add("tok-1")
                return self._send(200, {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 60})
            if form["grant_type"] == "refresh_token" and form["refresh_token"] == "ref-1":
                type(self).tokens.add("tok-2")
                return self._send(200, {"access_token": "tok-2", "expires_in": 60})
            return self._send(400, {"error": "invalid_grant"})
        token = (self.headers.get("Authorization") or "")[7:]
        if token not in type(self).tokens:
            meta = f"http://127.0.0.1:{self.server.server_port}/.well-known/oauth-protected-resource/mcp"
            return self._send(401, {"error": "unauthorized"},
                              {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
        reply = Fake(lambda m: None).handle(json.loads(raw))
        if reply is None:
            self.send_response(202)
            self.end_headers()
            return
        self._send(200, reply)


def test_a_remote_server_is_signed_in_to_with_oauth():
    import urllib.parse

    from coder import mcpauth
    from coder.mcp import MCPAuthRequired, client_for

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OAuthFake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg = {"url": f"http://127.0.0.1:{httpd.server_port}/mcp"}
    try:
        try:
            client_for("guarded", cfg).connect()
            raise AssertionError("a 401 must ask for a sign-in")
        except MCPAuthRequired as exc:
            challenge = exc.challenge
        assert "resource_metadata" in challenge
        url = mcpauth.begin("guarded", cfg, "http://127.0.0.1:47811/mcp/oauth/callback", challenge)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        assert url.startswith(cfg["url"][:-4] + "/authorize?") and q["client_id"] == "cid-1"
        assert q["code_challenge_method"] == "S256" and q["resource"] == cfg["url"]
        assert _OAuthFake.registered == [["http://127.0.0.1:47811/mcp/oauth/callback"]]
        _OAuthFake.codes["code-9"] = q["code_challenge"]            # the user signed in
        try:
            mcpauth.finish("not-a-state", "code-9")
            raise AssertionError("an unknown state must be refused")
        except mcpauth.AuthError:
            pass
        assert mcpauth.finish(q["state"], "code-9") == "guarded"
        assert keystore.get("mcp:guarded") == "tok-1" and mcpauth.signed_in("guarded")

        client = client_for("guarded", cfg)
        client.connect()
        assert client.call_tool("echo", {"text": "in"}) == "echo:in"
        _OAuthFake.tokens.discard("tok-1")                         # the token expires
        assert client.call_tool("echo", {"text": "again"}) == "echo:again"   # renewed by itself
        assert keystore.get("mcp:guarded") == "tok-2"
        client.close()
        mcpauth.forget("guarded")
        assert not mcpauth.signed_in("guarded") and keystore.get("mcp:guarded") is None
    finally:
        httpd.shutdown()
        mcpauth.forget("guarded")
    print("PASS test_a_remote_server_is_signed_in_to_with_oauth")

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
