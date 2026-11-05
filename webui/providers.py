"""Model providers added in the app, and the API keys saved for them.

Providers from config.yaml stay as they are. Ones added in Settings > Models (any
OpenAI-compatible address, or a Claude-API proxy) are kept in <workspace>/webui/providers.json,
which holds no secrets. Keys go to Windows Credential Manager through keystore.py, never to a file.

Changing a saved provider's address removes its key, so a key can never be sent somewhere other
than where it was entered for.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List

import keystore

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CLIENTS = ("openai", "anthropic")


class Providers:
    def __init__(self, app) -> None:
        self.app = app
        self.path = os.path.join(app.webui_dir, "providers.json")
        self._lock = threading.Lock()
        self.custom: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
        providers = app.config.providers
        for name, entry in (saved.items() if isinstance(saved, dict) else []):
            if NAME_RE.fullmatch(str(name)) and isinstance(entry, dict) and name not in providers:
                self.custom[name] = self._clean(entry)
                providers[name] = dict(self.custom[name])

    @staticmethod
    def _clean(entry: Dict[str, Any]) -> Dict[str, Any]:
        models = entry.get("models") if isinstance(entry.get("models"), list) else []
        return {
            "base_url": str(entry.get("base_url") or "")[:400],
            "api_key_env": "",
            "client": entry.get("client") if entry.get("client") in CLIENTS else "openai",
            "models": [str(m).strip()[:200] for m in models if str(m).strip()][:50],
            "vision": bool(entry.get("vision")),
            # one reply's length; 0: the app's default (webui/app.py client_for)
            "max_tokens": max(0, min(200000, int(entry.get("max_tokens") or 0))),
            # the model's window in tokens; 0: reported by a local server, or the app's default
            "context_window": max(0, min(1_000_000_000, int(entry.get("context_window") or 0))),
        }

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.custom, fh, indent=2)
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- keys ---

    def key_status(self, name: str) -> Dict[str, Any]:
        """Where this provider's key comes from: the environment, Credential Manager, or nowhere."""
        entry = self.app.config.providers.get(name) or {}
        env_var = entry.get("api_key_env") if "api_key_env" in entry else "CODEMONKEY_API_KEY"
        if env_var and os.environ.get(env_var, "").strip():
            return {"source": "env", "env": env_var, "hint": keystore.hint(os.environ[env_var].strip())}
        stored = keystore.get(name)
        if stored:
            return {"source": "stored", "env": env_var or "", "hint": keystore.hint(stored)}
        return {"source": "", "env": env_var or "", "hint": ""}

    def set_key(self, name: str, key: str) -> Dict[str, Any]:
        if name not in self.app.config.providers:
            raise KeyError(name)
        key = str(key or "").strip()
        if not 8 <= len(key) <= 500 or any(c.isspace() for c in key):
            raise ValueError("That does not look like an API key (no spaces, 8 to 500 characters).")
        keystore.set(name, key)
        return self.key_status(name)

    def delete_key(self, name: str) -> Dict[str, Any]:
        if name not in self.app.config.providers:
            raise KeyError(name)
        keystore.delete(name)
        return self.key_status(name)

    # ----------------------------------------------------------- providers ---

    def save(self, name: str, base_url: str, models: List[str], client: str = "openai",
             key: str = "", vision: bool = False) -> Dict[str, Any]:
        """Add a provider, or change one added here before."""
        name = str(name or "").strip().lower()
        if not NAME_RE.fullmatch(name):
            raise ValueError("Name: lowercase letters, digits, - and _, up to 32 characters.")
        if name in self.app.config.providers and name not in self.custom:
            raise ValueError(f"{name!r} is set in config.yaml; choose another name.")
        base_url = str(base_url or "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", base_url):
            raise ValueError("Address: an http:// or https:// URL, such as https://api.example.com/v1.")
        if isinstance(models, str):
            models = [m for m in re.split(r"[,\n]", models)]
        entry = self._clean({"base_url": base_url, "client": client, "models": list(models or []), "vision": vision})
        with self._lock:
            before = self.custom.get(name)
            if before and before.get("base_url") != entry["base_url"]:
                keystore.delete(name)   # a key goes only where it was entered for
            self.custom[name] = entry
            self.app.config.providers[name] = dict(entry)
            self._save()
        if key:
            self.set_key(name, key)
        return {"name": name, **entry, "key": self.key_status(name)}

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self.custom:
                return False
            del self.custom[name]
            self.app.config.providers.pop(name, None)
            self._save()
        keystore.delete(name)
        if self.app.settings.get("default_provider") == name:
            self.app.update_settings({"default_provider": "", "default_model": ""})
        return True
