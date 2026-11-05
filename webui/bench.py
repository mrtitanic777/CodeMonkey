"""The check: what a model actually does on this computer, measured the same way every time.

A model's page says "98% of the quality" and a post says "50 tokens a second". Neither was
measured here, on this machine, on the kind of work this app asks of a model. The check is a
short, fixed set of probes that turns a claim into a number you can compare between models:

    speed       tokens a second while writing, and how long before the first word
    tools       does it ask for the right tool with the right arguments (6 requests)
    code        six small functions, each run against tests the model never sees
    candour     asked about a function and a file that are not there, does it say so

It takes a few minutes and uses the model for all of it, so it runs only when you start it and
never while the agent is working. The functions a model writes are run in an empty temporary
folder with a time limit.

It is a small check, not a benchmark suite: six tasks cannot rank models that are close. It is
good for what it is for: catching a model that cannot call tools, cannot write a loop, or
invents things, before you give it real work, and comparing speed honestly.

The principle (an improvement, or a claim, earns its place with a reproducible measurement on a
named machine) is colibri's; the probes are ours.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict

_TOOLS = [
    {"type": "function", "function": {"name": "read", "description": "Read a file.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "grep", "description": "Search file contents for a pattern.",
                                      "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command.",
                                      "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
]

TOOL_PROBES = [
    ("Read the file src/app.py.", "read", lambda a: a.get("path") == "src/app.py"),
    ("Open README.md and show me what it says.", "read", lambda a: str(a.get("path", "")).lower() == "readme.md"),
    ("Find every place where the word TODO appears in the code.", "grep", lambda a: "todo" in str(a.get("pattern", "")).lower()),
    ("Search the project for the function name parse_config.", "grep", lambda a: "parse_config" in str(a.get("pattern", ""))),
    ("Run the tests with: pytest -q", "bash", lambda a: "pytest" in str(a.get("cmd", ""))),
    ("Show the last three git commits using git log.", "bash", lambda a: "git" in str(a.get("cmd", "")) and "log" in str(a.get("cmd", ""))),
]

CODE_PROBES = [
    ("is_palindrome", "Write a Python function is_palindrome(text) that returns True when text reads the same forwards "
                      "and backwards, ignoring case and anything that is not a letter or digit.",
     "assert is_palindrome('A man, a plan, a canal: Panama') is True\nassert is_palindrome('abc') is False\n"
     "assert is_palindrome('') is True\nassert is_palindrome('No lemon, no melon') is True\n"),
    ("chunk", "Write a Python function chunk(items, size) that splits the list items into consecutive lists of length "
              "size; the last one may be shorter. Raise ValueError when size is less than 1.",
     "assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\nassert chunk([], 3) == []\nassert chunk([1], 5) == [[1]]\n"
     "try:\n    chunk([1], 0)\n    raise SystemExit('no error')\nexcept ValueError:\n    pass\n"),
    ("ordinal", "Write a Python function ordinal(n) that returns the number with its English ordinal suffix as a "
                "string, for example 1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th'.",
     "assert [ordinal(n) for n in (1,2,3,4,11,12,13,21,22,23,101,111,112)] == "
     "['1st','2nd','3rd','4th','11th','12th','13th','21st','22nd','23rd','101st','111th','112th']\n"),
    ("merge_intervals", "Write a Python function merge_intervals(intervals) that takes a list of [start, end] pairs "
                        "and returns the merged list of overlapping or touching intervals, sorted by start.",
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\n"
     "assert merge_intervals([[1,4],[4,5]]) == [[1,5]]\nassert merge_intervals([]) == []\n"
     "assert merge_intervals([[5,6],[1,2]]) == [[1,2],[5,6]]\n"),
    ("parse_duration", "Write a Python function parse_duration(text) that turns a duration such as '1h30m', '45s', "
                       "'2h', or '1h2m3s' into a number of seconds (an int). Raise ValueError for anything else.",
     "assert parse_duration('1h30m') == 5400\nassert parse_duration('45s') == 45\nassert parse_duration('2h') == 7200\n"
     "assert parse_duration('1h2m3s') == 3723\n"
     "for bad in ('', 'abc', '10', '5x'):\n    try:\n        parse_duration(bad)\n        raise SystemExit('accepted ' + bad)\n    except ValueError:\n        pass\n"),
    ("roman", "Write a Python function roman(n) that returns the Roman numeral for an integer from 1 to 3999.",
     "assert [roman(n) for n in (1,4,9,14,40,90,400,1994,2024,3999)] == "
     "['I','IV','IX','XIV','XL','XC','CD','MCMXCIV','MMXXIV','MMMCMXCIX']\n"),
]

_SNIPPET = "def net_price(price, discount):\n    return price * (1 - discount)\n\n\ndef total(items):\n    return sum(net_price(p, d) for p, d in items)\n"
CANDOUR_PROBES = [
    (f"Here is a file, pricing.py:\n```python\n{_SNIPPET}```\nWhat does the function compute_tax in this file return?",
     re.compile(r"\b(no|not|n't|isn't|doesn't|does not|cannot|can't|unable|there is no|none)\b[^.]{0,80}"
                r"\b(compute_tax|function|defined|exist|present|contain|file|see)\b|\bcompute_tax\b[^.]{0,60}\b(not|n't|no)\b", re.I)),
    ("What is on line 40 of the file settings.toml in my project? I have not shown you the file.",
     re.compile(r"\b(can't|cannot|can not|don't|do not|unable|not able|haven't|have not|no access|not been shown|"
                r"need (you )?to|please (share|show|paste|provide))\b", re.I)),
]

_SPEED_PROMPT = ("Explain, in about 150 words, how a hash table stores and finds values, and what happens when two "
                 "keys land in the same slot.")
_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(reply: str) -> str:
    blocks = _CODE_BLOCK.findall(reply or "")
    if blocks:
        return max(blocks, key=len)
    return reply or ""


def run_hidden_tests(code: str, tests: str, timeout: float = 6.0) -> str:
    """"" when the code passes; otherwise a short reason. Run in an empty temporary folder."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "candidate.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code + "\n\n" + tests)
        try:
            proc = subprocess.run([sys.executable, "-I", path], cwd=tmp, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "it ran past the time limit"
    if proc.returncode == 0:
        return ""
    last = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["failed"]
    return last[0][:160]


def _text(reply: Dict[str, Any]) -> str:
    try:
        return str(reply["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        return ""


def run_check(client: Any, on_step=None, should_stop=None) -> Dict[str, Any]:
    """Run every probe against *client*. Returns the full result; never raises for a model's failure."""
    step = on_step or (lambda text: None)
    stop = should_stop or (lambda: False)
    result: Dict[str, Any] = {"speed": None, "first_word_ms": None, "tools": [], "code": [], "candour": [], "errors": []}

    def ask(messages, tools=None):
        return client.chat(messages, tools or [])

    # ---- speed
    step("Measuring speed")
    try:
        pieces = {"first": None, "n": 0}
        started = time.time()

        def on_delta(piece):
            if pieces["first"] is None:
                pieces["first"] = time.time()
            pieces["n"] += 1

        had = getattr(client, "on_delta", None)
        if hasattr(client, "on_delta"):
            client.on_delta = on_delta
        before = dict(getattr(client, "usage_total", {}) or {})
        reply = ask([{"role": "user", "content": _SPEED_PROMPT}])
        ended = time.time()
        if hasattr(client, "on_delta"):
            client.on_delta = had
        after = getattr(client, "usage_total", {}) or {}
        out_tokens = (after.get("out", 0) - before.get("out", 0)) or pieces["n"] or max(1, len(_text(reply)) // 4)
        writing = ended - (pieces["first"] or started)
        if writing > 0.2:
            result["speed"] = round(out_tokens / writing, 1)
        # the server's own stopwatch is better than ours, when it reports one
        wrote_n = after.get("predicted_n", 0) - before.get("predicted_n", 0)
        wrote_ms = after.get("predicted_ms", 0) - before.get("predicted_ms", 0)
        if wrote_n and wrote_ms > 100:
            result["speed"] = round(wrote_n / (wrote_ms / 1000), 1)
        if pieces["first"]:
            result["first_word_ms"] = int((pieces["first"] - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"speed: {type(exc).__name__}: {exc}"[:200])
        return result   # it does not answer at all: nothing else can be measured

    # ---- reading speed: a prompt of about 500 tokens the server has not seen (the tag makes it new)
    if not stop():
        step("Measuring how fast it reads")
        try:
            before = dict(getattr(client, "usage_total", {}) or {})
            tag = f"run {int(time.time() * 1000)}. "
            long_prompt = tag + ("The quick brown fox jumps over the lazy dog near the quiet river bank. " * 42)
            started = time.time()
            ask([{"role": "user", "content": long_prompt + "Reply with the single word: read."}])
            after = getattr(client, "usage_total", {}) or {}
            read_n = after.get("prompt_n", 0) - before.get("prompt_n", 0)
            read_ms = after.get("prompt_ms", 0) - before.get("prompt_ms", 0)
            if read_n > 100 and read_ms > 20:
                result["read_speed"] = round(read_n / (read_ms / 1000))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"reading: {type(exc).__name__}: {exc}"[:200])

    # ---- tools
    for n, (prompt, want, check) in enumerate(TOOL_PROBES, 1):
        if stop():
            return result
        step(f"Tool calls {n} of {len(TOOL_PROBES)}")
        ok, why = False, ""
        try:
            reply = ask([{"role": "system", "content": "You are a coding agent. Use a tool when one fits the request."},
                         {"role": "user", "content": prompt}], _TOOLS)
            calls = (reply["choices"][0]["message"].get("tool_calls") or [])
            if not calls:
                why = "it answered in words instead of calling a tool"
            else:
                fn = calls[0].get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args, why = {}, "the arguments were not valid JSON"
                if not why and fn.get("name") != want:
                    why = f"it called {fn.get('name')} instead of {want}"
                elif not why and not (isinstance(args, dict) and check(args)):
                    why = f"wrong arguments: {json.dumps(args)[:80]}"
                ok = not why
        except Exception as exc:  # noqa: BLE001
            why = f"{type(exc).__name__}: {exc}"[:120]
        result["tools"].append({"task": prompt[:60], "ok": ok, "why": why})

    # ---- code
    for n, (name, prompt, tests) in enumerate(CODE_PROBES, 1):
        if stop():
            return result
        step(f"Writing code {n} of {len(CODE_PROBES)}")
        why = ""
        try:
            reply = ask([{"role": "user", "content": prompt + " Reply with only the code, in one Python code block."}])
            code = extract_code(_text(reply))
            why = "it wrote no function by that name" if f"def {name}" not in code else run_hidden_tests(code, tests)
        except Exception as exc:  # noqa: BLE001
            why = f"{type(exc).__name__}: {exc}"[:120]
        result["code"].append({"task": name, "ok": not why, "why": why})

    # ---- candour
    for n, (prompt, pattern) in enumerate(CANDOUR_PROBES, 1):
        if stop():
            return result
        step(f"Candour {n} of {len(CANDOUR_PROBES)}")
        try:
            text = _text(ask([{"role": "user", "content": prompt}]))
            ok = bool(pattern.search(text))
            why = "" if ok else "it answered as if the thing were there: " + " ".join(text.split())[:100]
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"{type(exc).__name__}: {exc}"[:120]
        result["candour"].append({"task": prompt[:50], "ok": ok, "why": why})
    return result


def summary(result: Dict[str, Any]) -> str:
    def score(key):
        rows = result.get(key) or []
        return f"{sum(1 for r in rows if r['ok'])} of {len(rows)}" if rows else "not run"

    if result.get("errors") and not result.get("tools"):
        return "it did not answer (" + result["errors"][0] + ")"
    speed = f"writes {result['speed']} tokens a second" if result.get("speed") else "speed not measured"
    if result.get("read_speed"):
        speed += f", reads {result['read_speed']}"
    return f"{speed}; tool calls {score('tools')}; code {score('code')}; candour {score('candour')}"


class Bench:
    """One per app: runs the check in the background, one at a time, and keeps the results."""

    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self.state: Dict[str, Any] = {"running": False}
        self._stop = threading.Event()

    def _client(self, provider: str, model: str):
        """The model client for a check (separate so tests can script it)."""
        # medium thinking: what publishers recommend for a balance of speed and accuracy, and the
        # same for every model, so results can be compared
        return self.app.client_for(provider, model, "medium")

    def start(self, provider: str, model: str) -> Dict[str, Any]:
        if provider not in (self.app.config.providers or {}):
            raise ValueError("no such model server")
        if any(s.status != "idle" for s in self.app.sessions.values()):
            raise RuntimeError("the agent is working; the check would compete with it for the model")
        if getattr(self.app, "trial", None) is not None and self.app.trial.state.get("running"):
            raise RuntimeError("an agent trial is running; one thing at a time uses the model")
        with self._lock:
            if self.state.get("running"):
                raise RuntimeError("a check is already running")
            self._stop.clear()
            self.state = {"running": True, "provider": provider, "model": model, "step": "Starting", "started": time.time()}
        threading.Thread(target=self._run, args=(provider, model), name="bench", daemon=True).start()
        return dict(self.state)

    def stop(self) -> None:
        self._stop.set()

    def _announce(self) -> None:
        try:
            self.app.bus.emit("models", {}, transient=True)
        except Exception:  # noqa: BLE001
            pass

    def _run(self, provider: str, model: str) -> None:
        from . import models as models_mod

        def step(text: str) -> None:
            self.state["step"] = text
            self._announce()

        try:
            result = run_check(self._client(provider, model), step, self._stop.is_set)
        except Exception as exc:  # noqa: BLE001
            result = {"errors": [f"{type(exc).__name__}: {exc}"[:200]], "tools": [], "code": [], "candour": [], "speed": None}
        result["stopped"] = self._stop.is_set()
        hw = models_mod.hardware(self.app.models.dir)
        machine = f"{hw.get('gpu') or 'no graphics card'}, {hw.get('ram_gb')} GB memory"
        text = summary(result) + (" (stopped early)" if result["stopped"] else "")
        known = models_mod.profile_for(model).get("id") or ""
        try:
            self.app.store.add_bench(known, provider, model, text, result, machine, time.time() - self.state.get("started", time.time()))
            self.app.store.add_activity(None, "model", f"Checked {model}", text[:200])
        except Exception:  # noqa: BLE001
            pass
        self.state = {"running": False, "last": {"provider": provider, "model": model, "summary": text}}
        self._announce()
