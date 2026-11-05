"""Getting a model running, for someone who has never done it.

What is on this computer (Ollama, LM Studio, keys for Claude or OpenAI, the graphics card and
memory), which model would fit, and the few steps that can be done from here: start Ollama, open
LM Studio, download a model with Ollama. Every action happens only when the person asks for it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from . import machine

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,79}$")
_PCT = re.compile(r"(\d{1,3})%")

#: What to download with Ollama, by the room a model needs (its download is about this size, and it
#: needs about this much graphics memory to run at a good speed). Largest first.
CHOICES = [
    {"model": "qwen3-coder:30b", "gb": 19.0, "need": 20, "about": "Strong at code; needs a large graphics card"},
    {"model": "qwen3:14b", "gb": 9.3, "need": 11, "about": "Good all round, and at code"},
    {"model": "qwen3:8b", "gb": 5.2, "need": 6, "about": "A good start on most computers"},
    {"model": "qwen3:4b", "gb": 2.6, "need": 0, "about": "Small and quick; for computers without a big graphics card"},
]


def _ollama_exe() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for path in (os.path.join(local, "Programs", "Ollama", "ollama.exe"), "/usr/local/bin/ollama"):
        if path and os.path.isfile(path):
            return path
    return None


def _lmstudio_exe() -> Optional[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    for path in (os.path.join(local, "Programs", "LM Studio", "LM Studio.exe"),
                 os.path.join(local, "Programs", "lm-studio", "LM Studio.exe"),
                 "/Applications/LM Studio.app"):
        if path and os.path.exists(path):
            return path
    return None


def recommend(vram_gb: Optional[float], ram_gb: Optional[float]) -> List[Dict[str, Any]]:
    """The choices that fit, best first. Without a graphics card the model runs from memory, slower,
    so it is sized to about a third of the memory."""
    room = vram_gb if vram_gb else (ram_gb or 8) / 3
    fits = [dict(c, recommended=False) for c in CHOICES if c["need"] <= room]
    if fits:
        fits[0]["recommended"] = True
    return fits


class ModelSetup:
    """One per app: the status it reports, and the one download that may be running."""

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.Lock()
        self.pull: Dict[str, Any] = {"running": False, "model": "", "pct": 0, "line": "", "error": "", "done": False}

    # ------------------------------------------------------------ status ---

    def _provider(self, name: str) -> Dict[str, Any]:
        if name not in (self.app.config.providers or {}):
            return {"configured": False, "running": False, "models": []}
        try:
            r = self.app.test_provider(name)
        except Exception:  # noqa: BLE001
            r = {"ok": False, "models": []}
        return {"configured": True, "running": bool(r.get("ok")), "models": list(r.get("models") or [])}

    def status(self) -> Dict[str, Any]:
        mem, gpu = machine.memory(), machine.gpu()
        with ThreadPoolExecutor(2) as pool:   # both at once: a server that is down costs its timeout
            asked = {name: pool.submit(self._provider, name) for name in ("ollama", "lmstudio")}
        ollama = dict(asked["ollama"].result(), installed=bool(_ollama_exe()))
        lmstudio = dict(asked["lmstudio"].result(), installed=bool(_lmstudio_exe()))
        keys = {}
        for name in ("anthropic", "openai"):
            if name in (self.app.config.providers or {}):
                try:
                    keys[name] = bool(self.app.providers.key_status(name).get("source"))
                except Exception:  # noqa: BLE001
                    keys[name] = False
        with self._lock:
            pull = dict(self.pull)
        return {
            "computer": {"ram_gb": mem.get("total_gb"), "gpu": (gpu or {}).get("name"), "vram_gb": (gpu or {}).get("total_gb")},
            "ollama": ollama,
            "lmstudio": lmstudio,
            "keys": keys,
            "choices": recommend((gpu or {}).get("total_gb"), mem.get("total_gb")),
            "pull": pull,
            "ready": ollama["running"] and bool(ollama["models"]) or lmstudio["running"] and bool(lmstudio["models"])
                     or any(keys.values()),
        }

    # ----------------------------------------------------------- actions ---

    def start_ollama(self) -> Dict[str, Any]:
        exe = _ollama_exe()
        if not exe:
            raise ValueError("Ollama is not installed yet. Download it first.")
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                         close_fds=True, start_new_session=sys.platform != "win32")
        return {"started": True}

    def open_lmstudio(self) -> Dict[str, Any]:
        exe = _lmstudio_exe()
        if not exe:
            raise ValueError("LM Studio is not installed yet. Download it first.")
        if sys.platform == "win32":
            os.startfile(exe)  # noqa: S606 - the person asked to open it
        else:
            subprocess.Popen(["open", exe] if exe.endswith(".app") else [exe])
        return {"opened": True}

    def start_pull(self, model: str) -> Dict[str, Any]:
        model = str(model or "").strip().lower()
        if not _MODEL_NAME.match(model):
            raise ValueError("that is not a model name Ollama knows")
        exe = _ollama_exe()
        if not exe:
            raise ValueError("Ollama is not installed yet. Download it first.")
        with self._lock:
            if self.pull["running"]:
                raise ValueError("a download is already running: " + self.pull["model"])
            self.pull = {"running": True, "model": model, "pct": 0, "line": "Starting…", "error": "", "done": False}
        threading.Thread(target=self._pull, args=(exe, model), daemon=True, name="ollama-pull").start()
        return {"started": model}

    def _pull(self, exe: str, model: str) -> None:
        error = ""
        try:
            proc = subprocess.Popen([exe, "pull", model], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            buf = b""
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                buf = (buf + chunk)[-4000:]
                # progress is drawn with carriage returns: the last piece is the current state
                last = re.split(rb"[\r\n]+", buf.strip())[-1].decode("utf-8", "replace")
                last = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", last).strip()
                pct = _PCT.findall(last)
                with self._lock:
                    self.pull["line"] = last[:160]
                    if pct:
                        self.pull["pct"] = min(100, int(pct[-1]))
            code = proc.wait()
            if code != 0:
                error = (self.pull.get("line") or "the download stopped") + f" (exit {code})"
        except OSError as exc:
            error = str(exc)
        with self._lock:
            self.pull.update(running=False, done=not error, error=error, pct=100 if not error else self.pull["pct"])
        cached = getattr(self.app, "_available", None)
        if cached:
            self.app._available = None   # the model menu asks again
