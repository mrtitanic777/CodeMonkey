"""Coding harness: a Claude Code-style agentic coding subsystem.

Built on the framework's AgentLoop, ToolRegistry, approval gate, skills,
and memory. Used by the app (webui/app.py).
"""

from .tools import TodoState, coder_tools
from .context import load_project_context
from .commands import Command, expand_command, find_command, load_commands
from .session import SessionManager
from .loop import CoderLoop

__all__ = [
    "TodoState",
    "coder_tools",
    "load_project_context",
    "Command",
    "expand_command",
    "find_command",
    "load_commands",
    "SessionManager",
    "CoderLoop",
]
