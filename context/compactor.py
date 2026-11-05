"""Summarize-then-continue compaction for long agent sessions.

When a message history exceeds its budget, the oldest middle section is
summarized into a dense structured brief and the session continues as::

    [system prompt, summary brief, most recent turns...]

The summary is produced by the model itself (via any ``ModelClient``),
map-reduce style: the middle is split into chunks, each chunk is
summarized, and the partial summaries are combined. One chunk in the
common case means exactly one extra model call per compaction.

``ContextManagedClient`` wraps any ``ModelClient`` and compacts
automatically on every ``chat()`` call that would exceed the budget, so the
agent loop needs no changes at all.
"""

from __future__ import annotations

import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .budget import history_chars, message_chars

SUMMARIZER_SYSTEM = (
    "You are a conversation summarizer for an AI assistant. Produce a dense, "
    "structured brief of the conversation below so the assistant can continue "
    "its work with full context. Use exactly these sections:\n"
    "## Goal\n## Key decisions and facts\n## Files touched and outcomes\n"
    "## Current state\n## Open items and todos\n"
    "Be dense and specific: names, file paths, numbers, exact outcomes. "
    "Omit greetings and chit-chat. If a section has nothing, write 'none'."
)

SUMMARY_PREFIX = (
    "[Context compacted {date}: the brief below summarizes earlier "
    "conversation. It is not a new user message — continue the work.]"
)


def _render_message(message: Dict) -> str:
    role = message.get("role", "?")
    content = message.get("content") or ""
    if not content:
        calls = message.get("tool_calls") or []
        if calls:
            names = ", ".join(
                c.get("function", {}).get("name", "?") for c in calls
            )
            content = f"(called tools: {names})"
    text = str(content).strip()
    return f"[{role}]\n{text}" if text else ""


def _summarize_text(client, text: str, note: str = "") -> str:
    """One summarizer call; returns the brief text (never raises)."""
    prompt = text if not note else f"{note}\n\n{text}"
    try:
        response = client.chat(
            [
                {"role": "system", "content": SUMMARIZER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            [],
        )
        content = response["choices"][0]["message"].get("content") or ""
        return content.strip() or "(summarizer returned empty text)"
    except Exception as exc:  # noqa: BLE001 - compaction must not kill a session
        return f"(summarization failed: {type(exc).__name__}: {exc})"


def summarize_history(
    client, messages: List[Dict], chunk_chars: int = 60_000
) -> str:
    """Summarize *messages* into a dense brief (map-reduce over chunks)."""
    rendered = [_render_message(m) for m in messages]
    rendered = [r for r in rendered if r]
    if not rendered:
        return "(nothing to summarize)"
    chunks: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    for r in rendered:
        if current and current_chars + len(r) > chunk_chars:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(r)
        current_chars += len(r)
    if current:
        chunks.append(current)
    if len(chunks) == 1:
        return _summarize_text(client, "\n\n".join(chunks[0]))
    partials = [
        _summarize_text(client, "\n\n".join(c), note=f"[Part {i} of {len(chunks)}]")
        for i, c in enumerate(chunks, 1)
    ]
    combined = "\n\n".join(
        f"[Summary part {i}]\n{p}" for i, p in enumerate(partials, 1)
    )
    return _summarize_text(
        client, combined, note="[Combine the partial summaries below into one brief.]"
    )


def compact_history(
    messages: List[Dict],
    client,
    max_chars: int = 200_000,
    keep_recent_chars: int = 40_000,
    chunk_chars: int = 60_000,
) -> Tuple[List[Dict], Dict]:
    """Compact *messages* if over budget.

    Returns ``(new_messages, info)``. When no compaction was needed,
    ``new_messages`` is the original list and ``info["compacted"]`` is
    False. Otherwise the result is ``[system, summary, *recent]``.
    """
    info: Dict = {
        "compacted": False,
        "before_chars": history_chars(messages),
        "after_chars": history_chars(messages),
        "dropped_messages": 0,
        "summary_chars": 0,
    }
    if not messages or info["before_chars"] <= max_chars:
        return messages, info
    system, rest = messages[0], messages[1:]

    # Walk from the newest message, keeping the recent tail verbatim.
    recent: List[Dict] = []
    budget = keep_recent_chars
    for m in reversed(rest):
        size = message_chars(m)
        if recent and budget - size < 0:
            break
        recent.append(m)
        budget -= size
    recent.reverse()
    middle = rest[: len(rest) - len(recent)] if recent else rest

    if not middle:
        # Nothing compactable (e.g. one giant message): leave it alone.
        return messages, info

    summary = summarize_history(client, middle, chunk_chars=chunk_chars)
    date = datetime.date.today().isoformat()
    summary_msg = {
        "role": "user",
        "content": SUMMARY_PREFIX.format(date=date) + "\n\n" + summary,
    }
    new_messages = [system, summary_msg] + recent
    info.update(
        compacted=True,
        after_chars=history_chars(new_messages),
        dropped_messages=len(middle),
        summary_chars=len(summary_msg["content"]),
    )
    return new_messages, info


class ContextManagedClient:
    """A ModelClient decorator that auto-compacts over-budget histories.

    Wraps any client (OpenAI-compatible, KeSSie, fake). Every ``chat()``
    checks the outgoing history against the budget and compacts first when
    needed. ``on_compact``, when given, is called with the info dict after
    each automatic compaction (useful for UI events / logging).
    """

    def __init__(
        self,
        wrapped,
        max_chars: int = 200_000,
        keep_recent_chars: int = 40_000,
        chunk_chars: int = 60_000,
        on_compact: Optional[Callable[[Dict], None]] = None,
    ):
        self._wrapped = wrapped
        self._max_chars = max_chars
        self._keep_recent_chars = keep_recent_chars
        self._chunk_chars = chunk_chars
        self._on_compact = on_compact
        self.compactions = 0

    def compact_now(self, messages: List[Dict]) -> Tuple[List[Dict], Dict]:
        """Manually compact *messages* (for /compact commands)."""
        new_messages, info = compact_history(
            messages,
            self._wrapped,
            max_chars=self._max_chars,
            keep_recent_chars=self._keep_recent_chars,
            chunk_chars=self._chunk_chars,
        )
        if info["compacted"]:
            self.compactions += 1
            if self._on_compact:
                self._on_compact(info)
        return new_messages, info

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        new_messages, info = self.compact_now(messages)
        if info["compacted"]:
            # Update in place: the loop keeps appending to this list, so the
            # compacted form must persist across iterations. Otherwise every
            # turn would re-summarize the same history.
            messages[:] = new_messages
        return self._wrapped.chat(messages, tools)

    def __getattr__(self, name: str):
        # Pass through anything else (base_url, conversation_id, ...).
        return getattr(self._wrapped, name)


def context_opts(config) -> Dict:
    """Read the ``context:`` config mapping with defaults."""
    opts = getattr(config, "context", None) or {}
    if not isinstance(opts, dict):
        opts = {}
    return {
        "enabled": bool(opts.get("enabled", True)),
        "max_chars": int(opts.get("max_chars", 200_000)),
        "keep_recent_chars": int(opts.get("keep_recent_chars", 40_000)),
        "chunk_chars": int(opts.get("chunk_chars", 60_000)),
    }


def wrap_client_if_enabled(config, client, on_compact=None):
    """Wrap *client* in a ContextManagedClient when context is enabled.

    Returns ``(client_to_use, manager_or_None)``. The manager is also the
    client when wrapping happened, so ``/compact`` can call
    ``manager.compact_now(history)`` on it.
    """
    opts = context_opts(config)
    if not opts["enabled"]:
        return client, None
    managed = ContextManagedClient(
        client,
        max_chars=opts["max_chars"],
        keep_recent_chars=opts["keep_recent_chars"],
        chunk_chars=opts["chunk_chars"],
        on_compact=on_compact,
    )
    return managed, managed
