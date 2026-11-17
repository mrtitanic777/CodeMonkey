"""Model servers that start with CodeMonkey.

A model server added in Settings > Models can carry a start command (its `start_command`, kept with
it in providers.json on this computer). When CodeMonkey opens, each one that is not answering yet is
started with that command, in the background. CodeMonkey does not wait for it: the model menu lists
the server's models once it answers. It is started on its own, outside CodeMonkey's group of
processes, so closing CodeMonkey leaves the model server running (it is often slow to load again).
What the command prints goes to <workspace>/webui/autostart-<name>.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List


def _start(command: str, log_path: str, cwd: str) -> subprocess.Popen:
    log = open(log_path, "a", encoding="utf-8")   # handed to the child; the child keeps it open
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}: {command}\n")
    log.flush()
    if sys.platform == "win32":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
        try:
            return subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        except OSError:   # a job that does not allow breaking away: start it without that
            flags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            return subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=flags)
    return subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)


class Autostart:
    """One per app: which servers were started, and how each start went."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.state: Dict[str, Dict[str, Any]] = {}   # name -> {"state": starting|running|already|failed, ...}
        self._lock = threading.Lock()

    def wanted(self) -> List[str]:
        return [name for name, entry in (self.app.config.providers or {}).items()
                if isinstance(entry, dict) and str(entry.get("start_command") or "").strip()]

    def start_all(self) -> None:
        """Start, in the background, every server with a start command that is not answering yet."""
        from .app import _listening

        for name in self.wanted():
            entry = self.app.config.providers[name]
            if _listening(entry.get("base_url") or ""):
                self._set(name, "already")
                continue
            path = os.path.join(self.app.webui_dir, f"autostart-{name}.log")
            try:
                proc = _start(entry["start_command"], path, os.path.expanduser("~"))
            except OSError as exc:
                self._set(name, "failed", error=str(exc))
                continue
            self._set(name, "starting", pid=proc.pid, log=path)
            threading.Thread(target=self._watch, args=(name, proc), daemon=True, name=f"autostart-{name}").start()

    def _watch(self, name: str, proc: subprocess.Popen, limit: float = 1800.0) -> None:
        """Until the server answers (a big model can take minutes to load), or its start command fails."""
        from .app import _listening

        url = self.app.config.providers.get(name, {}).get("base_url") or ""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if _listening(url) and self._answers(url):
                self._set(name, "running")
                self.app._available = None   # the model menu asks again
                self.app.bus.emit("settings", {})
                return
            code = proc.poll()
            if code not in (None, 0):
                self._set(name, "failed", error=f"its start command ended with code {code}")
                return
            time.sleep(3)
        self._set(name, "failed", error="it did not answer within 30 minutes")

    @staticmethod
    def _answers(base_url: str) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=5) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001 - not up yet
            return False

    def _set(self, name: str, state: str, **extra: Any) -> None:
        with self._lock:
            before = self.state.get(name) or {}
            kept = {k: before[k] for k in ("pid", "log") if k in before and state != "already"}
            self.state[name] = dict(kept, **extra, state=state, at=time.strftime("%H:%M:%S"))

    def status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self.state.items()}
