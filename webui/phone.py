"""Opening CodeMonkey on your phone, without typing a long link.

Switched on in Settings (or by starting with --host 0.0.0.0). The app then also listens on this
computer's address on the local network. A device that is not this computer gets nothing until
it is paired, in one of two ways:

    scan the QR code shown in Settings (it carries the key), or
    open the short address and type the 6-digit code shown next to it.

The code and the QR link each work once, for fifteen minutes, and the code is dead after five
wrong tries; a fresh pair is made as soon as one is used. A paired device keeps a key of its own
in a cookie (see devices.py). This computer's own browser never needs any of it.
"""

from __future__ import annotations

import hmac
import secrets
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from typing import Any, Dict, Optional

CODE_SECONDS = 15 * 60
MAX_TRIES = 5


def lan_address() -> str:
    """This computer's address on the local network (no packet is sent to find it)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Phone:
    def __init__(self, app):
        self.app = app
        self.handler = None           # set by serve()
        self.port: Optional[int] = None
        self.bound_all = False        # the main server already listens on every address
        self.server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        self.code = ""
        self.link_token = ""          # what the QR code carries; one use, like the code
        self.expires = 0.0
        self.fails = 0

    # ------------------------------------------------------------ listen ---

    def on_wifi(self) -> bool:
        return self.bound_all or self.server is not None

    def anywhere(self) -> str:
        """The Tailscale https address when CodeMonkey is served there (anywhere.py), else ""."""
        place = getattr(self.app, "anywhere", None)
        return f"https://{place.host}" if place is not None and place.on and place.host else ""

    def enabled(self) -> bool:
        return self.on_wifi() or bool(self.anywhere())

    def start(self) -> None:
        """Listen on the local-network address too. Raises RuntimeError when that cannot be done."""
        if self.on_wifi():
            return
        if self.handler is None or self.port is None:
            raise RuntimeError("the app is not serving yet")
        address = lan_address()
        if address.startswith("127."):
            raise RuntimeError("this computer is not on a network right now")
        try:
            server = ThreadingHTTPServer((address, self.port), self.handler)
        except OSError as exc:
            raise RuntimeError(f"could not listen on {address}:{self.port} ({exc.strerror or exc})") from exc
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="phone-listener", daemon=True).start()
        self.server = server
        self.new_code()

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
            server.server_close()

    # -------------------------------------------------------------- pair ---

    def new_code(self) -> str:
        with self._lock:
            self.code = f"{secrets.randbelow(1_000_000):06d}"
            self.link_token = secrets.token_urlsafe(18)
            self.expires = time.time() + CODE_SECONDS
            self.fails = 0
            return self.code

    def check(self, given: str) -> bool:
        """True when *given* is the current code. A right answer uses the code up."""
        given = "".join(ch for ch in str(given or "") if ch.isdigit())
        with self._lock:
            if not self.code or time.time() > self.expires or self.fails >= MAX_TRIES:
                return False
            if not hmac.compare_digest(given, self.code):
                self.fails += 1
                return False
            self.code = ""
        self.new_code()   # the next device needs a fresh one
        return True

    def check_link(self, given: str) -> bool:
        """True when *given* is what the current QR code carries. A right answer uses it up."""
        given = str(given or "")
        with self._lock:
            if not given or not self.link_token or time.time() > self.expires:
                return False
            if not hmac.compare_digest(given.encode("utf-8"), self.link_token.encode("utf-8")):
                return False
            self.link_token = ""
        self.new_code()
        return True

    def locked(self) -> str:
        """Why the code cannot be used right now, or ""."""
        with self._lock:
            if self.fails >= MAX_TRIES:
                return "Too many wrong tries. Make a new code on the computer (Settings, General)."
            if not self.code or time.time() > self.expires:
                return "That code has run out. Make a new one on the computer (Settings, General)."
        return ""

    def status(self) -> Dict[str, Any]:
        on = self.enabled()
        if on and (not self.code or time.time() > self.expires or self.fails >= MAX_TRIES):
            self.new_code()
        wifi = self.on_wifi()
        address = f"{lan_address()}:{self.port}" if self.port and wifi else ""
        far = self.anywhere()
        # the QR code opens the address that works everywhere, when there is one
        link = (f"{far}/?key={self.link_token}" if far else f"http://{address}/?key={self.link_token}") if on else ""
        return {
            "enabled": on,
            "wifi": wifi,
            "address": address,
            "anywhere": far,
            "code": self.code if on else "",
            "expires_in": max(0, int(self.expires - time.time())) if on else 0,
            "link": link,
            "can_switch": not self.bound_all,
        }

    def announce(self) -> None:
        """Say how to connect, in the window the app was started from."""
        from . import qr

        st = self.status()
        if not st["enabled"]:
            return
        where = (f"open http://{st['address']} (same Wi-Fi)" if st["address"] else "") +             (" or " if st["address"] and st["anywhere"] else "") +             (f"open {st['anywhere']} (anywhere, with Tailscale)" if st["anywhere"] else "")
        print(f"On your phone: {where} and enter the code {st['code'][:3]} {st['code'][3:]}")
        print("Or scan this with the phone's camera:")
        try:
            print(qr.text_blocks(st["link"]))
        except (UnicodeEncodeError, ValueError):
            print("  (this window cannot draw the code; it is also in Settings, General)")
