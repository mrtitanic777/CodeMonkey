"""Model client for Claude, through the official ``anthropic`` SDK.

The rest of the framework speaks OpenAI-style chat completions, so this adapter translates both
ways: the conversation (system prompt, tool calls, tool results, images) into Messages API
requests, and Claude's answer back into the ``{"choices": [...]}`` shape the agent loop reads.

What it does on top of the translation:
  - streams every request (text reaches on_delta as it arrives, and long answers cannot time out);
  - keeps Claude's thinking blocks and sends them back with the turns they belong to, which tool
    use needs. The harness rewrites its history (compaction, working set), so requests ask the
    API to drop a block whose conversation changed instead of refusing the whole request;
  - turns the session's effort setting into ``output_config.effort``;
  - asks for automatic prompt caching;
  - on models that support it, lets the API retry a declined request on another model
    (server-side fallbacks).

Selected by ``client: "anthropic"`` on a provider entry. Only this client needs the package
(``pip install anthropic``). Without it (the installers leave it out), Claude is reached through
Anthropic's OpenAI-compatible endpoint by the standard client instead: no thinking blocks or
caching, but it works out of the box (see kessie_client.client_from_config).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from agent import ModelClient, normalise_messages
from config import Config, resolve_model_endpoint

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
#: Models that accept ``fallbacks: "default"`` on the Claude API.
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
#: The session's effort words, as the Messages API spells them. "auto" sends nothing.
EFFORTS = {"off": "low", "low": "low", "medium": "medium", "high": "high"}
MAX_TOKENS = 32000
#: A refusal of one of the extras (betas, thinking settings, fallbacks, caching) by a model or proxy
#: that does not take them; the request is sent once more without any of them.
_EXTRAS_REFUSED = re.compile(r"block_binding|fallback|anthropic-beta|beta|thinking|cache_control|"
                             r"Extra inputs are not permitted", re.I)


def sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def compat_base_url(base_url: Optional[str]) -> str:
    """Anthropic's OpenAI-compatible endpoint for a Messages API base address."""
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _sdk():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError(
            "The Claude provider needs the anthropic package. Install it with: pip install anthropic"
        ) from exc
    return anthropic


class _BlockMemory:
    """Claude's own content blocks for the assistant turns it wrote, found again by tool call id
    (or by the text of a turn with no tool calls). Shared by all clients, so a session keeps its
    thinking across turns even though each turn makes a new client."""

    LIMIT = 2000

    def __init__(self) -> None:
        self._blocks: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(message: Dict[str, Any]) -> str:
        calls = message.get("tool_calls") or []
        if calls:
            return "calls:" + ",".join(str(c.get("id", "")) for c in calls)
        return "text:" + str(message.get("content") or "")

    def remember(self, message: Dict[str, Any], blocks: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._blocks[self.key(message)] = blocks
            self._blocks.move_to_end(self.key(message))
            while len(self._blocks) > self.LIMIT:
                self._blocks.popitem(last=False)

    def recall(self, message: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            return self._blocks.get(self.key(message))


BLOCKS = _BlockMemory()


def to_anthropic_tools(tools: List[Dict]) -> List[Dict]:
    out = []
    for tool in tools or []:
        fn = tool.get("function") or tool
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _content_blocks(content: Any) -> List[Dict[str, Any]]:
    """A user message's content (text, or OpenAI content parts) as Messages API blocks."""
    if isinstance(content, str) or content is None:
        return [{"type": "text", "text": content or "(empty)"}]
    blocks: List[Dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            m = re.match(r"data:(image/[a-z+.-]+);base64,(.*)", url, re.S)
            if m:
                blocks.append({"type": "image",
                               "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            elif url.startswith(("http://", "https://")):
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks or [{"type": "text", "text": "(empty)"}]


def to_anthropic_messages(messages: List[Dict]):
    """(system, messages) for the Messages API from an OpenAI-style conversation.

    Tool results always answer the tool calls just before them: an orphaned result becomes plain
    text, and a call with no result gets one saying so, because the API refuses either.
    """
    system = ""
    out: List[Dict[str, Any]] = []
    pending: List[str] = []   # tool call ids of the last assistant turn still waiting for results

    def close_pending() -> None:
        if pending:
            results = [{"type": "tool_result", "tool_use_id": i, "content": "(no result was recorded)"}
                       for i in pending]
            if out and out[-1]["role"] == "user" and out[-1].get("_results"):
                out[-1]["content"].extend(results)
            else:
                out.append({"role": "user", "content": results, "_results": True})
            pending.clear()

    for m in normalise_messages(messages):
        role = m.get("role")
        if role == "system":
            system = str(m.get("content") or "")
        elif role == "tool":
            call_id = str(m.get("tool_call_id") or "")
            text = str(m.get("content") or "") or "(empty)"
            if call_id in pending:
                pending.remove(call_id)
                block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
            else:
                block = {"type": "text", "text": "Tool result:\n" + text}
            if out and out[-1]["role"] == "user" and out[-1].get("_results"):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block], "_results": True})
        elif role == "assistant":
            close_pending()
            blocks = BLOCKS.recall(m)
            if blocks is None:
                blocks = []
                if str(m.get("content") or "").strip():
                    blocks.append({"type": "text", "text": str(m["content"])})
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": str(call.get("id") or ""),
                                   "name": str(fn.get("name") or ""), "input": args if isinstance(args, dict) else {}})
            if not blocks:
                continue
            pending.extend(b["id"] for b in blocks if b.get("type") == "tool_use")
            out.append({"role": "assistant", "content": blocks})
        else:
            close_pending()
            out.append({"role": "user", "content": _content_blocks(m.get("content"))})
    close_pending()
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The conversation continues.)"}]})
    for message in out:
        message.pop("_results", None)
    return system, out


def _block_dict(block: Any) -> Optional[Dict[str, Any]]:
    """A response block in the form it is sent back in, or None for kinds not sent back."""
    kind = getattr(block, "type", "")
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


class AnthropicClient(ModelClient):
    """ModelClient for Claude models on the Claude API (or a proxy that speaks it)."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, timeout: float = 600.0):
        anthropic = _sdk()
        kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 2}
        if base_url and "api.anthropic.com" not in base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.on_delta = None
        self.on_thinking = None   # Claude's thinking as it arrives, to be shown
        self.should_stop = None
        self.on_progress = None
        self.usage_total = {"in": 0, "out": 0, "gen_ms": 0, "ttft_ms": None, "reported": False, "reasoning": 0}
        self.params: Dict[str, Any] = {}   # set by the app from local-model profiles; not used for Claude
        self.notes: List[str] = []
        self.calm_effort = "medium"
        self.off_effort = ""
        self.effort = "auto"
        self._plain = False   # extras were refused once: send plain requests from then on

    @classmethod
    def from_config(cls, config: Config) -> "AnthropicClient":
        base_url, api_key, model = resolve_model_endpoint(config)
        if not api_key:
            raise RuntimeError(
                f"No API key for the {config.provider!r} provider. Add one in Settings > Providers."
            )
        return cls(api_key, model, base_url)

    @staticmethod
    def list_models(api_key: str, base_url: Optional[str] = None) -> List[str]:
        if not sdk_available():   # the models list is a plain GET; no package needed
            import urllib.request

            req = urllib.request.Request(compat_base_url(base_url) + "/models?limit=100", headers={
                "x-api-key": api_key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return [m["id"] for m in json.load(resp).get("data", [])]
        anthropic = _sdk()
        kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": 10.0, "max_retries": 0}
        if base_url and "api.anthropic.com" not in base_url:
            kwargs["base_url"] = base_url
        return [m.id for m in anthropic.Anthropic(**kwargs).models.list(limit=100)]

    # ------------------------------------------------------------ request ---

    def _request(self, system: str, messages: List[Dict], tools: List[Dict]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"model": self.model, "max_tokens": MAX_TOKENS, "messages": messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        effort = EFFORTS.get(self.effort)
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if self._plain:
            return kwargs
        betas = [THINKING_BINDING_BETA]
        extra: Dict[str, Any] = {"cache_control": {"type": "ephemeral"}}
        kwargs["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        if self.model in FALLBACK_MODELS:
            betas.append(FALLBACK_BETA)
            extra["fallbacks"] = "default"
        kwargs["betas"] = betas
        kwargs["extra_body"] = extra
        return kwargs

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        anthropic = _sdk()
        system, converted = to_anthropic_messages(messages)
        claude_tools = to_anthropic_tools(tools)
        try:
            return self._stream(self._request(system, converted, claude_tools))
        except anthropic.BadRequestError as exc:
            if self._plain or not _EXTRAS_REFUSED.search(str(exc)):
                raise RuntimeError(f"Model request failed ({self._explain(exc)})") from exc
            # a model or proxy that does not take the extras: once more without any of them,
            # and without the thinking blocks they would have been checked against
            self._plain = True
            self.notes.append("Claude refused the extra request settings (thinking controls, fallbacks, "
                              "caching), so plain requests are sent from now on.")
            for message in converted:
                if message["role"] == "assistant":
                    message["content"] = [b for b in message["content"]
                                          if b.get("type") not in ("thinking", "redacted_thinking")] or \
                                         [{"type": "text", "text": "(no answer)"}]
            try:
                return self._stream(self._request(system, converted, claude_tools))
            except anthropic.APIError as again:
                raise RuntimeError(f"Model request failed ({self._explain(again)})") from again
        except anthropic.APIError as exc:
            raise RuntimeError(f"Model request failed ({self._explain(exc)})") from exc

    @staticmethod
    def _explain(exc: Exception) -> str:
        anthropic = _sdk()
        if isinstance(exc, anthropic.AuthenticationError):
            return "the API key was refused; check it in Settings > Providers"
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "this API key may not use this model"
        if isinstance(exc, anthropic.NotFoundError):
            return "unknown model or address: " + str(getattr(exc, "message", exc))[:300]
        if isinstance(exc, anthropic.RateLimitError):
            return "rate limited by the Claude API; try again shortly"
        if isinstance(exc, anthropic.APIConnectionError):
            return "could not reach the Claude API: " + str(exc)[:200]
        return f"{type(exc).__name__}: {str(getattr(exc, 'message', exc))[:400]}"

    def _stream(self, kwargs: Dict[str, Any]) -> Dict:
        started = time.time()
        first_at: Optional[float] = None
        text_parts: List[str] = []
        stopped = False
        final = None
        api = self._client.messages if self._plain else self._client.beta.messages
        with api.stream(**kwargs) as stream:
            for event in stream:
                if self.should_stop is not None and self.should_stop():
                    stopped = True
                    break
                if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                    if first_at is None:
                        first_at = time.time()
                    text_parts.append(event.delta.text)
                    if self.on_delta is not None:
                        self.on_delta(event.delta.text)
                elif event.type == "content_block_delta" and getattr(event.delta, "type", "") == "thinking_delta":
                    if self.on_thinking is not None:
                        try:
                            self.on_thinking(event.delta.thinking)
                        except Exception:  # noqa: BLE001 - a display problem must not stop the answer
                            pass
            if not stopped:
                final = stream.get_final_message()
        ended = time.time()
        total = self.usage_total
        total["gen_ms"] += int((ended - (first_at or started)) * 1000)
        if total["ttft_ms"] is None and first_at is not None:
            total["ttft_ms"] = int((first_at - started) * 1000)
        if final is None:
            text = "".join(text_parts)
            return {"choices": [{"message": {"role": "assistant", "content": text or None, "tool_calls": None},
                                 "finish_reason": "stop"}], "usage": {}}
        return self._reply(final)

    def _reply(self, final: Any) -> Dict:
        texts: List[str] = []
        calls: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        for block in final.content:
            as_dict = _block_dict(block)
            if as_dict is not None:
                kept.append(as_dict)
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                calls.append({"id": block.id, "type": "function",
                              "function": {"name": block.name, "arguments": json.dumps(block.input)}})
            elif kind == "thinking":
                self.usage_total["reasoning"] += len(block.thinking or "")
        text = "".join(texts)
        stop = getattr(final, "stop_reason", None)
        if stop == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            text = (text + "\n\n" if text else "") + "[Claude declined this request" + \
                   (f" ({category})" if category else "") + ".]"
            calls = []   # a refusal can cut a tool call off part-way: none is run
        message = {"role": "assistant", "content": text or None, "tool_calls": calls or None}
        usage = getattr(final, "usage", None)
        prompt = completion = 0
        if usage is not None:
            prompt = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "cache_read_input_tokens", 0) or 0) \
                + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            completion = int(getattr(usage, "output_tokens", 0) or 0)
            self.usage_total["in"] += prompt
            self.usage_total["out"] += completion
            self.usage_total["reported"] = True
            cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            self.usage_total["cache_n"] = self.usage_total.get("cache_n", 0) + cached
            self.usage_total["last_prompt"] = prompt
        finish = {"max_tokens": "length", "tool_use": "tool_calls"}.get(stop or "", "stop")
        if finish == "length" and calls and getattr(final.content[-1], "type", "") == "tool_use":
            # the answer ran out inside the last call's input: that call is not run
            calls.pop()
            message["tool_calls"] = calls or None
        elif kept and stop != "refusal":
            BLOCKS.remember(message, kept)
        return {"choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
