"""Signing in to a remote MCP server with OAuth (the MCP authorization spec: OAuth 2.1 + PKCE).

A server that wants a sign-in answers 401 with a WWW-Authenticate header. From there:

  1. its protected-resource metadata (RFC 9728) names the authorization server;
  2. that server's metadata (RFC 8414, or OpenID discovery) gives the endpoints;
  3. CodeMonkey registers itself as a client (RFC 7591), unless mcp.json gives a client_id;
  4. the user signs in in a browser (authorization code with PKCE, S256), which comes back to
     a callback address on this machine: CodeMonkey's own server, or a short-lived one for
     the terminal;
  5. the code is exchanged for tokens.

The access token goes where a pasted key would (Credential Manager, "mcp:<name>"), so it is sent
the same way; the refresh token and the endpoints go to "mcp-oauth:<name>", also in Credential
Manager. A 401 later is answered by refreshing once before asking for a new sign-in.

mcp.json may set, per server: "oauth": {"client_id": "...", "client_secret": "...", "scope": "..."}.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

import keystore

_TIMEOUT = 20
_FLOW_LIFE = 15 * 60
_flows: Dict[str, Dict[str, Any]] = {}   # state -> a sign-in that has not come back yet
_lock = threading.Lock()


class AuthError(Exception):
    """A sign-in step failed; the message says which."""


# -------------------------------------------------------------------- http ---


def _get_json(url: str) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read() or b"{}")
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post(url: str, body: Dict[str, Any], as_json: bool = False) -> Dict[str, Any]:
    data = json.dumps(body).encode() if as_json else urllib.parse.urlencode(
        {k: v for k, v in body.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            parsed = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
            text = detail.get("error_description") or detail.get("error") or str(exc.reason)
        except ValueError:
            text = str(exc.reason)
        raise AuthError(f"{urllib.parse.urlsplit(url).netloc} said HTTP {exc.code}: {text}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AuthError(f"could not reach {url}: {getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:
        raise AuthError(f"{url} did not answer in JSON") from exc
    if not isinstance(parsed, dict):
        raise AuthError(f"{url} gave an unexpected answer")
    return parsed


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# --------------------------------------------------------------- discovery ---


def _challenge_param(challenge: str, name: str) -> str:
    m = re.search(name + r'\s*=\s*"([^"]*)"', challenge or "") or re.search(name + r"\s*=\s*([^\s,]+)", challenge or "")
    return m.group(1) if m else ""


def discover(url: str, challenge: str = "") -> Dict[str, Any]:
    """The endpoints to sign in to the server at *url*: authorize, token, register, the resource."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    candidates = [_challenge_param(challenge, "resource_metadata")] if _challenge_param(challenge, "resource_metadata") else []
    candidates += [_origin(url) + "/.well-known/oauth-protected-resource" + path,
                   _origin(url) + "/.well-known/oauth-protected-resource"]
    resource_meta = next((m for m in (_get_json(c) for c in dict.fromkeys(candidates) if c) if m), None) or {}
    issuers = resource_meta.get("authorization_servers") or [_origin(url)]
    issuer = str(issuers[0]).rstrip("/")
    ipath = urllib.parse.urlsplit(issuer).path.rstrip("/")
    meta = None
    for candidate in (_origin(issuer) + "/.well-known/oauth-authorization-server" + ipath,
                      _origin(issuer) + "/.well-known/openid-configuration" + ipath,
                      issuer + "/.well-known/openid-configuration"):
        meta = _get_json(candidate)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
        meta = None
    if meta is None:   # an older server with no metadata: the spec's default paths
        base = _origin(issuer)
        meta = {"authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                "registration_endpoint": base + "/register"}
    methods = meta.get("code_challenge_methods_supported")
    if methods and "S256" not in methods:
        raise AuthError("the sign-in server does not support PKCE with S256, which MCP requires")
    return {
        "authorize": meta["authorization_endpoint"], "token": meta["token_endpoint"],
        "register": meta.get("registration_endpoint") or "",
        "resource": str(resource_meta.get("resource") or url),
        "scope": " ".join(resource_meta.get("scopes_supported") or []) or _challenge_param(challenge, "scope"),
    }


def _client(name: str, cfg: Dict[str, Any], endpoints: Dict[str, Any], redirect_uri: str) -> Dict[str, str]:
    """A client id for this server: mcp.json's, one registered earlier for this callback, or a new one."""
    oauth = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
    if oauth.get("client_id"):
        return {"client_id": str(oauth["client_id"]), "client_secret": str(oauth.get("client_secret") or "")}
    saved = _state(name)
    if saved.get("client_id") and saved.get("redirect_uri") == redirect_uri and saved.get("token") == endpoints["token"]:
        return {"client_id": saved["client_id"], "client_secret": saved.get("client_secret", "")}
    if not endpoints.get("register"):
        raise AuthError("this server needs a client id: set \"oauth\": {\"client_id\": ...} for it in mcp.json")
    got = _post(endpoints["register"], {
        "client_name": "CodeMonkey", "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    }, as_json=True)
    if not got.get("client_id"):
        raise AuthError("the sign-in server did not give a client id")
    return {"client_id": str(got["client_id"]), "client_secret": str(got.get("client_secret") or "")}


# ----------------------------------------------------------------- stored ---


def _state(name: str) -> Dict[str, Any]:
    try:
        data = json.loads(keystore.get("mcp-oauth:" + name) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def signed_in(name: str) -> bool:
    return bool(_state(name).get("refresh_token") or (_state(name).get("token") and keystore.get("mcp:" + name)))


def forget(name: str) -> None:
    keystore.delete("mcp-oauth:" + name)
    keystore.delete("mcp:" + name)


def _store(name: str, tokens: Dict[str, Any], extra: Dict[str, Any]) -> str:
    access = str(tokens.get("access_token") or "")
    if not access:
        raise AuthError("the sign-in server gave no access token")
    keystore.set("mcp:" + name, access)
    state = dict(_state(name), **extra)
    if tokens.get("refresh_token"):
        state["refresh_token"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        state["expires_at"] = int(time.time() + int(tokens["expires_in"]))
    keystore.set("mcp-oauth:" + name, json.dumps(state))
    return access


# ------------------------------------------------------------------- flow ---


def begin(name: str, cfg: Dict[str, Any], redirect_uri: str, challenge: str = "") -> str:
    """Start a sign-in: the address to open in a browser. It comes back to *redirect_uri*."""
    endpoints = discover(str(cfg.get("url") or ""), challenge)
    client = _client(name, cfg, endpoints, redirect_uri)
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    oauth = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
    scope = str(oauth.get("scope") or endpoints.get("scope") or "")
    with _lock:
        now = time.time()
        for key in [k for k, f in _flows.items() if now - f["at"] > _FLOW_LIFE]:
            del _flows[key]
        _flows[state] = {"name": name, "verifier": verifier, "endpoints": endpoints, "client": client,
                         "redirect_uri": redirect_uri, "at": now}
    query = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri, "state": state,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256", "resource": endpoints["resource"],
    }
    if scope:
        query["scope"] = scope
    sep = "&" if "?" in endpoints["authorize"] else "?"
    return endpoints["authorize"] + sep + urllib.parse.urlencode(query)


def finish(state: str, code: str = "", error: str = "") -> str:
    """The browser came back: exchange the code. Returns the server's name."""
    with _lock:
        flow = _flows.pop(state or "", None)
    if flow is None:
        raise AuthError("this sign-in is unknown or too old: start it again")
    if error or not code:
        raise AuthError(f"the sign-in was not completed ({error or 'no code came back'})")
    endpoints, client = flow["endpoints"], flow["client"]
    tokens = _post(endpoints["token"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
        "client_id": client["client_id"], "client_secret": client.get("client_secret") or None,
        "code_verifier": flow["verifier"], "resource": endpoints["resource"],
    })
    _store(flow["name"], tokens, {"token": endpoints["token"], "resource": endpoints["resource"],
                                  "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                                  "redirect_uri": flow["redirect_uri"]})
    return flow["name"]


def refresher(name: str):
    """A function giving a new access token from the stored refresh token, or None."""
    def refresh() -> Optional[str]:
        state = _state(name)
        if not state.get("refresh_token") or not state.get("token"):
            return None
        try:
            tokens = _post(state["token"], {
                "grant_type": "refresh_token", "refresh_token": state["refresh_token"],
                "client_id": state.get("client_id"), "client_secret": state.get("client_secret") or None,
                "resource": state.get("resource"),
            })
            return _store(name, tokens, {})
        except AuthError:
            return None
    return refresh


def sign_in_here(name: str, cfg: Dict[str, Any], challenge: str = "", wait: float = 300) -> None:
    """The terminal's sign-in: a short-lived callback on this machine, and the system browser."""
    import http.server
    import webbrowser

    done: Dict[str, Any] = {}
    got = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server name
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            done.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p style='font-family:sans-serif'>Signed in. You can close this tab.</p>")
            got.set()

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = begin(name, cfg, f"http://127.0.0.1:{server.server_port}/callback", challenge)
        print(f"  Sign in to {name} in your browser. If it does not open, go to:\n  {url}")
        webbrowser.open(url)
        if not got.wait(wait):
            raise AuthError("no sign-in came back in time")
        finish(done.get("state", ""), done.get("code", ""), done.get("error", ""))
    finally:
        server.shutdown()
