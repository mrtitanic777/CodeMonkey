"""Event hooks: shell out to user scripts at agent lifecycle points.

A hook is a shell command declared in ``hooks.yaml``::

    hooks:
      - event: pre_tool
        matcher: bash|write_file
        command: hooks/examples/block-dangerous.py
        timeout: 10

Hook points (Claude Code's names in brackets): ``session_start`` (SessionStart),
``user_prompt`` (UserPromptSubmit: may block the message or add a note to it), ``pre_tool``
(PreToolUse), ``post_tool`` (PostToolUse), ``turn_end`` (Stop), ``session_end`` (SessionEnd).
``matcher`` (tool events only) is a regular expression the tool's name must match; empty or
``*`` matches every tool.

The command receives JSON on stdin, e.g.::

    {"event": "pre_tool", "tool": "bash", "args": {"cmd": "ls"}}

and returns a JSON decision on stdout::

    {"allow": true}
    {"allow": false, "message": "reason shown to the model"}
    {"allow": true, "modified_args": {"cmd": "ls -la"}}
    {"note": "appended to the tool result as a [hook note]"}

Decisions merge across matching hooks: any deny wins; ``modified_args``
chain through hooks in declaration order; ``note`` values are collected.
Hooks must never break the agent: timeouts (default 10s), non-zero exits,
and malformed output all fail OPEN with a logged warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import mini_yaml

from tools.registry import ToolRegistry

logger = logging.getLogger("hooks")

#: Lifecycle points hooks can attach to.
EVENTS = ("session_start", "user_prompt", "pre_tool", "post_tool", "turn_end", "session_end")

DEFAULT_TIMEOUT = 10.0


@dataclass
class Hook:
    """One declared hook: run *command* when *event* fires."""

    event: str
    command: str
    timeout: float = DEFAULT_TIMEOUT
    matcher: str = ""

    def matches(self, tool: str) -> bool:
        if not self.matcher or self.matcher.strip() == "*" or not tool:
            return True
        import re

        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return self.matcher == tool


class HookManager:
    """Loads hooks from ``hooks.yaml`` and runs them per event."""

    def __init__(
        self,
        hooks: Optional[List[Hook]] = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ):
        self.hooks: List[Hook] = list(hooks or [])
        self.default_timeout = default_timeout

    @classmethod
    def load(cls, path: str = "hooks.yaml") -> "HookManager":
        """Load hooks from a YAML file. A missing file means no hooks."""
        import os

        if not os.path.exists(path):
            logger.info("no hooks file at %s; hooks disabled", path)
            return cls([])
        try:
            data = mini_yaml.parse_file(path)
        except ValueError as exc:
            logger.warning("ignoring malformed hooks file %s: %s", path, exc)
            return cls([])
        entries = data.get("hooks") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            logger.warning("ignoring hooks file %s: expected a list", path)
            return cls([])
        hooks = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event = str(entry.get("event", "")).strip()
            command = str(entry.get("command", "")).strip()
            if event not in EVENTS or not command:
                logger.warning("ignoring invalid hook entry: %r", entry)
                continue
            try:
                timeout = float(entry.get("timeout", DEFAULT_TIMEOUT))
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
            hooks.append(Hook(event=event, command=command, timeout=timeout,
                              matcher=str(entry.get("matcher") or "").strip()))
        logger.info("loaded %d hook(s) from %s", len(hooks), path)
        return cls(hooks)

    def hooks_for(self, event: str, tool: str = "") -> List[Hook]:
        """Hooks declared for *event* (and matching *tool*), in declaration order."""
        return [h for h in self.hooks if h.event == event and h.matches(tool)]

    @staticmethod
    def save(path: str, hooks: List[Hook]) -> None:
        """Write *hooks* to a hooks.yaml (the whole file is replaced)."""
        import os

        def q(text: str) -> str:
            return json.dumps(str(text))   # a JSON string is a valid quoted YAML scalar
        lines = ["# Hooks: commands run at points in the agent's work (see hooks.py).", "hooks:"]
        for h in hooks:
            lines.append(f"  - event: {h.event}")
            if h.matcher:
                lines.append(f"    matcher: {q(h.matcher)}")
            lines.append(f"    command: {q(h.command)}")
            lines.append(f"    timeout: {h.timeout:g}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)

    def run(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run matching hooks; merge into one decision dict.

        Returns ``{"allow": bool, "message": str|None,
        "modified_args": dict|None, "notes": [str]}``. Any deny wins;
        ``modified_args`` chain through hooks in order; failures fail open.
        """
        decision: Dict[str, Any] = {
            "allow": True,
            "message": None,
            "modified_args": None,
            "notes": [],
        }
        args = dict(payload.get("args") or {})
        for hook in self.hooks_for(event, str(payload.get("tool") or "")):
            out = self._run_one(hook, {**payload, "args": args})
            if out is None:
                continue  # failed open; already logged
            if out.get("allow") is False:
                decision["allow"] = False
                message = out.get("message")
                if message:
                    prev = decision["message"]
                    decision["message"] = (
                        f"{prev}; {message}" if prev else str(message)
                    )
            modified = out.get("modified_args")
            if isinstance(modified, dict) and modified:
                args.update(modified)
                decision["modified_args"] = dict(args)
            note = out.get("note")
            if note:
                decision["notes"].append(str(note))
        return decision

    def _run_one(
        self, hook: Hook, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Run one hook command. Returns its decision dict, or None to fail open."""
        from procutil import run_shell

        try:
            proc = run_shell(hook.command, timeout=hook.timeout, stdin_text=json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - hooks must never break the agent
            logger.warning("hook failed to run (failing open): %s: %s", hook.command, exc)
            return None
        if proc.timed_out:
            logger.warning(
                "hook timed out after %ss and was stopped (failing open): %s",
                hook.timeout,
                hook.command,
            )
            return None
        if proc.code != 0:
            logger.warning(
                "hook exited %d (failing open): %s; stderr: %s",
                proc.code,
                hook.command,
                proc.stderr[:500],
            )
            return None
        try:
            out = json.loads(proc.stdout or "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "hook returned bad JSON (failing open): %s: %s",
                hook.command,
                exc,
            )
            return None
        if not isinstance(out, dict):
            logger.warning(
                "hook returned non-object JSON (failing open): %s", hook.command
            )
            return None
        return out


class HookedRegistry(ToolRegistry):
    """A ToolRegistry that fires pre_tool/post_tool hooks around execute().

    Build via :func:`wrap_registry`; never modify ``tools/registry.py`` for this.
    """

    def __init__(self, registry: ToolRegistry, manager: HookManager):
        super().__init__(auto_approve=registry.auto_approve)
        self.tools = dict(registry.tools)
        self.hook_manager = manager
        # The wrapped registry we delegate the actual tool run to.
        # Delegating (not super().execute()) preserves instance-level
        # execute patches on the inner registry, e.g. attach_tracing.
        self._inner = registry

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        decision = self.hook_manager.run(
            "pre_tool", {"event": "pre_tool", "tool": name, "args": arguments}
        )
        if not decision["allow"]:
            message = decision["message"] or f"hook denied tool {name!r}"
            return f"ERROR: blocked by hook: {message}"
        args = decision["modified_args"] or arguments
        result = self._inner.execute(name, args)
        post = self.hook_manager.run(
            "post_tool",
            {
                "event": "post_tool",
                "tool": name,
                "args": args,
                "result": result[:4000],
            },
        )
        notes = [n for n in post["notes"] if n]
        if notes:
            result = result + "\n" + "\n".join(f"[hook note] {n}" for n in notes)
        return result


def wrap_registry(
    registry: ToolRegistry, manager: HookManager
) -> ToolRegistry:
    """Wrap *registry* so tool calls pass through pre/post_tool hooks.

    Pure wrapper: the original registry is untouched, and with no hooks
    declared the behavior is identical to the unwrapped registry.
    """
    return HookedRegistry(registry, manager)
