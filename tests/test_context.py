"""Tests for the context subsystem (context/).

Run from the project root:
    python tests/test_context.py

Uses fake clients only; no network, no API key.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import Config  # noqa: E402
from context import (  # noqa: E402
    ContextManagedClient,
    compact_history,
    context_opts,
    estimate_tokens,
    history_chars,
    wrap_client_if_enabled,
)


class FakeSummarizer:
    """Records summarizer calls; returns canned briefs."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append(messages)
        n = len(self.calls)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"BRIEF-{n}",
                        "tool_calls": [],
                    }
                }
            ]
        }


class FakeInner:
    """Inner client: records what it received, replies with text."""

    def __init__(self):
        self.received = []

    def chat(self, messages, tools):
        self.received.append(list(messages))
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "ok", "tool_calls": []}}
            ]
        }


def make_messages(n_middle=10, size=1000):
    msgs = [{"role": "system", "content": "SYSTEM"}]
    for i in range(n_middle):
        msgs.append({"role": "user", "content": f"old-{i} " + "x" * size})
        msgs.append({"role": "assistant", "content": f"reply-{i} " + "y" * size})
    msgs.append({"role": "user", "content": "latest question"})
    return msgs


def test_estimate_tokens_rough():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100, estimate_tokens("a" * 400)
    assert history_chars([{"role": "user", "content": "abc"}]) == 3
    print("PASS test_estimate_tokens_rough")


def test_compact_noop_when_under_budget():
    msgs = make_messages(n_middle=2, size=100)
    summarizer = FakeSummarizer()
    new_msgs, info = compact_history(msgs, summarizer, max_chars=1_000_000)
    assert new_msgs is msgs, "must return the original list when unchanged"
    assert info["compacted"] is False
    assert summarizer.calls == [], "summarizer must not be called"
    print("PASS test_compact_noop_when_under_budget")


def test_compact_structure():
    msgs = make_messages(n_middle=6, size=2000)
    summarizer = FakeSummarizer()
    new_msgs, info = compact_history(
        msgs, summarizer, max_chars=10_000, keep_recent_chars=2_000
    )
    assert info["compacted"] is True, info
    assert new_msgs[0]["content"] == "SYSTEM", "system prompt must stay first"
    assert "Context compacted" in new_msgs[1]["content"], new_msgs[1]
    assert "BRIEF-1" in new_msgs[1]["content"], new_msgs[1]
    assert new_msgs[-1]["content"] == "latest question", "newest turn kept"
    assert info["dropped_messages"] > 0, info
    assert info["after_chars"] < info["before_chars"], info
    # summarizer saw the old middle, not the recent tail
    sent = summarizer.calls[0][1]["content"]
    assert "old-0" in sent, "middle must reach the summarizer"
    assert "latest question" not in sent, "recent tail must not be summarized"
    print("PASS test_compact_structure")


def test_compact_chunked_map_reduce():
    msgs = make_messages(n_middle=8, size=5000)
    summarizer = FakeSummarizer()
    new_msgs, info = compact_history(
        msgs, summarizer, max_chars=10_000, keep_recent_chars=1_000,
        chunk_chars=5_000,
    )
    assert info["compacted"] is True
    # multiple chunk summaries + one combine call
    assert len(summarizer.calls) > 2, len(summarizer.calls)
    assert "BRIEF-" in new_msgs[1]["content"]
    print("PASS test_compact_chunked_map_reduce")


def test_compact_nothing_compactable():
    # a single giant message: nothing sensible to drop
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "z" * 50_000},
    ]
    summarizer = FakeSummarizer()
    new_msgs, info = compact_history(msgs, summarizer, max_chars=1_000)
    assert info["compacted"] is False
    assert new_msgs is msgs
    assert summarizer.calls == []
    print("PASS test_compact_nothing_compactable")


def test_wrapper_auto_compacts_and_persists():
    inner = FakeInner()
    fired = []
    managed = ContextManagedClient(
        inner, max_chars=10_000, keep_recent_chars=2_000,
        on_compact=fired.append,
    )
    msgs = make_messages(n_middle=6, size=2000)
    resp = managed.chat(msgs, [])
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert len(fired) == 1, "on_compact must fire once"
    # inner client received the compacted form (its last call — the first
    # recorded call is the summarizer invocation, which shares the client)
    got = inner.received[-1]
    assert any("Context compacted" in m.get("content", "") for m in got), got
    # ...and the caller's list was updated in place, so the next turn
    # does NOT re-summarize from scratch.
    assert any("Context compacted" in m.get("content", "") for m in msgs), msgs
    before = len(inner.received)
    managed.chat(msgs, [])
    assert len(inner.received) == before + 1
    print("PASS test_wrapper_auto_compacts_and_persists")


def test_wrapper_passes_through_when_small():
    inner = FakeInner()
    fired = []
    managed = ContextManagedClient(inner, on_compact=fired.append)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    managed.chat(msgs, [])
    assert inner.received[0] == msgs, "small histories must pass through untouched"
    assert fired == []
    print("PASS test_wrapper_passes_through_when_small")


def test_wrap_client_if_enabled():
    inner = FakeInner()
    cfg = Config(context={"enabled": True, "max_chars": 12345})
    wrapped, mgr = wrap_client_if_enabled(cfg, inner)
    assert isinstance(wrapped, ContextManagedClient), type(wrapped)
    assert mgr is wrapped
    assert wrapped._max_chars == 12345

    cfg2 = Config(context={"enabled": False})
    wrapped2, mgr2 = wrap_client_if_enabled(cfg2, inner)
    assert wrapped2 is inner and mgr2 is None

    cfg3 = Config()  # default: enabled
    wrapped3, _ = wrap_client_if_enabled(cfg3, inner)
    assert isinstance(wrapped3, ContextManagedClient)
    print("PASS test_wrap_client_if_enabled")


def test_context_opts_defaults():
    opts = context_opts(Config())
    assert opts["enabled"] is True
    assert opts["max_chars"] == 200_000
    assert opts["keep_recent_chars"] == 40_000
    print("PASS test_context_opts_defaults")


def test_compact_now_manual():
    inner = FakeInner()
    managed = ContextManagedClient(inner, max_chars=10_000, keep_recent_chars=2_000)
    msgs = make_messages(n_middle=6, size=2000)
    new_msgs, info = managed.compact_now(msgs)
    assert info["compacted"] is True
    assert info["before_chars"] > info["after_chars"]
    # manual call does not mutate the input list (caller decides)
    assert len(msgs) > len(new_msgs)
    assert managed.compactions == 1
    print("PASS test_compact_now_manual")


def main():
    tests = [
        test_estimate_tokens_rough,
        test_compact_noop_when_under_budget,
        test_compact_structure,
        test_compact_chunked_map_reduce,
        test_compact_nothing_compactable,
        test_wrapper_auto_compacts_and_persists,
        test_wrapper_passes_through_when_small,
        test_wrap_client_if_enabled,
        test_context_opts_defaults,
        test_compact_now_manual,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
