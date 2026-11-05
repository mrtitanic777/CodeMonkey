"""Paired devices: each phone or tablet that was let in, by name, and a way to turn one away.

Pairing (the scanned code or the 6 digits) gives a device a key of its own, kept in a cookie.
Only a hash of that key is stored here, so reading this computer's files does not hand anyone a
working key. Settings lists the devices with when each was last active; one can be disconnected
without disturbing the others, and a device can sign itself out. A disconnected device is cut
off at once, including a conversation it has open.

This computer's own browser is never in the list: it needs no key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

MAX_DEVICES = 24
SAVE_SEEN_EVERY = 60.0   # "last active" is written to disk at most this often


def describe(user_agent: str) -> str:
    """A plain name for a browser, from what it says about itself: "iPhone, Safari"."""
    ua = str(user_agent or "")
    if "iPhone" in ua:
        kind = "iPhone"
    elif "iPad" in ua:
        kind = "iPad"
    elif "Android" in ua:
        kind = "Android phone" if "Mobile" in ua else "Android tablet"
    elif "Windows" in ua:
        kind = "Windows computer"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        kind = "Mac"
    elif "Linux" in ua or "X11" in ua:
        kind = "Linux computer"
    else:
        kind = "Device"
    browser = ""
    for mark, name in (("Edg", "Edge"), ("OPR", "Opera"), ("SamsungBrowser", "Samsung Internet"),
                       ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
                       ("Chrome", "Chrome"), ("Safari", "Safari")):
        if mark in ua:
            browser = name
            break
    return f"{kind}, {browser}" if browser else kind


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Devices:
    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._rows: Optional[List[Dict[str, Any]]] = None
        self._saved = 0.0

    # ------------------------------------------------------------ storage ---

    def _load(self) -> List[Dict[str, Any]]:
        if self._rows is None:
            try:
                rows = json.loads(self.app.store.get_meta("devices") or "[]")
            except (ValueError, TypeError):
                rows = []
            self._rows = [r for r in rows if isinstance(r, dict) and r.get("id") and r.get("hash")]
        return self._rows

    def _save(self) -> None:
        self._saved = time.time()
        self.app.store.set_meta("devices", json.dumps(self._rows or []))

    def _announce(self) -> None:
        try:
            self.app.bus.emit("devices", {})
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- pairing ---

    def pair(self, user_agent: str, address: str) -> str:
        """Let a new device in. Returns the cookie value it must keep (shown to nobody else)."""
        secret = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            rows = self._load()
            row = {"id": secrets.token_hex(5), "name": describe(user_agent), "hash": _hash(secret),
                   "paired": now, "seen": now, "address": str(address)}
            rows.append(row)
            rows.sort(key=lambda r: r.get("seen", 0))
            del rows[:-MAX_DEVICES]   # the longest-idle ones make room
            self._save()
        self._announce()
        return f"{row['id']}.{secret}"

    def check(self, cookie: str, address: str = "") -> Optional[str]:
        """The device id when *cookie* is a paired device's key, else None."""
        dev_id, _, secret = str(cookie or "").partition(".")
        if not dev_id or not secret:
            return None
        with self._lock:
            for row in self._load():
                if row["id"] == dev_id:
                    if not hmac.compare_digest(row["hash"], _hash(secret)):
                        return None
                    row["seen"] = time.time()
                    if address:
                        row["address"] = address
                    if row["seen"] - self._saved > SAVE_SEEN_EVERY:
                        self._save()
                    return dev_id
        return None

    def alive(self, dev_id: str) -> bool:
        with self._lock:
            return any(r["id"] == dev_id for r in self._load())

    # ------------------------------------------------------------- managing ---

    def listing(self, current: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            rows = sorted(self._load(), key=lambda r: -r.get("seen", 0))
            return [{"id": r["id"], "name": r["name"], "paired": r.get("paired", 0),
                     "seen": r.get("seen", 0), "address": r.get("address", ""),
                     "current": r["id"] == current} for r in rows]

    def rename(self, dev_id: str, name: str) -> bool:
        name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:40]
        if not name:
            return False
        with self._lock:
            for row in self._load():
                if row["id"] == dev_id:
                    row["name"] = name
                    self._save()
                    break
            else:
                return False
        self._announce()
        return True

    def remove(self, dev_id: str) -> bool:
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r["id"] != dev_id]
            if len(kept) == len(rows):
                return False
            self._rows = kept
            self._save()
        self._announce()
        return True

    def remove_all(self) -> int:
        with self._lock:
            count = len(self._load())
            self._rows = []
            self._save()
        self._announce()
        return count
