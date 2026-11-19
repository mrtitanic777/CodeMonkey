"""KeSSie backend adapter.

KeSSie (https://github.com/philtimmes/KeSSie) is a source-available LLM
serving layer that exposes an OpenAI-compatible ``/v1/chat/completions``
endpoint and gives a model recall over conversations far longer than its
context window. Tool calls come back as OpenAI-style ``tool_calls`` from
current KeSSie servers, or, from older ones, as text blocks::

    <tool_call>
    {"name": "tool_name", "arguments": {"key": "value"}}
    </tool_call>

Both are turned into the ``tool_calls`` our ``AgentLoop`` expects. This module
is a **black-box HTTP integration**: it speaks to a running KeSSie server over
HTTP. No KeSSie code is copied or reimplemented here.

Sharp edges:

- KeSSie keeps each conversation's memory under the ``conversation_id`` it is
  sent (webui/app.py gives a session's turns the session's id). It takes a
  history that starts inside what it already holds as a continuation, but one
  that differs from it (a summary in place of older messages) makes it start
  over, so CodeMonkey sends the recent messages (``max_history_chars``) and
  does not summarise for it.
- Do not trust the advertised multi-million-token ``max_model_len`` for latency
  planning: what the model reads per request is KeSSie's window.
- Mid-generation recall can truncate a ``<tool_call>`` block; the parser
  below tolerates unterminated blocks exactly like KeSSie's own parser does.
- Our loop's ``tool``-role messages are translated to ``user``-role messages
  before sending, since KeSSie only understands system/user/assistant roles.
"""

from __future__ import annotations

import json
import re
import urllib.request
import uuid
from typing import Dict, List, Tuple

from agent import ModelClient, OpenAICompatClient
from config import Config, resolve_model_endpoint

# Same tolerant shape as KeSSie's own parser: an unterminated block runs to
# end-of-text instead of being dropped.
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)


def parse_kessie_tool_calls(text: str) -> Tuple[str, List[Dict]]:
    """Split KeSSie ``<tool_call>`` JSON blocks out of *text*.

    Returns ``(clean_text, calls)`` where *calls* are OpenAI-style tool-call
    dicts: ``{"id", "type": "function", "function": {"name", "arguments"}}``
    with arguments as a JSON string, matching what ``AgentLoop`` consumes.
    Unparseable blocks are dropped rather than leaked into the reply text.
    """
    calls: List[Dict] = []

    def _replace(match: "re.Match") -> str:
        raw = match.group(1).strip()
        if raw:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return ""
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = {}
            if name and isinstance(args, dict):
                calls.append(
                    {
                        "id": f"kessie_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args),
                        },
                    }
                )
        return ""

    clean = TOOL_CALL_RE.sub(_replace, text or "")
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, calls


def _to_kessie_messages(messages: List[Dict]) -> List[Dict]:
    """Translate our loop's message history into roles KeSSie understands."""
    out: List[Dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f"[tool '{m.get('name', '?')}' returned]\n{content}",
                }
            )
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if not content and calls:
                names = ", ".join(
                    c.get("function", {}).get("name", "?") for c in calls
                )
                content = f"[called tools: {names}]"
            if content:
                out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": role or "user", "content": content})
    return out


def _message_size(m: Dict) -> int:
    content = m.get("content")
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content or ""))


def trim_history(messages: List[Dict], max_chars: int) -> List[Dict]:
    """Keep the first (system) message plus the most recent ones fitting.

    KeSSie re-ingests everything per request, so without a cap a long agent
    session gets slower every turn. Always keeps at least the latest message.
    """
    if not messages:
        return []
    first, rest = messages[0], messages[1:]
    budget = max_chars - _message_size(first)
    kept: List[Dict] = []
    for m in reversed(rest):
        size = _message_size(m)
        if kept and budget - size < 0:
            break
        kept.append(m)
        budget -= size
    kept.reverse()
    return [first] + kept


class KeSSieClient(ModelClient):
    """ModelClient speaking to a KeSSie server's OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        conversation_id: str | None = None,
        timeout: int = 180,
        max_history_chars: int = 200_000,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.model = model
        # Stable across the turns of one session; KeSSie routes its (short-
        # lived) per-conversation cache by this id.
        self.conversation_id = conversation_id or uuid.uuid4().hex[:16]
        self.timeout = timeout
        self.max_history_chars = max_history_chars

    @classmethod
    def from_config(
        cls, config: Config, conversation_id: str | None = None
    ) -> "KeSSieClient":
        base_url, api_key, model = resolve_model_endpoint(config)
        entry = config.providers.get(config.provider) or {}
        opts = entry.get("kessie") or {} if isinstance(entry, dict) else {}
        return cls(
            base_url,
            api_key or "",
            model,
            conversation_id=conversation_id,
            timeout=int(opts.get("timeout", 180)),
            max_history_chars=int(opts.get("max_history_chars", 200_000)),
        )

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        trimmed = trim_history(_to_kessie_messages(messages), self.max_history_chars)
        payload = {
            "model": self.model,
            "messages": trimmed,
            "tools": tools,
            "tool_choice": "auto",
            "conversation_id": self.conversation_id,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response = json.load(resp)
        except Exception as exc:  # noqa: BLE001 - wrapped for the loop
            raise RuntimeError(
                f"KeSSie request failed ({type(exc).__name__}: {exc})"
            ) from exc
        try:
            msg = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Malformed KeSSie response: {str(response)[:200]} ({exc})"
            )
        clean_text, tool_calls = parse_kessie_tool_calls(msg.get("content") or "")
        if msg.get("tool_calls"):
            # newer KeSSie servers send the calls already parsed, OpenAI's way, with empty text
            tool_calls = [{"id": c.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                           "function": {"name": (c.get("function") or {}).get("name", ""),
                                        "arguments": (c.get("function") or {}).get("arguments") or "{}"}}
                          for c in msg["tool_calls"]] + (tool_calls or [])
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": clean_text,
                        "tool_calls": tool_calls,
                    }
                }
            ]
        }


def client_from_config(
    config: Config, conversation_id: str | None = None
) -> ModelClient:
    """Build the configured model client.

    A provider entry with ``client: kessie`` selects the KeSSie adapter,
    ``client: anthropic`` the Claude adapter (anthropic_client.py); anything
    else uses the standard OpenAI-compatible client.
    """
    entry = config.providers.get(config.provider) or {}
    if isinstance(entry, dict) and entry.get("client") == "kessie":
        return KeSSieClient.from_config(config, conversation_id=conversation_id)
    if isinstance(entry, dict) and entry.get("client") == "anthropic":
        from anthropic_client import AnthropicClient, compat_base_url, sdk_available

        if sdk_available():
            return AnthropicClient.from_config(config)
        # no anthropic package (the installers leave it out): Claude's OpenAI-compatible endpoint
        base_url, api_key, model = resolve_model_endpoint(config)
        if not api_key:
            raise RuntimeError(f"No API key for the {config.provider!r} provider. Add one in Settings > Models.")
        return OpenAICompatClient(compat_base_url(base_url), api_key, model)
    return OpenAICompatClient.from_config(config)
