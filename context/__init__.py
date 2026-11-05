"""Context subsystem: budgeting, compaction, and context-aware clients.

Long agent sessions die from context rot, not model stupidity: the prompt
fills with stale turns and the agent starts making confused edits. This
package is the fix:

- ``budget``: cheap token/char estimation and history measurement.
- ``compactor``: summarize-then-continue. When history exceeds a budget,
  the oldest middle is summarized (map-reduce over chunks) into a dense
  brief and the session continues with [system, brief, recent turns].

The main entry point is ``ContextManagedClient``, a ``ModelClient``
decorator: wrap any client (OpenAI-compatible, KeSSie, fake) and every
``chat()`` call is automatically compacted when over budget. Because it is
a wrapper, it composes with everything and requires zero changes to the
agent loop.
"""

from .budget import estimate_tokens, history_chars
from .compactor import (
    ContextManagedClient,
    compact_history,
    context_opts,
    summarize_history,
    wrap_client_if_enabled,
)

__all__ = [
    "estimate_tokens",
    "history_chars",
    "ContextManagedClient",
    "compact_history",
    "context_opts",
    "summarize_history",
    "wrap_client_if_enabled",
]
