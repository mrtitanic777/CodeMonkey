"""HTTP server for the web UI: static files, JSON API, SSE streams.

ThreadingHTTPServer: each request runs on its own thread. Agent work always
happens on per-session worker threads, never on request threads (except the
cheap synchronous reads).
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".idea", ".vscode",
}


def _fuzzy(needle: str, hay: str) -> bool:
    """Every letter of *needle* appears in *hay*, in order."""
    at = 0
    for ch in needle:
        at = hay.find(ch, at) + 1
        if not at:
            return False
    return True


_FILES_CACHE: Dict[str, tuple] = {}   # folder -> (when listed, files)


def _project_files(project_dir: str) -> list:
    """The project's files as relative paths. In a git repository, what git sees (tracked and
    untracked, without what .gitignore leaves out), as Claude Code lists them; otherwise a walk
    that skips the usual heavy folders. Kept for a few seconds so typing stays quick."""
    import subprocess
    import time
    now = time.monotonic()
    hit = _FILES_CACHE.get(project_dir)
    if hit and now - hit[0] < 5:
        return hit[1]
    files: list = []
    try:
        out = subprocess.run(["git", "-C", project_dir, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             capture_output=True, timeout=5)
        if out.returncode == 0:
            files = [f for f in out.stdout.decode("utf-8", errors="replace").split("\0") if f][:50000]
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        for root, dirs, names in os.walk(project_dir):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
            rel_root = os.path.relpath(root, project_dir)
            for name in sorted(names):
                files.append((name if rel_root == "." else os.path.join(rel_root, name)).replace(os.sep, "/"))
            if len(files) > 20000:   # a huge tree: enough to choose from
                break
    _FILES_CACHE[project_dir] = (now, files)
    return files


def _list_files(project_dir: str, prefix: str, limit: int = 50) -> list:
    """Files for @-mentions, best first, as Claude Code ranks them: the name starts with what you
    typed, then the path does, then the name or path contains it, then its letters in order."""
    needle = prefix.lstrip("/").lower().replace("\\", "/")
    ranked: list = []
    for rel in _project_files(project_dir):
        low = rel.lower()
        base = low.rsplit("/", 1)[-1]
        if not needle or base.startswith(needle):
            score = 0
        elif low.startswith(needle):
            score = 1
        elif needle in base:
            score = 2
        elif needle in low:
            score = 3
        elif _fuzzy(needle, base):
            score = 4
        else:
            continue
        ranked.append((score, rel.count("/"), len(rel), rel))
    ranked.sort()
    return [r[3] for r in ranked[:limit]]


def _file_tree(project_dir: str, limit: int = 20000) -> Dict[str, Any]:
    """The Files pane's tree: the project's files (as @-mentions see them) and what git calls
    changed in each: M changed, A added, ? new and not added, D deleted."""
    files = _project_files(project_dir)
    changed: Dict[str, str] = {}
    try:
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, timeout=5)
        if out.returncode == 0:
            entries = iter(out.stdout.decode("utf-8", errors="replace").split("\0"))
            for entry in entries:
                if len(entry) > 3:
                    code = entry[:2].replace(" ", "")[:1] or "M"
                    changed[entry[3:]] = "?" if code == "?" else code if code in "MAD" else "M"
                    if code in "RC":   # a rename or copy: the old name follows
                        next(entries, None)
    except (OSError, subprocess.SubprocessError):
        pass
    return {"folder": os.path.basename(os.path.normpath(project_dir)), "files": files[:limit],
            "truncated": len(files) > limit, "changed": changed}


class WebHandler(BaseHTTPRequestHandler):
    app = None  # set by serve()
    _static: Dict[str, tuple] = {}  # path -> (content_type, bytes, gzipped bytes or None, etag)
    # HTTP/1.1 so the SSE stream can use chunked transfer encoding
    # (read-until-close framing confuses non-browser HTTP clients).
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ io ---

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        pass

    def _json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status=status)

    def _read_json(self) -> Dict[str, Any]:
        """The request body as JSON. Read once per request and kept.

        The connection is kept alive, so a body left unread would be taken for the next
        request line; do_POST and do_DELETE therefore always call this first.
        """
        cached = getattr(self, "_body", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length", 0) or 0)
        data: Dict[str, Any] = {}
        if length:
            try:
                parsed = json.loads(self.rfile.read(length) or b"{}")
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = {}
        self._body = data
        return data

    #: When the server listens only on this computer, requests must also be addressed to this
    #: computer by name. That stops a web page from reaching it through a domain name it
    #: controls and points at 127.0.0.1.
    loopback_only = True
    #: How often an idle event stream sends a keep-alive.
    ping_seconds = 10.0
    #: Set when the server is reachable from other devices (started with --host 0.0.0.0, to use
    #: it from a phone). Every request must then carry this key, as a cookie the private link
    #: sets once. Without it anyone on the same network could drive the agent.
    access_key: Optional[str] = None

    def _remote(self) -> bool:
        """True when the request comes from another device, not this computer."""
        return self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError):   # the other side went away: nothing to report
            pass

    #: The paired device this request comes from ("" for this computer itself).
    _device = ""

    def _cookie(self, wanted: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == wanted:
                return value
        return ""

    def _has_key(self) -> bool:
        import hmac

        self._device = ""
        if self.access_key:   # one fixed key for everyone (set by whoever embeds the server)
            return hmac.compare_digest(self._cookie("cm_key"), self.access_key)
        if not self._remote():
            return True
        # another device: it must be one that was paired, and not disconnected since
        self._device = self.app.devices.check(self._cookie("cm_dev"), self.client_address[0]) or ""
        return bool(self._device)

    def _let_in(self) -> None:
        """Give this device a key of its own and send it to the app."""
        self.send_response(302)   # the key never stays in the address bar or the history
        self.send_header("Location", "/")
        if self.access_key:
            cookie = f"cm_key={self.access_key}"
        elif self._has_key():   # already paired (the code was scanned again): keep its key
            cookie = ""
        else:
            cookie = "cm_dev=" + self.app.devices.pair(
                self.headers.get("User-Agent") or "", self.client_address[0])
        if cookie:
            self.send_header("Set-Cookie", cookie + "; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _pair_page(self, note: str = "") -> None:
        """What a device sees before it is paired: one box for the 6-digit code."""
        note_html = f"<p class=e>{note}</p>" if note else ""
        page = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>CodeMonkey</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#15130f;"
            "color:#f1ebe1;font:16px/1.5 system-ui,sans-serif}form{width:min(340px,86vw);text-align:center}"
            "h1{font:400 34px/1.1 Georgia,serif;margin:0 0 6px}h1 i{color:#d26a43}p{color:#b3aa9b;margin:0 0 18px}"
            "input{width:100%;box-sizing:border-box;height:56px;border-radius:14px;border:1px solid #4a443a;"
            "background:#24201a;color:#f1ebe1;font:28px ui-monospace,monospace;text-align:center;"
            "letter-spacing:.3em;outline:0}input:focus{border-color:#d26a43}button{margin-top:12px;width:100%;"
            "height:48px;border-radius:12px;border:0;background:#d26a43;color:#1a0f0a;font:600 16px system-ui}"
            ".e{color:#e28a80}</style><form method=post action=/pair><h1>Code<i>Monkey</i></h1>"
            "<p>Enter the 6-digit code shown on your computer, in Settings.</p>" + note_html +
            "<input name=code inputmode=numeric autocomplete=one-time-code maxlength=7 autofocus "
            "aria-label='6-digit code'><button>Connect</button></form></html>"
        )
        data = page.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ask_for_key(self, path: str, query: Dict[str, list]) -> bool:
        """Deal with a device that is not let in yet. Returns True when the request was answered."""
        import hmac

        if not self.access_key and not self._remote():
            return False
        given = (query.get("key") or [""])[0]
        if self.access_key:
            scanned = bool(given) and hmac.compare_digest(given, self.access_key)
        else:
            scanned = bool(given) and self.app.phone.check_link(given)
        if scanned:   # the scanned link
            self._let_in()
            return True
        if self._has_key():
            return False
        if path.startswith("/api/"):
            self._error("this CodeMonkey is private: pair this device first", 401)
        else:
            self._pair_page()
        return True

    def _addressed_here(self) -> bool:
        if self._remote():
            return True   # another device must show the key instead
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in ("127.0.0.1", "localhost", "[::1]") or not self.loopback_only

    def _own_origin(self) -> bool:
        """True unless the request was sent by some other web page.

        The server listens only on this computer, but any page open in the browser could still
        send it requests blindly. Browsers attach an Origin header to those; a request that
        changes something is refused when its Origin is not this server's own address. A page the
        agent made (served sandboxed, so its origin is "null") is refused the same way.
        """
        if not self._addressed_here():
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").lower()
        return urllib.parse.urlparse(origin).netloc.lower() == host and bool(host)

    def _serve_artifact(self, artifact_id: str, filename: str) -> None:
        record = self.app.store.artifact(artifact_id)
        if record is None or record["filename"] != filename:
            return self._error("not found", 404)
        try:
            with open(self.app.artifact_path(record), "rb") as fh:
                data = fh.read()
        except OSError:
            return self._error("not found", 404)
        ext = os.path.splitext(filename)[1].lower()
        types = {".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml", ".json": "application/json",
                 ".csv": "text/csv"}
        self.send_response(200)
        self.send_header("Content-Type", types.get(ext, "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # A page the agent wrote runs in a sandbox with no origin: its scripts work, but it
        # cannot read this app's data, and it may not load anything from the internet.
        # The one thing it may talk to is its own small data file, so a page can be a little
        # app that remembers things between visits.
        data_url = f"http://{self.headers.get('Host', '')}/artifacts/{artifact_id}/data"
        self.send_header(
            "Content-Security-Policy",
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            f"img-src data:; font-src data:; connect-src {data_url}",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _artifact_data(self, artifact_id: str, save: bool) -> None:
        """A page's own data: one JSON value it can read and replace. Nothing else is reachable."""
        record = self.app.store.artifact(artifact_id)
        if record is None:
            return self._error("not found", 404)
        path = os.path.join(os.path.dirname(self.app.artifact_path(record)), "data.json")
        if save:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 1_000_000:
                return self._error("a page may keep at most 1 MB", 413)
            raw = self.rfile.read(length) if length else b"null"
            try:
                json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return self._error("the data must be JSON", 400)
            with open(path, "wb") as fh:
                fh.write(raw)
            data = b'{"saved": true}'
        else:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = b"null"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "null")   # the page's sandbox has no origin
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        if not self._addressed_here() or not re.fullmatch(r"/artifacts/[0-9a-f]+/data", self.path):
            return self._error("not found", 404)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session(self, session_id: str):
        session = self.app.get_session(session_id)
        if session is None:
            self._error(f"unknown session {session_id}", 404)
            return None
        return session

    # ---------------------------------------------------------- get ---

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self._addressed_here():
            return self._error("this server only answers to its own address", 403)
        # a page the agent made reads its own data without the key (its sandbox sends no cookies);
        # the address is unguessable and gives nothing but that page's own data
        if not re.fullmatch(r"/artifacts/[0-9a-f]+/data", path) and path != "/mcp/oauth/callback" \
                and self._ask_for_key(path, query):
            return
        try:
            if path == "/mcp/oauth/callback":
                # back from an MCP server's sign-in, in the system browser; the state is the secret
                if self._remote():
                    return self._error("a sign-in comes back to this computer only", 403)
                first = lambda k: (query.get(k) or [""])[0]  # noqa: E731
                try:
                    name = self.app.mcp.signed_in(first("state"), first("code"), first("error"))
                    note = f"Signed in to {html.escape(name)}. You can close this tab and go back to CodeMonkey."
                except ValueError as exc:
                    note = "The sign-in did not work: " + html.escape(str(exc))
                page = ("<!doctype html><meta charset=utf-8><title>CodeMonkey</title><body style='font:16px "
                        "system-ui,sans-serif;background:#15130f;color:#f1ebe1;display:grid;place-items:center;"
                        f"min-height:90vh'><p>{note}</p>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                return
            if path == "/":
                return self._serve_static("index.html")
            if path.count("/") == 1 and os.path.splitext(path)[1] in _STATIC_TYPES:
                return self._serve_static(path[1:])
            if path.startswith("/fonts/") and path.count("/") == 2 and path.endswith(".woff2"):
                return self._serve_static(path[1:])
            if path.startswith("/vendor/") and path.count("/") == 2 and path.endswith(".js"):   # bundled libraries
                return self._serve_static(path[1:])
            if path.startswith("/js/") and path.count("/") == 2 and path.endswith(".js"):   # the page's scripts
                return self._serve_static(path[1:])
            if path == "/api/config":
                return self._json(self.app.config_snapshot())
            if path == "/api/models/live":   # what a model server on this computer is doing now (modellive.py)
                from .modellive import live

                name = (query.get("provider") or [""])[0]
                entry = (self.app.config.providers or {}).get(name) or {}
                return self._json(live(entry.get("base_url") or "") or {})
            if path == "/api/models/available":   # what the model menu may offer: live models only
                return self._json(self.app.available_models(bool((query.get("fresh") or [""])[0])))
            if path == "/api/mcp":
                return self._json(dict(self.app.mcp.status(), prompts=self.app.mcp.prompts()))
            if path == "/api/hooks":
                return self._json(self.app.hook_list())
            if path == "/api/settings":
                return self._json(self.app.settings)
            if path == "/api/commands":
                return self._json(self.app.command_list())
            if path == "/api/hold":
                # Holds the page's load event so a headless screenshot waits for the content.
                import time

                time.sleep(min(5000, int((query.get("ms") or ["0"])[0] or 0)) / 1000)
                data = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/folders":
                return self._json(self.app.folders())
            if path == "/api/browse":
                try:
                    return self._json(self.app.browse((query.get("path") or [""])[0]))
                except ValueError as exc:
                    return self._error(str(exc), 400)
            if path == "/api/hardware":
                from . import machine

                data = machine.snapshot()
                data["min_free_ram_gb"] = self.app.settings.get("min_free_ram_gb", 0)
                data["stops"] = self.app.watchdog.tripped
                return self._json(data)
            if path == "/api/usage":
                return self._json(
                    self.app.usage.summary(self.app.settings.get("cost_rates") or {})
                )
            if path == "/api/updates":   # is there a newer CodeMonkey (the last answer)
                return self._json(self.app.updates.status())
            if path == "/api/about":
                return self._json(self.app.about())
            if path == "/api/phone":
                status = self.app.phone.status()
                if self._remote():   # a paired phone has no use for the code or the link
                    status.update(code="", link="", can_switch=False, remote=True)
                return self._json(status)
            if path == "/api/devices":
                rows = self.app.devices.listing(self._device)
                if self._remote():   # a device sees itself, not the others
                    rows = [r for r in rows if r["current"]]
                return self._json({"devices": rows, "remote": self._remote()})
            if path == "/api/phone/qr.svg":
                if self._remote():
                    return self._error("only on the computer itself", 403)
                from . import qr

                status = self.app.phone.status()
                if not status["enabled"]:
                    return self._error("not switched on", 404)
                data = qr.svg(status["link"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/memory":
                return self._json(self.app.memory_claims())
            if path == "/api/people":
                return self._json(self.app.people())
            if path == "/api/models":
                return self._json(self.app.models.overview())
            if path == "/api/bench":
                return self._json({"state": dict(self.app.bench.state), "trial": dict(self.app.trial.state),
                                   "results": self.app.store.bench_results()})
            m = re.fullmatch(r"/artifacts/([0-9a-f]+)/data", path)
            if m:
                return self._artifact_data(m.group(1), save=False)
            m = re.fullmatch(r"/artifacts/([0-9a-f]+)/([A-Za-z0-9._-]+)", path)
            if m:
                return self._serve_artifact(m.group(1), m.group(2))
            if path == "/api/artifacts":
                return self._json([
                    dict(a, url=f"/artifacts/{a['artifact_id']}/{a['filename']}") for a in self.app.store.artifacts()
                ])
            if path == "/api/reviews":
                return self._json(self.app.store.reviews())
            if path == "/api/persona":
                return self._json(self.app.persona())
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/statusline", path)
            if m:
                session = self.app.get_session(m.group(1))
                if session is None:
                    return self._error("unknown session", 404)
                return self._json({"text": self.app.statusline(session)})
            if path == "/api/lsp":
                from coder.lsp import LSP

                project = (query.get("project_dir") or [""])[0] or self.app.default_project
                live = next((s.lsp for s in list(self.app.sessions.values())
                             if os.path.normcase(s.project_dir) == os.path.normcase(project)), None)
                return self._json((live or LSP(project)).status())
            if path == "/api/styles":
                from coder import styles

                return self._json(styles.listing(self.app.styles((query.get("project_dir") or [""])[0])))
            if path == "/api/worktrees":
                return self._json(self.app.worktree_list())
            if path == "/api/plugins":
                return self._json(self.app.plugins.listing())
            if path == "/api/agents":
                project = (query.get("project_dir") or [""])[0]
                return self._json([a.as_dict() for a in self.app.agents(project).values()])
            if path == "/api/ideas":
                from . import ideas

                return self._json(ideas.listing(self.app, (query.get("project_dir") or [""])[0] or None))
            if path == "/api/skills":
                return self._json(self.app.skills_overview())
            m = re.fullmatch(r"/api/skills/([a-z0-9_-]+)", path)
            if m:
                detail = self.app.skill_detail(m.group(1))
                if detail is None:
                    return self._error("unknown skill", 404)
                return self._json(detail)
            if path == "/api/goals":
                return self._json(self.app.store.goals())
            if path == "/api/learning":
                return self._json(self.app.learner.status())
            if path == "/api/jobs":
                return self._json(self.app.jobs.listing())
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/runs", path)
            if m:
                return self._json(self.app.store.runs(m.group(1), 20))
            if path == "/api/activity":
                return self._json(
                    self.app.store.recent_activity(
                        (query.get("session") or [None])[0],
                        (query.get("type") or [None])[0],
                        int((query.get("limit") or ["20"])[0] or 20),
                    )
                )
            if path == "/api/codekb":
                return self._json(self.app.codeknow.status())
            if path == "/api/kernel":
                from . import kerneltool

                try:
                    return self._json(kerneltool.info((query.get("domain") or ["thren"])[0]))
                except ValueError as exc:
                    return self._error(str(exc), 400)
            if path == "/api/sessions":
                return self._json(self.app.list_sessions())
            if path == "/api/setup":   # Set up a model: Ollama, LM Studio, keys, this computer, what fits
                return self._json(self.app.modelsetup.status())
            if path == "/api/search":   # words inside past sessions (Ctrl K)
                return self._json(self.app.search_messages((query.get("q") or [""])[0]))
            if path == "/api/events":   # what every page hears: sessions appearing, changing, working
                return self._serve_sse(
                    self.app.bus, lambda: ("hello", {"seq": self.app.bus.seq}), self._resume_point(query))
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                return self._json(session.snapshot())
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/events", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                return self._serve_sse(
                    session.bus, lambda: ("state", session.snapshot()), self._resume_point(query))
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/export", path)
            if m:   # /export: the conversation as a Markdown file
                session = self._session(m.group(1))
                if session is None:
                    return
                data = session.export_markdown().encode("utf-8")
                name = re.sub(r"[^A-Za-z0-9 _-]+", "", session.title).strip()[:60] or "conversation"
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}.md"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/agents/([0-9a-f]+)", path)
            if m:   # a helper's whole run: its brief, every step, its report
                row = next((r for r in self.app.store.agents(m.group(1)) if r["agent_id"] == m.group(2)), None)
                if row is None:
                    return self._error("no such helper in this session", 404)
                return self._json({"id": row["agent_id"], "brief": row.get("prompt") or "", "status": row.get("status"),
                                   "agent": row.get("agent_type") or "", "report": row.get("final_response") or "",
                                   "items": self.app.store.items(row["agent_id"])})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/memory", path)
            if m:
                session = self._session(m.group(1))
                return self._json(session.memory_files()) if session is not None else None
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/processes/([A-Za-z0-9_.-]+)/logs", path)
            if m:   # a background program's latest output
                session = self._session(m.group(1))
                if session is None:
                    return
                run = session.processes.running.get(m.group(2))
                if run is None:
                    return self._error("no such program", 404)
                with run.changed:
                    lines = list(run.lines)[-200:]
                return self._json({"name": run.name, "state": run.state(), "lines": lines})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/(rewind|context)", path)
            if m:   # the messages you can rewind to; what the context holds
                session = self._session(m.group(1))
                if session is None:
                    return
                if m.group(2) == "context":
                    return self._json(session.context_usage())
                return self._json({"points": session.rewind_points()})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/git", path)
            if m:   # the branch bar's facts (?fresh=1 looks the pull request up again)
                session = self._session(m.group(1))
                if session is None:
                    return
                return self._json(session.git_state(bool((query.get("fresh") or [""])[0])))
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/file", path)
            if m:   # the file pane: one text file from the session's folder
                session = self._session(m.group(1))
                if session is None:
                    return
                try:
                    return self._json(session.read_file((query.get("path") or [""])[0]))
                except FileNotFoundError as exc:
                    return self._error(str(exc), 404)
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/picture", path)
            if m:   # a picture in the project, shown in a reply (webui/pictures.py)
                session = self._session(m.group(1))
                if session is None:
                    return
                try:
                    data, media = session.picture_file((query.get("path") or [""])[0])
                except FileNotFoundError as exc:
                    return self._error(str(exc), 404)
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
                self.send_response(200)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/browser-view\.png", path)
            if m:   # what the agent's browser shows now (the Browser pane's Agent view)
                session = self._session(m.group(1))
                if session is None:
                    return
                try:
                    with open(session.browser_view_path(), "rb") as fh:
                        data = fh.read()
                except OSError:
                    return self._error("the agent has not opened a page", 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/terminal", path)
            if m:   # the terminal's output since line ?after=
                if self._remote():
                    return self._error("the terminal is only on this computer", 403)
                session = self._session(m.group(1))
                if session is None:
                    return
                try:
                    after = int((query.get("after") or ["0"])[0] or 0)
                except ValueError:
                    after = 0
                return self._json(session.terminal().read(after))
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/changes/([0-9a-f]+)", path)
            if m:   # a turn's changed files, or with ?path= one file's diff
                session = self._session(m.group(1))
                if session is None:
                    return
                rel = (query.get("path") or [""])[0]
                try:
                    if rel:
                        return self._json({"path": rel, "diff": session.changes.diff(m.group(2), rel)})
                    record = session.changes.get(m.group(2))
                except (KeyError, ValueError) as exc:
                    return self._error(str(exc).strip("'\""), 404)
                return self._json(record) if record else self._error("unknown turn", 404)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/history", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                with session._lock:   # the record and its event number are read together
                    transcript = list(session.transcript)
                    todos = list(session.todo_state.items)
                    seq = session.bus.seq
                return self._json({"transcript": transcript, "todos": todos, "seq": seq})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/tree", path)
            if m:   # the Files pane: every file, and which ones git sees changed
                session = self._session(m.group(1))
                if session is None:
                    return
                return self._json(_file_tree(session.project_dir))
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/files", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                prefix = (query.get("prefix") or [""])[0]
                return self._json(
                    _list_files(session.project_dir, prefix)
                )
            return self._error("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - JSON error to client
            traceback.print_exc()   # kept in server.log for a problem report
            self._error(f"{type(exc).__name__}: {exc}", 500)

    # --------------------------------------------------------- post ---

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/artifacts/([0-9a-f]+)/data", path)
        if m:   # a page saving its own data: allowed from its sandbox, and it can touch nothing else
            if not self._addressed_here():
                return self._error("this server only answers to its own address", 403)
            return self._artifact_data(m.group(1), save=True)
        if path == "/pair":   # a device typing the 6-digit code
            length = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(length, 200)).decode("utf-8", "replace"))
            if not self._own_origin():
                return self._error("requests from other pages are not accepted", 403)
            if self.app.phone.check((form.get("code") or [""])[0]):
                return self._let_in()
            return self._pair_page(self.app.phone.locked() or "That is not the code. Check it and try again.")
        self._body = None
        self._read_json()
        if not self._own_origin():
            return self._error("requests from other pages are not accepted", 403)
        if not self._has_key():
            return self._error("this CodeMonkey is private: pair this device first", 401)
        try:
            if path in ("/api/phone", "/api/phone/code", "/api/phone/forget"):
                if self._remote():
                    return self._error("only on the computer itself", 403)
                if path == "/api/phone/forget":
                    self.app.devices.remove_all()   # every paired device's key stops working
                    self.app.phone.new_code()
                elif path == "/api/phone/code":
                    self.app.phone.new_code()
                else:
                    on = bool(self._read_json().get("enabled"))
                    try:
                        if on:
                            self.app.phone.start()
                        else:
                            self.app.phone.stop()
                    except RuntimeError as exc:
                        return self._error(str(exc), 409)
                    self.app.update_settings({"phone_access": on})
                return self._json(self.app.phone.status())
            if path in ("/api/bench", "/api/bench/stop"):
                if self._remote():
                    return self._error("only on the computer itself", 403)
                body = self._read_json()
                try:
                    if path == "/api/bench":
                        self.app.bench.start(str(body.get("provider") or ""), str(body.get("model") or ""))
                    else:
                        self.app.bench.stop()
                except ValueError as exc:
                    return self._error(str(exc), 400)
                except RuntimeError as exc:
                    return self._error(str(exc), 409)
                return self._json({"state": dict(self.app.bench.state), "results": self.app.store.bench_results()})
            if path in ("/api/trial", "/api/trial/stop"):
                if self._remote():
                    return self._error("only on the computer itself", 403)
                body = self._read_json()
                try:
                    if path == "/api/trial":
                        self.app.trial.start(str(body.get("provider") or ""), str(body.get("model") or ""),
                                             bool(body.get("compare")), bool(body.get("hard")), int(body.get("repeat") or 1))
                    else:
                        self.app.trial.stop()
                except ValueError as exc:
                    return self._error(str(exc), 400)
                except RuntimeError as exc:
                    return self._error(str(exc), 409)
                return self._json({"state": dict(self.app.bench.state), "trial": dict(self.app.trial.state),
                                   "results": self.app.store.bench_results()})
            if path.startswith("/api/models/"):
                if self._remote():   # fetching and starting models is done at the computer
                    return self._error("only on the computer itself", 403)
                body = self._read_json()
                try:
                    if path == "/api/models/download":
                        self.app.models.start_download(str(body.get("id") or ""))
                    elif path == "/api/models/download/cancel":
                        self.app.models.cancel_download()
                    elif path == "/api/models/start":
                        self.app.models.start(str(body.get("id") or ""))
                    elif path == "/api/models/stop":
                        self.app.models.stop()
                    elif path == "/api/models/start-all":
                        self.app.models.start_router()
                    elif path == "/api/models/load":
                        self.app.models.load(str(body.get("id") or ""))
                    elif path == "/api/models/unload":
                        self.app.models.unload(str(body.get("id") or ""))
                    else:
                        return self._error("not found", 404)
                except ValueError as exc:
                    return self._error(str(exc), 400)
                except RuntimeError as exc:
                    return self._error(str(exc), 409)
                return self._json(self.app.models.overview())
            if path == "/api/devices/signout":   # a device leaving by itself
                if self._device:
                    self.app.devices.remove(self._device)
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/devices/([0-9a-f]+)/(remove|rename)", path)
            if m:
                if self._remote():
                    return self._error("only on the computer itself", 403)
                if m.group(2) == "remove":
                    done = self.app.devices.remove(m.group(1))
                else:
                    done = self.app.devices.rename(m.group(1), self._read_json().get("name"))
                if not done:
                    return self._error("no such device", 404)
                return self._json({"devices": self.app.devices.listing(), "remote": False})
            if path == "/api/sessions":
                body = self._read_json()
                try:
                    project = (
                        self.app.chat_dir if body.get("no_project") else body.get("project_dir")
                    )
                    session = self.app.create_session(project, body.get("title"))
                except ValueError as exc:
                    return self._error(str(exc), 400)
                return self._json(session.snapshot(), 201)
            m = re.fullmatch(r"/api/setup/(ollama-start|lmstudio-open|pull)", path)
            if m:   # Set up a model: steps that run programs on this computer, only when asked
                if self._remote():
                    return self._error("setting up a model is done on the computer itself", 403)
                body = self._read_json()
                try:
                    if m.group(1) == "ollama-start":
                        return self._json(self.app.modelsetup.start_ollama())
                    if m.group(1) == "lmstudio-open":
                        return self._json(self.app.modelsetup.open_lmstudio())
                    return self._json(self.app.modelsetup.start_pull(str(body.get("model") or "")))
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
            if path == "/api/settings":
                changes = dict(self._read_json())
                if self._remote():
                    # a paired phone may change how the app behaves, but not which program this
                    # computer will run or where it listens
                    for key in ("llama_server_path", "phone_access", "statusline_command"):
                        changes.pop(key, None)
                return self._json(self.app.update_settings(changes))
            if path == "/api/jobs":
                body = self._read_json()
                try:
                    return self._json(
                        self.app.jobs.add(
                            body.get("title", ""), body.get("task", ""), body.get("schedule", ""),
                            body.get("project_dir") or self.app.default_project, body.get("watch", ""),
                        ),
                        201,
                    )
                except ValueError as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)(/run)?", path)
            if m:
                try:
                    if m.group(2):
                        return self._json({"started": self.app.jobs.run_now(m.group(1))})
                    return self._json(self.app.jobs.update(m.group(1), self._read_json()))
                except KeyError:
                    return self._error("unknown job", 404)
                except ValueError as exc:
                    return self._error(str(exc), 400)
            if path == "/api/persona":
                return self._json(self.app.save_persona(self._read_json()))
            m = re.fullmatch(r"/api/ideas/(\d+)", path)
            if m:
                status = str(self._read_json().get("status", ""))
                if status not in ("dismissed", "started", "new"):
                    return self._error("status must be dismissed, started or new", 400)
                if not self.app.store.set_idea_status(int(m.group(1)), status):
                    return self._error("unknown idea", 404)
                return self._json({"ok": True})
            if path == "/api/reviews":
                from . import review

                review.run(self.app, force=True)
                return self._json(self.app.store.reviews())
            if path == "/api/skills":
                body = self._read_json()
                try:
                    return self._json(self.app.save_skill(
                        body.get("name", ""), body.get("description", ""), body.get("keywords", []),
                        body.get("body", "")))
                except ValueError as exc:
                    return self._error(str(exc), 400)
            if path == "/api/skills/mine":
                return self._json(self.app.mine_skills())
            m = re.fullmatch(r"/api/skills/proposals/([a-z0-9_-]+)/(install|dismiss)", path)
            if m:
                done = (self.app.install_proposal if m.group(2) == "install"
                        else self.app.dismiss_proposal)(m.group(1))
                if not done:
                    return self._error("no such proposal, or a skill with that name exists", 404)
                return self._json(self.app.skills_overview())
            if path == "/api/goals":
                body = self._read_json()
                try:
                    self.app.store.add_goal(
                        body.get("title", ""), body.get("description", ""),
                        os.path.abspath(body.get("project_dir") or self.app.default_project),
                    )
                except ValueError as exc:
                    return self._error(str(exc), 400)
                return self._json(self.app.store.goals(), 201)
            m = re.fullmatch(r"/api/goals/([0-9a-f]+)", path)
            if m:
                body = self._read_json()
                ok = True
                if body.get("note"):
                    ok = self.app.store.log_goal(m.group(1), body["note"], "you")
                if ok and body.get("status") in ("active", "completed"):
                    ok = self.app.store.set_goal_status(m.group(1), body["status"])
                if not ok:
                    return self._error("unknown goal", 404)
                return self._json(self.app.store.goals())
            if path == "/api/browse/new":   # the folder chooser's New folder
                body = self._read_json()
                try:
                    return self._json(self.app.make_folder(str(body.get("parent") or ""), str(body.get("name") or "")))
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
            if path == "/api/updates/check":
                return self._json(self.app.updates.check())
            if path == "/api/report":   # Report a problem: one zip to send
                from . import report

                body = self._read_json()
                session = self.app.get_session(str(body.get("session_id") or "")) if body.get("conversation") else None
                page = body.get("page") if isinstance(body.get("page"), dict) else {}
                data = report.build(self.app, str(body.get("note") or "")[:5000], page, session)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{report.file_name()}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/doctor":
                return self._json(self.app.health_check())
            if path == "/api/settings/reset":
                return self._json(self.app.reset_settings())
            if path in ("/api/worktrees/merge", "/api/worktrees/discard") or \
                    re.fullmatch(r"/api/sessions/[0-9a-f]+/worktree", path):
                from coder.worktrees import WorktreeError

                body = self._read_json()
                try:
                    if path.endswith("/merge"):
                        result = self.app.worktree_merge(str(body.get("path") or ""), body.get("remove", True) is not False,
                                                         str(body.get("message") or ""))
                    elif path.endswith("/discard"):
                        self.app.worktree_discard(str(body.get("path") or ""))
                        result = {"discarded": True}
                    else:
                        session = self.app.get_session(path.split("/")[3])
                        if session is None:
                            return self._error("unknown session", 404)
                        result = session.move_to_worktree(str(body.get("name") or ""))
                except (WorktreeError, RuntimeError, ValueError) as exc:
                    return self._error(str(exc), 400)
                self.app.bus.emit("sessions", {})
                return self._json(result)
            if path == "/api/agents":
                body = self._read_json()
                try:
                    saved = self.app.save_agent(body)
                except ValueError as exc:
                    return self._error(str(exc), 400)
                return self._json(saved)
            if path == "/api/commands":
                body = self._read_json()
                try:
                    self.app.save_command(
                        body.get("name", ""), body.get("description", ""), body.get("body", "")
                    )
                except ValueError as exc:
                    return self._error(str(exc), 400)
                return self._json(self.app.command_list())
            if path == "/api/memory":
                fact = str(self._read_json().get("fact", "")).strip()
                if not fact:
                    return self._error("nothing to remember", 400)
                self.app.remember(fact)
                return self._json(self.app.memory_claims())
            if path == "/api/memory/forget":
                if not self.app.forget_memory(self._read_json().get("id", "")):
                    return self._error("no such memory", 404)
                return self._json(self.app.memory_claims())
            if path in ("/api/people", "/api/people/rename", "/api/people/forget", "/api/people/claim"):
                body = self._read_json()
                try:
                    if path == "/api/people":
                        self.app.note_about(body.get("name"), body.get("fact"))
                    elif path == "/api/people/rename":
                        if not self.app.rename_person(body.get("name"), body.get("to")):
                            return self._error("no such person", 404)
                    elif path == "/api/people/forget":
                        if not self.app.forget_person(body.get("name")):
                            return self._error("no such person", 404)
                    else:   # one note: forget it, or confirm it
                        done = (self.app.confirm_memory(str(body.get("id", ""))) if body.get("confirm")
                                else self.app.forget_memory(body.get("id", "")))
                        if not done:
                            return self._error("no such note", 404)
                except ValueError as exc:
                    return self._error(str(exc), 400)
                return self._json(self.app.people())
            if path == "/api/memory/confirm":
                if not self.app.confirm_memory(str(self._read_json().get("id", ""))):
                    return self._error("no such memory", 404)
                return self._json(self.app.memory_claims())
            if path == "/api/kernel/run":
                from . import kerneltool

                try:
                    return self._json(kerneltool.run(*(lambda b: (b.get("text", ""), b.get("domain") or "thren"))(self._read_json())))
                except ValueError as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/plugins(?:/install|/([^/]+)/(enabled|update))", path)
            if m:
                if self._remote():
                    # a plugin's hooks and MCP servers are programs this computer would run
                    return self._error("plugins can only be changed on this computer", 403)
                from coder.plugins import PluginError

                body = self._read_json()
                try:
                    if m.group(1) is None:
                        result = self.app.plugins.install(str(body.get("source") or ""))
                    elif m.group(2) == "enabled":
                        result = self.app.plugins.set_enabled(urllib.parse.unquote(m.group(1)), bool(body.get("enabled")))
                    else:
                        result = self.app.plugins.update(urllib.parse.unquote(m.group(1)))
                except PluginError as exc:
                    return self._error(str(exc), 400)
                self.app.plugins_changed()
                return self._json(result)
            if path == "/api/permissions/check":
                body = self._read_json()
                return self._json(self.app.check_permission(
                    str(body.get("tool") or "bash"), str(body.get("value") or ""), str(body.get("project_dir") or "")))
            if path == "/api/hooks":
                if self._remote():
                    # a hook is a command this computer runs
                    return self._error("hooks can only be changed on this computer", 403)
                try:
                    return self._json(self.app.save_hooks(self._read_json().get("hooks") or []))
                except ValueError as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/mcp(?:/([^/]+)/(restart|enabled|signin|signout))?", path)
            if m:
                if self._remote():
                    # an MCP server is a program this computer runs, or a place its keys are sent
                    return self._error("MCP servers can only be changed on this computer", 403)
                body = self._read_json()
                try:
                    if m.group(1) is None:
                        result = self.app.mcp.save(body)
                    else:
                        name = urllib.parse.unquote(m.group(1))
                        if m.group(2) == "signin":
                            # the sign-in comes back here, to this computer's own address
                            host = self.headers.get("Host") or "127.0.0.1"
                            return self._json({"url": self.app.mcp.sign_in(name, f"http://{host}/mcp/oauth/callback")})
                        if m.group(2) == "signout":
                            self.app.mcp.sign_out(name)
                        elif m.group(2) == "restart":
                            self.app.mcp.restart(name)
                        else:
                            self.app.mcp.set_enabled(name, bool(body.get("enabled")))
                        result = self.app.mcp.status()
                except KeyError:
                    return self._error("unknown MCP server", 404)
                except (ValueError, RuntimeError) as exc:
                    return self._error(str(exc), 400)
                return self._json(result)
            if path == "/api/providers" or re.fullmatch(r"/api/providers/[^/]+/key", path):
                if self._remote():
                    # a key, or where keys are sent, is set only at this computer
                    return self._error("API keys and model servers can only be changed on this computer", 403)
                body = self._read_json()
                try:
                    if path == "/api/providers":
                        result = self.app.providers.save(
                            body.get("name", ""), body.get("base_url", ""), body.get("models") or [],
                            body.get("client", "openai"), body.get("key", ""), bool(body.get("vision")))
                    else:
                        name = urllib.parse.unquote(path.split("/")[3])
                        result = self.app.providers.set_key(name, body.get("key", ""))
                except KeyError:
                    return self._error("unknown provider", 404)
                except (ValueError, RuntimeError) as exc:   # RuntimeError: no key store on this system
                    return self._error(str(exc), 400)
                self.app.bus.emit("settings", {})
                return self._json(result)
            m = re.fullmatch(r"/api/providers/([^/]+)/start", path)
            if m:   # the command that starts this server when CodeMonkey opens
                if self._remote():
                    return self._error("A start command can only be set on this computer", 403)
                body = self._read_json()
                try:
                    result = self.app.providers.set_start(urllib.parse.unquote(m.group(1)), body.get("command", ""))
                except KeyError:
                    return self._error("only servers added in Settings can start with CodeMonkey", 404)
                except ValueError as exc:
                    return self._error(str(exc), 400)
                self.app.bus.emit("settings", {})
                return self._json(result)
            m = re.fullmatch(r"/api/providers/([^/]+)/test", path)
            if m:
                try:
                    return self._json(
                        self.app.test_provider(urllib.parse.unquote(m.group(1)))
                    )
                except KeyError:
                    return self._error("unknown provider", 404)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/folder", path)
            if m:
                body = self._read_json()
                try:
                    session = self.app.switch_folder(
                        m.group(1),
                        None if body.get("no_project") else body.get("project_dir"),
                    )
                except KeyError:
                    return self._error("unknown session", 404)
                except ValueError as exc:
                    return self._error(str(exc), 400)
                except RuntimeError as exc:
                    return self._error(str(exc), 409)
                return self._json(session.snapshot())
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/duplicate", path)
            if m:
                try:
                    session = self.app.duplicate_session(m.group(1), str(self._read_json().get("turn") or ""))
                except KeyError as exc:
                    return self._error(str(exc).strip("'\""), 404)
                if session is None:
                    return self._error("unknown session", 404)
                return self._json(session.snapshot(), 201)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/message", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                text = str(body.get("text", ""))
                attachments = body.get("attachments") or []
                if not text.strip() and not attachments:
                    return self._error("empty message", 400)
                position = session.enqueue_message(text, attachments, str(body.get("client_id") or ""))
                if position == 0:
                    return self._json({"queued": True, "position": 1, "duplicate": True})
                self.app.refresh_index_entry(session)
                return self._json({"queued": True, "position": position})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/cancel", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                session.cancel()
                return self._json({"cancelled": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/permission", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                ok = session.gate.resolve(
                    str(body.get("request_id", "")),
                    str(body.get("decision", "deny")),
                    body.get("message"),
                )
                if not ok:
                    return self._error("unknown or expired request", 410)
                return self._json({"resolved": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/changes/([0-9a-f]+)", path)
            if m:   # keep or undo one file of a turn's changes (or all of them)
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                action, turn = str(body.get("action") or ""), m.group(2)
                record = session.changes.get(turn)
                if record is None:
                    return self._error("unknown turn", 404)
                paths = [str(body.get("path") or "")] if action in ("keep", "undo") else \
                    [f["path"] for f in record["files"] if f["state"] == "pending"]
                one = action.replace("_all", "")
                errors = []
                for rel in paths:
                    try:
                        session.changes.decide(turn, rel, one)
                    except (KeyError, ValueError, OSError) as exc:
                        errors.append(str(exc).strip("'\""))
                if errors and action in ("keep", "undo"):
                    return self._error(errors[0], 409)
                result = dict(session.changes.get(turn), errors=errors)
                session.bus.emit("turn_changes", result)
                return self._json(result)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/memory", path)
            if m:   # "#" adds a line to a memory file; /memory saves one whole
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                try:
                    return self._json(session.save_memory(str(body.get("scope") or ""), body.get("text") or "",
                                                          bool(body.get("append"))))
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/reveal", path)
            if m:   # the session's folder in Explorer (Finder, a file manager): this computer only
                if self._remote():
                    return self._error("only this computer can open its folders", 403)
                session = self._session(m.group(1))
                if session is None:
                    return
                folder = str(session.project_dir or "")
                if not folder or not os.path.isdir(folder):
                    return self._error("this session has no folder", 400)
                inner = str(self._read_json().get("path") or "").strip()
                if inner:   # a file the chat mentions: its own folder, and never outside the project
                    root = os.path.realpath(folder)
                    target = os.path.realpath(os.path.join(root, inner))
                    if os.path.commonpath([root, target]) != root or not os.path.exists(target):
                        return self._error("that path is not in this session's folder", 400)
                    folder = target if os.path.isdir(target) else os.path.dirname(target)
                try:
                    if sys.platform == "win32":
                        os.startfile(folder)  # noqa: S606 - a folder of the user's own session
                    else:
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
                except OSError as exc:
                    return self._error(str(exc), 500)
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/file", path)
            if m:   # save your own edit from the file pane; 409 when it changed on disk meanwhile
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                try:
                    mtime = body.get("mtime")
                    return self._json(session.write_file(str(body.get("path") or ""), str(body.get("text") or ""),
                                                         float(mtime) if mtime is not None else None, bool(body.get("force"))))
                except FileExistsError as exc:
                    return self._error(str(exc), 409)
                except PermissionError as exc:
                    return self._error(str(exc), 403)
                except (ValueError, OSError) as exc:
                    return self._error(str(exc), 400)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/terminal", path)
            if m:   # run a line in the terminal, or {"stop": true}; this computer only
                if self._remote():
                    return self._error("the terminal is only on this computer", 403)
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                term = session.terminal()
                if body.get("stop"):
                    return self._json({"stopped": term.stop()})
                try:
                    term.run(str(body.get("command") or ""))
                except RuntimeError as exc:
                    return self._error(str(exc), 409)
                return self._json({"ok": True, "cwd": term.cwd})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/git/(message|commit|push|pr|merge|autofix)", path)
            if m:   # the branch bar: write a message, commit, push, or open a pull request
                from .gitflow import GitError
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                action = m.group(2)
                try:
                    if action == "message":
                        return self._json(session.git_message(str(body.get("kind") or "commit")))
                    if action == "commit":
                        return self._json(session.git_commit(str(body.get("message") or "")))
                    if action == "push":
                        return self._json(session.git_push())
                    if action == "merge":
                        return self._json(session.git_merge_when_ready())
                    if action == "autofix":
                        return self._json(session.set_autofix(bool(body.get("on"))))
                    return self._json(session.git_create_pr(
                        str(body.get("title") or ""), str(body.get("body") or ""), bool(body.get("draft")),
                        str(body.get("branch") or ""), str(body.get("message") or "")))
                except (GitError, ValueError) as exc:
                    return self._error(str(exc), 400)
                except Exception as exc:  # noqa: BLE001 - the model or a command failed: said plainly
                    return self._error(str(exc), 502)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/suggest", path)
            if m:   # the grey next prompt after a reply; empty when there is nothing to suggest
                session = self._session(m.group(1))
                if session is None:
                    return
                try:
                    return self._json({"text": session.suggest_prompt()})
                except Exception:  # noqa: BLE001 - a suggestion is optional: no answer, no suggestion
                    return self._json({"text": ""})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/side", path)
            if m:   # a side question (Ctrl+; or /btw); {"clear": true} starts the side thread over
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                if body.get("clear"):
                    session.clear_side_chat()
                    return self._json({"ok": True})
                try:
                    return self._json({"answer": session.side_chat(str(body.get("question") or ""))})
                except ValueError as exc:
                    return self._error(str(exc), 400)
                except Exception as exc:  # noqa: BLE001 - the model's own failure, said plainly
                    return self._error(f"the model did not answer: {exc}", 502)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/background", path)
            if m:   # Ctrl+B: the running command goes on in the background
                session = self._session(m.group(1))
                if session is None:
                    return
                return self._json({"moved": session.to_background()})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/processes/([A-Za-z0-9_.-]+)/stop", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                text = session.processes.stop(m.group(2))
                session.bus.emit("processes", {"items": session.processes.describe()})
                return self._json({"text": text})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/elicitation", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                try:
                    ok = session.answer_elicitation(str(body.get("request_id", "")), str(body.get("action", "cancel")),
                                                    body.get("content") if isinstance(body.get("content"), dict) else {})
                except ValueError as exc:
                    return self._error(str(exc), 400)
                if not ok:
                    return self._error("unknown or expired request", 410)
                return self._json({"resolved": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/plan", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                result = session.decide_plan(
                    str(body.get("action", "")), body.get("feedback")
                )
                if not result.get("ok"):
                    return self._error(result.get("error", "failed"), 400)
                return self._json(result)
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/checkpoint", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                session.enqueue_control("checkpoint")
                return self._json({"queued": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/rewind", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                if body.get("confirm") is not True:
                    return self._error(
                        "rewind is destructive: confirmation required", 400
                    )
                if body.get("turn"):   # back to before one message: code, conversation or both
                    restore = str(body.get("restore") or "both")
                    if restore not in ("both", "chat", "code"):
                        return self._error("restore is both, chat or code", 400)
                    try:
                        return self._json(session.rewind_to(str(body["turn"]), restore))
                    except KeyError as exc:
                        return self._error(str(exc).strip("'\""), 404)
                    except (ValueError, OSError) as exc:
                        return self._error(str(exc), 409)
                session.enqueue_control("rewind")
                return self._json({"queued": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/archive", path)
            if m:   # {"archived": false} brings it back
                if not self.app.archive_session(m.group(1), self._read_json().get("archived", True) is not False):
                    return self._error("unknown session", 404)
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/rename", path)
            if m:
                ok = self.app.rename_session(
                    m.group(1), str(self._read_json().get("title", ""))
                )
                if not ok:
                    return self._error("unknown session", 404)
                return self._json({"renamed": True})
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/reopen", path)
            if m:
                session = self.app.reopen_session(m.group(1))
                if session is None:
                    return self._error("unknown session", 404)
                return self._json(session.snapshot())
            m = re.fullmatch(r"/api/sessions/([0-9a-f]+)/settings", path)
            if m:
                session = self._session(m.group(1))
                if session is None:
                    return
                body = self._read_json()
                allowed = ("provider", "model", "effort", "permission_mode", "context_budget", "output_style")
                with session._lock:
                    for key in allowed:
                        if key in body:
                            session.settings[key] = body[key]
                session.bus.emit("settings_changed", dict(session.settings))
                return self._json({"settings": dict(session.settings)})
            return self._error("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        self._body = None
        self._read_json()
        if not self._own_origin():
            return self._error("requests from other pages are not accepted", 403)
        if not self._has_key():
            return self._error("this CodeMonkey is private: pair this device first", 401)
        m = re.fullmatch(r"/api/plugins/([^/]+)", path)
        if m:
            if self._remote():
                return self._error("plugins can only be changed on this computer", 403)
            from coder.plugins import PluginError

            try:
                self.app.plugins.remove(urllib.parse.unquote(m.group(1)))
            except PluginError as exc:
                return self._error(str(exc), 404)
            self.app.plugins_changed()
            return self._json({"removed": True})
        m = re.fullmatch(r"/api/mcp/([^/]+)", path)
        if m:
            if self._remote():
                return self._error("MCP servers can only be changed on this computer", 403)
            try:
                self.app.mcp.remove(urllib.parse.unquote(m.group(1)))
            except KeyError:
                return self._error("unknown MCP server", 404)
            return self._json(self.app.mcp.status())
        m = re.fullmatch(r"/api/providers/([^/]+)(/key)?", path)
        if m:
            if self._remote():
                return self._error("API keys and model servers can only be changed on this computer", 403)
            name = urllib.parse.unquote(m.group(1))
            try:
                if m.group(2):
                    result = self.app.providers.delete_key(name)
                elif self.app.providers.remove(name):
                    result = {"deleted": True}
                else:
                    return self._error("only providers added in the app can be removed", 404)
            except KeyError:
                return self._error("unknown provider", 404)
            self.app.bus.emit("settings", {})
            return self._json(result)
        m = re.fullmatch(r"/api/skills/([a-z0-9_-]+)", path)
        if m:
            if not self.app.delete_skill(m.group(1)):
                return self._error("unknown skill", 404)
            return self._json(self.app.skills_overview())
        m = re.fullmatch(r"/api/artifacts/([0-9a-f]+)", path)
        if m:
            if not self.app.delete_artifact(m.group(1)):
                return self._error("unknown artifact", 404)
            return self._json({"deleted": True})
        m = re.fullmatch(r"/api/goals/([0-9a-f]+)", path)
        if m:
            if not self.app.store.delete_goal(m.group(1)):
                return self._error("unknown goal", 404)
            return self._json(self.app.store.goals())
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if m:
            if not self.app.jobs.remove(m.group(1)):
                return self._error("unknown job", 404)
            return self._json(self.app.jobs.listing())
        m = re.fullmatch(r"/api/agents/([^/]+)", path)
        if m:
            project = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("project_dir") or [""])[0]
            if not self.app.delete_agent(urllib.parse.unquote(m.group(1)), project):
                return self._error("unknown agent", 404)
            return self._json({"deleted": True})
        m = re.fullmatch(r"/api/commands/([^/]+)", path)
        if m:
            if not self.app.delete_command(urllib.parse.unquote(m.group(1))):
                return self._error("unknown command", 404)
            return self._json(self.app.command_list())
        m = re.fullmatch(r"/api/sessions/([0-9a-f]+)", path)
        if not m:
            return self._error("not found", 404)
        if not self.app.delete_session(m.group(1)):
            return self._error("unknown session", 404)
        self._json({"deleted": True})

    # -------------------------------------------------------- static ---

    @classmethod
    def _load_static(cls) -> None:
        """Read the page's files once: each with a gzipped copy (text only) and an ETag, so a
        page opened again is answered with 304 Not Modified instead of the file."""
        import gzip
        import hashlib

        loaded: Dict[str, tuple] = {}   # swapped in whole, so a request never sees it half-built
        fonts_dir, js_dir = os.path.join(STATIC_DIR, "fonts"), os.path.join(STATIC_DIR, "js")
        names = sorted(os.listdir(STATIC_DIR)) + (
            ["fonts/" + f for f in sorted(os.listdir(fonts_dir)) if f.endswith(".woff2")] if os.path.isdir(fonts_dir) else []) + (
            ["js/" + f for f in sorted(os.listdir(js_dir)) if f.endswith(".js")] if os.path.isdir(js_dir) else []) + (
            ["vendor/" + f for f in sorted(os.listdir(os.path.join(STATIC_DIR, "vendor"))) if f.endswith(".js")]
            if os.path.isdir(os.path.join(STATIC_DIR, "vendor")) else [])
        for name in names:
            content_type = _STATIC_TYPES.get(os.path.splitext(name)[1])
            path = os.path.join(STATIC_DIR, *name.split("/"))
            if content_type is None or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            packed = gzip.compress(data, 6, mtime=0) if not name.endswith(".woff2") else None
            etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
            loaded["/" + name] = (content_type, data, packed if packed and len(packed) < len(data) else None, etag)
        cls._static = loaded

    def _serve_static(self, name: str) -> None:
        if os.environ.get("CODEMONKEY_DEV"):
            self._load_static()  # pick up edits without a restart
        entry = self._static.get("/" + name)
        if entry is None:
            return self._error("not found", 404)
        content_type, data, packed, etag = entry
        # fonts change only with an update; the rest is checked on every load (a 304 is tiny)
        cache = "public, max-age=604800" if name.endswith(".woff2") else "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        gzip_ok = packed is not None and "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = packed if gzip_ok else data
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        if packed is not None:
            self.send_header("Vary", "Accept-Encoding")
        if gzip_ok:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ sse ---

    def _send_sse(self, event_type: str, data: Any, seq: Optional[int] = None) -> None:
        head = f"id: {seq}\n" if seq is not None else ""
        payload = f"{head}event: {event_type}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        # one write per event: split writes wait on each other and delay small events
        self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _serve_sse(self, bus, first, after: Optional[int]) -> None:
        """Stream a bus to this page. *first* gives the opening event (type, data).

        *after* is the last event number the page already has. What it missed is sent first;
        when that is no longer available the page is told to reload ("resync").
        """
        import socket

        try:   # send each event at once instead of waiting to fill a packet
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        q, missed = bus.subscribe_from(after)
        try:
            self._send_sse(*first())
            if after is not None and missed is None:
                self._send_sse("resync", {"seq": bus.seq})
            for event in missed or []:
                self._send_sse(event["type"], event["data"], event["seq"])
            while True:
                try:
                    event = q.get(timeout=self.ping_seconds)
                except queue.Empty:
                    if self._device and not self.app.devices.alive(self._device):
                        break
                    self._send_sse("ping", {"seq": bus.seq})
                    continue
                if self._device and not self.app.devices.alive(self._device):
                    break
                self._send_sse(event["type"], event["data"], event["seq"])
                if event["type"] == "session_end":
                    break
        except (ConnectionError, OSError):  # the page closed the stream
            pass
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")  # end of chunks
                self.wfile.flush()
            except OSError:
                pass
            bus.unsubscribe(q)

    def _resume_point(self, query: Dict[str, list]) -> Optional[int]:
        """The last event number the page says it has (from the browser, or from ?after=)."""
        raw = self.headers.get("Last-Event-ID") or (query.get("after") or [""])[0]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


def serve(app, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """Start the server (blocking)."""
    from . import report

    report.capture_output(app.webui_dir)   # what is printed is kept, for Report a problem
    WebHandler.app = app
    WebHandler._load_static()
    WebHandler.loopback_only = host in ("127.0.0.1", "localhost", "::1")
    server = ThreadingHTTPServer((host, port), WebHandler)
    app.phone.handler = WebHandler
    app.phone.port = server.server_port
    app.phone.bound_all = not WebHandler.loopback_only
    if app.phone.bound_all:
        app.phone.new_code()
    elif app.settings.get("phone_access"):
        try:
            app.phone.start()
        except RuntimeError as exc:
            print(f"Phone access is switched on but could not start: {exc}")
    server.daemon_threads = True
    if not app.watchdog.is_alive():
        app.watchdog.start()
    app.jobs.start()
    app.learner.start()
    app.codeknow.start()   # the C/C++ knowledge base: built or refreshed in the background
    app.updates.start()
    threading.Thread(target=app.autostart.start_all, daemon=True, name="autostart").start()
    print(f"Web UI: http://{host}:{server.server_port}")
    app.phone.announce()
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return server
