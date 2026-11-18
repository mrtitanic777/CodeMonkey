"""Speculative execution engine: prefetch safe reads, serve on exact match.

EXPERIMENTAL and off by default — see :mod:`speculation` and
this module's docstrings for the design, the safety argument, and
the wiring discussion.

How it works: after each real tool call completes, an optional fast
predictor model proposes up to ``max_prefetch`` likely *next* safe
(read/glob/grep) actions. Proposals that pass
:func:`speculation.policy.is_speculatable` are pre-executed immediately
and cached keyed by exact (tool, normalized-args). When the main model
issues a call matching a cache key exactly, the cached result is served
(a verified hit). Any mismatch discards the whole cache and execution
proceeds normally; any state-mutating tool on the main path invalidates
the cache too.

The safety core is exact-match verification plus a prefetch path that can
never mutate: prefetch calls ``tool.func`` directly and only for tools
the policy allowlists, so speculative guesses never fire hooks, never hit
the approval gate, and never enter the case trace.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from tools.registry import ToolRegistry

from speculation.policy import SAFE_TOOLS, is_speculatable

PREFETCH_SYSTEM = (
    "You are a prefetch predictor for a read-only speculative executor "
    "inside an AI coding agent. After each tool call, you guess the agent's "
    "most likely NEXT tool calls so their results can be pre-computed while "
    "the agent thinks.\n"
    "Rules:\n"
    "- Propose AT MOST {max_prefetch} calls.\n"
    "- Allowed tools ONLY: read (args: path, offset, limit), glob (args: "
    "pattern), grep (args: pattern, path, context). Never propose any "
    "other tool.\n"
    "- Paths must be project-relative: no leading '/', no '..' escapes, "
    "no '~'.\n"
    "- Keep reads small (limit <= 200) and grep context <= 3.\n"
    "- Output ONLY a JSON array, no markdown fences, no explanation. "
    "Example: [{{\"tool\": \"read\", \"args\": {{\"path\": \"src/app.py\", "
    "\"limit\": 80}}}}]\n"
    "- If you cannot predict the next calls with reasonable confidence, "
    "output exactly: []"
)

_RESULT_FOR_PREDICTOR_CHARS = 4000


def _cache_key(name: str, arguments: Any) -> str:
    """Canonical cache key: exact (tool, normalized-args) match.

    Arguments are normalized through a JSON round-trip with sorted keys,
    so ``{"b": 1, "a": 2}`` and ``{"a": 2, "b": 1}`` share a key — but
    ``{"limit": 80}`` and ``{"limit": "80"}`` do not. Exactness is the
    safety core: a near-match is a miss, never a hit.
    """
    try:
        normalized = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        normalized = str(arguments)
    return f"{name}::{normalized}"


def _parse_proposals(text: str, max_prefetch: int) -> List[Dict[str, Any]]:
    """Strictly parse the predictor's JSON array. Fail-open: [] on doubt."""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Strip a markdown fence if the model added one anyway.
            lines = cleaned.splitlines()
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()
        proposals = json.loads(cleaned)
        if not isinstance(proposals, list):
            return []
        valid = []
        for item in proposals[:max_prefetch]:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool")
            args = item.get("args")
            if isinstance(tool_name, str) and isinstance(args, dict):
                valid.append({"tool": tool_name, "args": args})
        return valid
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


def propose_actions(client: Any, last_name: str, last_args: Any,
                    last_result: Any,
                    max_prefetch: int) -> List[Dict[str, Any]]:
    """Ask the fast client for likely next safe tool calls.

    Fail-open by contract: any client error, empty reply, or unparseable
    output yields ``[]`` and the agent loop is never affected.
    """
    if client is None or max_prefetch <= 0:
        return []
    try:
        args_json = json.dumps(last_args, sort_keys=True, default=str)[:2000]
        result_text = "" if last_result is None else str(last_result)
        if len(result_text) > _RESULT_FOR_PREDICTOR_CHARS:
            result_text = (
                result_text[:_RESULT_FOR_PREDICTOR_CHARS] + "\n[... truncated]"
            )
        response = client.chat(
            [
                {
                    "role": "system",
                    "content": PREFETCH_SYSTEM.format(
                        max_prefetch=max_prefetch
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Last tool call: {last_name}({args_json})\n"
                        f"Result:\n{result_text}"
                    ),
                },
            ],
            [],
        )
        content = response["choices"][0]["message"].get("content") or ""
        return _parse_proposals(content, max_prefetch)
    except Exception:
        return []


class SpeculativeRegistry(ToolRegistry):
    """A ToolRegistry that speculatively pre-executes safe read actions.

    Build via :func:`wrap_registry`; never modify ``tools/registry.py``
    for this. The main path *delegates* to the wrapped registry untouched:
    when disabled (the default) or on a cache miss, behavior is
    byte-identical to the unwrapped stack. Prefetch calls ``tool.func``
    directly — never through any wrapper's ``execute`` — so guesses never
    fire hooks, never prompt for approval, and never enter the case trace.

    Intended stack position is OUTERMOST (see
    the policy module for the argument): the case recorder
    attaches to the outermost registry, so verified hits are recorded as
    real model actions while prefetches stay invisible.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        fast_client: Any = None,
        enabled: bool = False,
        max_prefetch: int = 3,
        project_dir: Optional[str] = None,
        cache_max_entries: int = 64,
    ):
        super().__init__(auto_approve=registry.auto_approve)
        # Copied so schemas()/names() keep working on the wrapper; the
        # main path delegates execution to the wrapped registry.
        self.tools = dict(registry.tools)
        self.approver = registry.approver
        self._inner = registry
        self.fast_client = fast_client
        self.enabled = bool(enabled)
        self.max_prefetch = max(0, int(max_prefetch))
        self.project_dir = project_dir
        self.cache_max_entries = max(1, int(cache_max_entries))
        self._cache: Dict[str, str] = {}
        # The agent loop may run several read-only calls through this layer
        # at once (agent.py's parallel batch): consume-once pops, the miss
        # invalidate and the stats counters all need serialising.
        self._lock = threading.Lock()
        # tools/registry.py's parallel_safe honours this: a cache hit skips
        # every inner layer (no hooks, no approval gate, nothing recorded),
        # so until its correctness under concurrent calls is proven, calls
        # that may hit the prefetch cache run strictly one at a time.
        self.PARALLEL_NEVER = SAFE_TOOLS
        self._stats = {
            "hits": 0,
            "misses": 0,
            "discarded": 0,
            "prefetch_calls": 0,
        }

    @classmethod
    def from_config(
        cls,
        registry: ToolRegistry,
        config: Dict[str, Any],
        fast_client: Any = None,
    ) -> "SpeculativeRegistry":
        """Build from a config dict's ``speculation`` section (off default)."""
        spec_cfg = config.get("speculation", {}) or {}
        return cls(
            registry,
            fast_client=fast_client,
            enabled=bool(spec_cfg.get("enabled", False)),
            max_prefetch=int(spec_cfg.get("max_prefetch", 3)),
            project_dir=spec_cfg.get("project_dir"),
        )

    @property
    def stats(self) -> Dict[str, int]:
        """A copy of the counters: hits, misses, discarded, prefetch_calls."""
        return dict(self._stats)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def reset(self) -> None:
        """Clear the prefetch cache and zero the stats (for tests)."""
        self._cache.clear()
        for key in self._stats:
            self._stats[key] = 0

    # ---------------------------------------------------------- execute ---

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """Run a tool, serving an exact-match prefetched result on a hit."""
        if self.enabled:
            key = _cache_key(name, arguments)
            # The pop and the miss-invalidate are one decision under the
            # lock: two concurrent calls must never both serve one cached
            # result, and a miss must not discard a hit taken a moment ago.
            # (PARALLEL_NEVER keeps read/glob/grep sequential today; the
            # lock is the guarantee for whenever that is relaxed.)
            with self._lock:
                if key in self._cache:
                    result = self._cache.pop(key)  # consume-once: never serve twice
                    self._stats["hits"] += 1
                    hit = True
                else:
                    # A miss means the model's actual next action diverged
                    # from every prediction: the lookahead is stale, discard.
                    self._invalidate("prediction miss")
                    self._stats["misses"] += 1
                    hit = False
            if hit:
                self._prefetch_after(name, arguments, result)
                return result
        result = self._inner.execute(name, arguments)
        if self.enabled:
            if name not in SAFE_TOOLS:
                # State may have changed under the prefetched reads.
                self._invalidate("state-mutating tool executed")
            self._prefetch_after(name, arguments, result)
        return result

    # --------------------------------------------------------- prefetch ---

    def _invalidate(self, _reason: str) -> None:
        """Drop all prefetched results, counting them as discarded."""
        if self._cache:
            self._stats["discarded"] += len(self._cache)
            self._cache.clear()

    def _prefetch_after(
        self, name: str, arguments: Any, result: Any
    ) -> None:
        """Ask the predictor for next actions and pre-execute the safe ones."""
        if (
            not self.enabled
            or self.fast_client is None
            or self.max_prefetch <= 0
        ):
            return
        try:
            proposals = propose_actions(
                self.fast_client, name, arguments, result, self.max_prefetch
            )
        except Exception:
            return  # fail-open: predictor failure never breaks the loop
        for proposal in proposals:
            self._prefetch_one(proposal)

    def _prefetch_one(self, proposal: Any) -> None:
        """Validate and pre-execute one proposal. Fail-open throughout."""
        if not isinstance(proposal, dict):
            self._stats["discarded"] += 1
            return
        tool_name = proposal.get("tool")
        args = proposal.get("args")
        if not isinstance(tool_name, str) or not isinstance(args, dict):
            self._stats["discarded"] += 1
            return
        tool = self.tools.get(tool_name)
        if tool is None or not is_speculatable(
            tool_name, args, self.project_dir, tool
        ):
            self._stats["discarded"] += 1
            return
        key = _cache_key(tool_name, args)
        if key in self._cache or len(self._cache) >= self.cache_max_entries:
            self._stats["discarded"] += 1
            return
        try:
            # Direct func call: prefetch never passes through any
            # wrapper's execute, so no hooks fire, no approval prompts,
            # and nothing is recorded as a model action.
            raw = tool.func(**args)
        except Exception:
            self._stats["discarded"] += 1
            return
        text = "" if raw is None else str(raw)
        if text.startswith("ERROR"):
            # Don't cache failures: the model's live call deserves the
            # live error, which may differ (e.g. a file created since).
            self._stats["discarded"] += 1
            return
        self._cache[key] = text
        self._stats["prefetch_calls"] += 1


def wrap_registry(
    registry: ToolRegistry,
    fast_client: Any = None,
    enabled: bool = False,
    max_prefetch: int = 3,
    project_dir: Optional[str] = None,
) -> ToolRegistry:
    """Wrap *registry* with speculative prefetching (off by default).

    Pure wrapper: the original registry is untouched, and with
    ``enabled=False`` (the default) every call delegates straight through
    with zero behavior change and zero predictor cost.
    """
    return SpeculativeRegistry(
        registry,
        fast_client=fast_client,
        enabled=enabled,
        max_prefetch=max_prefetch,
        project_dir=project_dir,
    )
