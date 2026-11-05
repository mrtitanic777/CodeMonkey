"""MCP client: local servers over stdio, remote servers over Streamable HTTP (or the older SSE).

What a server offers becomes part of the agent:
  - tools        -> framework Tools named ``mcp__<server>__<tool>`` (approval-gated unless listed
                    in the server's reviewed "read_only_tools"); results keep their text, and
                    images come through as pictures (images.py), resources as their text;
  - resources    -> ``mcp_resources`` lists them, ``mcp_read`` reads one;
  - prompts      -> slash commands in the web UI (/<server>:<prompt>).
Lists are re-read when a server says they changed (notifications/*/list_changed). Long calls
report progress, are given up after the server's timeout and then cancelled on the server
(notifications/cancelled). Requests a server sends us are answered: ping, an empty roots list, and,
when the program running us offers them (a ``host``), sampling and elicitation:

  - sampling/createMessage: the server asks for an answer from our model. The host asks the user
    (like a tool call) and answers with the model of the session whose tool call is running;
  - elicitation/create: the server asks the user for a few fields (a form). The host shows it.

A server reached over HTTP that answers 401 can be signed in to with OAuth (coder/mcpauth.py):
the host is told, the user signs in in a browser, and the token is kept and refreshed.

Config file (``mcp.json``)::

    {"servers": {
        "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                   "env": {"FOO": "bar"}, "cwd": ".", "read_only_tools": ["read_file"]},
        "remote": {"url": "https://example.com/mcp", "headers": {"X-Team": "a"},
                   "auth_header": "Authorization", "timeout": 120, "disabled": false}
    }}

A remote server's key is never written here: it is kept in Windows Credential Manager
(keystore.py, under "mcp:<name>") and sent as ``<auth_header>: Bearer <key>``.

Hard protocol rule for stdio: the server's stdout carries only JSON-RPC frames; anything it logs
goes to stderr, whose tail is kept for the status page and error messages.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools import Tool

PROTOCOL_VERSION = "2025-06-18"
_READ_TIMEOUT = 30           # initialize and list calls
_CALL_TIMEOUT = 120          # a tool call, unless the server's config says otherwise
_LOG_LINES = 200


class MCPError(Exception):
    """The MCP server returned an error or misbehaved."""


class MCPAuthRequired(MCPError):
    """A remote server wants a sign-in (HTTP 401). ``challenge`` is its WWW-Authenticate header."""

    def __init__(self, message: str, challenge: str = "") -> None:
        super().__init__(message)
        self.challenge = challenge


#: Who is calling a tool on this thread (a web session, the terminal...). A server request that
#: arrives while that call runs (sampling, elicitation) is put to them. Set by the caller's thread.
CALLER = threading.local()


# ------------------------------------------------------------------ transports ---


class _Stdio:
    """A server we start: one JSON object per line on its stdin and stdout."""

    def __init__(self, client: "MCPClient", argv: List[str], env: Optional[Dict[str, str]], cwd: Optional[str]):
        self.client, self.argv, self.env, self.cwd = client, argv, env, cwd
        self.proc: Optional[subprocess.Popen] = None
        self._write = threading.Lock()

    def start(self) -> None:
        env = dict(os.environ)
        env.update(self.env or {})
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=self.cwd or None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise MCPError(f"could not start {self.argv[0]!r}: {exc}") from exc
        proc = self.proc

        def pump_out() -> None:
            try:
                for line in proc.stdout:
                    if not line.strip():
                        continue
                    try:
                        self.client._dispatch(json.loads(line))
                    except json.JSONDecodeError:
                        self.client._log("not JSON on stdout (logs belong on stderr): " + line.strip()[:200])
            except (OSError, ValueError):
                pass
            self.client._closed("the server closed its output")

        def pump_err() -> None:
            try:
                for line in proc.stderr:
                    if line.strip():
                        self.client._log(line.rstrip())
            except (OSError, ValueError):
                pass

        for target in (pump_out, pump_err):
            threading.Thread(target=target, name=f"mcp-{self.client.name}", daemon=True).start()

    def send(self, message: Dict[str, Any]) -> None:
        if self.proc is None or self.proc.poll() is not None:
            raise MCPError("the server is not running")
        with self._write:
            try:
                self.proc.stdin.write(json.dumps(message) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"could not write to the server: {exc}") from exc

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass


def _sse_events(resp):
    """(event, data) pairs from a text/event-stream response, as they arrive."""
    event, data = "message", []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


class _Http:
    """A remote server: Streamable HTTP (one endpoint; replies as JSON or an event stream), with a
    fallback to the older two-endpoint SSE transport when the server only speaks that."""

    def __init__(self, client: "MCPClient", url: str, headers: Dict[str, str], mode: str = "auto"):
        self.client, self.url, self.headers, self.mode = client, url, dict(headers or {}), mode
        self.session_id: Optional[str] = None
        self.post_url: Optional[str] = None    # legacy SSE: where messages are posted
        self._open: List[Any] = []
        self._stopped = False

    def start(self) -> None:
        if self.mode == "sse":
            self._start_legacy()

    def _request(self, method: str, url: str, body: Optional[bytes], accept: str) -> Any:
        headers = dict(self.headers, Accept=accept)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.client.protocol:
            headers["MCP-Protocol-Version"] = self.client.protocol
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=max(self.client.timeout, _READ_TIMEOUT))

    def send(self, message: Dict[str, Any]) -> None:
        if self.post_url:
            return self._send_legacy(message)
        body = json.dumps(message).encode("utf-8")
        try:
            try:
                resp = self._request("POST", self.url, body, "application/json, text/event-stream")
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or not self._refreshed():
                    raise
                resp = self._request("POST", self.url, body, "application/json, text/event-stream")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise MCPAuthRequired(f"{self.client.name} needs a sign-in (HTTP 401)",
                                      exc.headers.get("WWW-Authenticate", "") if exc.headers else "") from exc
            if self.mode == "auto" and message.get("method") == "initialize" and exc.code in (400, 404, 405):
                self._start_legacy()          # an SSE-only server: start over on its transport
                return self._send_legacy(message)
            if exc.code == 404 and self.session_id:
                raise MCPError("the server ended the session (HTTP 404); restart it") from exc
            raise MCPError(f"HTTP {exc.code}: {_http_body(exc)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"could not reach {self.url}: {getattr(exc, 'reason', exc)}") from exc
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        kind = resp.headers.get("Content-Type", "")
        if resp.status == 202 or "id" not in message:
            resp.close()
            return
        if "text/event-stream" in kind:
            self._open.append(resp)
            threading.Thread(target=self._read_stream, args=(resp,), daemon=True).start()
            return
        data = resp.read()
        resp.close()
        if data.strip():
            parsed = json.loads(data)
            for item in parsed if isinstance(parsed, list) else [parsed]:
                self.client._dispatch(item)

    def _refreshed(self) -> bool:
        """After a 401: a fresh token from the refresh token, if there is one (coder/mcpauth.py)."""
        refresh = getattr(self.client, "refresh_auth", None)
        token = refresh() if refresh else None
        if not token:
            return False
        header = getattr(self.client, "auth_header", "") or "Authorization"
        self.headers[header] = "Bearer " + token if header.lower() == "authorization" else token
        return True

    def listen(self) -> None:
        """Open the server's own event stream for notifications it sends unasked (optional)."""
        if self.post_url:
            return
        def run() -> None:
            try:
                resp = self._request("GET", self.url, None, "text/event-stream")
            except (urllib.error.URLError, OSError):
                return   # 405: the server has no such stream, which is allowed
            self._open.append(resp)
            self._read_stream(resp)
        threading.Thread(target=run, name=f"mcp-{self.client.name}-listen", daemon=True).start()

    def _read_stream(self, resp) -> None:
        try:
            for _event, data in _sse_events(resp):
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for item in parsed if isinstance(parsed, list) else [parsed]:
                    self.client._dispatch(item)
        except (OSError, ValueError, AttributeError):
            pass

    # the older transport: an event stream that first names the URL to post to
    def _start_legacy(self) -> None:
        url = self.url
        try:
            resp = self._request("GET", url, None, "text/event-stream")
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code} opening the event stream: {_http_body(exc)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"could not reach {url}: {getattr(exc, 'reason', exc)}") from exc
        ready = threading.Event()
        events = _sse_events(resp)
        self._open.append(resp)

        def run() -> None:
            try:
                for event, data in events:
                    if event == "endpoint" and not self.post_url:
                        self.post_url = urllib.parse.urljoin(url, data.strip())
                        ready.set()
                        continue
                    try:
                        self.client._dispatch(json.loads(data))
                    except json.JSONDecodeError:
                        continue
            except (OSError, ValueError, AttributeError):
                pass
            ready.set()
            if not self._stopped:
                self.client._closed("the event stream ended")

        threading.Thread(target=run, name=f"mcp-{self.client.name}-sse", daemon=True).start()
        if not ready.wait(_READ_TIMEOUT) or not self.post_url:
            raise MCPError("the server's event stream never said where to post messages")

    def _send_legacy(self, message: Dict[str, Any]) -> None:
        try:
            resp = self._request("POST", self.post_url, json.dumps(message).encode("utf-8"), "application/json")
            resp.close()
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code}: {_http_body(exc)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"could not reach {self.post_url}: {getattr(exc, 'reason', exc)}") from exc

    def close(self) -> None:
        self._stopped = True
        for resp in self._open:
            _abort(resp)
        self._open = []
        if self.session_id and not self.post_url:
            try:
                self._request("DELETE", self.url, None, "application/json").close()
            except Exception:  # noqa: BLE001 - ending a session is a courtesy
                pass


def _abort(resp: Any) -> None:
    """End an event stream another thread is reading. Closing the response alone waits for that
    reader (it holds the buffer's lock until a line arrives, which may be never): the socket is
    shut down first, which wakes it, and the close happens off this thread."""
    import socket

    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError, ValueError):
        pass
    threading.Thread(target=lambda: _quiet(resp.close), daemon=True).start()


def _quiet(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


def _http_body(exc: urllib.error.HTTPError) -> str:
    try:
        return " ".join(exc.read(400).decode("utf-8", "replace").split()) or str(exc.reason)
    except Exception:  # noqa: BLE001
        return str(exc.reason)


# ---------------------------------------------------------------------- client ---


class MCPClient:
    """One MCP server connection, local or remote."""

    def __init__(
        self,
        name: str,
        command: str = "",
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        *,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
        transport: str = "auto",
        cwd: Optional[str] = None,
        timeout: float = _CALL_TIMEOUT,
    ):
        self.name = name
        self.timeout = float(timeout or _CALL_TIMEOUT)
        self.protocol = ""
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.instructions = ""
        self.log: "collections.deque[str]" = collections.deque(maxlen=_LOG_LINES)
        self.on_change: Optional[Callable[[str], None]] = None   # "tools" | "resources" | "prompts" | "closed"
        self.progress: Dict[str, str] = {}
        # the program running us: .sample(client, params) and .elicit(client, params), each
        # optional; what it has is declared to the server at initialize
        self.host: Any = None
        self.callers: List[Any] = []                 # CALLER.value of each tool call running now
        self.refresh_auth: Optional[Callable[[], Optional[str]]] = None   # OAuth: a new token
        self.auth_header = "Authorization"
        self._ids = itertools.count(1)
        self._pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self.alive = False
        if url:
            self.transport: Any = _Http(self, url, headers or {}, transport if transport in ("http", "sse") else "auto")
        else:
            if not command:
                raise MCPError("a server needs a command or a url")
            self.transport = _Stdio(self, [command] + list(args or []), env, cwd)

    @property
    def proc(self):   # the local server's process, for callers that look at it
        return getattr(self.transport, "proc", None)

    # ------------------------------------------------------------ plumbing ---

    def _log(self, line: str) -> None:
        self.log.append(time.strftime("%H:%M:%S ") + line)

    def _closed(self, why: str) -> None:
        if not self.alive:
            return
        self.alive = False
        self._log("disconnected: " + why)
        with self._lock:
            waiting = list(self._pending.values())
        for q in waiting:
            q.put({"error": {"code": -32000, "message": "server disconnected: " + why}})
        if self.on_change:
            self.on_change("closed")

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "method" in message and "id" in message:        # the server asks us something
            self._answer(message)
            return
        if "method" in message:                              # a notification
            self._notified(message.get("method", ""), message.get("params") or {})
            return
        with self._lock:
            q = self._pending.get(message.get("id"))
        if q is not None:
            q.put(message)

    def _offers(self, what: str) -> bool:
        return callable(getattr(self.host, what, None))

    def _answer(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if method in ("sampling/createMessage", "elicitation/create"):
            # these wait for a person and a model: never on the thread reading the server
            threading.Thread(target=self._answer_host, args=(message,), name=f"mcp-{self.name}-ask",
                             daemon=True).start()
            return
        if method == "ping":
            reply: Dict[str, Any] = {"result": {}}
        elif method == "roots/list":
            reply = {"result": {"roots": []}}
        else:
            reply = {"error": {"code": -32601, "message": f"{method} is not supported by this client"}}
        self._reply(message, reply)

    def _answer_host(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        what = "sample" if method == "sampling/createMessage" else "elicit"
        params = message.get("params") or {}
        if not self._offers(what):
            reply: Dict[str, Any] = {"error": {"code": -32601, "message": f"{method} is not supported by this client"}}
        else:
            try:
                result = getattr(self.host, what)(self, params)
                reply = {"result": result} if isinstance(result, dict) else \
                    {"error": {"code": -1, "message": str(result or "declined")}}
            except Exception as exc:  # noqa: BLE001 - the server is told, we carry on
                reply = {"error": {"code": -1, "message": str(exc)[:300] or type(exc).__name__}}
        self._log(f"{method}: " + ("answered" if "result" in reply else reply["error"]["message"][:120]))
        self._reply(message, reply)

    def caller(self) -> Any:
        """Who the server's request is for: the most recent tool call still running."""
        return self.callers[-1] if self.callers else None

    def _reply(self, message: Dict[str, Any], reply: Dict[str, Any]) -> None:
        try:
            self.transport.send({"jsonrpc": "2.0", "id": message.get("id"), **reply})
        except MCPError:
            pass

    def _notified(self, method: str, params: Dict[str, Any]) -> None:
        if method.endswith("/list_changed"):
            kind = method.split("/")[1]                      # tools | resources | prompts
            self._log(f"the server's {kind} changed")
            if self.on_change:
                self.on_change(kind)
        elif method == "notifications/progress":
            token = str(params.get("progressToken", ""))
            total = params.get("total")
            text = params.get("message") or ""
            self.progress[token] = f"{params.get('progress')}" + (f"/{total}" if total else "") + (f" {text}" if text else "")
        elif method == "notifications/message":
            data = params.get("data")
            self._log(f"[{params.get('level', 'info')}] " + (data if isinstance(data, str) else json.dumps(data)[:300]))

    def _request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        if not self.alive and method != "initialize":
            raise MCPError(f"MCP server {self.name!r} is not connected")
        msg_id = next(self._ids)
        answer: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._pending[msg_id] = answer
        params = dict(params)
        if method == "tools/call":
            params["_meta"] = {"progressToken": str(msg_id)}
        try:
            self.transport.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            limit = timeout or _READ_TIMEOUT
            try:
                response = answer.get(timeout=limit)
            except queue.Empty:
                try:   # give up here, and tell the server to stop working on it
                    self.transport.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                         "params": {"requestId": msg_id, "reason": f"no answer in {limit:.0f}s"}})
                except MCPError:
                    pass
                raise MCPError(f"MCP server {self.name!r} gave no answer to {method} in {limit:.0f}s"
                               f"{self._stderr_hint()}") from None
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)
            self.progress.pop(str(msg_id), None)
        if "error" in response:
            err = response["error"] or {}
            raise MCPError(f"MCP error {err.get('code')}: {err.get('message')}{self._stderr_hint()}")
        return response.get("result")

    def _stderr_hint(self) -> str:
        time.sleep(0.05)
        tail = list(self.log)[-5:]
        return ("\n  server log:\n  " + "\n  ".join(tail)) if tail else ""

    def _paged(self, method: str, key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(50):
            result = self._request(method, {"cursor": cursor} if cursor else {}) or {}
            page = result.get(key) if isinstance(result, dict) else None
            items.extend(i for i in (page or []) if isinstance(i, dict))
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        return items

    # ----------------------------------------------------------------- api ---

    def connect(self) -> None:
        """Start (or reach) the server and run the initialize handshake."""
        self.transport.start()
        self.alive = True
        try:
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": self._capabilities(),
                "clientInfo": {"name": "codemonkey", "version": "0.2"},
            }, timeout=_READ_TIMEOUT)
        except Exception:
            self.close()
            raise
        if not isinstance(result, dict):
            self.close()
            raise MCPError("bad initialize response from MCP server")
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")[:2000]
        self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if isinstance(self.transport, _Http):
            self.transport.listen()
        self._log(f"connected: {self.server_info.get('name', self.name)} {self.server_info.get('version', '')}".strip())

    def _capabilities(self) -> Dict[str, Any]:
        caps: Dict[str, Any] = {"roots": {"listChanged": False}}
        if self._offers("sample"):
            caps["sampling"] = {}
        if self._offers("elicit"):
            caps["elicitation"] = {}
        return caps

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._paged("tools/list", "tools")

    def list_resources(self) -> List[Dict[str, Any]]:
        return self._paged("resources/list", "resources") if self.has("resources") else []

    def list_resource_templates(self) -> List[Dict[str, Any]]:
        if not self.has("resources"):
            return []
        try:
            return self._paged("resources/templates/list", "resourceTemplates")
        except MCPError:
            return []

    def list_prompts(self) -> List[Dict[str, Any]]:
        return self._paged("prompts/list", "prompts") if self.has("prompts") else []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        who = getattr(CALLER, "value", None)
        self.callers.append(who)
        try:
            result = self._request("tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout=self.timeout)
        finally:
            self.callers.remove(who)
        if not isinstance(result, dict):
            return "ERROR: bad tools/call response from MCP server"
        text = render_content(result.get("content"), self.name)
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"], indent=1)[:20000]
        if result.get("isError"):
            return "ERROR: MCP tool failed: " + (text or "(no message)")
        return text or "(empty result)"

    def read_resource(self, uri: str) -> str:
        result = self._request("resources/read", {"uri": uri}, timeout=self.timeout) or {}
        return render_content([dict(c, type="resource_contents") for c in (result.get("contents") or [])
                               if isinstance(c, dict)], self.name) or "(empty resource)"

    def get_prompt(self, name: str, arguments: Dict[str, str]) -> str:
        result = self._request("prompts/get", {"name": name, "arguments": arguments or {}}, timeout=self.timeout) or {}
        parts = []
        for message in result.get("messages") or []:
            body = render_content([message.get("content")] if isinstance(message.get("content"), dict)
                                  else message.get("content"), self.name)
            if body:
                parts.append(body)
        return "\n\n".join(parts)

    def close(self) -> None:
        self.alive = False
        self.transport.close()


def _shot_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "codemonkey-mcp")
    os.makedirs(path, exist_ok=True)
    return path


def render_content(content: Any, server: str = "") -> str:
    """MCP content blocks as text for the model; images become image markers (images.py)."""
    import base64

    import images

    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "resource_contents", "resource") and (item.get("data") or item.get("blob")
                                                                     or isinstance(item.get("resource"), dict)):
            inner = item.get("resource") if isinstance(item.get("resource"), dict) else item
            if inner.get("text") is not None:
                parts.append(f"[{inner.get('uri', 'resource')}]\n{inner['text']}" if kind != "image" else inner["text"])
                continue
            mime = str(inner.get("mimeType") or "")
            data = inner.get("data") or inner.get("blob") or ""
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime)
            if ext:
                path = os.path.join(_shot_dir(), f"{re.sub(r'[^A-Za-z0-9_-]', '_', server)[:20]}-{time.time_ns()}{ext}")
                try:
                    with open(path, "wb") as fh:
                        fh.write(base64.b64decode(data))
                    parts.append(f"(image from {server or 'the server'})\n" + images.mark(path))
                except (OSError, ValueError):
                    parts.append(f"(an image from {server} could not be decoded)")
            else:
                parts.append(f"({inner.get('uri', 'binary content')}: {mime or 'binary'}, about {len(data) * 3 // 4:,} bytes)")
        elif kind == "resource_contents" and item.get("text") is not None:
            parts.append(f"[{item.get('uri', 'resource')}]\n{item['text']}")
        elif kind == "resource_link":
            parts.append(f"(resource: {item.get('uri')}" + (f" · {item.get('name')}" if item.get("name") else "")
                         + (f" · {item.get('description')}" if item.get("description") else "") + ")")
        elif kind == "audio":
            parts.append(f"(audio, {item.get('mimeType', 'unknown type')}: not playable here)")
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------- tools from servers ---

#: Full names (mcp__server__tool) of tools the user has marked read-only in mcp.json with a
#: per-server "read_only_tools" list. A server's own claim that a tool is harmless is not
#: trusted: only this locally reviewed list lets a tool run without approval.
READ_ONLY_TOOLS: set = set()


def server_tools(client: MCPClient, cfg: Dict[str, Any], entries: List[Dict[str, Any]]) -> List[Tool]:
    """Framework Tools for *entries* (a server's tools/list)."""
    out: List[Tool] = []
    reviewed = cfg.get("read_only_tools") if isinstance(cfg, dict) else None
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        tool_name = entry["name"]
        read_only = isinstance(reviewed, list) and tool_name in reviewed
        if read_only:
            READ_ONLY_TOOLS.add(f"mcp__{client.name}__{tool_name}")
        schema = entry.get("inputSchema") or {"type": "object"}
        if not isinstance(schema, dict):
            schema = {"type": "object"}

        def _make_func(cli: MCPClient, tname: str):
            def _call(**kwargs: Any) -> str:
                try:
                    return cli.call_tool(tname, kwargs)
                except MCPError as exc:
                    return f"ERROR: {exc}"
            return _call

        title = entry.get("title") or (entry.get("annotations") or {}).get("title") or ""
        out.append(Tool(
            name=f"mcp__{client.name}__{tool_name}",
            description=f"[MCP:{client.name}] " + (f"{title}: " if title else "") +
                        str(entry.get("description") or "No description."),
            parameters=schema,
            func=_make_func(client, tool_name),
            # MCP tools can do anything the server allows; gate them, except
            # the ones the user has reviewed and listed as read-only.
            needs_approval=not read_only,
        ))
    return out


def load_mcp_tools(mcp_config_path: str, host: Any = None) -> Tuple[List[Tool], List[MCPClient]]:
    """Connect to configured MCP servers and wrap their tools (the terminal coder's entry point).

    Returns (tools, clients). Missing/unreadable config -> ([], []).
    A server that fails to start is skipped with a warning on stdout. *host* answers sampling and
    elicitation; one with a ``sign_in(name, cfg, challenge)`` is offered a server's OAuth sign-in.
    """
    tools: List[Tool] = []
    clients: List[MCPClient] = []
    servers = read_config(mcp_config_path)
    for server_name, cfg in servers.items():
        if cfg.get("disabled"):
            continue
        client = None
        for attempt in (1, 2):   # a second try after signing in
            try:
                client = client_for(server_name, cfg, os.path.dirname(os.path.abspath(mcp_config_path)))
                client.host = host
                client.connect()
                entries = client.list_tools()
                break
            except MCPAuthRequired as exc:
                client = None
                if attempt == 2 or not callable(getattr(host, "sign_in", None)) \
                        or not host.sign_in(server_name, cfg, exc.challenge):
                    print(f"Warning: MCP server {server_name!r} needs a sign-in; skipped")
                    break
            except MCPError as exc:
                print(f"Warning: MCP server {server_name!r} failed: {exc}")
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                client = None
                break
        if client is None:
            continue
        clients.append(client)
        tools.extend(server_tools(client, cfg, entries))
    return tools, clients


def read_config(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read MCP config {path!r}: {exc}")
        return {}
    servers = data.get("servers", data.get("mcpServers", {})) if isinstance(data, dict) else {}
    out = {}
    for name, cfg in (servers.items() if isinstance(servers, dict) else []):
        if isinstance(cfg, dict) and (cfg.get("command") or cfg.get("url")):
            out[str(name)] = cfg
        else:
            print(f"Warning: skipping MCP server {name!r}: it needs a command or a url")
    return out


def client_for(name: str, cfg: Dict[str, Any], base_dir: str = ".") -> MCPClient:
    """An MCPClient for one server entry of mcp.json (a remote one gets its stored key)."""
    timeout = float(cfg.get("timeout") or _CALL_TIMEOUT)
    if cfg.get("url"):
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        import keystore

        from coder import mcpauth

        key = keystore.get("mcp:" + name)
        header = str(cfg.get("auth_header") or "Authorization")
        if key:
            headers[header] = ("Bearer " + key) if header.lower() == "authorization" and " " not in key else key
        client = MCPClient(name, url=str(cfg["url"]), headers=headers, transport=str(cfg.get("transport") or "auto"),
                           timeout=timeout)
        client.auth_header = header
        client.refresh_auth = mcpauth.refresher(name)   # an OAuth sign-in's token is renewed on a 401
        return client
    args = cfg.get("args") or []
    if isinstance(args, str):
        args = shlex.split(args, posix=False)
    cwd = cfg.get("cwd")
    if cwd and not os.path.isabs(cwd):
        cwd = os.path.join(base_dir, cwd)
    return MCPClient(name, str(cfg["command"]), [str(a) for a in args],
                     {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}, cwd=cwd, timeout=timeout)


# ------------------------------------------------- what servers ask of us ---


def sampling_messages(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A sampling/createMessage request as chat messages for our model (text; pictures are named)."""
    out: List[Dict[str, Any]] = []
    if params.get("systemPrompt"):
        out.append({"role": "system", "content": str(params["systemPrompt"])[:20000]})
    for m in params.get("messages") or []:
        if not isinstance(m, dict):
            continue
        blocks = m.get("content") if isinstance(m.get("content"), list) else [m.get("content")]
        text = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                text.append(str(b.get("text", "")))
            elif isinstance(b, dict):
                text.append(f"({b.get('type', 'content')} from the server, {b.get('mimeType', '')}: not shown)")
        out.append({"role": "assistant" if m.get("role") == "assistant" else "user", "content": "\n".join(text)[:40000]})
    return out


def describe_sampling(client: MCPClient, params: Dict[str, Any]) -> str:
    """What the server asks the model, for the person deciding."""
    lines = [f"{client.name} asks your model for an answer"
             + (f" (at most {params['maxTokens']} tokens)" if params.get("maxTokens") else "") + ":"]
    for m in sampling_messages(params):
        lines.append(f"[{m['role']}] {m['content'][:1200]}")
    return "\n".join(lines)[:4000]


def sample_with(model_client: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Answer a sampling/createMessage request with *model_client* (any of our chat clients)."""
    messages = sampling_messages(params)
    if not any(m["role"] == "user" for m in messages):
        raise MCPError("the request had no message to answer")
    reply = model_client.chat(messages, [])
    message = ((reply or {}).get("choices") or [{}])[0].get("message") or {}
    text = str(message.get("content") or "")
    cap = int(params.get("maxTokens") or 0)
    if cap > 0:
        text = text[: cap * 4]   # about four characters a token
    return {"role": "assistant", "content": {"type": "text", "text": text},
            "model": str(getattr(model_client, "model", "") or "codemonkey"),
            "stopReason": "endTurn"}


def elicit_fields(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The form an elicitation/create asks for: a flat list of simple fields (the spec allows no more)."""
    schema = params.get("requestedSchema") or {}
    required = set(schema.get("required") or [])
    fields = []
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type") if spec.get("type") in ("string", "number", "integer", "boolean") else "string"
        fields.append({"name": str(name), "title": str(spec.get("title") or name), "type": kind,
                       "description": str(spec.get("description") or ""), "required": name in required,
                       "enum": [str(e) for e in spec.get("enum") or []], "enum_names": spec.get("enumNames") or [],
                       "default": spec.get("default"), "format": spec.get("format") or ""})
    return fields[:30]


def check_elicited(fields: List[Dict[str, Any]], content: Dict[str, Any]) -> Dict[str, Any]:
    """The answers typed to an elicitation, as the types the server asked for. Raises ValueError."""
    out: Dict[str, Any] = {}
    for f in fields:
        value = (content or {}).get(f["name"])
        if value in (None, ""):
            if f["required"]:
                raise ValueError(f"{f['title']} is required")
            continue
        if f["type"] == "boolean":
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "y", "on")
        elif f["type"] in ("number", "integer"):
            try:
                value = int(value) if f["type"] == "integer" else float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{f['title']} must be a number") from None
        else:
            value = str(value)
            if f["enum"] and value not in f["enum"]:
                raise ValueError(f"{f['title']} must be one of: {', '.join(f['enum'])}")
        out[f["name"]] = value
    return out
