"""Context offload: keeping a long turn inside the model's window without losing the thread.

A turn that reads, searches and runs things fills the window with tool output. Most of it stops
mattering a few steps later, and a small model works worse the fuller the window gets. So output
is moved out of what the model is sent, in stages, by how full the window is:

    under half       nothing is removed (only noise is squeezed out of long command output)
    half full        the results that can best be replaced by one line are replaced: readings
                     that were superseded first, then listings, then older readings, and so on
                     down a ladder, until the window is under half again
    85% full         the oldest steps are dropped whole (a tool call always goes together with
                     its result), down to 60%; a journal of what they were stays in view
    95% full         whatever is largest is cut short, so the turn can always go on

What is replaced or dropped stays that way for the rest of the turn, so the conversation the
server has already read does not keep changing under it. The last results are never touched,
nor is anything the user wrote. Nothing is summarised by a model: the one-line stand-ins are
made from the call itself (what was read, what a command exited with).

The record of the session is not altered. This is only what the model is sent.

The staged thresholds, the "most replaceable first" ladder and never splitting a call from its
result follow the context-offload design in TencentDB Agent Memory; squeezing command noise
follows what output filters such as RTK do. The code is written here.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

MILD, AGGRESSIVE, EMERGENCY, TARGET = 0.60, 0.85, 0.95, 0.60
# Once the mild stage starts it clears down to here in one go. Moving one result out per round
# changes an early message on every request, and the server then reads the whole conversation
# again each time (measured: 9-13 thousand tokens, 30-40 seconds a round). One larger move costs
# that once and then leaves the conversation alone for many rounds.
MILD_TARGET = 0.35
KEEP_RECENT = 4
CHARS_PER_TOKEN = 3.5      # an estimate on the safe side for code and logs
DEFAULT_WINDOW = 32_768
LADDER = (9, 7, 5, 3, 1)
EMERGENCY_CHARS = 2000

_NOISE = [
    re.compile(r"^\s*(Requirement already satisfied|Collecting |Downloading |Using cached |Installing collected|"
               r"Successfully (installed|built)|Building wheel|Preparing metadata|Getting requirements)", re.I),
    re.compile(r"^\s*(npm (warn|notice|http)|added \d+ packages|\d+ packages are looking for funding|up to date)", re.I),
    re.compile(r"^.*\b(PASSED|passed|ok)\s*(\[\s*\d+%\])?\s*$"),
    re.compile(r"^\s*[\w./\\-]+\s+[.sx]+\s*(\[\s*\d+%\])?\s*$"),           # pytest progress dots
    re.compile(r"^\s*\d+%\|.*\|\s*\d+/\d+"),                                # progress bars
    re.compile(r"^\s*(Compiling|Compiled|Checking|Fresh|Downloaded|Unpacking|Fetching|remote: |Receiving objects|Resolving deltas)", re.I),
    re.compile(r"^\s*$"),
]
_KEEP = re.compile(r"error|fail|exception|traceback|warning|assert|denied|not found|cannot|undefined|\bE\s{2,}", re.I)


def squeeze(text: str, limit: int = 2500) -> str:
    """Take the routine lines out of long command output; anything that looks like a problem stays."""
    if len(text) <= limit:
        return text
    out: List[str] = []
    hidden = 0

    def flush() -> None:
        nonlocal hidden
        if hidden:
            out.append(f"[… {hidden} routine line{'s' if hidden != 1 else ''} not shown]")
            hidden = 0

    previous = None
    for line in text.splitlines():
        shape = re.sub(r"\d+", "0", line)
        routine = not _KEEP.search(line) and (any(p.search(line) for p in _NOISE) or shape == previous)
        previous = shape
        if routine and out:
            hidden += 1
            continue
        flush()
        out.append(line)
    flush()
    squeezed = "\n".join(out)
    return squeezed if len(squeezed) < len(text) * 0.9 else text


def summarize(name: str, args: Dict[str, Any], content: str) -> str:
    """One line that stands in for a tool result, made from the call itself."""
    args = args or {}
    lines = content.count("\n") + 1
    failed = content.lstrip().startswith("ERROR")
    if name == "read":
        return f"read {args.get('path')}" + (": failed" if failed else f" ({lines} lines)")
    if name in ("edit", "write_file"):
        return ("tried to change " if failed else "changed ") + str(args.get("path")) + (": refused or failed" if failed else "")
    if name == "bash":
        m = re.match(r"\[exit (-?\d+)\]", content)
        last = next((ln.strip() for ln in reversed(content.splitlines()) if ln.strip()), "")
        return f"ran `{str(args.get('cmd', ''))[:80]}`: exit {m.group(1) if m else '?'}; last line: {last[:120]}"
    if name in ("grep", "glob"):
        return f"{name} {str(args.get('pattern', ''))[:60]!r}: " + ("failed" if failed else f"{lines} lines of matches")
    if name.startswith("code_"):
        return f"{name} {str(args.get('symbol') or args.get('query') or '')[:60]}: {lines} lines"
    head = " ".join(content.split())[:140]
    return f"{name}: {head}"


def chars_of(messages: List[Dict], extra_chars: int = 0) -> int:
    total = extra_chars
    for m in messages:
        total += len(str(m.get("content") or "")) + 12
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total


def tokens_of(messages: List[Dict], extra_chars: int = 0, per_token: float = 0.0) -> int:
    if per_token:
        return int(chars_of(messages, extra_chars) / per_token)
    total = extra_chars
    for m in messages:
        total += len(str(m.get("content") or "")) + 12
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return int(total / CHARS_PER_TOKEN)


def detect_window(base_url: str, model: str, timeout: float = 1.2) -> Optional[int]:
    """Ask a local server how large its window is. None when it does not say."""
    root = str(base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    if not root.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return None   # only a server on this computer is asked
    from urllib.parse import quote

    for path, pick in (
        # a server that switches between models answers for the one named, without loading it
        (f"/props?model={quote(str(model), safe='')}&autoload=false",
         lambda d: (d.get("default_generation_settings") or {}).get("n_ctx")),
        ("/props", lambda d: (d.get("default_generation_settings") or {}).get("n_ctx")),           # llama.cpp
        ("/api/v0/models", lambda d: next(
            (m.get("loaded_context_length") or m.get("max_context_length")
             for m in d.get("data", []) if m.get("id") == model and m.get("state", "loaded") == "loaded"), None)),   # LM Studio
    ):
        try:
            with urllib.request.urlopen(root + path, timeout=timeout) as resp:
                value = pick(json.loads(resp.read().decode("utf-8", "replace")))
            if value and int(value) >= 1024:
                return int(value)
        except Exception:  # noqa: BLE001 - not that kind of server, or not running
            continue
    return None


class Offloader:
    """One per turn. `apply` gives the view of the conversation the model is sent."""

    def __init__(self, window_tokens: int, reserve_tokens: Optional[int] = None, per_token: float = 0.0):
        self.window = max(2048, int(window_tokens or DEFAULT_WINDOW))
        # characters per token for this model, measured from what its server counted (0: the estimate)
        self.per_token = min(6.0, max(1.5, per_token)) if per_token else CHARS_PER_TOKEN
        reserve = reserve_tokens if reserve_tokens is not None else min(4096, int(self.window * 0.15))
        self.usable = max(1024, self.window - reserve)
        self.stale_now = False   # True: take stale readings out at once, whatever it costs the cache
        self.replaced: Dict[int, str] = {}
        self.dropped: Set[int] = set()
        self.journal: List[str] = []
        self.tier = "none"
        self.last: Dict[str, Any] = {}

    # ------------------------------------------------------------- scoring ---

    @staticmethod
    def _score(i: int, call: Dict[str, Any], content: str, later: Dict[str, Any]) -> int:
        """0-10: how well one line can stand in for this result. Higher goes first."""
        name, args = call.get("name", ""), call.get("args", {})
        failed = content.lstrip().startswith("ERROR")
        key = str(args.get("path") or args.get("cmd") or args.get("pattern") or args.get("symbol") or args.get("query") or "")
        again = later.get((name, key), -1) > i     # the same call was made again later
        if name == "read":
            return 10 if again else 6
        if name == "bash":
            if again:
                return 9
            return 2 if re.match(r"\[exit (?!0\])", content) else 6    # a failure that was not re-run is still news
        if name in ("glob", "grep", "tools_load", "todo_write", "todo_list") or name.startswith("code_"):
            return 8
        if failed:
            return 7 if again else 2
        if name in ("edit", "write_file"):
            return 4
        return 5

    # --------------------------------------------------------------- apply ---

    def apply(self, messages: List[Dict], calls: Dict[str, Dict[str, Any]], stale: Set[int],
              tools_chars: int = 0) -> List[Dict]:
        tool_at = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        protected = set(tool_at[-KEEP_RECENT:])
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
        call_of = {i: calls.get(messages[i].get("tool_call_id", ""), {}) for i in tool_at}

        # Readings made before the file changed are taken out when output is next moved out in a
        # batch, or at once where there is room to spare and nothing to lose by it (the caller says
        # which). Taking one out changes an early message, and the server then reads on from there.
        batch = self.stale_now or tokens_of(messages, tools_chars, self.per_token) >= self.usable * MILD
        for i in tool_at:   # readings made before the file changed, and command noise
            if i in self.replaced or i in self.dropped:
                continue
            content = str(messages[i].get("content") or "")
            call = call_of[i]
            if i in stale and not batch:
                continue
            if i in stale:
                self.replaced[i] = (f"[An earlier reading of {call.get('args', {}).get('path')}. The file was changed after "
                                    "this, so the text is out of date and is not shown. Read it again before you rely on it or edit it.]")
            elif call.get("name") == "bash" and len(content) > 2500:
                squeezed = squeeze(content)
                if squeezed is not content:
                    self.replaced[i] = squeezed

        def build() -> List[Dict]:
            out = []
            for i, m in enumerate(messages):
                if i in self.dropped:
                    continue
                out.append(dict(m, content=self.replaced[i]) if i in self.replaced else m)
            return out

        view = build()
        used = tokens_of(view, tools_chars, self.per_token)
        self.tier = "none"

        if used >= self.usable * MILD:
            later: Dict[Tuple[str, str], int] = {}
            for i in tool_at:
                args = call_of[i].get("args", {})
                key = str(args.get("path") or args.get("cmd") or args.get("pattern") or args.get("symbol") or args.get("query") or "")
                later[(call_of[i].get("name", ""), key)] = i
            candidates = []
            for i in tool_at:
                content = str(messages[i].get("content") or "")
                if i in protected or i in self.dropped or i in stale or len(self.replaced.get(i, content)) < 400:
                    continue
                if i in self.replaced and self.replaced[i].startswith("[Offloaded"):
                    continue
                candidates.append((self._score(i, call_of[i], content, later), i, content))
            for rung in LADDER:
                # the batch clears deep through what one line can stand in for; what is still news
                # (a failure nobody re-ran) goes only if the window is over the line without it
                goal = self.usable * (MILD_TARGET if rung >= 3 else MILD)
                if used < goal:
                    break
                for score, i, content in sorted(candidates, key=lambda c: (-c[0], c[1])):
                    if score < rung or (i in self.replaced and self.replaced[i].startswith("[Offloaded")):
                        continue
                    line = summarize(call_of[i].get("name", "tool"), call_of[i].get("args", {}), content)
                    stand_in = f"[Offloaded to save room: {line}. Run the tool again if you need the full result.]"
                    before = len(self.replaced.get(i, content))
                    self.replaced[i] = stand_in
                    used -= int((before - len(stand_in)) / self.per_token)
                    self.tier = "mild"
                    if used < goal:
                        break
            view = build()
            used = tokens_of(view, tools_chars, self.per_token)

        if used >= self.usable * AGGRESSIVE:
            # the oldest steps go whole: a call and its result are never separated
            groups: List[Tuple[int, List[int]]] = []
            for a, m in enumerate(messages):
                if a <= last_user or m.get("role") != "assistant" or not m.get("tool_calls") or a in self.dropped:
                    continue
                ids = {c.get("id") for c in m["tool_calls"]}
                results = [i for i in tool_at if messages[i].get("tool_call_id") in ids]
                if results and not any(i in protected for i in results):
                    groups.append((a, results))
            for a, results in groups:
                if used < self.usable * TARGET:
                    break
                freed = len(json.dumps(messages[a].get("tool_calls"))) + len(str(messages[a].get("content") or ""))
                for i in results:
                    content = str(messages[i].get("content") or "")
                    freed += len(self.replaced.get(i, content))
                    self.journal.append(summarize(call_of[i].get("name", "tool"), call_of[i].get("args", {}), content))
                    self.dropped.add(i)
                self.dropped.add(a)
                used -= int(freed / self.per_token)
                self.tier = "aggressive"
            view = build()
            used = tokens_of(view, tools_chars, self.per_token)

        if used >= self.usable * EMERGENCY:
            # still too full: cut the largest things short so the turn can go on
            sizes = sorted(((len(str(m.get("content") or "")), i) for i, m in enumerate(messages)
                            if i not in self.dropped and i != last_user and m.get("role") != "system"), reverse=True)
            for size, i in sizes:
                if used < self.usable * TARGET or size <= EMERGENCY_CHARS * 2:
                    break
                content = self.replaced.get(i, str(messages[i].get("content") or ""))
                if len(content) <= EMERGENCY_CHARS * 2:
                    continue
                cut = content[:EMERGENCY_CHARS] + f"\n[… cut short to fit the model's window; {len(content) - EMERGENCY_CHARS} more characters not shown]"
                self.replaced[i] = cut
                used -= int((len(content) - len(cut)) / self.per_token)
                self.tier = "emergency"
            view = build()
            used = tokens_of(view, tools_chars, self.per_token)

        self.last = {"tokens": used, "window": self.window, "fill": round(used / self.usable, 2), "tier": self.tier,
                     "offloaded": sum(1 for v in self.replaced.values() if v.startswith("[Offloaded")),
                     "dropped": len([i for i in self.dropped if i in tool_at])}
        return view

    def journal_lines(self, limit: int = 20) -> List[str]:
        if not self.journal:
            return []
        shown = self.journal[-limit:]
        lines = ["Earlier steps of this turn whose output was dropped to save room (run a tool again if you need it):"]
        lines += [f"{n}. {text[:160]}" for n, text in enumerate(shown, len(self.journal) - len(shown) + 1)]
        return lines
