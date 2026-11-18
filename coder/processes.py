"""Long-running programs the agent starts and comes back to: dev servers, watchers, workers.

``bash`` runs one command and waits for it, so a server started there either blocks the turn or
dies when the call returns. Here a program is started in the background under a short name, its
output is kept (the last few thousand lines), and the agent reads it, waits for a line that says
it is ready, and stops it, together with everything it started. Every program a session started
is stopped when the session closes.
"""

from __future__ import annotations

import atexit
import collections
import re
import threading
import time
from typing import Callable, Dict, List, Optional

import procutil

MAX_RUNNING = 6
KEEP_LINES = 4000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class _Running:
    def __init__(self, name: str, command: str, proc) -> None:
        self.name, self.command, self.proc = name, command, proc
        self.started = time.time()
        self.ended: Optional[float] = None
        self.stopped = False     # stopped on purpose (process_stop): nobody needs telling it ended
        self.lines: "collections.deque[str]" = collections.deque(maxlen=KEEP_LINES)
        self.total = 0           # lines seen in all, so a reader can ask for what is new
        self.read_to = 0         # where the agent last read up to
        self.changed = threading.Condition()
        self.reader = threading.Thread(target=self._pump, name=f"proc-{name}", daemon=True)
        self.reader.start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                with self.changed:
                    self.lines.append(_ANSI.sub("", line.rstrip("\n")))
                    self.total += 1
                    self.changed.notify_all()
        except (OSError, ValueError):
            pass
        with self.changed:
            self.changed.notify_all()

    def state(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited with code {code}"

    def tail(self, count: int, new_only: bool) -> List[str]:
        with self.changed:
            lines = list(self.lines)
            first = self.total - len(lines)   # number of the oldest line still kept
            if new_only:
                lines = lines[max(0, self.read_to - first):]
            self.read_to = self.total
        return lines[-count:]

    def wait_for(self, pattern: "re.Pattern", seconds: float, after: int = 0) -> Optional[str]:
        """The first line numbered *after* or later that matches *pattern*, within *seconds*.

        Lines are numbered from the program's start, so an old "ready" from before is not taken
        for a new one.
        """
        deadline = time.monotonic() + seconds
        pos = after
        with self.changed:
            while True:
                lines = list(self.lines)
                first = self.total - len(lines)
                for i in range(max(pos, first), self.total):
                    if pattern.search(lines[i - first]):
                        return lines[i - first]
                pos = self.total
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return None
                self.changed.wait(min(left, 0.5))


class ProcessManager:
    """The background programs of one session."""

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.running: Dict[str, _Running] = {}
        self._lock = threading.Lock()
        #: called with a command moved to the background once it ends by itself, so the session can say so
        self.on_finish: Optional[Callable[[_Running], None]] = None
        atexit.register(self.stop_all)   # nothing outlives the program that started it

    def close(self) -> None:
        """Stop everything and forget this manager: the session that owned it has ended.

        The atexit entry is dropped too. Without it every session that ever ran a background
        command leaves one behind for the life of the app, and at exit they all run again over
        programs that are already gone.
        """
        atexit.unregister(self.stop_all)
        self.stop_all()
        self.running.clear()

    def start(self, command: str, name: str = "", wait_for: str = "", wait_seconds: float = 20) -> str:
        command = str(command or "").strip()
        if not command:
            return "ERROR: give the command to start."
        name = str(name or "").strip() or re.sub(r"[^A-Za-z0-9_.-]+", "-", command.split()[0])[:24] or "proc"
        if not _NAME.fullmatch(name):
            return "ERROR: name: letters, digits, '.', '_' and '-' only, up to 40."
        try:
            pattern = re.compile(wait_for, re.I) if wait_for else None
        except re.error as exc:
            return f"ERROR: wait_for is not a valid regular expression: {exc}"
        with self._lock:
            old = self.running.get(name)
            if old is not None and old.proc.poll() is None:
                return f"ERROR: {name!r} is already running ({old.command}). Stop it first or pick another name."
            if sum(1 for r in self.running.values() if r.proc.poll() is None) >= MAX_RUNNING:
                return f"ERROR: {MAX_RUNNING} programs are already running; stop one first."
            from coder import sandbox

            lvl = sandbox.level()
            refused = sandbox.check(command, self.project_dir, lvl)
            if refused:
                return f"ERROR: blocked by the command sandbox ({lvl}): {refused}."
            try:
                proc = procutil.start_shell(procutil.strip_own_cd(command, self.project_dir), cwd=self.project_dir,
                                            sandbox=lvl)
            except OSError as exc:
                return f"ERROR: could not start: {exc}"
            run = self.running[name] = _Running(name, command, proc)
        head = f"Started {name!r} (pid {proc.pid}): {command}"
        if pattern is not None:
            seconds = max(1.0, min(float(wait_seconds or 20), 120.0))
            hit = run.wait_for(pattern, seconds)
            if hit is not None:
                return head + f"\nReady: {hit}\n" + self._show(run, 30, True)
            if run.proc.poll() is not None:
                return head + f"\nERROR: it {run.state()} before printing anything matching {wait_for!r}.\n" + \
                    self._show(run, 60, False)
            return head + f"\nNot ready after {seconds:.0f}s (nothing matched {wait_for!r}); it is still running.\n" + \
                self._show(run, 30, True)
        time.sleep(1.5)   # what it says in its first moment is usually what matters
        return head + "\n" + self._show(run, 30, True)

    def run_or_detach(self, command: str, timeout: float, detach: threading.Event,
                      why: Callable[[], str] = lambda: "The user moved this command to the background") -> str:
        """Run a shell command and wait for it, like bash, unless *detach* is set while it runs
        (Ctrl+B, or a new message from the user): then it keeps running here as a background
        program, the wait ends, and on_finish is called when it ends by itself."""
        from coder import sandbox

        lvl = sandbox.level()
        refused = sandbox.check(command, self.project_dir, lvl)
        if refused:
            return (f"ERROR: blocked by the command sandbox ({lvl}): {refused}. Do it another way inside the "
                    "project, or tell the user what you need.")
        try:
            proc = procutil.start_shell(procutil.strip_own_cd(command, self.project_dir), cwd=self.project_dir,
                                        sandbox=lvl)
        except OSError as exc:
            return f"ERROR: could not start: {exc}"
        run = _Running("", command, proc)
        deadline = time.monotonic() + max(1.0, float(timeout))
        while proc.poll() is None:
            if detach.is_set():
                with self._lock:
                    n = 1 + sum(1 for k in self.running if k.startswith("bash-"))
                    run.name = f"bash-{n}"
                    self.running[run.name] = run
                threading.Thread(target=self._watch, args=(run,), name=f"watch-{run.name}", daemon=True).start()
                return (f"{why()} as {run.name!r}; it is still running.\n"
                        + self._show(run, 30, True)
                        + f"\nYou will be told when it finishes. Read more with process_logs(name={run.name!r}); "
                        "stop it with process_stop.")
            if time.monotonic() > deadline:
                procutil.stop_tree(proc)
                return f"ERROR: command timed out after {timeout:.0f}s and was stopped: {command[:200]}"
            time.sleep(0.1)
        run.reader.join(2)   # the last lines may still be on their way
        out = "\n".join(run.lines).strip() or "(no output)"
        from coder.tools import _truncate

        return f"[exit {proc.returncode}]\n" + _truncate(out)

    def _watch(self, run: _Running) -> None:
        """Wait for a command moved to the background to end, then say so (unless it was stopped)."""
        run.proc.wait()
        run.ended = time.time()
        run.reader.join(5)   # its last lines
        if not run.stopped and self.on_finish is not None:
            try:
                self.on_finish(run)
            except Exception:  # noqa: BLE001 - the program ended either way
                pass

    def describe(self) -> List[Dict[str, object]]:
        """What runs in the background, for the page."""
        return [{"name": r.name, "command": r.command, "state": r.state(), "lines": r.total,
                 "seconds": int(time.time() - r.started)} for r in self.running.values()]

    def logs(self, name: str, lines: int = 60, new_only: bool = True, wait_for: str = "",
             wait_seconds: float = 20) -> str:
        run = self.running.get(str(name or ""))
        if run is None:
            return self._unknown(name)
        if wait_for:
            try:
                hit = run.wait_for(re.compile(wait_for, re.I), max(1.0, min(float(wait_seconds or 20), 120.0)),
                                   after=run.read_to)   # only what it printed since the last look
            except re.error as exc:
                return f"ERROR: wait_for is not a valid regular expression: {exc}"
            if hit is None:
                return f"Nothing matched {wait_for!r} ({run.state()}).\n" + self._show(run, int(lines or 60), new_only)
        return self._show(run, int(lines or 60), new_only)

    def stop(self, name: str) -> str:
        run = self.running.get(str(name or ""))
        if run is None:
            return self._unknown(name)
        was = run.state()
        run.stopped = True
        procutil.stop_tree(run.proc)
        try:
            run.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        last = run.tail(10, True)
        return f"Stopped {name!r} (it was {was})." + ("\nLast output:\n" + "\n".join(last) if last else "")

    def listing(self) -> str:
        if not self.running:
            return "No background programs."
        rows = []
        for run in self.running.values():
            rows.append(f"{run.name}: {run.state()}, {int(time.time() - run.started)}s, "
                        f"{run.total} lines of output · {run.command}")
        return "\n".join(rows)

    def stop_all(self) -> None:
        for run in list(self.running.values()):
            procutil.stop_tree(run.proc)

    def _show(self, run: _Running, count: int, new_only: bool) -> str:
        count = max(1, min(count, 400))
        lines = run.tail(count, new_only)
        head = f"[{run.name}: {run.state()}]"
        if not lines:
            return head + " (no new output)" if new_only else head + " (no output yet)"
        return head + "\n" + "\n".join(line[:1000] for line in lines)

    def _unknown(self, name: str) -> str:
        known = ", ".join(self.running) or "none"
        return f"ERROR: no background program named {name!r} (running here: {known})."
