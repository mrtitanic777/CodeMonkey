"""Plugins: skills, commands, agents, reply styles, hooks and MCP servers, shared as one package.

A plugin is a folder:

    my-plugin/
      plugin.json          {"name": "my-plugin", "version": "1.0.0", "description": "...", "author": "..."}
      skills/<skill>/SKILL.md
      commands/*.md        become /my-plugin:<command>
      agents/*.md
      styles/*.md
      hooks.yaml           run on tool calls, like your own hooks.yaml
      mcp.json             {"servers": {...}}: become MCP servers named my-plugin-<server>

Installed plugins live in plugins/ next to CodeMonkey. One is installed from a folder, a .zip, or
a git URL, and arrives switched OFF: hooks and MCP servers run programs on this computer, so it is
only switched on after you have seen what it brings (``summary``). Switching one off or removing
it takes all of it away again.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Dict, List, Tuple

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
KINDS = ("skills", "commands", "agents", "styles")


class PluginError(RuntimeError):
    pass


def _rmtree(path: str) -> None:
    """Delete a folder, read-only files too (git keeps its objects read-only on Windows)."""
    import stat

    def again(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onexc=again) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=again)


def _manifest(folder: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(folder, "plugin.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PluginError(f"no readable plugin.json in {folder}: {exc}") from exc
    if not isinstance(data, dict) or not NAME_RE.fullmatch(str(data.get("name") or "")):
        raise PluginError("plugin.json needs a name: lowercase letters, digits, - and _, up to 40 characters")
    return data


def _find_root(folder: str) -> str:
    """The folder holding plugin.json: the given one, or its only sub-folder (as zips often are)."""
    if os.path.isfile(os.path.join(folder, "plugin.json")):
        return folder
    subs = [os.path.join(folder, d) for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))]
    if len(subs) == 1 and os.path.isfile(os.path.join(subs[0], "plugin.json")):
        return subs[0]
    raise PluginError("plugin.json not found (it must be at the top of the plugin, or of its only folder)")


class PluginManager:
    def __init__(self, root: str) -> None:
        self.root = root
        self.state_path = os.path.join(root, "plugins.json")

    # ------------------------------------------------------------ state ---

    def _state(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, self.state_path)

    def installed(self) -> List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]:
        """(name, folder, manifest, state) for each plugin folder that is readable."""
        out = []
        state = self._state()
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return out
        for name in names:
            folder = os.path.join(self.root, name)
            if not os.path.isdir(folder):
                continue
            try:
                manifest = _manifest(folder)
            except PluginError:
                continue
            out.append((manifest["name"], folder, manifest, state.get(manifest["name"], {"enabled": False})))
        return out

    def enabled(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        return [(n, f, m) for n, f, m, s in self.installed() if s.get("enabled")]

    # ------------------------------------------------------------ what they bring ---

    def dirs(self, kind: str) -> List[Tuple[str, str]]:
        """(folder, origin) of each switched-on plugin's skills/commands/agents/styles folder."""
        return [(os.path.join(f, kind), f"plugin:{n}") for n, f, _m in self.enabled() if os.path.isdir(os.path.join(f, kind))]

    def hook_files(self) -> List[str]:
        return [os.path.join(f, "hooks.yaml") for _n, f, _m in self.enabled() if os.path.isfile(os.path.join(f, "hooks.yaml"))]

    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for name, folder, _m in self.enabled():
            path = os.path.join(folder, "mcp.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            for server, cfg in ((data.get("servers") or data.get("mcpServers") or {}) if isinstance(data, dict) else {}).items():
                if isinstance(cfg, dict) and (cfg.get("command") or cfg.get("url")):
                    cfg = dict(cfg)
                    if cfg.get("cwd") and not os.path.isabs(cfg["cwd"]):
                        cfg["cwd"] = os.path.join(folder, cfg["cwd"])
                    elif cfg.get("command") and not cfg.get("cwd"):
                        cfg["cwd"] = folder        # a plugin's server runs from the plugin's folder
                    out[f"{name}-{server}"] = cfg
        return out

    def summary(self, name: str) -> Dict[str, Any]:
        for n, folder, manifest, state in self.installed():
            if n == name:
                return self._describe(n, folder, manifest, state)
        raise PluginError(f"no plugin named {name!r}")

    def _describe(self, name: str, folder: str, manifest: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        def listed(kind: str) -> List[str]:
            path = os.path.join(folder, kind)
            if not os.path.isdir(path):
                return []
            if kind == "skills":
                return sorted(d for d in os.listdir(path) if os.path.isfile(os.path.join(path, d, "SKILL.md")))
            return sorted(f[:-3] for f in os.listdir(path) if f.endswith(".md"))

        hooks: List[Dict[str, str]] = []
        hook_path = os.path.join(folder, "hooks.yaml")
        if os.path.isfile(hook_path):
            try:
                from hooks import HookManager

                hooks = [{"event": h.event, "command": h.command} for h in HookManager.load(hook_path).hooks]
            except Exception:  # noqa: BLE001
                hooks = [{"event": "?", "command": "(hooks.yaml could not be read)"}]
        mcp = [{"name": k, "runs": v.get("url") or " ".join([str(v.get("command", ""))] + [str(a) for a in v.get("args") or []])}
               for k, v in self._servers_of(name, folder).items()]
        return {
            "name": name, "version": str(manifest.get("version") or ""), "description": str(manifest.get("description") or ""),
            "author": str(manifest.get("author") or ""), "homepage": str(manifest.get("homepage") or ""),
            "enabled": bool(state.get("enabled")), "source": state.get("source", ""), "installed": state.get("installed", ""),
            "path": folder, "skills": listed("skills"), "commands": [f"{name}:{c}" for c in listed("commands")],
            "agents": listed("agents"), "styles": listed("styles"), "hooks": hooks, "mcp": mcp,
        }

    def _servers_of(self, name: str, folder: str) -> Dict[str, Dict[str, Any]]:
        path = os.path.join(folder, "mcp.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        servers = (data.get("servers") or data.get("mcpServers") or {}) if isinstance(data, dict) else {}
        return {f"{name}-{k}": v for k, v in servers.items() if isinstance(v, dict)}

    def listing(self) -> List[Dict[str, Any]]:
        return [self._describe(n, f, m, s) for n, f, m, s in self.installed()]

    # ------------------------------------------------------------ install ---

    def install(self, source: str, replace: bool = False) -> Dict[str, Any]:
        source = str(source or "").strip().strip('"')
        if not source:
            raise PluginError("give a folder, a .zip file or a git URL")
        work = tempfile.mkdtemp(prefix="cm-plugin-")
        try:
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(work, "p"), ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
                got = os.path.join(work, "p")
            elif source.lower().endswith(".zip") and os.path.isfile(source):
                got = os.path.join(work, "p")
                with zipfile.ZipFile(source) as zf:
                    for member in zf.namelist():   # nothing may land outside the folder
                        target = os.path.realpath(os.path.join(got, member))
                        if not target.startswith(os.path.realpath(got) + os.sep) and target != os.path.realpath(got):
                            raise PluginError(f"the zip has a path that escapes it: {member}")
                    zf.extractall(got)
            elif re.match(r"^(https?://|git@|ssh://)", source) or source.endswith(".git"):
                got = os.path.join(work, "p")
                proc = subprocess.run(["git", "clone", "--depth", "1", source, got], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=180,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0:
                    raise PluginError("git clone failed: " + (proc.stderr or proc.stdout).strip()[:400])
                _rmtree(os.path.join(got, ".git"))
            else:
                raise PluginError(f"not a folder, a .zip or a git URL: {source}")
            root = _find_root(got)
            manifest = _manifest(root)
            name = manifest["name"]
            dest = os.path.join(self.root, name)
            state = self._state()
            if os.path.exists(dest):
                if not replace:
                    raise PluginError(f"a plugin named {name!r} is installed already; remove it or update it")
                _rmtree(dest)
            os.makedirs(self.root, exist_ok=True)
            shutil.move(root, dest)
            keep_on = replace and state.get(name, {}).get("enabled")
            state[name] = {"enabled": bool(keep_on), "source": source,
                           "installed": datetime.datetime.now().isoformat(timespec="seconds")}
            self._save(state)
            return self.summary(name)
        finally:
            _rmtree(work)

    def update(self, name: str) -> Dict[str, Any]:
        source = self._state().get(name, {}).get("source")
        if not source:
            raise PluginError(f"{name} has no source to update from")
        return self.install(source, replace=True)

    def set_enabled(self, name: str, on: bool) -> Dict[str, Any]:
        self.summary(name)   # it must exist
        state = self._state()
        state.setdefault(name, {})["enabled"] = bool(on)
        self._save(state)
        return self.summary(name)

    def remove(self, name: str) -> None:
        folder = self.summary(name)["path"]
        _rmtree(folder)
        state = self._state()
        state.pop(name, None)
        self._save(state)
