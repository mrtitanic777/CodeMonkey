"""Report a problem: one zip a friend can send to whoever gave them CodeMonkey.

What it holds: what they wrote, the version and this computer, the settings, which model servers
are set up (never a key), what the server printed lately, the page's own errors, and (only if they
tick it) the conversation. Anything shaped like a key or password is blanked on the way in.

The server's output is kept here as it is printed (`capture_output`): the last lines in memory and
in server.log, with the run before kept as server.old.log.
"""

from __future__ import annotations

import collections
import datetime
import io
import json
import os
import platform
import re
import sys
import threading
import zipfile
from typing import Any, Deque, Dict, List, Optional

_LINES: Deque[str] = collections.deque(maxlen=3000)
_LOCK = threading.Lock()
_LOG: Dict[str, Any] = {"path": "", "fh": None, "size": 0}
_MAX_LOG = 2 * 1024 * 1024

_SECRETS = [
    re.compile(r"\b(sk-(?:ant-)?[A-Za-z0-9_-]{8,})"),
    re.compile(r"\b((?:ghp|gho|github_pat|xox[abp]|AIza)[A-Za-z0-9_-]{10,})"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})"),
]


def scrub(text: str) -> str:
    """Key- and password-shaped text blanked out."""
    for pattern in _SECRETS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: m.group(1) + "[hidden]", text)
        else:
            text = pattern.sub("[hidden]", text)
    return text


class _Tee(io.TextIOBase):
    """Writes go where they always went, and into the log."""

    def __init__(self, stream: Any):
        self.stream = stream
        self._partial = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        try:
            if self.stream is not None:
                self.stream.write(text)
        except (OSError, ValueError):
            pass
        _record(text, self)
        return len(text)

    def flush(self) -> None:
        try:
            if self.stream is not None:
                self.stream.flush()
        except (OSError, ValueError):
            pass

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self.stream, "encoding", None) or "utf-8"

    def isatty(self) -> bool:
        return bool(getattr(self.stream, "isatty", lambda: False)())


def _record(text: str, tee: _Tee) -> None:
    with _LOCK:
        text = tee._partial + text
        *whole, tee._partial = text.split("\n")
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        for line in whole:
            line = f"{stamp} {line}"
            _LINES.append(line)
            fh = _LOG["fh"]
            if fh is not None and _LOG["size"] < _MAX_LOG:
                try:
                    fh.write(line + "\n")
                    fh.flush()
                    _LOG["size"] += len(line) + 1
                except (OSError, ValueError):
                    pass


def capture_output(folder: str) -> None:
    """From now on, what the server prints is also kept (once per process)."""
    if isinstance(sys.stdout, _Tee):
        return
    path = os.path.join(folder, "server.log")
    try:
        if os.path.exists(path):
            os.replace(path, os.path.join(folder, "server.old.log"))
        _LOG.update(path=path, fh=open(path, "w", encoding="utf-8", errors="replace"), size=0)
    except OSError:
        pass
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)   # a background thread's crash is printed there, so it is kept too


def recent_lines() -> List[str]:
    with _LOCK:
        return list(_LINES)


def _old_log(folder: str) -> str:
    try:
        with open(os.path.join(folder, "server.old.log"), encoding="utf-8", errors="replace") as fh:
            return fh.read()[-_MAX_LOG:]
    except OSError:
        return ""


def build(app: Any, note: str, page: Dict[str, Any], session: Optional[Any] = None) -> bytes:
    """The zip's bytes."""
    from . import machine
    from .app import app_version

    now = datetime.datetime.now()
    mem, gpu = machine.memory(), machine.gpu() or {}
    lines = [
        "CodeMonkey problem report",
        "Made: " + now.isoformat(timespec="seconds"),
        "",
        "What happened (in their words):",
        (note or "").strip() or "(nothing written)",
        "",
        "Version: " + (app_version() or "unknown"),
        "System: %s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
        "Python: " + sys.version.split()[0],
        "Memory: %s GB, graphics: %s (%s GB)" % (mem.get("total_gb"), gpu.get("name") or "none found", gpu.get("total_gb")),
        "Page: " + str(page.get("agent") or "")[:300],
        "Window: " + str(page.get("screen") or "")[:60],
        "Conversation included: " + ("yes" if session is not None else "no"),
        "",
        "Model servers set up:",
    ]
    for name, entry in sorted((app.config.providers or {}).items()):
        try:
            key = app.providers.key_status(name).get("source") or "none"
        except Exception:  # noqa: BLE001
            key = "unknown"
        lines.append("  %s: %s, key: %s" % (name, (entry or {}).get("base_url", ""), key))
    lines += ["", "Default model: %s / %s" % (app.settings.get("default_provider"), app.settings.get("default_model"))]

    files = {
        "report.txt": "\n".join(lines) + "\n",
        "settings.json": json.dumps(app.settings, indent=2, default=str),
        "server.log": "\n".join(recent_lines()) + "\n",
        "page-errors.txt": "\n".join(str(e)[:2000] for e in (page.get("errors") or [])[-100:]) or "(none)\n",
    }
    old = _old_log(app.webui_dir)
    if old:
        files["server.old.log"] = old
    if session is not None:
        try:
            files["conversation.md"] = session.export_markdown()
        except Exception as exc:  # noqa: BLE001
            files["conversation.md"] = f"(the conversation could not be added: {exc})"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, scrub(text))
    return out.getvalue()


def file_name() -> str:
    return "CodeMonkey-report-" + datetime.datetime.now().strftime("%Y-%m-%d-%H%M") + ".zip"
