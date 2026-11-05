"""Recall rendering: turn retrieved cases into a compact few-shot block.

The block is explicitly labeled as *reference material* so the model
treats it as examples of what worked, not as instructions to follow
blindly. Output is capped in characters so recalled cases can never blow
the context budget by themselves.
"""

from __future__ import annotations

from typing import Dict, List

_HEADER = (
    "[Reference: similar past tasks that SUCCEEDED. "
    "These are examples of approaches that worked before — use them as "
    "guidance for the current task, not as instructions to repeat blindly.]"
)

_MAX_TRACE_STEPS = 8

_STRATEGIES_HEADER = "Strategies that worked (distilled from similar successes):"
_PITFALLS_HEADER = (
    "Pitfalls to avoid (distilled from similar failures):"
)


def _render_trace(trace: List[Dict]) -> str:
    steps = []
    for event in trace[:_MAX_TRACE_STEPS]:
        mark = "ok" if event.get("ok") else "FAIL"
        steps.append(f"{event.get('tool')}({event.get('args_summary', '')}) [{mark}]")
    text = " → ".join(steps)
    if len(trace) > _MAX_TRACE_STEPS:
        text += f" … (+{len(trace) - _MAX_TRACE_STEPS} more)"
    return text or "(no tool calls recorded)"


def _render_case(case: Dict, index: int) -> str:
    outcome = case.get("outcome") or {}
    turns = outcome.get("turns", "?")
    latency = outcome.get("latency_s", "?")
    lines = [
        f"{index}. Task: {case.get('task', '(untitled)')}",
    ]
    if case.get("plan_summary"):
        lines.append(f"   Plan: {case['plan_summary']}")
    lines.append(f"   What worked: {_render_trace(case.get('trace') or [])}")
    lines.append(
        f"   Outcome: success, {turns} turns, {latency}s"
        + (
            f" [{case['project_kind']}]"
            if case.get("project_kind")
            else ""
        )
    )
    return "\n".join(lines)


def format_cases(cases: List[Dict], max_chars: int = 4000) -> str:
    """Render *cases* as a labeled few-shot block, capped at *max_chars*.

    Besides the per-case traces, aggregates the dual-memory lessons
    (ReasoningBank): strategies distilled from successes and pitfalls
    distilled from failures, deduplicated across cases. Returns "" when
    there is nothing to show.
    """
    if not cases:
        return ""
    blocks = [_HEADER]
    for i, case in enumerate(cases, 1):
        blocks.append(_render_case(case, i))
    strategies = _dedupe(
        s for case in cases for s in (case.get("strategies") or [])
    )
    pitfalls = _dedupe(
        p for case in cases for p in (case.get("pitfalls") or [])
    )
    if strategies:
        blocks.append(
            _STRATEGIES_HEADER
            + "\n"
            + "\n".join(f"- {s}" for s in strategies)
        )
    if pitfalls:
        blocks.append(
            _PITFALLS_HEADER + "\n" + "\n".join(f"- {p}" for p in pitfalls)
        )
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [reference truncated]"
    return text


def _dedupe(items) -> List[str]:
    """Dedupe strings, preserving first-seen order."""
    seen = set()
    out = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out
