"""Trace recording: capture tool calls for later case storage.

:class:`TraceRecorder` subscribes to tool execution using the exact same
instance-wrapping style as the coding loop's tracing (wrap
``registry.execute`` on the instance, guard with a flag) — but instead of
printing, it records ``{tool, args_summary, ok}`` events. It composes with
tracing: both wrappers can be attached to the same registry.

``ok`` is derived from the registry's own convention: ``execute()``
returns ``"ERROR: ..."`` strings for unknown tools, denied approvals, bad
arguments, and tool exceptions. A tool whose *legitimate* output starts
with "ERROR" would be misrecorded as failed — an accepted, documented
heuristic.

Distillation (ReasoningBank's dual-memory idea, heuristic v1): successful
traces are distilled into transferable *strategies*; failed traces into
*corrective pitfalls*. The v1 rules below are cheap trace-shape
heuristics — no model call. LLM-AS-JUDGE UPGRADE PATH: replace
:func:`distill_strategies` / :func:`distill_pitfalls` with a single
summarizer call ("given this trace and outcome, write 1-3 strategy
sentences / pitfalls") — the schema already holds the lists, so no
migration is needed.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

_ARG_LIMIT = 200


def _short(value: object, limit: int = _ARG_LIMIT) -> str:
    text = str(value).replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + f"… (+{len(text) - limit} chars)"
    return text


def summarize_args(arguments: Dict) -> str:
    """Compact one-line ``k=v`` rendering of tool arguments."""
    return ", ".join(f"{k}={_short(v)!r}" for k, v in arguments.items())


class TraceRecorder:
    """Records tool-call events off a ToolRegistry."""

    def __init__(self, arg_limit: int = _ARG_LIMIT):
        self.arg_limit = arg_limit
        self.events: List[Dict] = []
        self._t0: Optional[float] = None

    # ------------------------------------------------------------- input ---

    def attach(self, registry) -> "TraceRecorder":
        """Wrap ``registry.execute`` to record every call. Idempotent."""
        if getattr(registry, "_recorder_attached", False):
            return self
        original = registry.execute

        def recorded(name: str, arguments: dict) -> str:
            result = original(name, arguments)
            self.record(
                name, arguments, ok=not str(result).startswith("ERROR")
            )
            return result

        registry.execute = recorded  # type: ignore[method-assign]
        registry._recorder_attached = True
        return self

    def record(self, name: str, arguments: Dict, ok: bool) -> None:
        self.events.append(
            {
                "tool": name,
                "args_summary": summarize_args(arguments),
                "ok": bool(ok),
            }
        )

    # ------------------------------------------------------------ output ---

    def start(self) -> None:
        """Mark the beginning of a task (for latency measurement)."""
        self._t0 = time.time()

    def reset(self) -> None:
        """Clear events and the timer; ready for the next task."""
        self.events = []
        self._t0 = None

    def latency_s(self) -> Optional[float]:
        if self._t0 is None:
            return None
        return round(time.time() - self._t0, 2)

    def to_case(
        self,
        task: str,
        outcome: Dict,
        plan_summary: str = "",
        project_kind: str = "",
    ) -> Dict:
        """Build a case dict from the recorded trace.

        ``outcome`` accepts partial dicts; missing keys default sensibly.
        Distills dual-memory lessons: strategies from successes, pitfalls
        from failures (see :func:`distill_strategies`,
        :func:`distill_pitfalls`). The returned dict has no ``id`` /
        ``timestamp`` — ``CaseStore.add_case`` assigns those on write.
        """
        success = bool(outcome.get("success", False))
        events = list(self.events)
        return {
            "task": task,
            "plan_summary": plan_summary,
            "trace": events,
            "outcome": {
                "success": success,
                "tests_passed": bool(outcome.get("tests_passed", False)),
                "turns": int(outcome.get("turns", 0)),
                "latency_s": outcome.get("latency_s", self.latency_s()),
            },
            "project_kind": project_kind,
            "embedding": None,
            "strategies": distill_strategies(events, plan_summary)
            if success
            else [],
            "pitfalls": distill_pitfalls(events) if not success else [],
        }


# -------------------------------------------------------- distillation ---

_EXPLORE_TOOLS = {"read", "glob", "grep"}
_EDIT_TOOLS = {"edit", "write_file"}

_PITFALL_HINTS = {
    "edit": "check that the exact old_text exists in the file first",
    "write_file": "check that the parent directory exists",
    "bash": "check command syntax and the working directory",
    "grep": "check path patterns and project scope",
    "glob": "check path patterns and project scope",
    "read": "check that the path exists and is inside the project",
}


def distill_strategies(
    events: List[Dict], plan_summary: str = ""
) -> List[str]:
    """Heuristic strategy sentences from a *successful* trace (max 3).

    Priority-ordered rules over trace shape; the first three matches win.
    """
    tools = [e.get("tool") for e in events]
    strategies: List[str] = []
    if plan_summary and plan_summary.strip():
        strategies.append(
            "Planned before acting: broke the task into explicit steps."
        )
    first_edit = next(
        (i for i, t in enumerate(tools) if t in _EDIT_TOOLS), None
    )
    if first_edit is not None and any(
        t in _EXPLORE_TOOLS for t in tools[:first_edit]
    ):
        strategies.append(
            "Explored the codebase (read/grep/glob) before making edits."
        )
    if tools and "todo_write" in tools:
        strategies.append("Tracked multi-step work with a todo list.")
    if tools and "delegate" in tools:
        strategies.append("Delegated a sub-task to a subagent.")
    if (
        events
        and events[-1].get("tool") == "bash"
        and events[-1].get("ok")
    ):
        strategies.append(
            "Verified changes by running tests/build commands after editing."
        )
    if len(set(tools)) >= 4:
        strategies.append(
            "Combined multiple tool types (explore/edit/verify) in one task."
        )
    return strategies[:3]


def distill_pitfalls(events: List[Dict]) -> List[str]:
    """Corrective rules from failed tool calls (one per failed tool, max 3).

    Names the failed tool and its argument pattern plus a targeted hint.
    """
    pitfalls: List[str] = []
    seen = set()
    for event in events:
        if event.get("ok"):
            continue
        tool = event.get("tool") or "?"
        if tool in seen:
            continue
        seen.add(tool)
        hint = _PITFALL_HINTS.get(
            tool, "check arguments and preconditions before retrying"
        )
        pitfalls.append(
            f"Tool '{tool}' failed "
            f"(args: {event.get('args_summary', '')}) — {hint}."
        )
        if len(pitfalls) >= 3:
            break
    return pitfalls
