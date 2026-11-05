"""Cheap context budgeting.

Token counting without a tokenizer: the ``chars / 4`` heuristic is rough
(usually within 2x for English text and code) but free, dependency-free,
and good enough for budgeting decisions. It is deliberately conservative
only in one direction: we never claim precision.
"""

from __future__ import annotations

from typing import Dict, List


def estimate_tokens(text: str) -> int:
    """Rough token estimate for *text* (chars // 4 heuristic)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def message_chars(message: Dict) -> int:
    """Character size of a message dict (content only)."""
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if content is None:
        # content-less tool-call messages still cost a little
        return 200
    return len(str(content))


def history_chars(messages: List[Dict]) -> int:
    """Total characters across a message history."""
    return sum(message_chars(m) for m in messages)


def history_tokens(messages: List[Dict]) -> int:
    """Rough token estimate across a message history."""
    return estimate_tokens(" ".join(
        str(m.get("content") or "") for m in messages
    ))
