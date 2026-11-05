"""The MCP servers of the whole app, shared by every session.

Each server in mcp.json is started (or reached) once, in the background, so a slow or broken one
never holds up the app. Sessions take their tools from here and are told when they change: a
server announced a new tool list, was restarted, switched off or removed. Settings > MCP servers
reads and changes everything through this class; the file is rewritten on each change, keeping
any field it does not know. A remote server's key goes to Credential Manager, never the file.

A remote server that wants a sign-in is shown as "signin": Settings offers a Sign in button, which
runs OAuth in a browser (coder/mcpauth.py) and comes back to /mcp/oauth/callback. Servers that ask
for sampling or a form (elicitation) are answered through ``host`` (the app), which puts the
question to the session whose tool call is running.
"""

from __future__ import annotations

import json
import os
import shlex
import threading
from typing import Any, Callable, Dict, List, Optional

import keystore
from coder import mcpauth
from coder.mcp import MCPAuthRequired, MCPClient, MCPError, client_for, read_config, server_tools
from tools.registry import Tool

_NAME = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


class _Server:
    def __init__(self, name: str, cfg: Dict[str, Any]) -> None:
        self.name, self.cfg = name, cfg
        self.status = "off" if cfg.get("disabled") else "starting"
        self.error = ""
        self.challenge = ""          # the server's WWW-Authenticate, when it wants a sign-in
        self.client: Optional[MCPClient] = None
        self.tools: List[Tool] = []
        self.resources: List[Dict[str, Any]] = []
        self.templates: List[Dict[str, Any]] = []
        self.prompts: List[Dict[str, Any]] = []
        self.lock = threading.Lock()


class MCPHub:
    def __init__(self, config_path: str, on_change: Optional[Callable[[], None]] = None) -> None:
        self.path = os.path.abspath(config_path)
        self.on_change = on_change
        self.servers: Dict[str, _Server] = {}
        self.external: set = set()   # servers a plugin brings: not in mcp.json, changed only through it
        self._listeners: List[Callable[[], None]] = []
        self.host: Any = None        # answers sampling and elicitation (set by the app)
        for name, cfg in read_config(self.path).items():
            self.servers[name] = _Server(name, cfg)

    # ------------------------------------------------------------ lifecycle ---

    def start_all(self) -> None:
        for server in list(self.servers.values()):
            if server.status != "off":
                self._start_soon(server)

    def _start_soon(self, server: _Server) -> None:
        threading.Thread(target=self._start, args=(server,), name=f"mcp-start-{server.name}", daemon=True).start()

    def _start(self, server: _Server) -> None:
        with server.lock:
            self._stop_client(server)
            server.status, server.error = "starting", ""
            self._changed()
            try:
                client = client_for(server.name, server.cfg, os.path.dirname(self.path))
                client.on_change = lambda kind, s=server: self._server_changed(s, kind)
                client.host = self.host
                client.connect()
                server.client = client
                self._relist(server)
                server.status, server.challenge = "connected", ""
            except MCPAuthRequired as exc:
                server.status, server.challenge = "signin", exc.challenge
                server.error = "This server wants you to sign in."
                self._stop_client(server)
            except (MCPError, OSError, ValueError) as exc:
                server.status, server.error = "failed", str(exc)[:600]
                self._stop_client(server)
        self._changed()

    def _relist(self, server: _Server, kinds=("tools", "resources", "prompts")) -> None:
        client = server.client
        if client is None:
            return
        if "tools" in kinds:
            server.tools = server_tools(client, server.cfg, client.list_tools())
        if "resources" in kinds:
            server.resources = client.list_resources()
            server.templates = client.list_resource_templates()
        if "prompts" in kinds:
            server.prompts = client.list_prompts()

    def _server_changed(self, server: _Server, kind: str) -> None:
        if kind == "closed":
            server.status, server.error = "failed", "the server stopped or the connection dropped; restart it"
            server.tools = []
            self._changed()
            return
        def relist() -> None:
            try:
                self._relist(server, (kind,))
            except MCPError as exc:
                server.error = str(exc)[:300]
            self._changed()
        threading.Thread(target=relist, daemon=True).start()   # not on the thread reading the server

    def _stop_client(self, server: _Server) -> None:
        if server.client is not None:
            try:
                server.client.close()
            except Exception:  # noqa: BLE001
                pass
        server.client = None
        server.tools, server.resources, server.templates, server.prompts = [], [], [], []

    def close_all(self) -> None:
        for server in self.servers.values():
            self._stop_client(server)

    # ------------------------------------------------------------- changes ---

    def listen(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def unlisten(self, callback: Callable[[], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _changed(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - one session's trouble is not the hub's
                pass
        if self.on_change:
            self.on_change()

    def restart(self, name: str) -> None:
        server = self.servers[name]
        if server.status == "off":
            raise ValueError(f"{name} is switched off")
        self._start_soon(server)

    def set_enabled(self, name: str, on: bool) -> None:
        server = self.servers[name]
        if name in self.external:
            raise ValueError(f"{name} comes from a plugin: switch the plugin off instead.")
        server.cfg = dict(server.cfg)
        if on:
            server.cfg.pop("disabled", None)
            self._write()
            self._start_soon(server)
        else:
            server.cfg["disabled"] = True
            self._write()
            with server.lock:
                self._stop_client(server)
                server.status, server.error = "off", ""
            self._changed()

    def sign_in(self, name: str, redirect_uri: str) -> str:
        """Start an OAuth sign-in for a remote server: the address to open in a browser."""
        server = self.servers[name]
        if not server.cfg.get("url"):
            raise ValueError(f"{name} runs on this computer: there is nothing to sign in to")
        try:
            return mcpauth.begin(name, server.cfg, redirect_uri, server.challenge)
        except mcpauth.AuthError as exc:
            raise ValueError(str(exc)) from exc

    def signed_in(self, state: str, code: str, error: str = "") -> str:
        """The browser came back from a sign-in: keep the token and connect again."""
        try:
            name = mcpauth.finish(state, code, error)
        except mcpauth.AuthError as exc:
            raise ValueError(str(exc)) from exc
        if name in self.servers and self.servers[name].status != "off":
            self._start_soon(self.servers[name])
        return name

    def sign_out(self, name: str) -> None:
        server = self.servers[name]
        mcpauth.forget(name)
        if server.status != "off":
            self._start_soon(server)

    def set_external(self, servers: Dict[str, Dict[str, Any]], start: bool = True) -> None:
        """Make the plugin-supplied servers exactly *servers*: new ones start, gone ones stop."""
        for name in list(self.external - set(servers)):
            server = self.servers.pop(name, None)
            self.external.discard(name)
            if server is not None:
                with server.lock:
                    self._stop_client(server)
        for name, cfg in servers.items():
            if name in self.servers and name not in self.external:
                continue   # your own server of that name wins
            if name in self.external and self.servers[name].cfg == cfg:
                continue
            self.servers[name] = _Server(name, cfg)
            self.external.add(name)
            if start:
                self._start_soon(self.servers[name])
        self._changed()

    def save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Add a server, or change one: from the Settings form."""
        name = str(body.get("name") or "").strip()
        if not _NAME.fullmatch(name):
            raise ValueError("Name: letters, digits, - and _, up to 32 characters.")
        if name in self.external:
            raise ValueError(f"{name} comes from a plugin: change it there, or switch the plugin off.")
        old = self.servers.get(name)
        cfg: Dict[str, Any] = dict(old.cfg) if old else {}
        for key in ("command", "args", "env", "cwd", "url", "headers", "auth_header", "transport"):
            cfg.pop(key, None)
        url = str(body.get("url") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                raise ValueError("Address: an http:// or https:// URL.")
            if old and old.cfg.get("url") != url:
                keystore.delete("mcp:" + name)    # a key goes only where it was entered for
            cfg["url"] = url
            if body.get("auth_header"):
                cfg["auth_header"] = str(body["auth_header"]).strip()[:80]
            if body.get("transport") in ("http", "sse"):
                cfg["transport"] = body["transport"]
        else:
            command = str(body.get("command") or "").strip()
            if not command:
                raise ValueError("Give a command to start the server, or an address to reach it.")
            parts = shlex.split(command, posix=False)
            cfg["command"] = parts[0].strip('"')
            cfg["args"] = [p.strip('"') for p in parts[1:]]
            env = {}
            before = (old.cfg.get("env") if old else None) or {}
            for line in str(body.get("env") or "").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip():
                        # "NAME=" with nothing after it keeps the value it had (values are not shown again)
                        env[key.strip()] = value.strip() or str(before.get(key.strip(), ""))
            if env:
                cfg["env"] = env
            if body.get("cwd"):
                cfg["cwd"] = str(body["cwd"]).strip()
        ro = [t.strip() for t in str(body.get("read_only_tools") or "").split(",") if t.strip()]
        if ro:
            cfg["read_only_tools"] = ro
        else:
            cfg.pop("read_only_tools", None)
        try:
            timeout = float(body.get("timeout") or 0)
        except (TypeError, ValueError):
            timeout = 0
        if timeout:
            cfg["timeout"] = max(5, min(timeout, 1800))
        if body.get("key"):
            keystore.set("mcp:" + name, str(body["key"]).strip())
        if old is not None:
            with old.lock:
                self._stop_client(old)
        server = _Server(name, cfg)
        self.servers[name] = server
        self._write()
        if server.status != "off":
            self._start_soon(server)
        else:
            self._changed()
        return self.describe(server)

    def remove(self, name: str) -> None:
        if name in self.external:
            raise ValueError(f"{name} comes from a plugin: switch the plugin off or remove it instead.")
        server = self.servers.pop(name)
        with server.lock:
            self._stop_client(server)
        keystore.delete("mcp:" + name)
        self._write()
        self._changed()

    def _write(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.pop("mcpServers", None)
        data["servers"] = {name: server.cfg for name, server in self.servers.items() if name not in self.external}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)

    # --------------------------------------------------------- what sessions use ---

    def tools(self) -> List[Tool]:
        out: List[Tool] = []
        readable = []
        for server in self.servers.values():
            if server.status == "connected":
                out.extend(server.tools)
                if server.resources or server.templates:
                    readable.append(server)
        if readable:
            out.extend(self._resource_tools())
        return out

    def _resource_tools(self) -> List[Tool]:
        def listing(server: str = "") -> str:
            rows = []
            for s in self.servers.values():
                if s.status != "connected" or (server and s.name != server):
                    continue
                for r in s.resources[:100]:
                    rows.append(f"{s.name}  {r.get('uri')}" + (f"  ({r.get('name')})" if r.get("name") else "")
                                + (f"  - {r.get('description')}" if r.get("description") else ""))
                for t in s.templates[:40]:
                    rows.append(f"{s.name}  {t.get('uriTemplate')}  (template: fill in the {{...}} parts)"
                                + (f"  - {t.get('description')}" if t.get("description") else ""))
            return "\n".join(rows) or "No MCP resources."

        def read(server: str, uri: str) -> str:
            s = self.servers.get(server)
            if s is None or s.client is None or s.status != "connected":
                return f"ERROR: no connected MCP server named {server!r}."
            try:
                return s.client.read_resource(uri)
            except MCPError as exc:
                return f"ERROR: {exc}"

        prop = lambda d: {"type": "string", "description": d}  # noqa: E731
        return [
            Tool(name="mcp_resources", description="List what MCP servers offer to read (files, records, "
                 "documents), by server and URI.",
                 parameters={"type": "object", "properties": {"server": prop("Only this server (optional).")}},
                 func=listing),
            Tool(name="mcp_read", description="Read one MCP resource by its server and URI (see mcp_resources).",
                 parameters={"type": "object", "properties": {"server": prop("Server name."), "uri": prop("Resource URI.")},
                             "required": ["server", "uri"]},
                 func=read),
        ]

    def prompts(self) -> List[Dict[str, Any]]:
        out = []
        for s in self.servers.values():
            if s.status != "connected":
                continue
            for p in s.prompts:
                out.append({"server": s.name, "name": p.get("name"), "description": p.get("description") or "",
                            "arguments": [a for a in (p.get("arguments") or []) if isinstance(a, dict)]})
        return out

    def run_prompt(self, server: str, name: str, text: str) -> str:
        """A prompt's messages as one text, its arguments filled from what follows the command."""
        s = self.servers.get(server)
        if s is None or s.client is None:
            raise ValueError(f"no connected MCP server named {server!r}")
        spec = next((p for p in s.prompts if p.get("name") == name), None)
        if spec is None:
            raise ValueError(f"{server} has no prompt named {name!r}")
        params = [a for a in (spec.get("arguments") or []) if isinstance(a, dict) and a.get("name")]
        args: Dict[str, str] = {}
        pairs = dict(part.split("=", 1) for part in shlex.split(text or "", posix=True) if "=" in part) \
            if "=" in (text or "") else {}
        if pairs:
            args = {k: v for k, v in pairs.items()}
        elif params and text:
            args[params[0]["name"]] = text   # one free-text argument: the whole of it
        missing = [a["name"] for a in params if a.get("required") and a["name"] not in args]
        if missing:
            raise ValueError("this prompt needs: " + ", ".join(missing) + " (write name=value after the command)")
        return s.client.get_prompt(name, args)

    # ------------------------------------------------------------- status ---

    def describe(self, server: _Server) -> Dict[str, Any]:
        cfg = server.cfg
        client = server.client
        return {
            "name": server.name,
            "kind": "remote" if cfg.get("url") else "local",
            "target": cfg.get("url") or " ".join([str(cfg.get("command", ""))] + [str(a) for a in cfg.get("args") or []]),
            "status": server.status,
            "error": server.error,
            "server": (client.server_info if client else {}) or {},
            "tools": [t.name.split("__", 2)[-1] for t in server.tools],
            "resources": len(server.resources) + len(server.templates),
            "prompts": [p.get("name") for p in server.prompts],
            "log": list(client.log)[-40:] if client else [],
            "read_only_tools": cfg.get("read_only_tools") or [],
            "timeout": cfg.get("timeout") or 120,
            "env_keys": sorted((cfg.get("env") or {}).keys()),
            "auth_header": cfg.get("auth_header") or "",
            "has_key": bool(cfg.get("url")) and bool(keystore.get("mcp:" + server.name)),
            "signed_in": bool(cfg.get("url")) and mcpauth.signed_in(server.name),
            "offers": sorted(k for k in ("sampling", "elicitation") if client is not None
                             and k in (client._capabilities() if client.host else {})),
            "disabled": bool(cfg.get("disabled")),
            "plugin": server.name in self.external,
        }

    def status(self) -> Dict[str, Any]:
        return {"path": self.path, "servers": [self.describe(s) for s in self.servers.values()]}
