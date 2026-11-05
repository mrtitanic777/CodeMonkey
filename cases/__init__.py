"""Case-based reasoning: the learning flywheel.

Every completed task is recorded as a *case* (task, plan summary, tool
trace, outcome) in a local JSONL log. On a new task, the most similar past
*successful* cases are retrieved and rendered as few-shot examples, so the
agent gets smarter the more it is used. All data stays on this machine.

Pieces:
- ``store``: :class:`CaseStore` — JSONL persistence + keyword retrieval.
- ``recorder``: :class:`TraceRecorder` — records tool calls via the same
  instance-wrapping style as the coding loop's tracing.
- ``recall``: :func:`format_cases` — renders cases as a compact few-shot
  block for the prompt.

Wiring is a future pass; this package is
additive and touches no existing code.
"""

from .recorder import TraceRecorder, distill_pitfalls, distill_strategies
from .recall import format_cases
from .store import CaseStore

__all__ = [
    "CaseStore",
    "TraceRecorder",
    "format_cases",
    "distill_strategies",
    "distill_pitfalls",
]
