"""The integrated terminal: commands you type yourself, run in the session's folder.

Not a full terminal emulator: each line runs to completion in the user's shell (PowerShell on
Windows, sh elsewhere), its output collected for the page to poll. ``cd`` is handled here so the
folder carries over between lines, as it does in a real terminal. One command runs at a time;
Stop ends it with everything it started. When a command fails (and wasn't stopped), a "fail" line
follows its end: why, in plain words (explain.py), the command and the last of its output, for
the page's "Fix it for me" card.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

from .explain import ANSI

MAX_LINES = 5000
FAIL_TAIL = 80   # lines of a failed command's output handed to the card (and to the agent)


class Terminal:
    def __init__(self, cwd: str) -> None:
        self.root = os.path.realpath(cwd)
        self.cwd = self.root
        self.lines: List[Dict[str, Any]] = []   # {"n", "kind": "cmd"|"out"|"err"|"end"|"fail", "text"}
        self.seq = 0
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.stopped = False

    # ------------------------------------------------------------------ output ---
    def _add(self, kind: str, text: str, **extra: Any) -> None:
        with self.lock:
            self.seq += 1
            self.lines.append(dict({"n": self.seq, "kind": kind, "text": text}, **extra))
            if len(self.lines) > MAX_LINES:
                del self.lines[: len(self.lines) - MAX_LINES]

    def read(self, after: int = 0) -> Dict[str, Any]:
        with self.lock:
            lines = [l for l in self.lines if l["n"] > after]
        return {"lines": lines, "running": self.running, "cwd": self.cwd}

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------- input ---
    def run(self, command: str) -> None:
        command = str(command or "").strip()
        if not command:
            return
        if self.running:
            raise RuntimeError("a command is still running: stop it first")
        self._add("cmd", command)
        if command == "cd" or command.startswith(("cd ", "cd\t")):
            self._cd(command[2:].strip().strip('"').strip("'"))
            return
        if command in ("clear", "cls"):
            with self.lock:
                self.lines = []
            return
        if sys.platform == "win32":
            # PowerShell reports any failure as 1; pass a program's own exit code through (a crash's, say)
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command +
                    "\n$cmOk = $?; $cmCode = $LASTEXITCODE; if ($cmCode) { exit $cmCode } elseif (-not $cmOk) { exit 1 }"]
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            argv = [os.environ.get("SHELL") or "/bin/sh", "-c", command]
            flags = 0
        self.stopped = False
        try:
            self.proc = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=flags, start_new_session=sys.platform != "win32")
        except OSError as exc:
            self._add("err", str(exc))
            self._add("end", "exit 127")
            return
        threading.Thread(target=self._pump, args=(self.proc, command), daemon=True).start()

    def _pump(self, proc: subprocess.Popen, command: str) -> None:
        assert proc.stdout is not None
        tail: List[str] = []
        for raw in iter(proc.stdout.readline, b""):
            text = ANSI.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            tail.append(text)
            del tail[:-FAIL_TAIL]
            self._add("out", text)
        code = proc.wait()
        self._add("end", f"exit {code}")
        if code and not self.stopped:
            from .explain import explain

            output = "\n".join(tail)
            why = explain(command, output, code)
            if why.get("file"):   # the file pane opens it from here, whichever folder the command ran in
                why["file"] = os.path.normpath(os.path.join(self.cwd, why["file"]))
            self._add("fail", f"exit {code}", command=command, cwd=self.cwd, code=code, output=output, why=why)

    def _cd(self, where: str) -> None:
        target = os.path.expanduser(where) if where else self.root
        target = os.path.realpath(os.path.join(self.cwd, target))
        if os.path.isdir(target):
            self.cwd = target
        else:
            self._add("err", f"cd: no such folder: {where}")

    def stop(self) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        self.stopped = True
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return True
