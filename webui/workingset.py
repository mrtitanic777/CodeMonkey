"""The working set: what a small model needs in front of it on every round, and nothing stale.

Over a long turn the conversation fills with tool output. A small model loses the task in it,
works from a reading of a file it has since changed, and starts calling things that are not
there. Three things are done about that, all from the session's record and with no model call:

    stale readings   an earlier reading of a file that was changed later is replaced by a line
                     saying so, so old text is never copied into a new edit
    old output       as the model's window fills, older results are replaced by one line, then
                     the oldest steps are dropped whole, in stages (offload.py); what is moved
                     out stays out, so the server's cache of the conversation keeps working
    where things     when something has changed (a file edited, tests run, a change refused), the
    stand            newest tool result carries a short note written by the system: the task in
                     the user's words, the round, what was changed, whether the tests are out of
                     date, what each failing test said, the to-do list, and the real signatures
                     of the functions in the files being worked on

The view only ever grows at its end. A note stays on the result it was written on, as what was
true at that round, and a new one is written only when something has changed since. A local
model server keeps its work on a conversation it has already read; a view that rewrites an
earlier message, even its last one, has the server read thousands of tokens again on every
round (measured: 5-8 thousand, 10-15 seconds of each round). Stale readings are named in the
note at once and taken out when older output is next moved out in a batch.

The model is sent this view; the record itself is not altered.
"""

from __future__ import annotations

import ast
import json
import os
from typing import Any, Dict, List, Optional

SIG_CHARS = 1100


def signatures(path: str, limit: int = SIG_CHARS) -> str:
    """The functions and classes a Python file really defines, one per line."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return ""
    lines: List[str] = []

    def sig(node: Any, indent: str = "") -> str:
        try:
            args = ast.unparse(node.args)
        except Exception:  # noqa: BLE001
            args = "..."
        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{indent}{kind} {node.name}({args})"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(sig(node))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            lines.append(f"class {node.name}" + (f"({bases})" if bases else ""))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (
                        sub.name.startswith("_") and sub.name != "__init__"):
                    lines.append(sig(sub, "    "))
    out, used = [], 0
    for line in lines:
        if used + len(line) > limit:
            out.append("…")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def standing(session, task: str, round_no: int) -> str:
    """The note that says where things stand. Built from the record; short on purpose."""
    ledger = session.ledger
    lines = [f"[Where things stand at round {round_no}: written by the system from this session's record. "
             "It replaces any earlier note like it.]",
             "Task, in the user's words: " + " ".join(str(task or "").split())[:320],
             f"Round {round_no} of {session.loop.config.max_iterations}."]
    changed = [p for p in ledger.touched if p in ledger.changed]
    if changed:
        lines.append("Changed so far: " + ", ".join(changed[-6:]))
    last = next((r for r in reversed(ledger.runs) if r.get("tests")), None)
    if last is None:
        if changed:
            lines.append("Tests: not run in this session.")
    elif last["at"] < ledger.last_change_at:
        lines.append(f"Tests: `{last['cmd'][:80]}` was last run BEFORE your latest change. Its result is out of date.")
    else:
        failing = last.get("failing") or []
        lines.append(f"Tests: `{last['cmd'][:80]}` last gave exit {last['exit']}"
                     + (f": {len(failing)} failing" if failing else "") + ".")
        # what each failure said, in the test runner's own words: what was wanted and what came back
        said = [" ".join(l.split())[:230] for l in str(last.get("text") or "").splitlines()
                if l.startswith(("FAILED ", "ERROR ")) and " - " in l]
        if said:
            lines.append("What failed (from that run):\n" + "\n".join("  " + l for l in said[:8])
                         + (f"\n  ... and {len(said) - 8} more" if len(said) > 8 else ""))
        elif failing:
            lines.append("Failing: " + ", ".join(failing[:5]))
    gate = getattr(session, "codegate", None)
    if gate is not None:
        if gate.broken:
            lines.append("Broken by your changes and not mended: " + ", ".join(sorted(gate.broken)[:5]))
        if gate.refused:
            lines.append(f"Changes refused this turn: {gate.refused}.")
    todos = [t for t in getattr(session.todo_state, "items", []) if t.get("status") != "completed"]
    if todos:
        lines.append("To do: " + "; ".join(
            f"[{t.get('status', 'pending')}] {str(t.get('content', ''))[:70]}" for t in todos[:5]))
    budget = SIG_CHARS
    for rel in [p for p in reversed(ledger.touched) if p.endswith(".py")][:2]:
        text = signatures(os.path.join(session.project_dir, rel), budget)
        if text:
            lines.append(f"Defined in {rel}:\n{text}")
            budget -= len(text)
        if budget < 200:
            break
    try:   # who depends on what was changed: the callers a change must not leave behind
        from .codegraph import graph_for

        graph = graph_for(session.project_dir)
        used: List[str] = []
        for rel in [p for p in reversed(ledger.touched) if p in ledger.changed and p.endswith(".py")][:2]:
            real = graph._match_file(rel) or rel
            for sid in graph.files.get(real, {}).get("ids", []):
                sym = graph.symbols[sid]
                if sym.kind == "module":
                    continue
                outside = [f"{c.file}:{line}" for c, line, certain, _a in graph.callers(sym) if certain and c.file != real]
                if outside:
                    used.append(f"{sym.qual} <- " + ", ".join(outside[:4]) + (" …" if len(outside) > 4 else ""))
        if used:
            lines.append("Called from other files (keep these working): " + "; ".join(used[:6]))
    except Exception:  # noqa: BLE001
        pass
    lines.append("Use only names that appear here or in what you have read. If you need one that is not, "
                 "read or search for it first.")
    return "\n".join(lines)


LEVELS = ("off", "medium", "high")
NOTE_EVERY = 8      # rounds after which the standing note is written again though nothing changed


def thinking_level(session, refused_before: int) -> str:
    """How much the model should think on the next request, from how the work is going.

    Most rounds are a lookup or a small edit, and a model that deliberates at length before each
    one spends most of the turn doing it (measured: 86% of a fifteen-minute task; the same
    request answered in 4 seconds with thinking off and 70 with a 2,048-token budget). So a
    round is answered without thinking, and thinking is turned on by a sign of trouble: the
    tests fail on the code as it now is, they failed twice running, a change was refused. It is
    turned off again when the tests pass. The session's effort setting is the most it rises to.

    A small budget is not a middle way: cut off mid-thought, a model carries on in its answer
    (measured at 512 tokens: 8,000 characters of deliberation and no action). A model with no
    word for "off" is given the lowest effort it has instead.
    """
    setting = str(session.settings.get("effort", "auto"))
    ceiling = {"medium": 1, "auto": 1, "high": 2}.get(setting, 1)
    ledger = session.ledger
    # only runs made after this turn's first change: a test that fails before anything was
    # changed is the task as it was given, not a sign the work is going badly
    first = getattr(ledger, "first_change_at", 0)
    runs = [r for r in ledger.runs if r.get("tests") and first > ledger.turn_mark and r["at"] > first]
    trouble = 0
    if runs and runs[-1]["at"] >= ledger.last_change_at and (runs[-1].get("failing") or runs[-1].get("exit") not in (0, None)):
        trouble = 1
        if len(runs) >= 2 and (runs[-2].get("failing") or runs[-2].get("exit") not in (0, None)):
            trouble = 2
    gate = getattr(session, "codegate", None)
    if gate is not None and int(getattr(gate, "refused", 0) or 0) > refused_before:
        trouble = max(trouble, 1)
    return LEVELS[min(ceiling, trouble)]


class GroundedClient:
    """Wraps the model client for one turn: every request is sent the working-set view."""

    def __init__(self, inner, session, task: str):
        self.__dict__["inner"] = inner
        self.__dict__["session"] = session
        self.__dict__["task"] = task
        self.__dict__["round_no"] = 0
        self.__dict__["trimmed_chars"] = 0  # for the record: how much was kept out
        self.__dict__["notes_at"] = {}      # tool_call_id -> the note written on that result, for good
        self.__dict__["noted"] = (None, 0)  # (what the last note was about, the round it was written)
        from .offload import Offloader

        self.__dict__["offloader"] = Offloader(session.context_window(), per_token=getattr(session, "_chars_per_token", 0.0))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["inner"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__dict__:
            self.__dict__[name] = value
        else:
            setattr(self.__dict__["inner"], name, value)

    def view(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> List[Dict]:
        calls: Dict[str, Dict[str, Any]] = {}
        for m in messages:
            for call in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
                func = call.get("function") or {}
                try:
                    args = json.loads(func.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls[call.get("id", "")] = {"name": func.get("name", ""), "args": args if isinstance(args, dict) else {}}
        tool_at = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if not tool_at:
            return messages
        # which file was changed at which point, to spot readings made before a change
        from .epistemic import _norm

        changed_at: Dict[str, int] = {}
        for i in tool_at:
            call = calls.get(messages[i].get("tool_call_id", ""), {})
            if call.get("name") in ("edit", "write_file") and not str(messages[i].get("content", "")).lstrip().startswith("ERROR"):
                changed_at[_norm(call["args"].get("path", ""))] = i
        stale = set()
        stale_paths = set()
        latest_read: Dict[str, int] = {}
        for i in tool_at:
            call = calls.get(messages[i].get("tool_call_id", ""), {})
            if call.get("name") == "read":
                path = _norm(call.get("args", {}).get("path", ""))
                latest_read[path] = i
                if changed_at.get(path, -1) > i:
                    stale.add(i)
        # a file whose every reading is older than its last change has not been looked at since
        stale_paths = {p for p, i in latest_read.items() if changed_at.get(p, -1) > i}
        # how full the window is decides how much older output is moved out (see offload.py)
        out = self.offloader.apply(messages, calls, stale, len(json.dumps(tools)) if tools else 0)
        self.trimmed_chars = sum(len(str(m.get("content") or "")) for m in messages) - sum(
            len(str(m.get("content") or "")) for m in out)
        if out and out[-1].get("role") == "tool" and self.round_no >= 2:
            state = self._state(stale_paths)
            about, when = self.noted
            if state != about or self.round_no - when >= NOTE_EVERY:
                note = standing(self.session, self.task, self.round_no)
                if stale_paths:
                    note += ("\nChanged since you last read it: " + ", ".join(sorted(stale_paths)[:5])
                             + ". A reading of it above is out of date: read it again before you edit it.")
                journal = self.offloader.journal_lines()
                if journal:
                    note += "\n" + "\n".join(journal)
                self.notes_at[out[-1].get("tool_call_id", "")] = note
                self.noted = (state, self.round_no)
        if self.notes_at:
            out = [dict(m, content=str(m.get("content") or "") + "\n\n" + self.notes_at[m["tool_call_id"]])
                   if m.get("role") == "tool" and m.get("tool_call_id") in self.notes_at else m for m in out]
        return out

    def _state(self, stale_paths) -> tuple:
        """What a standing note is about: a new one is written only when this has changed."""
        session = self.session
        ledger = session.ledger
        gate = getattr(session, "codegate", None)
        last = next((r["at"] for r in reversed(ledger.runs) if r.get("tests")), 0)
        todos = tuple((str(t.get("content", ""))[:40], t.get("status")) for t in getattr(session.todo_state, "items", []))
        return (ledger.last_change_at, len(ledger.changed), last, int(getattr(gate, "refused", 0) or 0),
                tuple(sorted(getattr(gate, "broken", None) or ())), todos, tuple(sorted(stale_paths)),
                len(self.offloader.journal))

    def _set_thinking(self, force: str = "") -> str:
        """Set this request's thinking. Returns the level the work calls for ("" when left alone).

        A model with a word for "do not think" is always asked without thinking first: looking
        things up is most of a turn and needs none (measured: with thinking allowed after a failed
        test, the model spent 70 seconds deliberating before each plain file read). The level the
        work calls for is returned, and chat() spends it where it counts: on a change to code.
        *force* sets that level outright.
        """
        session = self.session
        try:
            if not session.app.settings.get("adaptive_thinking", True) or session.settings.get("effort") == "low":
                return ""
            # the client that really holds the request settings: wrappers pass a read through
            # to it, but a value set on a wrapper stays on the wrapper
            owner = self.inner
            for _ in range(8):
                held = vars(owner)
                if "params" in held:
                    break
                owner = (held["_built"][held["_current"]] if "_built" in held
                         else held.get("_wrapped") or held.get("inner"))
                if owner is None:
                    return ""
            params = getattr(owner, "params", None)
            if not isinstance(params, dict):
                return ""
            from .models import profile_for, reasoning_params

            profile = profile_for(session.settings.get("model") or session.config.model)
            if force:
                fields = reasoning_params(profile, force)
                if fields:
                    owner.params = dict(params, **fields)
                    self.__dict__["thinking"] = force
                return force
            gate = getattr(session, "codegate", None)
            refused = int(getattr(gate, "refused", 0) or 0)
            level = thinking_level(session, self.__dict__.get("_refused_seen", refused))
            self.__dict__["_refused_seen"] = refused
            off = reasoning_params(profile, "off")
            fields = off or reasoning_params(profile, "low" if level == "off" else level)
            if fields:
                owner.params = dict(params, **fields)
                self.__dict__["thinking"] = "off" if off else level
            return level if off else ""
        except Exception:  # noqa: BLE001 - never stops a turn
            return ""

    def _changes_code(self, reply: Dict) -> bool:
        """True when a reply's tool calls would change a code file (not a test's own notes or prose)."""
        try:
            calls = reply["choices"][0]["message"].get("tool_calls") or []
        except (KeyError, IndexError, TypeError, AttributeError):
            return False
        for call in calls:
            func = call.get("function") or {}
            if func.get("name") not in ("edit", "write_file"):
                continue
            try:
                path = str(json.loads(func.get("arguments") or "{}").get("path") or "")
            except ValueError:
                path = ""
            if path and not path.endswith((".md", ".rst", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".lock")):
                return True
        return False

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        if self.__dict__.get("_conversation") != id(messages):
            # A new run of the loop (the reply gate sending a reply back, say) is a new list of
            # messages. What was moved out is remembered by position in the old list, so carrying
            # it over would cut the wrong messages: start afresh, keeping what was learnt about
            # the model's window and the size of its tokens.
            from .offload import Offloader

            if "_conversation" in self.__dict__:
                self.__dict__["offloader"] = Offloader(self.offloader.window, per_token=self.offloader.per_token)
                self.__dict__["round_no"] = 0
                self.__dict__["notes_at"] = {}
                self.__dict__["noted"] = (None, 0)
            self.__dict__["_conversation"] = id(messages)
        self.round_no += 1
        wanted = self._set_thinking()
        try:
            view = self.view(messages, tools)
        except Exception:  # noqa: BLE001 - grounding must never break a turn
            view = messages
        from .failover import ContextOverflow
        from .offload import chars_of

        try:
            before = int((getattr(self.inner, "usage_total", None) or {}).get("prompt_n", 0)
                         + (getattr(self.inner, "usage_total", None) or {}).get("cache_n", 0))
            noted = len(getattr(self.inner, "notes", None) or [])
            reply = self.inner.chat(view, tools)
            if wanted not in ("", "off") and self._changes_code(reply):
                # The work is in trouble and the model is about to change code without having
                # thought about it. This is the request thinking is for: ask it again, thinking.
                self._set_thinking(force=wanted)
                try:
                    again = self.inner.chat(view, tools)
                    if self._changes_code(again):
                        reply = again
                except RuntimeError:
                    pass
                noted = -1   # two requests were counted: not a measure of this one
            usage = getattr(self.inner, "usage_total", None) or {}
            counted = int(usage.get("prompt_n", 0) + usage.get("cache_n", 0)) - before
            if len(getattr(self.inner, "notes", None) or []) != noted:
                counted = 0   # it was asked again inside the call: the count covers two requests
            if counted > 200:
                # the server counted this request's tokens: learn this model's characters per token
                measured = chars_of(view, len(json.dumps(tools)) if tools else 0) / counted
                if 1.5 <= measured <= 6.0:
                    self.offloader.per_token = measured
                    self.session._chars_per_token = measured
            return reply
        except ContextOverflow:
            # The server says the request is too long: its window is smaller than we thought.
            # Move more out and send again, once.
            self.offloader.usable = max(1024, int(self.offloader.last.get("tokens", self.offloader.usable) * 0.6))
            self.offloader.window = self.offloader.usable
            try:
                self.session.bus.emit("notice", {"text": "The model's window was smaller than expected. Older output "
                                                         "was moved out and the request sent again."})
            except Exception:  # noqa: BLE001
                pass
            return self.inner.chat(self.view(messages, tools), tools)
