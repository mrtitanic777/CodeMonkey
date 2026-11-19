"""Using CodeMonkey from anywhere: a private https address through Tailscale.

Tailscale joins this computer and the phone into a private network of their own (a "tailnet").
`tailscale serve` then gives this computer an https address only those devices can open, such as
https://my-pc.tail1234.ts.net, and passes what arrives there on to the app on this computer.

The address stays the same from one start to the next, so a phone can keep it on its Home Screen.
It is https, so the phone lets a page use the microphone (voice calls). A device that opens it still
has to pair first, like one on the Wi-Fi: requests that come through Tailscale are never treated
as this computer's own (server.py, _forwarded).

Tailscale has to be installed and signed in on both devices, and the tailnet has to allow https
addresses (Tailscale asks for that once, on its own site: the link is shown in Settings).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

DOWNLOAD = "https://tailscale.com/download"
#: How long a start waits for Tailscale to say "on" or "needs enabling" before reporting back;
#: the command itself goes on waiting (for the user to enable https) for WAIT_FOR_ENABLE.
FIRST_ANSWER = 8.0
WAIT_FOR_ENABLE = 15 * 60
_URL = re.compile(r"https://login\.tailscale\.com/\S+")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def program() -> str:
    """The tailscale command-line program, or ""."""
    found = shutil.which("tailscale")
    if found:
        return found
    for path in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe"),
                 "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale",
                 "/usr/local/bin/tailscale"):
        if os.path.isfile(path):
            return path
    return ""


def _run(args: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run([program()] + args, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)


class Anywhere:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.port: Optional[int] = None
        self.on = False            # serving through Tailscale now
        self.state = "off"         # off | starting | enable | on | error
        self.message = ""
        self.enable_link = ""
        self.host = ""             # this computer's Tailscale name, e.g. my-pc.tail1234.ts.net
        self._proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------ look ---

    def look(self) -> Dict[str, Any]:
        """What Tailscale on this computer is like: installed, signed in, its name."""
        if not program():
            return {"installed": False}
        try:
            done = _run(["status", "--json"])
            data = json.loads(done.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            return {"installed": True, "running": False}
        me = data.get("Self") or {}
        name = str(me.get("DNSName") or "").rstrip(".")
        return {"installed": True, "running": data.get("BackendState") == "Running", "host": name}

    def status(self) -> Dict[str, Any]:
        found = self.look() if self.state != "on" else {"installed": True, "running": True, "host": self.host}
        with self._lock:
            return {
                **found,
                "state": self.state,
                "on": self.on,
                "message": self.message,
                "enable_link": self.enable_link,
                "address": f"https://{self.host}" if self.on and self.host else "",
                "download": DOWNLOAD,
            }

    # ----------------------------------------------------------- start ---

    def start(self, wait: float = FIRST_ANSWER) -> Dict[str, Any]:
        """Serve this app at the computer's Tailscale https address. Returns the status soon; when
        Tailscale first needs https allowed, the command keeps waiting in the background and the
        address comes on by itself once the user has allowed it."""
        if self.port is None:
            raise RuntimeError("the app is not serving yet")
        found = self.look()
        if not found.get("installed"):
            raise RuntimeError("Tailscale is not installed on this computer")
        if not found.get("running"):
            raise RuntimeError("Tailscale is not signed in or not running on this computer")
        self._stop_waiting()
        with self._lock:
            self.host = found.get("host") or ""
            self.state, self.message, self.enable_link = "starting", "", ""
        try:
            proc = subprocess.Popen(
                [program(), "serve", "--bg", "--yes", f"http://127.0.0.1:{self.port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                creationflags=_NO_WINDOW)
        except OSError as exc:
            raise RuntimeError(f"could not run Tailscale ({exc})") from exc
        self._proc = proc
        answered = threading.Event()
        threading.Thread(target=self._follow, args=(proc, answered), name="tailscale-serve", daemon=True).start()
        answered.wait(wait)
        return self.status()

    def _follow(self, proc: subprocess.Popen, answered: threading.Event) -> None:
        said: List[str] = []
        killer = threading.Timer(WAIT_FOR_ENABLE, proc.kill)
        killer.daemon = True
        killer.start()
        try:
            for line in proc.stdout or []:
                said.append(line.strip())
                link = _URL.search(line)
                if link:
                    with self._lock:
                        self.state, self.enable_link = "enable", link.group(0)
                        self.message = "Tailscale needs https addresses allowed on your tailnet, once."
                    answered.set()
            code = proc.wait()
        finally:
            killer.cancel()
        with self._lock:
            if self._proc is not proc:   # stopped or started again since
                answered.set()
                return
            if code == 0:
                self.on, self.state, self.message, self.enable_link = True, "on", "", ""
            elif self.state != "off":
                text = " ".join(s for s in said if s)[-300:]
                self.on, self.state = False, "error"
                self.message = ("Tailscale stopped waiting for https to be allowed. Switch this on again "
                                "after allowing it." if self.enable_link else (text or f"Tailscale exited with {code}"))
            self._proc = None
        answered.set()

    def _stop_waiting(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()

    def stop(self) -> None:
        """Stop serving at the Tailscale address (other things served through Tailscale stay)."""
        self._stop_waiting()
        with self._lock:
            was_on = self.on
            self.on, self.state, self.message, self.enable_link = False, "off", "", ""
        if was_on and program():
            try:
                _run(["serve", "--https=443", "off"])
            except (OSError, subprocess.SubprocessError):
                pass

    def allows(self, host_header: str) -> bool:
        """Whether a request addressed to *host_header* came in through this Tailscale address."""
        name = str(host_header or "").lower().rsplit(":", 1)[0]
        return bool(self.on and self.host and name == self.host.lower())

    def wait_until(self, states, seconds: float) -> str:
        end = time.time() + seconds
        while self.state not in states and time.time() < end:
            time.sleep(0.1)
        return self.state
