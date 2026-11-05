"""Retrievable tool-output condensation.

Big tool outputs (test logs, grep dumps, build transcripts) are the
fastest way to rot a session's context: the base registry blind-truncates
them at 8000 chars, which keeps the newest bytes and silently drops the
rest. This module replaces blind truncation with *summarize-and-keep*,
governed by a per-tool policy table (adapted from nanitics'
context-management taxonomy — patterns borrowed, all code original):

- **errors** always pass through verbatim, never condensed (nanitics'
  recommended default: surface the failure so the model reformulates);
- **delegation** (subagent) outputs are exempt — the model needs the full
  handoff;
- **bash** output (test/build logs): summarized aggressively, the summary
  template preserves failures, paths, and counts verbatim;
- **grep/glob** output (match dumps): *masked* — first lines as preview
  plus the id pointer, no model call (per the JetBrains finding that
  masking can beat summarizing for list-like outputs);
- **read** output (file contents): head-truncated (the head of a file is
  usually the most informative part);
- everything else falls through to a default summarize policy.

Oversized results are saved verbatim to an :class:`OutputStore` and the
model sees the condensed form plus a note like
``[full output saved as out-3 — ask to recall it with /show out-3]``;
the raw log stays one recall away for the model *and* for the user.

Fail-open everywhere: a summarizer exception falls back to head
truncation (still with the id note), and condensation can never break the
agent loop. Pure wrapper — ``tools/registry.py`` is untouched.

Policies are configurable via the ``policies`` constructor arg (list of
``{"tools": [<fnmatch patterns>], "mode": <mode>, "threshold_chars": N,
"preview_lines": M}``); first match wins. ``threshold_chars`` remains as
a shorthand that overrides the default ("*") policy's threshold.

Wiring (applied by a future pass, not here)::

    from condense import CondensingRegistry, OutputStore, model_summarizer

    store = OutputStore()
    registry = CondensingRegistry(
        registry, store=store,
        summarizer=model_summarizer(model_client),
    )
"""

from __future__ import annotations

import fnmatch
import json
import os
from typing import Callable, Dict, List, Optional

from tools.registry import ToolRegistry

CONDENSE_SYSTEM = (
    "You are a tool-output condenser for an AI coding agent. Summarize the "
    "tool output below in about 15 lines so the agent can act on it. "
    "Preserve: errors and failures with their exact messages, file paths, "
    "line numbers, test names and pass/fail counts, numbers, and the overall "
    "outcome (success or failure). Omit repetitive boilerplate and progress "
    "spam. Plain text, no preamble, no markdown headers."
)

SUMMARY_NOTE = (
    "[full output ({chars:,} chars) saved as {id} — "
    "ask to recall it with /show {id}]"
)

TRUNCATED_NOTE = (
    "[... output truncated ({chars:,} chars total; summarizer unavailable); "
    "full output saved as {id} — ask to recall it with /show {id}]"
)

MASK_NOTE = (
    "[output masked: showing first {preview} of {lines} line(s) "
    "({chars:,} chars total); full output saved as {id} — "
    "ask to recall it with /show {id}]"
)


# ------------------------------------------------------------ policies ---

# Handling modes:
#   "summarize"   model summary + id note (fail-open to truncate)
#   "truncate"    head slice at threshold + truncated note
#   "mask"        first preview_lines as preview + id note, no model call
#   "passthrough" never condensed, returned verbatim
_VALID_MODES = {"summarize", "truncate", "mask", "passthrough"}

# Safety backstop for tool outputs when this registry takes over size
# policy from the inner registry (whose blind truncation is raised to this
# on wrap). Large enough that no sane policy threshold is affected.
_INNER_BACKSTOP = 1_000_000

# Adapted from nanitics' context-management policy taxonomy (Apache-2.0,
# patterns only): errors surface verbatim, delegation is exempt, and each
# tool class gets the handling that fits its output shape. First match
# wins; the "*" entry is the default. threshold_chars=None means "never
# condense on size" (used by passthrough).
DEFAULT_POLICIES: List[Dict] = [
    {"tools": ["delegate"], "mode": "passthrough", "threshold_chars": None},
    {"tools": ["bash"], "mode": "summarize", "threshold_chars": 8000},
    {
        "tools": ["grep", "glob"],
        "mode": "mask",
        "threshold_chars": 4000,
        "preview_lines": 10,
    },
    {"tools": ["read"], "mode": "truncate", "threshold_chars": 12000},
    {"tools": ["*"], "mode": "summarize", "threshold_chars": 8000},
]


def _validate_policies(policies: List[Dict]) -> List[Dict]:
    """Light validation of a user-supplied policy table."""
    checked = []
    for entry in policies:
        entry = dict(entry)
        tools = entry.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError(
                f"policy entry needs a non-empty 'tools' list: {entry!r}"
            )
        mode = entry.get("mode", "summarize")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"policy mode must be one of {sorted(_VALID_MODES)}: "
                f"{entry!r}"
            )
        entry["mode"] = mode
        checked.append(entry)
    return checked


def _policy_for(policies: List[Dict], name: str) -> Dict:
    """First policy whose tools patterns match *name* (fnmatch)."""
    for policy in policies:
        if any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in policy.get("tools", [])
        ):
            return policy
    return {"tools": ["*"], "mode": "summarize", "threshold_chars": 8000}


# ------------------------------------------------------------ store ---


class OutputStore:
    """Per-session store of full raw tool outputs.

    In-memory dict (insertion-ordered) with a total-bytes cap; oldest
    entries are evicted first. Ids are monotonic (``out-1``, ``out-2``, …)
    and never reused, so an id always means the same output. Optionally
    appends every save to a JSONL file for audit/persistence (recall is
    always served from memory).
    """

    def __init__(
        self, max_bytes: int = 5_000_000, file_path: Optional[str] = None
    ):
        self.max_bytes = max_bytes
        self._outputs: Dict[str, str] = {}
        self._total_bytes = 0
        self._counter = 0
        self._file_path = file_path
        if file_path:
            directory = os.path.dirname(os.path.abspath(file_path))
            os.makedirs(directory, exist_ok=True)

    def __len__(self) -> int:
        return len(self._outputs)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def ids(self) -> List[str]:
        """Ids currently held, oldest first."""
        return list(self._outputs.keys())

    def save(self, output) -> str:
        """Save *output* (any type; stored as text). Returns the id."""
        text = output if isinstance(output, str) else str(output)
        size = len(text.encode("utf-8", "replace"))
        # Evict oldest until the new entry fits. A single entry larger
        # than the whole cap is still kept (evicting everything else):
        # the agent just produced it, dropping it would be a surprise.
        while (
            self._outputs and self._total_bytes + size > self.max_bytes
        ):
            oldest = next(iter(self._outputs))
            self._total_bytes -= len(
                self._outputs.pop(oldest).encode("utf-8", "replace")
            )
        self._counter += 1
        output_id = f"out-{self._counter}"
        self._outputs[output_id] = text
        self._total_bytes += size
        if self._file_path:
            with open(self._file_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": output_id, "output": text}) + "\n")
        return output_id

    def recall(self, output_id: str) -> str:
        """Return the raw output for *output_id*; raises KeyError."""
        return self._outputs[output_id]


def recall_output(store: OutputStore, output_id: str) -> str:
    """Recall helper for the future ``/show <id>`` command.

    Never raises: unknown or evicted ids become an ERROR string the model
    can react to.
    """
    try:
        return store.recall(output_id)
    except KeyError:
        return (
            f"ERROR: unknown output id {output_id!r} "
            "(it may have been evicted from the store)."
        )


# ------------------------------------------------------ summarizer ---


def model_summarizer(client, max_input_chars: int = 60_000):
    """Build a summarizer callable backed by a model client.

    The callable takes raw tool-output text and returns the summary text.
    Fail-open handling lives in :class:`CondensingRegistry.execute`, which
    catches any exception from the callable.
    """

    def summarize(text: str) -> str:
        response = client.chat(
            [
                {"role": "system", "content": CONDENSE_SYSTEM},
                {"role": "user", "content": text[:max_input_chars]},
            ],
            [],
        )
        content = response["choices"][0]["message"].get("content") or ""
        return content.strip()

    return summarize


# -------------------------------------------------------- registry ---


class CondensingRegistry(ToolRegistry):
    """A ToolRegistry that condenses oversized tool results per policy.

    Build via :func:`wrap_registry`; never modify ``tools/registry.py``
    for this. ``execute()`` mirrors ``ToolRegistry.execute`` exactly —
    including the approval gate — with one deliberate difference: the
    base class's blind 8000-char truncation is replaced by the policy
    table (see module docstring). Two unconditional rules:

    - results starting with ``"ERROR"`` pass through verbatim, however
      long (the model needs the full failure to reformulate);
    - results at or under the matched policy's threshold pass through
      byte-identical to the base class.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        store: Optional[OutputStore] = None,
        summarizer: Optional[Callable[[str], str]] = None,
        threshold_chars: int = 8000,
        policies: Optional[List[Dict]] = None,
    ):
        super().__init__(auto_approve=registry.auto_approve)
        self.tools = dict(registry.tools)
        # The wrapped registry we delegate execution to. Delegation (not
        # mirroring) keeps hooks, approval gates, and tracing on the inner
        # stack working — see execute().
        self._inner = registry
        # Note: no approver copy needed. ToolRegistry.approver is a
        # forwarding property: reads/writes on any wrapper layer reach the
        # innermost registry, where execution (and the approval gate) lives.
        self.store = store if store is not None else OutputStore()
        self.summarizer = summarizer
        if policies is None:
            policies = [dict(entry) for entry in DEFAULT_POLICIES]
            # Backward-compat: threshold_chars overrides the "*" default.
            for entry in policies:
                if entry.get("tools") == ["*"]:
                    entry["threshold_chars"] = threshold_chars
        self.policies = _validate_policies(policies)
        self.threshold_chars = threshold_chars  # kept for introspection
        # The policy table governs output size from here on: make sure no
        # registry in the delegation chain blind-truncates below (or at)
        # our highest threshold, or policies like read-truncate could never
        # fire. Walk to the bottom of the chain (wrappers delegate via
        # _inner) — only the innermost ToolRegistry.execute truncates.
        # This only ever RAISES caps; _INNER_BACKSTOP stays as the safety
        # backstop against pathological tool outputs.
        seen = set()
        reg = registry
        while reg is not None and id(reg) not in seen:
            seen.add(id(reg))
            if isinstance(reg, ToolRegistry):
                if getattr(reg, "max_result_chars", 8000) < _INNER_BACKSTOP:
                    reg.max_result_chars = _INNER_BACKSTOP
            reg = getattr(reg, "_inner", None)

    def execute(self, name: str, arguments: Dict[str, str]) -> str:
        # Delegate to the wrapped registry: hooks, approval gates, and any
        # tracing on the inner stack all run there; condensation applies to
        # the final result only. (An earlier version mirrored
        # ToolRegistry.execute line-for-line here, which silently bypassed
        # pre/post_tool hooks when wrapping a HookedRegistry, and dropped
        # instance-level execute patches such as attach_tracing.)
        result = self._inner.execute(name, arguments)
        text = "" if result is None else str(result)

        # --- policy handling: the one deliberate difference ---
        # nanitics' recommended default: surface errors verbatim so the
        # model can reformulate — never condense an error, however long.
        if text.startswith("ERROR"):
            return text
        policy = _policy_for(self.policies, name)
        threshold = policy.get("threshold_chars")
        mode = policy.get("mode", "summarize")
        if mode == "passthrough" or threshold is None or len(text) <= threshold:
            return text
        output_id = self.store.save(text)
        if mode == "mask":
            # JetBrains finding: for list-like outputs, hiding with a
            # pointer beats summarizing. Preview + id, no model call.
            lines = text.splitlines()
            preview_lines = int(policy.get("preview_lines", 10))
            preview = "\n".join(lines[:preview_lines])
            return preview + "\n\n" + MASK_NOTE.format(
                preview=min(preview_lines, len(lines)),
                lines=len(lines),
                chars=len(text),
                id=output_id,
            )
        if mode == "truncate":
            return text[:threshold] + "\n\n" + TRUNCATED_NOTE.format(
                chars=len(text), id=output_id
            )
        # mode == "summarize": model summary, fail-open to truncation.
        summary = None
        if self.summarizer is not None:
            try:
                summary = self.summarizer(text)
            except Exception:  # noqa: BLE001 - fail open, never break the loop
                summary = None
        if summary and summary.strip():
            return (
                f"{summary.strip()}\n\n"
                + SUMMARY_NOTE.format(chars=len(text), id=output_id)
            )
        head = text[:threshold]
        return f"{head}\n\n" + TRUNCATED_NOTE.format(
            chars=len(text), id=output_id
        )


def wrap_registry(
    registry: ToolRegistry,
    store: Optional[OutputStore] = None,
    summarizer: Optional[Callable[[str], str]] = None,
    threshold_chars: int = 8000,
    policies: Optional[List[Dict]] = None,
) -> ToolRegistry:
    """Wrap *registry* so oversized tool results are condensed, not cut.

    Pure wrapper: the original registry is untouched, and results at or
    under the matched policy's threshold behave byte-identically to the
    unwrapped registry. ``threshold_chars`` overrides the default ("*")
    policy's threshold; pass ``policies`` for full control of the table.
    """
    return CondensingRegistry(
        registry,
        store=store,
        summarizer=summarizer,
        threshold_chars=threshold_chars,
        policies=policies,
    )
