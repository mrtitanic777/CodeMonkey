"""Tests for task-level speculative execution (speculation/).

Run from the project root:
    python tests/test_speculation.py

Uses fake tools and a fake predictor client; no network, no API key.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from speculation import (  # noqa: E402
    SAFE_TOOLS,
    is_speculatable,
    propose_actions,
    wrap_registry,
)
from tools.registry import Tool, ToolRegistry  # noqa: E402


# ------------------------------------------------------------- fixtures ---


def make_registry(error_on_read=False):
    calls = {"read": 0, "glob": 0, "grep": 0, "write_file": 0, "bash": 0}

    def read(path, offset=1, limit=200):
        calls["read"] += 1
        if error_on_read:
            return "ERROR: file not found"
        return f"content of {path} [{offset}:{limit}]"

    def glob(pattern):
        calls["glob"] += 1
        return f"matches for {pattern}"

    def grep(pattern, path=".", context=0):
        calls["grep"] += 1
        return f"hits for {pattern} in {path}"

    def write_file(path, content):
        calls["write_file"] += 1
        return f"wrote {path}"

    def bash(command):
        calls["bash"] += 1
        return "done"

    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(name="read", description="d",
                      parameters={}, func=read))
    reg.register(Tool(name="glob", description="d",
                      parameters={}, func=glob))
    reg.register(Tool(name="grep", description="d",
                      parameters={}, func=grep))
    reg.register(Tool(name="write_file", description="d", parameters={},
                      func=write_file, needs_approval=True))
    reg.register(Tool(name="bash", description="d", parameters={},
                      func=bash, needs_approval=True))
    return reg, calls


class FakePredictor:
    """Scripted fast client: chat() returns canned JSON proposals."""

    def __init__(self, proposals=None, explode=False, raw=None):
        self._proposals = proposals if proposals is not None else []
        self._explode = explode
        self._raw = raw
        self.calls = 0
        self.last_messages = None

    def chat(self, messages, tools):
        self.calls += 1
        self.last_messages = messages
        if self._explode:
            raise RuntimeError("predictor is down")
        content = self._raw if self._raw is not None else json.dumps(
            self._proposals
        )
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [],
                }
            }]
        }


def read_proposal(path, **kw):
    args = {"path": path}
    args.update(kw)
    return {"tool": "read", "args": args}


class ScriptedPredictor(FakePredictor):
    """Predictor returning a different proposal list per turn."""

    def __init__(self, scripts):
        super().__init__(proposals=[])
        self._scripts = list(scripts)

    def chat(self, messages, tools):
        self.calls += 1
        self.last_messages = messages
        proposals = self._scripts.pop(0) if self._scripts else []
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps(proposals),
                    "tool_calls": [],
                }
            }]
        }


# ---------------------------------------------------------------- tests ---


def test_disabled_by_default_does_zero_prefetching():
    reg, calls = make_registry()
    predictor = FakePredictor(proposals=[read_proposal("a.py")])
    wrapped = wrap_registry(reg, fast_client=predictor)  # enabled=False default
    assert wrapped.enabled is False
    out = wrapped.execute("read", {"path": "a.py"})
    assert out == "content of a.py [1:200]", out
    assert predictor.calls == 0, "disabled must never consult the predictor"
    assert calls["read"] == 1
    assert wrapped.stats == {
        "hits": 0, "misses": 0, "discarded": 0, "prefetch_calls": 0,
    }, wrapped.stats
    print("PASS test_disabled_by_default_does_zero_prefetching")


def test_exact_match_hit_serves_cached_result():
    reg, calls = make_registry()
    predictor = ScriptedPredictor([
        [read_proposal("a.py")],  # after glob: prefetch read a.py
        [],                        # after the hit: predict nothing
    ])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})  # triggers prefetch
    assert predictor.calls == 1
    assert wrapped.stats["prefetch_calls"] == 1, wrapped.stats
    assert calls["read"] == 1, "prefetch executed the read once"
    out = wrapped.execute("read", {"path": "a.py"})  # exact match
    assert out == "content of a.py [1:200]", out
    assert calls["read"] == 1, "hit must not re-execute the tool"
    stats = wrapped.stats
    assert stats["hits"] == 1 and stats["misses"] == 1, stats
    print("PASS test_exact_match_hit_serves_cached_result")


def test_mismatch_discards_cache_and_executes_normally():
    reg, calls = make_registry()
    predictor = ScriptedPredictor([
        [read_proposal("a.py")],  # after glob: prefetch read a.py
        [],                        # after the miss: predict nothing
        [],                        # after the later a.py call
    ])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})  # prefetch read a.py
    assert wrapped.cache_size == 1 and calls["read"] == 1
    out = wrapped.execute("read", {"path": "b.py"})  # mismatch
    assert out == "content of b.py [1:200]", out
    assert calls["read"] == 2, "b.py ran live exactly once"
    assert wrapped.cache_size == 0, "stale a.py lookahead must be discarded"
    stats = wrapped.stats
    assert stats["hits"] == 0 and stats["misses"] == 2, stats
    assert stats["discarded"] == 1, stats  # the stale a.py entry
    # a.py is gone for good: a later a.py call runs live, never stale
    assert wrapped.execute("read", {"path": "a.py"}) == \
        "content of a.py [1:200]"
    assert calls["read"] == 3
    print("PASS test_mismatch_discards_cache_and_executes_normally")


def test_exact_args_required_for_hit():
    reg, calls = make_registry()
    predictor = ScriptedPredictor([
        [read_proposal("a.py", limit=80)],  # prefetch with limit=80
        [],                                 # after the miss
    ])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})
    out = wrapped.execute("read", {"path": "a.py"})  # no limit: near-match
    assert out == "content of a.py [1:200]", out
    assert calls["read"] == 2, "near-match is a miss, executed live"
    assert wrapped.stats["hits"] == 0, wrapped.stats
    print("PASS test_exact_args_required_for_hit")


def test_unsafe_proposals_never_prefetched():
    reg, calls = make_registry()
    predictor = FakePredictor(proposals=[
        {"tool": "write_file",
         "args": {"path": "x.py", "content": "evil"}},
        {"tool": "bash", "args": {"command": "rm -rf /"}},
        {"tool": "delegate", "args": {"task": "pwn"}},
        {"tool": "edit", "args": {"path": "x.py"}},
        read_proposal("safe.py"),
    ])
    wrapped = wrap_registry(
        reg, fast_client=predictor, enabled=True, max_prefetch=5
    )
    wrapped.execute("glob", {"pattern": "*.py"})
    assert calls["write_file"] == 0 and calls["bash"] == 0
    assert wrapped.stats["prefetch_calls"] == 1, wrapped.stats
    assert wrapped.stats["discarded"] == 4, wrapped.stats
    assert wrapped.cache_size == 1, "only the safe read is cached"
    print("PASS test_unsafe_proposals_never_prefetched")


def test_path_escape_proposals_rejected():
    reg, calls = make_registry()
    predictor = FakePredictor(proposals=[
        read_proposal("../../etc/passwd"),
        read_proposal("/etc/passwd"),
        read_proposal("~/secret"),
        {"tool": "grep",
         "args": {"pattern": "x", "path": ".."}},
    ])
    wrapped = wrap_registry(
        reg, fast_client=predictor, enabled=True, max_prefetch=4,
        project_dir="/proj",
    )
    wrapped.execute("glob", {"pattern": "*.py"})
    assert calls["read"] == 0 and calls["grep"] == 0, \
        "no escaping proposal may execute"
    assert wrapped.stats["prefetch_calls"] == 0, wrapped.stats
    assert wrapped.stats["discarded"] == 4, wrapped.stats
    print("PASS test_path_escape_proposals_rejected")


def test_write_invalidates_cache():
    reg, calls = make_registry()
    predictor = ScriptedPredictor([
        [read_proposal("a.py")],  # after glob: prefetch read a.py
        [],                        # after the write: predict nothing
        [],                        # after the later read
    ])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})  # prefetch read a.py
    assert wrapped.cache_size == 1 and calls["read"] == 1
    wrapped.execute("write_file", {"path": "a.py", "content": "new"})
    assert wrapped.cache_size == 0, "mutation must invalidate prefetches"
    assert wrapped.stats["discarded"] == 1, wrapped.stats
    out = wrapped.execute("read", {"path": "a.py"})
    assert out == "content of a.py [1:200]"
    assert calls["read"] == 2, "post-write read executes live (fresh)"
    print("PASS test_write_invalidates_cache")


def test_error_results_not_cached():
    reg, calls = make_registry(error_on_read=True)
    predictor = ScriptedPredictor([
        [read_proposal("a.py")],  # prefetch attempt fails with ERROR
        [],                        # after the live read
    ])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})
    assert wrapped.stats["prefetch_calls"] == 0, wrapped.stats
    assert wrapped.stats["discarded"] == 1, wrapped.stats
    assert wrapped.cache_size == 0, "errors must not be cached"
    out = wrapped.execute("read", {"path": "a.py"})
    assert out.startswith("ERROR")
    assert calls["read"] == 2, "failed prefetch + live call, never served"
    print("PASS test_error_results_not_cached")


def test_predictor_failure_is_fail_open():
    reg, calls = make_registry()
    predictor = FakePredictor(explode=True)
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    out = wrapped.execute("read", {"path": "a.py"})
    assert out == "content of a.py [1:200]", out
    assert calls["read"] == 1
    # garbage replies are equally harmless
    predictor2 = FakePredictor(raw="not json at all {{{")
    wrapped2 = wrap_registry(reg, fast_client=predictor2, enabled=True)
    assert wrapped2.execute("read", {"path": "a.py"}) == \
        "content of a.py [1:200]"
    predictor3 = FakePredictor(raw='{"tool": "read"}')  # not a list
    wrapped3 = wrap_registry(reg, fast_client=predictor3, enabled=True)
    assert wrapped3.execute("glob", {"pattern": "x"}) == "matches for x"
    print("PASS test_predictor_failure_is_fail_open")


def test_needs_approval_tool_never_speculatable():
    reg, _ = make_registry()
    gated = Tool(name="read", description="d", parameters={},
                 func=lambda path: "x", needs_approval=True)
    assert is_speculatable("read", {"path": "a.py"}, tool=gated) is False
    # engine-level: a needs_approval "read" in the tools dict is refused
    reg2 = ToolRegistry(auto_approve=True)
    reg2.register(gated)
    predictor = FakePredictor(proposals=[read_proposal("a.py")])
    wrapped = wrap_registry(reg2, fast_client=predictor, enabled=True)
    # drive prefetch through the safe trigger tool
    reg2.register(Tool(name="glob", description="d", parameters={},
                       func=lambda pattern: "m"))
    wrapped.execute("glob", {"pattern": "x"})
    assert wrapped.stats["prefetch_calls"] == 0, wrapped.stats
    assert wrapped.cache_size == 0
    print("PASS test_needs_approval_tool_never_speculatable")


def test_cache_key_normalization():
    from speculation import _cache_key
    a = _cache_key("read", {"path": "a.py", "limit": 80})
    b = _cache_key("read", {"limit": 80, "path": "a.py"})
    c = _cache_key("read", {"path": "a.py", "limit": "80"})
    assert a == b, "key order must not matter"
    assert a != c, "80 vs '80' must differ: exactness is the safety core"
    print("PASS test_cache_key_normalization")


def test_stats_reset():
    reg, _ = make_registry()
    predictor = FakePredictor(proposals=[read_proposal("a.py")])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})
    wrapped.execute("read", {"path": "a.py"})
    assert wrapped.stats["hits"] == 1
    wrapped.reset()
    assert wrapped.stats == {
        "hits": 0, "misses": 0, "discarded": 0, "prefetch_calls": 0,
    }
    assert wrapped.cache_size == 0
    print("PASS test_stats_reset")


def test_original_registry_untouched():
    reg, calls = make_registry()
    predictor = FakePredictor(proposals=[read_proposal("a.py")])
    wrapped = wrap_registry(reg, fast_client=predictor, enabled=True)
    wrapped.execute("glob", {"pattern": "*.py"})
    assert calls["glob"] == 1, "main path delegates to the inner registry"
    assert calls["read"] == 1, "prefetch ran once"
    # the inner registry itself never prefetches
    predictor.calls = 0
    assert reg.execute("read", {"path": "z.py"}) == "content of z.py [1:200]"
    assert predictor.calls == 0
    print("PASS test_original_registry_untouched")


def test_propose_actions_parsing():
    predictor = FakePredictor(raw="""```json
[{"tool": "read", "args": {"path": "a.py"}}]
```""")
    out = propose_actions(predictor, "glob", {"pattern": "*"}, "res", 3)
    assert out == [{"tool": "read", "args": {"path": "a.py"}}], out
    assert propose_actions(None, "x", {}, "", 3) == []
    assert propose_actions(predictor, "x", {}, "", 0) == []
    print("PASS test_propose_actions_parsing")


def test_end_to_end_two_turns():
    # Turn 1: model globs; predictor (correctly) foresees read of app.py.
    # Turn 2: model reads app.py -> served from cache, no second execution.
    reg, calls = make_registry()
    predictor = ScriptedPredictor([
        [read_proposal("src/app.py", limit=80),
         {"tool": "grep", "args": {"pattern": "main"}}],
        [],  # after the hit: predict nothing
    ])
    wrapped = wrap_registry(
        reg, fast_client=predictor, enabled=True, max_prefetch=2
    )
    assert wrapped.execute("glob", {"pattern": "src/*.py"}) == \
        "matches for src/*.py"
    assert wrapped.stats["prefetch_calls"] == 2, wrapped.stats
    out = wrapped.execute("read", {"path": "src/app.py", "limit": 80})
    assert out == "content of src/app.py [1:80]", out
    assert calls["read"] == 1, "served from cache"
    assert wrapped.stats["hits"] == 1, wrapped.stats
    # predictor saw the last call and result (grounding for next prediction)
    last_user = predictor.last_messages[-1]["content"]
    assert "src/app.py" in last_user and "content of" in last_user
    print("PASS test_end_to_end_two_turns")


# --------------------------------------------------------------- policy ---


def test_policy_safe_tools():
    assert SAFE_TOOLS == {"read", "glob", "grep"}
    assert is_speculatable("read", {"path": "a.py"})
    assert is_speculatable("glob", {"pattern": "**/*.py"})
    assert is_speculatable("grep", {"pattern": "def"})
    assert is_speculatable("grep", {"pattern": "def", "path": "src"})
    for unsafe in ("write_file", "edit", "bash", "delegate", "todo_write"):
        assert is_speculatable(unsafe, {}) is False, unsafe
    assert is_speculatable("nope", {}) is False
    print("PASS test_policy_safe_tools")


def test_policy_arg_allowlist_and_types():
    assert is_speculatable("read", {"path": "a.py", "bogus": 1}) is False
    assert is_speculatable("read", {"path": "a.py", "offset": [1]}) is False
    assert is_speculatable("read", "not-a-dict") is False
    assert is_speculatable("glob", {}) is False, "pattern required"
    assert is_speculatable("read", {"path": "a.py", "offset": 0}) is False
    assert is_speculatable(
        "read", {"path": "a.py", "limit": 999999}) is False
    assert is_speculatable(
        "grep", {"pattern": "x", "context": 999}) is False
    assert is_speculatable("grep", {"pattern": "x", "context": 3}) is True
    assert is_speculatable("read", {"path": ""}) is False
    assert is_speculatable("read", {"path": "a.py", "limit": "80"}) is True, \
        "string ints are tool-acceptable"
    print("PASS test_policy_arg_allowlist_and_types")


def test_policy_project_containment():
    assert is_speculatable("read", {"path": "a/b.py"}, "/proj") is True
    assert is_speculatable("read", {"path": "../x.py"}, "/proj") is False
    assert is_speculatable("read", {"path": "/etc/passwd"}, "/proj") is False
    assert is_speculatable("read", {"path": "sub/../../x"}, "/proj") is False
    assert is_speculatable("grep", {"pattern": "x", "path": ".."},
                           "/proj") is False
    # without a project root: lexical check only
    assert is_speculatable("read", {"path": "a.py"}) is True
    assert is_speculatable("read", {"path": "../a.py"}) is False
    assert is_speculatable("read", {"path": "/a.py"}) is False
    print("PASS test_policy_project_containment")


def main():
    tests = [
        test_disabled_by_default_does_zero_prefetching,
        test_exact_match_hit_serves_cached_result,
        test_mismatch_discards_cache_and_executes_normally,
        test_exact_args_required_for_hit,
        test_unsafe_proposals_never_prefetched,
        test_path_escape_proposals_rejected,
        test_write_invalidates_cache,
        test_error_results_not_cached,
        test_predictor_failure_is_fail_open,
        test_needs_approval_tool_never_speculatable,
        test_cache_key_normalization,
        test_stats_reset,
        test_original_registry_untouched,
        test_propose_actions_parsing,
        test_end_to_end_two_turns,
        test_policy_safe_tools,
        test_policy_arg_allowlist_and_types,
        test_policy_project_containment,
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
