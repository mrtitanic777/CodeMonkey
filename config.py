"""Configuration loading for CodeMonkey.

API keys come from environment variables or, for keys entered in the app,
from Windows Credential Manager (see keystore.py). They are never read from
config files, and config files must never contain them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import keystore
import mini_yaml

API_KEY_ENV_VAR = "CODEMONKEY_API_KEY"
LEGACY_KEY_ENV_VAR = "PERSONAL_AGENT_API_KEY"   # the name before CodeMonkey had its own; still read


@dataclass
class Config:
    """Runtime configuration for the agent."""

    base_url: str = "http://localhost:11434/v1"
    model: str = "default"
    agent_name: str = "Assistant"
    workspace: str = "./agent-workspace"
    approval_mode: str = "ask"  # "ask" | "auto"
    max_iterations: int = 200
    skills_dir: str = "./skills"
    # Coding-harness settings (see the "coder:" section in config.yaml).
    # Keys used: project_dir, approval_mode, context_files, commands_dir,
    # mcp_config, sessions_dir.
    coder: dict = field(default_factory=dict)
    # Provider-agnostic model config (OpenCode pattern): `provider` names an
    # entry in `providers`; each entry may set base_url, api_key_env, and a
    # models list. Unset keys fall back to the top-level base_url and the
    # default CODEMONKEY_API_KEY env var.
    provider: str = "default"
    providers: dict = field(default_factory=dict)
    # Context management (see context/): automatic compaction of long
    # sessions. Keys: enabled (default True), max_chars, keep_recent_chars,
    # chunk_chars.
    context: dict = field(default_factory=dict)


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from a YAML file. Missing keys keep defaults."""
    data = mini_yaml.parse_file(path) if os.path.exists(path) else {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path!r} must contain a mapping")
    known = {f for f in Config.__dataclass_fields__}
    clean = {k: v for k, v in data.items() if k in known}
    unknown = sorted(set(data) - known)
    if unknown:
        print(f"Warning: ignoring unknown config keys: {', '.join(unknown)}")
    return Config(**clean)


def get_api_key(env_var: str = API_KEY_ENV_VAR) -> str:
    """Return the API key from the environment, or raise a helpful error."""
    key = os.environ.get(env_var, "").strip() or os.environ.get(LEGACY_KEY_ENV_VAR, "").strip()
    if not key:
        raise RuntimeError(
            f"No API key found. Set the {env_var} environment variable, e.g.\n"
            f'  export {env_var}="sk-..."\n'
            "The key is never stored in config files or the repository."
        )
    return key


def resolve_model_endpoint(config: Config):
    """Resolve (base_url, api_key_or_None, model) for the chosen provider.

    Provider lookup (OpenCode-style): ``config.provider`` names an entry in
    ``config.providers``; each entry may define ``base_url`` and
    ``api_key_env``. Missing entries fall back to the top-level ``base_url``
    and the default key env var. An entry with an empty ``api_key_env``
    means the endpoint needs no key (e.g. local Ollama) — api_key is None
    and the client omits the Authorization header. A key saved in the app
    for the provider is used when its environment variable is not set.
    """
    entry = config.providers.get(config.provider) or {}
    if not isinstance(entry, dict):
        entry = {}
    base_url = entry.get("base_url") or config.base_url
    if "api_key_env" in entry:
        env_var = entry.get("api_key_env") or ""
        api_key = os.environ.get(env_var, "").strip() if env_var else ""
        api_key = api_key or keystore.get(config.provider) or None
    else:
        api_key = (os.environ.get(API_KEY_ENV_VAR, "").strip() or os.environ.get(LEGACY_KEY_ENV_VAR, "").strip()
                   or keystore.get(config.provider) or get_api_key())
    return base_url, api_key, config.model


def resolve_workspace(config: Config) -> str:
    """Return the absolute workspace path, creating it if needed."""
    path = os.path.abspath(os.path.expanduser(config.workspace))
    os.makedirs(path, exist_ok=True)
    return path


def merge_app_providers(config: Config) -> Config:
    """Add the providers saved in the app (<workspace>/webui/providers.json) to *config*.

    The web UI keeps them there (webui/providers.py); scheduled jobs and helpers
    runs read them too, so a model added in Settings works everywhere. Providers
    from config.yaml win over a saved one of the same name. The file holds no keys.
    """
    import json

    path = os.path.join(os.path.abspath(os.path.expanduser(config.workspace)), "webui", "providers.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return config
    for name, entry in (saved.items() if isinstance(saved, dict) else []):
        if isinstance(entry, dict) and name not in config.providers and entry.get("base_url"):
            config.providers[name] = {
                "base_url": str(entry["base_url"]),
                "api_key_env": "",
                "client": entry.get("client") if entry.get("client") in ("openai", "anthropic") else "openai",
                "models": [str(m) for m in entry.get("models") or [] if str(m).strip()],
                "vision": bool(entry.get("vision")),
            }
    return config


def app_settings(config: Config) -> dict:
    """The app's saved Settings (<workspace>/webui/settings.json), or {} when there are none."""
    import json

    path = os.path.join(os.path.abspath(os.path.expanduser(config.workspace)), "webui", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def app_default_model(config: Config) -> str:
    """The "provider/model" chosen in the app's Settings, or "" when none is (or it is unknown here)."""
    saved = app_settings(config)
    provider = str(saved.get("default_provider") or "")
    model = str(saved.get("default_model") or "")
    return f"{provider}/{model}" if provider in config.providers and model else ""
