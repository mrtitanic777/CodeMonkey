"""What the agent has actually seen in a session, typed with the MSHMOE kernel's states.

The kernel's rule is that a completion is cheap and a promotion is expensive: nothing the model
says counts as known until something supports it. Here the support is the session's own evidence:
files the agent read, searches it ran, commands it ran and their output, and what you told it.

Two uses, both deterministic (no model call):

- ``Ledger.check_action`` types an action before it runs. An edit to a file the agent never read is
  a guess (CONFABULATED); a path that does not exist is an UNKNOWN_REFERENCE.
- ``Ledger.label_reply`` types the checkable claims in a reply: files it names, test results it
  reports, identifiers it quotes.

This is a claim checker, not a proof of correctness: KNOWN means "the agent saw this in the
session", not "this is true".
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

_KERNEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kernel"
)
if _KERNEL_DIR not in sys.path:
    # Appended, not inserted: kernel/ has its own tools/ and tests/ folders.
    sys.path.append(_KERNEL_DIR)

from mshmoe.states import SUPPORTED, State, combine  # noqa: E402

# Plain-language names for the states the interface shows.
LABELS = {
    State.KNOWN: "Checked",
    State.DERIVED: "Seen, not opened",
    State.CONFABULATED: "Guess",
    State.CONFLICTED: "Contradicts a result",
    State.UNKNOWN_REFERENCE: "Not found",
}

_SOURCE_EXT = (
    "py", "pyi", "js", "jsx", "ts", "tsx", "json", "md", "rst", "txt", "toml",
    "yaml", "yml", "cfg", "ini", "sh", "bat", "ps1", "c", "h", "cpp", "hpp",
    "cc", "rs", "go", "java", "kt", "cs", "rb", "php", "html", "css", "sql",
    "xml", "lock", "csv",
)
_PATH_RE = re.compile(
    r"(?<![\w./\\-])((?:[\w.-]+[/\\])*[\w.-]+\.(?:" + "|".join(_SOURCE_EXT) + r"))(?![\w/\\])"
)
_TICK_RE = re.compile(r"`([^`\n]{1,80})`")  # from 1: a lone `<` must not shift the pairing
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_COUNT_RE = re.compile(
    r"\b(\d+)\s+(?:of\s+\d+\s+)?(?:tests?\s+)?(pass(?:ed|ing|es)?|fail(?:ed|ing|ures?|s)?|errors?)\b",
    re.I,
)
_ALL_PASS_RE = re.compile(
    r"\b(?:all|every)\s+(?:the\s+)?(?:\d+\s+)?tests?\s+(?:now\s+)?pass|\btests?\s+(?:now\s+)?pass(?:es|ed)?\b"
    r"|\bsuite\s+(?:is\s+)?(?:now\s+)?(?:green|pass(?:es|ed|ing)?)",
    re.I,
)
_EXIT_RE = re.compile(r"^\[exit (-?\d+)\]")
# "I've fixed it", "is now updated": said in the present, so it must have happened in this turn.
_DID_RE = re.compile(
    r"\bI(?:'ve|\u2019ve| have)\s+(?:now\s+|just\s+|also\s+)?(?:fixed|updated|changed|added|implemented|created|"
    r"removed|refactored|modified|edited|rewritten|written|applied|replaced|renamed|deleted|patched|corrected)\b"
    r"|\b(?:is|are)\s+now\s+(?:fixed|updated|implemented|corrected|in place|resolved)\b"
    r"|\bhas\s+(?:now\s+)?been\s+(?:fixed|updated|changed|added|implemented|removed|corrected|applied|resolved)\b",
    re.I,
)
_RAN_RE = re.compile(
    r"\bI(?:'ve|\u2019ve| have)\s+(?:now\s+|just\s+|also\s+)?(?:re-?)?(?:run|ran|executed|tested|verified)\b"
    r"|\bI\s+just\s+(?:re-?)?(?:ran|executed|tested|verified)\b",
    re.I,
)
# tools that only look: everything else counts as having done something
_LOOKING = re.compile(r"^(read|glob|grep|tools_load|web_search|web_fetch|todo_list|show)$|^code_|_(list|search|status|get|read)$")
_FILE_MARK_RE = re.compile(r"\[File: ([^\]\n]+)\]")
_SEEN_CAP = 400_000  # characters of tool output kept for look-ups


def _norm(path: str) -> str:
    path = str(path or "").strip().strip("\"'").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lower() if os.name == "nt" else path


def command_key(cmd: str) -> str:
    """The part of a shell command an "always allow" rule is about: ``pytest``, ``git status``."""
    parts = str(cmd or "").strip().split()
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts = parts[1:]  # leading VAR=value
    if not parts:
        return ""
    head = os.path.basename(parts[0].strip("\"'")).lower()
    if head.endswith(".exe"):
        head = head[:-4]
    if head in ("python", "python3", "py") and len(parts) >= 3 and parts[1] == "-m":
        return f"{head} -m {parts[2]}"
    if head in ("git", "npm", "pnpm", "yarn", "pip", "cargo", "go", "docker", "dotnet", "uv") and len(parts) >= 2:
        if not parts[1].startswith("-"):
            return f"{head} {parts[1]}"
    return head


class Verdict:
    """The typed result of checking one action."""

    def __init__(self, state: State = State.KNOWN, reasons: Optional[List[str]] = None):
        self.state = state
        self.reasons = list(reasons or [])

    @property
    def supported(self) -> bool:
        return self.state in SUPPORTED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "label": LABELS.get(self.state, self.state.value),
            "reasons": self.reasons,
        }


class Ledger:
    """Evidence gathered in one session."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        self.read: set = set()      # files whose content the agent has seen
        self.listed: set = set()    # paths that appeared in a listing or a search
        self.changed: set = set()   # files the agent edited or wrote
        self.runs: List[Dict[str, Any]] = []
        self._seen: List[str] = []
        self.user_texts: List[str] = []
        self._seen_chars = 0
        self.tool_calls = 0
        # order of events, so a result can be told from one that is out of date
        self.clock = 0
        self.last_change_at = 0     # the last time a file was changed
        self.first_change_at = 0    # the first time one was changed in the current turn
        self.turn_mark = 0          # where the current turn began
        self.turn_actions = 0       # tool calls this turn that did something (not just looked)
        self.turn_runs = 0          # commands run this turn
        self.touched: List[str] = []   # files read or changed, most recent last

    # ---------------------------------------------------------- record ---

    def _keep(self, text: str) -> None:
        if not text:
            return
        self._seen.append(text)
        self._seen_chars += len(text)
        while self._seen_chars > _SEEN_CAP and len(self._seen) > 1:
            self._seen_chars -= len(self._seen.pop(0))

    def note_seen(self, text: str) -> None:
        """Text the agent was shown that is neither the user's words nor a tool result."""
        self._keep(str(text or ""))

    def note_user(self, text: str) -> None:
        """What you said, including files attached with @: their content was shown to the model."""
        self._keep(text)
        self.user_texts.append(str(text or "")[:4000])
        del self.user_texts[:-40]
        for rel in _FILE_MARK_RE.findall(text or ""):
            self.read.add(_norm(rel))

    def start_turn(self) -> None:
        self.turn_mark = self.clock
        self.turn_actions = 0
        self.turn_runs = 0

    def _touch(self, path: str) -> None:
        if path in self.touched:
            self.touched.remove(path)
        self.touched.append(path)
        del self.touched[:-30]

    def note_run(self, cmd: str, result: str, seconds: float = -1.0, gate: bool = False,
                 failing: Optional[set] = None) -> None:
        """A command was run. *gate*: the code gate ran it again by itself after a change."""
        from .codegate import failing_tests, is_test_command

        self.clock += 1
        result = str(result or "")
        m = _EXIT_RE.match(result)
        code = int(m.group(1)) if m else None
        tests = is_test_command(cmd)
        self.runs.append({
            "cmd": str(cmd), "exit": code, "text": result, "at": self.clock, "seconds": seconds,
            "tests": tests, "gate": gate,
            "failing": sorted(failing if failing is not None else failing_tests(result, code)) if tests else [],
        })
        del self.runs[:-60]
        if gate:
            self._keep(result)

    def tests_stale(self) -> bool:
        """True when files were changed after the last test run (or tests were never run)."""
        last = next((r for r in reversed(self.runs) if r.get("tests")), None)
        return last is None or last["at"] < self.last_change_at

    def note_tool(self, name: str, args: Dict[str, Any], result: str, seconds: float = -1.0) -> None:
        args = args or {}
        result = str(result or "")
        failed = result.lstrip().startswith("ERROR")
        self.tool_calls += 1
        self.clock += 1
        if not failed and not _LOOKING.search(name):
            self.turn_actions += 1
        path = _norm(args.get("path", ""))
        if path and not failed and name in ("read", "edit", "write_file"):
            self._touch(path)
        if name == "read" and path and not failed:
            self.read.add(path)
        elif name in ("glob", "grep") and not failed:
            for found in _PATH_RE.findall(result):
                self.listed.add(_norm(found))
        elif name in ("edit", "write_file") and path and not failed:
            self.changed.add(path)
            if not path.endswith((".md", ".rst", ".txt", ".csv", ".lock", ".log")):
                self.last_change_at = self.clock   # prose does not put a test result out of date
                if self.first_change_at <= self.turn_mark:
                    self.first_change_at = self.clock   # the first change of this turn
            self.read.add(path)  # it now knows what the file holds there
            self._keep(str(args.get("new_text", "")) + str(args.get("content", "")))
        elif name == "bash" and failed:
            pass   # refused, or it never started: nothing was run, so there is no result to stand on
        elif name == "bash":
            self.clock -= 1   # note_run counts it
            self.turn_runs += 1
            self.note_run(str(args.get("cmd", "")), result, seconds)
        self._keep(result)

    def vouch(self, name: str, args: Dict[str, Any]) -> None:
        """You approved this action yourself: the file is no longer a guess."""
        path = _norm((args or {}).get("path", ""))
        if name in ("edit", "write_file") and path:
            self.read.add(path)

    def seen_texts(self) -> List[str]:
        return list(self._seen)

    # ----------------------------------------------------------- lookup ---

    def _exists(self, rel: str) -> bool:
        target = os.path.abspath(os.path.join(self.project_dir, rel))
        try:
            inside = os.path.commonpath([self.project_dir, target]) == self.project_dir
        except ValueError:
            inside = False
        return inside and os.path.exists(target)

    def _basename_known(self, rel: str, pool: set) -> bool:
        if rel in pool:
            return True
        if "/" in rel:
            return any(p.endswith("/" + rel) for p in pool)
        return any(p == rel or p.endswith("/" + rel) for p in pool)

    def _in_seen(self, needle: str) -> bool:
        return any(needle in chunk for chunk in self._seen)

    # ----------------------------------------------------------- actions ---

    def check_action(self, name: str, args: Dict[str, Any]) -> Verdict:
        args = args or {}
        if name == "edit":
            path = _norm(args.get("path", ""))
            if not path:
                return Verdict()
            if not self._exists(path):
                return Verdict(State.UNKNOWN_REFERENCE, [f"{args.get('path')} does not exist in this folder"])
            if path not in self.read:
                return Verdict(
                    State.CONFABULATED,
                    [f"the agent has not read {args.get('path')} in this session, so this edit is a guess"],
                )
            return Verdict()
        if name == "write_file":
            path = _norm(args.get("path", ""))
            if path and self._exists(path) and path not in self.read:
                return Verdict(
                    State.CONFABULATED,
                    [f"this would overwrite {args.get('path')}, which the agent has not read in this session"],
                )
            return Verdict()
        if name == "bash":
            cmd = str(args.get("cmd", ""))
            missing = []
            tokens = cmd.replace("|", " ").replace(";", " ").replace("&&", " ").split()
            skip = False
            for tok in tokens:
                if skip:  # the file a redirect or -o writes to may not exist yet
                    skip = False
                    continue
                if tok in (">", ">>", "-o", "--output") or tok.startswith(">"):
                    skip = tok in (">", ">>", "-o", "--output")
                    continue
                if "://" in tok or "=" in tok:
                    continue
                tok = tok.strip("\"'").split("::")[0]
                m = _PATH_RE.fullmatch(tok)
                if m and not os.path.isabs(tok) and not self._exists(_norm(tok)):
                    missing.append(tok)
            if missing and not re.match(r"\s*(touch|mkdir|echo|type nul|copy|cp|mv|move|ren)\b", cmd):
                return Verdict(
                    State.UNKNOWN_REFERENCE,
                    [f"the command names {m}, which does not exist in this folder" for m in missing[:3]],
                )
            return Verdict()
        return Verdict()

    # ------------------------------------------------------------ replies ---

    def _file_claim(self, shown: str) -> Optional[Dict[str, Any]]:
        rel = _norm(shown)
        if self._basename_known(rel, self.changed):
            return self._claim(shown, "file", State.KNOWN, "the agent changed this file in this session")
        if self._basename_known(rel, self.read):
            return self._claim(shown, "file", State.KNOWN, "the agent read this file in this session")
        if self._basename_known(rel, self.listed):
            return self._claim(shown, "file", State.DERIVED, "it appeared in a search, but was not opened")
        if self._exists(rel):
            return self._claim(shown, "file", State.DERIVED, "the file exists, but was not opened in this session")
        if self._in_seen(shown) or self._in_seen(shown.replace("\\", "/")):
            return self._claim(shown, "file", State.DERIVED, "it was mentioned in the session, but not opened")
        if "/" not in rel and not self.tool_calls:
            return None  # a bare file name in a plain chat: nothing to check it against
        return self._claim(shown, "file", State.UNKNOWN_REFERENCE, "no such file was found in this folder")

    @staticmethod
    def _claim(text: str, kind: str, state: State, why: str) -> Dict[str, Any]:
        return {
            "text": text,
            "kind": kind,
            "state": state.value,
            "label": LABELS.get(state, state.value),
            "why": why,
        }

    def label_reply(self, reply: str) -> Dict[str, Any]:
        """Type the checkable claims in *reply*. Returns {"state", "label", "claims", "counts"}."""
        reply = str(reply or "")
        prose = re.sub(r"```.*?```", " ", reply, flags=re.S)  # code blocks are content, not claims
        claims: List[Dict[str, Any]] = []
        done: set = set()

        for shown in _PATH_RE.findall(prose):
            key = ("file", _norm(shown))
            if key in done:
                continue
            done.add(key)
            claim = self._file_claim(shown)
            if claim:
                claims.append(claim)

        last_run = self.runs[-1] if self.runs else None
        for m in _COUNT_RE.finditer(prose):
            number, word = m.group(1), m.group(2).lower()
            key = ("count", number, word[:4])
            if key in done:
                continue
            done.add(key)
            shown = m.group(0)
            if last_run is None:
                claims.append(self._claim(shown, "result", State.CONFABULATED, "no command was run in this session"))
                continue
            stem = "pass" if word.startswith("pass") else "fail" if word.startswith("fail") else "error"
            backed = any(
                re.search(r"\b" + number + r"\s+" + stem, run["text"], re.I) for run in self.runs
            )
            if backed:
                claims.append(self._claim(shown, "result", State.KNOWN, "this number is in the output of a command it ran"))
            else:
                claims.append(
                    self._claim(shown, "result", State.CONFABULATED, "this number is not in the output of any command it ran")
                )

        m = _ALL_PASS_RE.search(prose)
        if m and ("count", "all") not in done:
            shown = m.group(0)
            if last_run is None:
                claims.append(self._claim(shown, "result", State.CONFABULATED, "no command was run in this session"))
            elif last_run["exit"] not in (0, None) or re.search(r"\b[1-9]\d*\s+failed\b", last_run["text"]):
                claims.append(
                    self._claim(shown, "result", State.CONFLICTED, "the last command it ran did not succeed")
                )
            else:
                claims.append(self._claim(shown, "result", State.KNOWN, "the last command it ran succeeded"))

        if self.tool_calls:  # identifiers are only checkable against something the agent looked at
            for quoted in _TICK_RE.findall(prose):
                quoted = quoted.strip().rstrip("()")
                if not _IDENT_RE.fullmatch(quoted) or _PATH_RE.fullmatch(quoted):
                    continue
                key = ("name", quoted)
                if key in done:
                    continue
                done.add(key)
                if self._in_seen(quoted) or self._in_seen(quoted.split(".")[-1]):
                    claims.append(self._claim(quoted, "name", State.KNOWN, "this name is in something the agent read or ran"))
                else:
                    claims.append(
                        self._claim(quoted, "name", State.CONFABULATED, "this name is not in anything the agent read or ran")
                    )

        stale = bool(self.runs) and self.last_change_at > self.runs[-1].get("at", 0)
        if stale:
            for c in claims:
                if c["kind"] == "result" and c["state"] == State.KNOWN.value and "fail" not in c["text"].lower():
                    c.update(self._claim(c["text"], "result", State.CONFLICTED,
                                         "files were changed after the last command was run, so this is out of date"))
        # things said to have been done just now, in a turn where nothing was done
        m = _DID_RE.search(prose)
        if m and self.turn_actions == 0:
            claims.append(self._claim(m.group(0), "action", State.CONFABULATED,
                                      "nothing was changed in this turn"))
        m = _RAN_RE.search(prose)
        if m and self.turn_runs == 0 and self.turn_actions == 0 and self.clock == self.turn_mark:
            claims.append(self._claim(m.group(0), "action", State.CONFABULATED,
                                      "no tool was used in this turn"))

        if claims:
            state, _ = combine([(State(c["state"]), ()) for c in claims])
        elif self.tool_calls:
            state = State.KNOWN
        else:
            state = State.CONFABULATED  # a plain answer: the model's own knowledge, unchecked
        counts: Dict[str, int] = {}
        for c in claims:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        return {
            "state": state.value,
            "label": LABELS.get(state, state.value),
            "claims": claims[:40],
            "counts": counts,
            "checked_against": {
                "files_read": len(self.read),
                "commands_run": len(self.runs),
                "tool_calls": self.tool_calls,
            },
        }
