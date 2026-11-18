"""Tool registry: named tools with JSON schemas and an approval gate.

A :class:`Tool` bundles a name, a human description, an OpenAI-style JSON
schema for its arguments, and the Python callable that implements it.
Tools that can change the world (shell, file writes) set
``needs_approval=True`` and are gated by :meth:`ToolRegistry.execute`
unless the registry runs in ``auto_approve`` mode. A tool may also provide
``preview``, a callable that renders a human-readable preview of the pending
action (e.g. a diff); the preview is shown inside the approval prompt so the
user approves with full context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


class ApprovalDenied(Exception):
    """Raised internally when the user declines an approval prompt."""


@dataclass
class Tool:
    """A single callable tool."""

    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., str]
    needs_approval: bool = False
    # Safe to run concurrently with other read_only tools (no writes, no
    # approval prompt). The agent loop runs consecutive read_only calls in
    # parallel; anything else stays strictly sequential.
    read_only: bool = False
    preview: Optional[Callable[[Dict[str, Any]], str]] = None

    def schema(self) -> Dict[str, Any]:
        """Return the OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _known_arguments(func: Callable[..., Any], arguments: Dict[str, Any]):
    """(the arguments *func* takes, the names of the others). A function with **kwargs takes all."""
    import inspect

    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return arguments, []
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return arguments, []
    known = {k: v for k, v in (arguments or {}).items() if k in params}
    return known, sorted(k for k in (arguments or {}) if k not in params)


def _confirm(prompt: str) -> bool:
    """Ask the user for interactive confirmation. Defaults to 'no'."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _innermost_registry(registry):
    """Follow ``_inner`` delegation links to the bottom of a wrapper stack.

    Wrappers (HookedRegistry, CondensingRegistry) delegate execution
    inward; the innermost registry is where tool calls actually run.
    """
    seen = set()
    while id(registry) not in seen:
        seen.add(id(registry))
        inner = getattr(registry, "_inner", None)
        if inner is None:
            return registry
        registry = inner
    return registry


def parallel_safe(registry, name: str) -> bool:
    """True when a tool call may run concurrently with other parallel-safe calls.

    A tool runs in parallel only when *every* layer of the registry stack
    says so: the Tool's own ``read_only`` flag, checked on each layer's
    copy of the tool table (wrappers hold their own dict; MCP tools are
    registered on every layer), and no wrapper overriding the decision
    with a per-layer ``PARALLEL_NEVER`` set (e.g. a speculation wrapper's
    cache-hit path). Unknown tools are not parallel.
    """
    seen = set()
    layer = registry
    while layer is not None and id(layer) not in seen:
        seen.add(id(layer))
        if name in getattr(layer, "PARALLEL_NEVER", ()):
            return False
        tool = layer.tools.get(name) if getattr(layer, "tools", None) else None
        if tool is None or not getattr(tool, "read_only", False):
            return False
        layer = getattr(layer, "_inner", None)
    return True


class ToolRegistry:
    """Holds tools by name and dispatches model-requested calls."""

    def __init__(self, auto_approve: bool = False,
                 max_result_chars: int = 8000):
        self.tools: Dict[str, Tool] = {}
        self.auto_approve = auto_approve
        # Backstop against pathological tool outputs. Wrappers that take
        # over size policy (e.g. CondensingRegistry) may raise this.
        self.max_result_chars = max_result_chars
        # Optional approval delegate used instead of the terminal prompt.
        # Called as approver(tool_name, arguments, preview_text) and must
        # return (allowed: bool, message: str | None). A message accompanies
        # denials so the model can tell the user why. Stored/forwarded via
        # the property below.
        self.approver = None

    @property
    def approver(self):
        """Approval delegate, transparently forwarded to the innermost registry.

        Because wrappers delegate execution inward, an approver set on any
        layer must live where execution actually happens. Reading or
        writing ``.approver`` on any layer reaches the innermost registry.
        """
        target = _innermost_registry(self)
        if target is self:
            return self.__dict__.get("approver")
        return target.approver

    @approver.setter
    def approver(self, value):
        target = _innermost_registry(self)
        if target is self:
            self.__dict__["approver"] = value
        else:
            target.approver = value

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def names(self) -> List[str]:
        return sorted(self.tools)

    def schemas(self) -> List[Dict[str, Any]]:
        return [self.tools[name].schema() for name in self.names()]

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """Run a tool by name. Always returns a string for the model.

        Unknown tools, denied approvals, and exceptions are converted into
        ``ERROR: ...`` strings so the model can see what went wrong and
        recover, instead of crashing the loop.
        """
        tool = self.tools.get(name)
        if tool is None:
            return (
                f"ERROR: unknown tool {name!r}. "
                f"Available tools: {', '.join(self.names())}"
            )
        if tool.needs_approval and not self.auto_approve:
            arg_preview = ", ".join(
                f"{k}={str(v)[:60]!r}" for k, v in arguments.items()
            )
            preview_shown = False
            preview_text = ""
            if tool.preview is not None:
                try:
                    preview_text = tool.preview(arguments)
                except Exception as exc:  # noqa: BLE001 - show, don't crash
                    preview_text = f"(preview unavailable: {exc})"
                if preview_text and self.approver is None:
                    # Cline-style: show exactly what will happen, then ask.
                    # (an approver gets the preview itself; printing would corrupt headless JSON output)
                    print(f"--- proposed change: '{name}' ---")
                    print(preview_text[:4000])
                    print("--- end of preview ---")
                if preview_text:
                    preview_shown = True
            if self.approver is not None:
                allowed, message = self.approver(
                    tool.name, arguments, preview_text if preview_shown else ""
                )
                if not allowed:
                    denied = (
                        f"ERROR: the user denied approval for tool {name!r}."
                    )
                    if message:
                        denied += f" The user said: {message}"
                    denied += (
                        " Tell the user what you wanted to do and ask how "
                        "to proceed."
                    )
                    return denied
            else:
                prompt = (
                    "Apply the change shown above?"
                    if preview_shown
                    else f"Allow tool '{name}' ({arg_preview})?"
                )
                if not _confirm(prompt):
                    return (
                        f"ERROR: the user denied approval for tool {name!r}. "
                        "Tell the user what you wanted to do and ask how to proceed."
                    )
        arguments, ignored = _known_arguments(tool.func, arguments)
        try:
            result = tool.func(**arguments)
        except TypeError as exc:
            return f"ERROR: bad arguments for tool {name!r}: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return f"ERROR: tool {name!r} failed: {type(exc).__name__}: {exc}"
        text = "" if result is None else str(result)
        if ignored:   # models add arguments of their own (description, workdir): said, not fatal
            text += f"\n(ignored arguments this tool does not take: {', '.join(ignored)})"
        return text[: self.max_result_chars]
