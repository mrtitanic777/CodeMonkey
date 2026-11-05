"""Task-level speculative execution (EXPERIMENTAL, off by default).

After each tool call completes, a fast model predicts the agent's likely
next *safe* (read-only) actions, pre-executes them, and serves the cached
result on an exact match next turn. Latency hiding for the read-heavy
explore phase.

Safety posture, in one paragraph: speculation is read-only prefetch with
exact-match verification. It cannot change what the agent does, only how
fast safe reads return. The prefetch path can never reach a mutating tool
(policy allowlist + per-tool arg allowlist + project containment +
needs_approval refusal), a cached result is served only for a
byte-identical (tool, normalized-args) call, any mismatch or any
state-mutating tool discards the cache, and the predictor failing —
or being absent — degrades to normal execution. Off by default, clearly
marked EXPERIMENTAL.
"""

from speculation.engine import (
    PREFETCH_SYSTEM,
    SpeculativeRegistry,
    _cache_key,
    propose_actions,
    wrap_registry,
)
from speculation.policy import SAFE_TOOLS, is_speculatable

__all__ = [
    "SAFE_TOOLS",
    "PREFETCH_SYSTEM",
    "SpeculativeRegistry",
    "is_speculatable",
    "propose_actions",
    "wrap_registry",
    "_cache_key",
]
