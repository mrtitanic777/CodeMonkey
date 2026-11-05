"""Custom agents: specialists the main agent hands work to by name.

An agent is a Markdown file: frontmatter for what it may use, the body for how it works.

    ---
    name: reviewer
    description: Reviews a change for bugs. Use after an edit, before saying it is done.
    tools: read, glob, grep, git_diff, git_status, code_*
    model: anthropic/claude-sonnet-5-5      (optional: provider/model; default the helper model)
    max_steps: 20                            (optional)
    isolation: worktree                      (optional: work on a branch of its own, see worktrees.py)
    ---
    You review code changes. ...

``tools`` is a comma-separated list (or a YAML list) of tool names, * allowed; ``read-only``
stands for the tools that only look. Left out, the agent gets every tool a helper gets. An agent
can never start helpers of its own.

Where they come from, later ones replacing earlier ones of the same name:
  agents/ next to CodeMonkey (built in, and yours), plugins' agents/, and the project's
  .codemonkey/agents/ (shared with whoever works on the project).
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from coder.commands import _split_frontmatter

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
READ_ONLY = ["read", "glob", "grep", "git_status", "git_diff", "code_*", "process_logs", "process_list",
             "diagnostics", "lsp_*", "mcp_resources", "mcp_read", "web_search", "web_fetch"]
#: Never given to an agent: it would start agents of its own.
NEVER = ("delegate", "subagent_spawn", "subagent_send", "subagent_close")


@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None      # None: everything a helper gets
    model: str = ""
    max_steps: int = 0
    origin: str = "yours"                  # built in | yours | project | plugin:<name>
    path: str = ""
    extra: Dict[str, object] = field(default_factory=dict)

    def allows(self, tool: str) -> bool:
        if tool in NEVER:
            return False
        if self.tools is None:
            return True
        return any(fnmatch.fnmatchcase(tool, pattern) for pattern in self.tools)

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "description": self.description, "prompt": self.prompt,
                "tools": self.tools, "model": self.model, "max_steps": self.max_steps,
                "isolation": str(self.extra.get("isolation") or ""), "origin": self.origin, "path": self.path}


def _tool_list(value) -> Optional[List[str]]:
    if value is None or value == "" or value == "all":
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    out: List[str] = []
    for item in items:
        item = str(item).strip()
        if item in ("read-only", "readonly"):
            out.extend(READ_ONLY)
        elif item:
            out.append(item)
    return out


def parse(text: str, origin: str = "yours", path: str = "") -> Optional[AgentDef]:
    front, body = _split_frontmatter(text)
    name = str(front.get("name") or os.path.splitext(os.path.basename(path))[0]).strip().lower()
    if not NAME_RE.fullmatch(name) or not body.strip():
        return None
    try:
        steps = int(front.get("max_steps") or front.get("max_iterations") or 0)
    except (TypeError, ValueError):
        steps = 0
    model = str(front.get("model") or "").strip()
    return AgentDef(
        name=name, description=" ".join(str(front.get("description") or "").split()),
        prompt=body.strip(), tools=_tool_list(front.get("tools")), model=model if "/" in model else "",
        max_steps=max(0, min(steps, 200)), origin=origin, path=path,
        extra={k: v for k, v in front.items() if k not in ("name", "description", "tools", "model", "max_steps")},
    )


def load_dir(folder: str, origin: str) -> List[AgentDef]:
    out: List[AgentDef] = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for fname in names:
        if not fname.endswith(".md"):
            continue
        path = os.path.join(folder, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                agent = parse(fh.read(), origin, path)
        except OSError:
            continue
        if agent is not None:
            out.append(agent)
    return out


def load_all(app_dir: str, project_dir: str = "", extra: Iterable[tuple] = ()) -> Dict[str, AgentDef]:
    """Every agent, by name. *extra*: (folder, origin) pairs, e.g. from plugins, read before the project's."""
    found: Dict[str, AgentDef] = {}
    sources = [(app_dir, "yours")] + list(extra)
    if project_dir:
        sources.append((os.path.join(project_dir, ".codemonkey", "agents"), "project"))
    for folder, origin in sources:
        for agent in load_dir(folder, origin):
            if origin == "yours" and agent.extra.get("builtin"):
                agent.origin = "built in"
            found[agent.name] = agent
    return found


def render(agent: AgentDef) -> str:
    """The Markdown file for *agent*."""
    lines = ["---", f"name: {agent.name}", f"description: {agent.description}"]
    if agent.tools is not None:
        lines.append("tools: " + ", ".join(agent.tools))
    if agent.model:
        lines.append(f"model: {agent.model}")
    if agent.max_steps:
        lines.append(f"max_steps: {agent.max_steps}")
    if agent.extra.get("isolation"):
        lines.append(f"isolation: {agent.extra['isolation']}")
    lines.append("---")
    return "\n".join(lines) + "\n" + agent.prompt.strip() + "\n"


def menu(agents: Dict[str, AgentDef]) -> str:
    """One line per agent, for a tool description: who is there and when to use each."""
    if not agents:
        return ""
    return "Agents you can give this to (agent=<name>): " + "; ".join(
        f"{a.name}: {a.description or 'no description'}" for a in agents.values()) + "."
