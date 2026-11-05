"""Helpers: sub-agents that work in the background while the main agent stays with you.

    subagent_spawn   start one; returns at once with its id
    subagent_list    what each is doing
    subagent_send    steer one that is running
    subagent_close   stop one

A helper is a second agent loop in the same folder. It is not a way around anything: its tools go
through the same approval gate, the same per-kind permissions and the same kernel guard as the
main agent's, and what it reads counts as evidence for the session. It cannot start helpers of
its own. When it finishes, its report comes back as a handoff: the main agent reads it and
decides what you need to hear. A helper's report is checked like any other claim, never trusted
as an instruction.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from coder import CoderLoop, coder_tools
from tools.registry import Tool, ToolRegistry

MAX_RUNNING = 3

_CHILD_PROMPT = (
    "You are a helper working for another agent, not talking to the user. Do exactly the brief "
    "below and stop. Your final message is your report: what you found or did, what you could "
    "not establish, and the file paths or command output that back each point. Say what you "
    "checked and what you are only inferring. Do not ask questions; if something is missing, "
    "report that.\n\nBrief:\n"
)


class Helpers:
    def __init__(self, session):
        self.session = session
        self._lock = threading.Lock()
        self.live: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------- running ---

    def _registry(self, agent_id: str, info: Dict[str, Any], agent=None, project_dir: str = "") -> ToolRegistry:
        from tools.registry import _innermost_registry

        from .app import _ANSI_RE, _short_args

        session = self.session
        registry = ToolRegistry(auto_approve=False)
        for tool in coder_tools(project_dir or session.project_dir, auto_approve=False, tracker=session.session_mgr):
            if agent is None or agent.allows(tool.name):
                registry.register(tool)
        if agent is not None:
            # a named agent may also use the session's other tools it lists (code graph, web, MCP)
            for name, tool in _innermost_registry(session.loop.registry).tools.items():
                if name not in registry.tools and agent.allows(name):
                    registry.register(tool)
        registry.approver = lambda name, args, preview: session.gate(
            name, args, preview, helper=info["brief"][:120]
        )
        original = registry.execute

        def traced(name: str, arguments: Dict[str, Any]) -> str:
            gate_refusal, gate_notes = session.codegate.before(name, arguments)   # a helper is held to the same gate
            began = time.monotonic()
            refusal = gate_refusal or session._refusal(name, arguments, registry)
            text = _ANSI_RE.sub("", str(refusal if refusal else original(name, arguments) or ""))
            session.ledger.note_tool(name, arguments, text, time.monotonic() - began)
            if not gate_refusal and name in ("edit", "write_file") and not text.lstrip().startswith("ERROR"):
                if gate_notes:
                    text += "\n[Gate] Check these: " + "; ".join(gate_notes)
                after = session.codegate.after(name, arguments, text)
                text = after.strip() if after.startswith("\nERROR") else text + after
                if not text.lstrip().startswith("ERROR"):
                    text += session.errors_after_edit(arguments)
            session._ledger_line(name, arguments, text)
            entry = {"kind": "tool", "name": name, "args": _short_args(arguments, 300),
                     "result": text[:2000], "truncated": len(text) > 2000}
            try:
                session.app.store.append_item(agent_id, entry, "subagent")
            except Exception:  # noqa: BLE001
                pass
            info["steps"] += 1
            args = arguments or {}
            info["last"] = f"{name} {args.get('path') or args.get('cmd') or args.get('pattern') or ''}".strip()[:120]
            session.bus.emit("subagent_progress", {
                "id": agent_id, "tool": name, "summary": info["last"], "steps": info["steps"],
                "ok": not text.lstrip().startswith("ERROR"),
            }, transient=True)
            with self._lock:
                notes, info["notes"] = info["notes"], []
            for note in notes:  # steering reaches the helper with its next tool result
                text += f"\n\n[Message from the main agent while you work: {note}]"
            return text

        registry.execute = traced  # type: ignore[method-assign]
        return registry

    def resolve(self, name: str):
        """The agent definition called *name* (None for a plain helper), or raise with the choices."""
        name = str(name or "").strip().lower()
        if not name:
            return None
        found = self.session.app.agents(self.session.project_dir)
        if name not in found:
            raise ValueError(f"no agent named {name!r}; there are: " + (", ".join(found) or "none"))
        return found[name]

    def run_child(self, agent_id: str, brief: str, agent=None) -> str:
        """Run one helper (or a named agent) to the end on the calling thread and return its report."""
        from dataclasses import replace

        session = self.session
        info = {"brief": str(brief), "steps": 0, "last": "", "notes": [], "started": time.time(), "loop": None,
                "agent": agent.name if agent else ""}
        with self._lock:
            self.live[agent_id] = info
        project = session.project_dir
        isolated = None
        try:
            if agent is not None and str(agent.extra.get("isolation") or "") == "worktree":
                # its own branch and folder: what it changes stays apart until you merge it
                isolated = session.app.worktrees.create(session.project_dir, agent.name, owner=agent_id)
                project = isolated["folder"]
                info["worktree"] = isolated["branch"]
            config = session.config
            if agent is not None and agent.max_steps:
                config = replace(config, max_iterations=agent.max_steps)
            client = None
            if agent is not None and agent.model:
                provider, _, model = agent.model.partition("/")
                if provider in (session.config.providers or {}):
                    client = session._raw_client(provider, model)
            loop = CoderLoop(
                config=config, project_dir=project,
                registry=self._registry(agent_id, info, agent, project), memory=session.loop.memory,
                skills=session.app.skills, client=client or session._build_child_client(),
                auto_approve=False, depth=session.loop.depth + 1,
            )
            info["loop"] = loop
            role = f"You are the {agent.name} agent.\n{agent.prompt}\n\n" if agent is not None else ""
            report = loop.run(role + _CHILD_PROMPT + str(brief))
            if isolated is not None:
                report = (str(report) + f"\n\n[This agent worked on branch {isolated['branch']} in {isolated['path']}. "
                          f"Nothing is in {isolated['repo']} yet: merge or discard it in Settings > Worktrees.]")
            return report
        finally:
            with self._lock:
                self.live.pop(agent_id, None)

    def _background(self, agent_id: str, brief: str, agent=None) -> None:
        from coder.mcp import CALLER

        session = self.session
        CALLER.value = session   # an MCP server asking during the helper's tool calls asks this session
        stopped = False
        try:
            report = self.run_child(agent_id, brief, agent)
            ok = not str(report).startswith("[Error")
            stopped = str(report).startswith("[Cancelled")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            report, ok = f"ERROR: {type(exc).__name__}: {exc}", False
        report = str(report or "")
        status = "stopped" if stopped else "done" if ok else "failed"
        session.app.store.finish_agent(agent_id, status, report[:8000])
        session._record({"kind": "subagent", "id": agent_id, "brief": brief[:600], "summary": report[:800]})
        session._activity("helper", "Helper: " + brief[:100], "", "success" if ok and not stopped else status)
        session.bus.emit("subagent_end", {"id": agent_id, "ok": ok and not stopped, "summary": report[:800]})
        if not stopped and session.alive:
            # The report goes to the main agent, which decides what you hear.
            session.inbox.put({"kind": "handoff", "id": agent_id, "brief": brief, "report": report, "ok": ok})

    # ------------------------------------------------------------ tools ---

    def spawn(self, brief: str, agent: str = "") -> str:
        brief = str(brief or "").strip()
        if not brief:
            return "ERROR: a helper needs a brief."
        try:
            definition = self.resolve(agent)
        except ValueError as exc:
            return f"ERROR: {exc}"
        session = self.session
        with self._lock:
            running = len(self.live)
        if running >= MAX_RUNNING:
            return (f"ERROR: {MAX_RUNNING} helpers are already running. Wait for one to report, "
                    "or close one with subagent_close.")
        agent_id = session.app.store.spawn_agent(
            session.id, session.id, brief, model=str(session.settings.get("model", "")),
            depth=session.loop.depth + 1,
        )
        session.bus.emit("subagent_start", {"id": agent_id, "brief": brief[:600],
                                            "agent": definition.name if definition else ""})
        threading.Thread(
            target=self._background, args=(agent_id, brief, definition), name=f"helper-{agent_id[:6]}", daemon=True
        ).start()
        who = f"The {definition.name} agent ({agent_id})" if definition else f"Helper {agent_id}"
        return (f"{who} started. It works in the background; its report will arrive "
                "later as a handoff. Do not wait for it or guess its result: tell the user it has "
                "started and carry on or end your turn.")

    def listing(self) -> str:
        rows = [a for a in self.session.app.store.agents(self.session.id) if a["kind"] != "root"]
        if not rows:
            return "No helpers have been started in this session."
        out = []
        with self._lock:
            live = {k: dict(v, loop=None) for k, v in self.live.items()}
        for a in rows[-12:]:
            line = f"{a['agent_id']}  {a['status']:8}  {str(a['prompt'] or '')[:80]}"
            if a["agent_id"] in live:
                info = live[a["agent_id"]]
                who = (info["agent"] + ", ") if info.get("agent") else ""
                line += f"  ({who}{info['steps']} steps, last: {info['last'] or 'starting'})"
            out.append(line)
        return "\n".join(out)

    def send(self, agent_id: str, message: str) -> str:
        with self._lock:
            info = self.live.get(str(agent_id))
            if info is None:
                return f"ERROR: helper {agent_id} is not running."
            info["notes"].append(str(message)[:2000])
        return f"Sent. Helper {agent_id} will see it with its next step."

    def close(self, agent_id: str) -> str:
        with self._lock:
            info = self.live.get(str(agent_id))
        if info is None or info.get("loop") is None:
            return f"ERROR: helper {agent_id} is not running."
        info["loop"].cancel_requested = True
        return f"Helper {agent_id} will stop at its next step. No report will be handed back."

    def stop_all(self) -> None:
        with self._lock:
            loops = [i["loop"] for i in self.live.values() if i.get("loop") is not None]
        for loop in loops:
            loop.cancel_requested = True

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = [a for a in self.session.app.store.agents(self.session.id) if a["kind"] != "root"]
        with self._lock:
            live = {k: (v["steps"], v["last"]) for k, v in self.live.items()}
        return [
            {"id": a["agent_id"], "brief": str(a["prompt"] or "")[:600], "status": a["status"],
             "summary": str(a["final_response"] or "")[:800],
             "steps": live.get(a["agent_id"], (0, ""))[0], "last": live.get(a["agent_id"], (0, ""))[1]}
            for a in rows[-12:]
        ]

    def tools(self) -> List[Tool]:
        from coder.agents import menu

        text = {"type": "string"}
        agents = menu(self.session.app.agents(self.session.project_dir))
        return [
            Tool(
                name="subagent_spawn",
                description=(
                    "Start a helper that works in the background on a self-contained task while you "
                    "stay with the user. Returns at once; the helper's report arrives later as a "
                    "handoff. Give a complete brief: the goal, where to look, and what to report. "
                    "Use one helper per independent task. Helpers follow the same permissions as you. "
                    + agents
                ),
                parameters={"type": "object", "properties": {
                    "brief": dict(text, description="Complete instructions."),
                    "agent": dict(text, description="A named agent to do it (optional; default a general helper)."),
                }, "required": ["brief"]},
                func=self.spawn,
            ),
            Tool(
                name="subagent_list",
                description="List this session's helpers with their status and latest step.",
                parameters={"type": "object", "properties": {}},
                func=self.listing,
            ),
            Tool(
                name="subagent_send",
                description="Send a message to a running helper to steer it. It sees it with its next step.",
                parameters={"type": "object", "properties": {"agent_id": text, "message": text},
                            "required": ["agent_id", "message"]},
                func=self.send,
            ),
            Tool(
                name="subagent_close",
                description="Stop a running helper whose work is no longer needed.",
                parameters={"type": "object", "properties": {"agent_id": text}, "required": ["agent_id"]},
                func=self.close,
            ),
        ]
