"""Is there a newer CodeMonkey? Asked of a small file at a link the person who builds it controls.

The file is either just a version ("0.4.4") or JSON:

    {"version": "0.4.4", "date": "2026-10-27", "notes": "What changed",
     "page": "<where all the downloads are>",
     "downloads": {"windows": "<link>", "linux-x64": "<link>", "pi-64": "<link>", "pi-32": "<link>", "mac": "<link>"}}

`npm run release-info` (desktop/) writes it for a release. A Google Drive or Dropbox share link works
as it is: it is turned into the link that gives the file itself. Nothing is downloaded or installed
here; the person is shown where the new version is.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

_MAX_BYTES = 64 * 1024
_DAY = 24 * 3600


def link_file(root: str) -> str:
    """The link a build carries (update-link.txt beside the code): the first line that is not a comment."""
    try:
        with open(os.path.join(root, "update-link.txt"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def direct_link(url: str) -> str:
    """A share link turned into the file itself (Google Drive, Dropbox); anything else as it is."""
    url = (url or "").strip()
    parts = urllib.parse.urlparse(url)
    host = parts.netloc.lower()
    if host.endswith("drive.google.com"):
        m = re.search(r"/file/d/([A-Za-z0-9_-]+)", parts.path)
        file_id = m.group(1) if m else (urllib.parse.parse_qs(parts.query).get("id") or [""])[0]
        if file_id:
            return "https://drive.google.com/uc?export=download&id=" + file_id
    if host.endswith("dropbox.com"):
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["dl"] = "1"
        return urllib.parse.urlunparse(parts._replace(query=urllib.parse.urlencode(query)))
    return url


def version_tuple(text: str) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(text or ""))[:4])


def this_system() -> str:
    """Which download fits this computer: windows, mac, linux-x64, pi-64 or pi-32."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "pi-64"
    if machine.startswith("arm"):
        return "pi-32"
    return "linux-x64"


def parse(text: str) -> Dict[str, Any]:
    """The release file's content: a JSON object, or a bare version on the first line."""
    text = (text or "").strip().lstrip("﻿")
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("the release file is not a JSON object")
    else:
        data = {"version": text.splitlines()[0].strip() if text else ""}
    if not version_tuple(data.get("version", "")):
        raise ValueError("the release file names no version")
    return data


class Updates:
    """One per app: the last answer, and a check once a day in the background."""

    def __init__(self, app: Any, root: str):
        self.app = app
        self.root = root
        self._lock = threading.Lock()
        self.last: Dict[str, Any] = {}

    def url(self) -> str:
        return str(self.app.settings.get("update_url") or "").strip() or link_file(self.root)

    def check(self, fetch: Optional[Any] = None) -> Dict[str, Any]:
        from .app import app_version

        current = app_version()
        result: Dict[str, Any] = {"current": current, "url": self.url(), "newer": False, "latest": "",
                                  "notes": "", "download": "", "page": "", "error": "",
                                  "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        if not result["url"]:
            result["error"] = "no update link is set"
        else:
            try:
                text = (fetch or _fetch)(direct_link(result["url"]))
                data = parse(text)
                result["latest"] = str(data["version"]).strip()
                result["notes"] = str(data.get("notes") or "")[:2000]
                result["page"] = str(data.get("page") or "")
                downloads = data.get("downloads") if isinstance(data.get("downloads"), dict) else {}
                result["download"] = str(downloads.get(this_system()) or result["page"] or "")
                result["newer"] = version_tuple(result["latest"]) > version_tuple(current)
            except Exception as exc:  # noqa: BLE001 - said plainly on the page
                result["error"] = _plain(exc)
        with self._lock:
            self.last = result
        return result

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last = dict(self.last)
        if not last:
            from .app import app_version

            last = {"current": app_version(), "url": self.url(), "newer": False, "latest": "", "checked_at": ""}
        last["auto"] = bool(self.app.settings.get("check_updates", True))
        last["system"] = this_system()
        return last

    def start(self) -> None:
        """Ask now (if a link is set and checking is on), then once a day."""
        def loop() -> None:
            import time

            while True:
                if self.app.settings.get("check_updates", True) and self.url():
                    self.check()
                time.sleep(_DAY)

        threading.Thread(target=loop, daemon=True, name="update-check").start()


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "CodeMonkey update check"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - the link the person set
        raw = resp.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("that link gives a large file, not a release file")
    text = raw.decode("utf-8", "replace")
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise ValueError("that link gives a web page, not the release file (is it shared with anyone who has the link?)")
    return text


def _plain(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"the link answered {exc.code} ({exc.reason})"
    if isinstance(exc, urllib.error.URLError):
        return f"could not reach the link ({exc.reason})"
    if isinstance(exc, json.JSONDecodeError):
        return "the release file is not valid JSON"
    return str(exc) or type(exc).__name__
