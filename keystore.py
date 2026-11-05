"""API keys entered in the app, kept in the system's own key store.

Keys never go into config files, the workspace or the repository. On Windows they are stored as
generic credentials ("CodeMonkey:provider:<name>"), encrypted by Windows under the signed-in user;
they can be seen and removed in Control Panel > Credential Manager > Windows Credentials. On a Mac
they go to the login Keychain (the `security` tool), and on Linux to the desktop's keyring through
`secret-tool` (libsecret: GNOME Keyring or KWallet). Without any of these there is no store: keys
come from environment variables only.

An environment variable named by a provider's ``api_key_env`` still wins over a stored key.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, Optional

TARGET_PREFIX = "CodeMonkey:provider:"


class KeyStoreUnavailable(RuntimeError):
    """There is nowhere safe to keep a key on this system."""


class _MemoryBackend:
    """Keys held only while the process runs (tests, and systems without a store)."""

    NAME = "memory (until CodeMonkey closes)"

    def __init__(self) -> None:
        self._keys: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, target: str) -> Optional[str]:
        with self._lock:
            return self._keys.get(target)

    def set(self, target: str, secret: str) -> None:
        with self._lock:
            self._keys[target] = secret

    def delete(self, target: str) -> bool:
        with self._lock:
            return self._keys.pop(target, None) is not None


class _CredManBackend:
    """Windows Credential Manager through advapi32 (standard library ctypes, nothing to install)."""

    NAME = "Windows Credential Manager"

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as wt

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD),
                ("Type", wt.DWORD),
                ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR),
            ]

        self._ct = ctypes
        self._CRED = CREDENTIALW
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        self._read = api.CredReadW
        self._read.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self._read.restype = wt.BOOL
        self._write = api.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIALW), wt.DWORD]
        self._write.restype = wt.BOOL
        self._delete = api.CredDeleteW
        self._delete.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
        self._delete.restype = wt.BOOL
        self._free = api.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def get(self, target: str) -> Optional[str]:
        ct = self._ct
        out = ct.POINTER(self._CRED)()
        if not self._read(target, self.CRED_TYPE_GENERIC, 0, ct.byref(out)):
            if ct.get_last_error() == self.ERROR_NOT_FOUND:
                return None
            raise OSError(ct.get_last_error(), "Credential Manager could not read the key")
        try:
            cred = out.contents
            blob = ct.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode("utf-8")
        finally:
            self._free(out)

    def set(self, target: str, secret: str) -> None:
        ct = self._ct
        data = secret.encode("utf-8")
        buf = (ct.c_ubyte * len(data)).from_buffer_copy(data)
        cred = self._CRED()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "API key saved by CodeMonkey"
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "CodeMonkey"
        if not self._write(ct.byref(cred), 0):
            raise OSError(ct.get_last_error(), "Credential Manager could not save the key")

    def delete(self, target: str) -> bool:
        if self._delete(target, self.CRED_TYPE_GENERIC, 0):
            return True
        if self._ct.get_last_error() == self.ERROR_NOT_FOUND:
            return False
        raise OSError(self._ct.get_last_error(), "Credential Manager could not remove the key")


class _KeychainBackend:
    """The Mac's login Keychain through /usr/bin/security. The key goes in on stdin, never on a command line."""

    NAME = "the macOS Keychain"
    NOT_FOUND = 44

    def __init__(self) -> None:
        self._exe = shutil.which("security") or "/usr/bin/security"

    def _run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([self._exe, *args], input=stdin, capture_output=True, text=True, timeout=30)

    def get(self, target: str) -> Optional[str]:
        proc = self._run("find-generic-password", "-a", "CodeMonkey", "-s", target, "-w")
        if proc.returncode == self.NOT_FOUND:
            return None
        if proc.returncode:
            raise OSError(proc.returncode, proc.stderr.strip() or "the Keychain could not read the key")
        return proc.stdout.rstrip("\n")

    def set(self, target: str, secret: str) -> None:
        quote = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
        line = "add-generic-password -U -a CodeMonkey -l %s -s %s -w %s\n" % (
            quote("CodeMonkey"), quote(target), quote(secret))
        proc = self._run("-i", stdin=line)   # interactive mode reads the command, key included, from stdin
        if proc.returncode or "error" in proc.stderr.lower():
            raise OSError(proc.returncode or 1, proc.stderr.strip() or "the Keychain could not save the key")

    def delete(self, target: str) -> bool:
        proc = self._run("delete-generic-password", "-a", "CodeMonkey", "-s", target)
        if proc.returncode == self.NOT_FOUND:
            return False
        if proc.returncode:
            raise OSError(proc.returncode, proc.stderr.strip() or "the Keychain could not remove the key")
        return True


class _SecretToolBackend:
    """The Linux desktop keyring (GNOME Keyring, KWallet) through libsecret's secret-tool."""

    NAME = "the system keyring"

    def __init__(self, exe: str) -> None:
        self._exe = exe

    def _run(self, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run([self._exe, *args], input=stdin, capture_output=True, text=True, timeout=30)

    def get(self, target: str) -> Optional[str]:
        proc = self._run("lookup", "application", "CodeMonkey", "target", target)
        if proc.returncode:   # 1 with no output: not found; anything on stderr: no keyring running
            if proc.stderr.strip():
                raise OSError(proc.returncode, proc.stderr.strip())
            return None
        return proc.stdout or None

    def set(self, target: str, secret: str) -> None:
        proc = self._run("store", "--label=CodeMonkey: " + target, "application", "CodeMonkey", "target", target,
                         stdin=secret)
        if proc.returncode:
            raise OSError(proc.returncode, proc.stderr.strip() or "the keyring could not save the key")

    def delete(self, target: str) -> bool:
        had = self.get(target) is not None
        proc = self._run("clear", "application", "CodeMonkey", "target", target)
        if proc.returncode and proc.stderr.strip():
            raise OSError(proc.returncode, proc.stderr.strip())
        return had


def _default_backend():
    if os.environ.get("CODEMONKEY_KEYSTORE") == "memory":
        return _MemoryBackend()
    if sys.platform == "win32":
        try:
            return _CredManBackend()
        except (OSError, AttributeError):
            return None
    if sys.platform == "darwin" and (shutil.which("security") or os.path.exists("/usr/bin/security")):
        return _KeychainBackend()
    exe = shutil.which("secret-tool")
    if exe:
        return _SecretToolBackend(exe)
    return None


_backend = _default_backend()


def use_memory() -> None:
    """Keep keys in memory only, for this process (tests call this so nothing touches Windows)."""
    global _backend
    _backend = _MemoryBackend()


def available() -> bool:
    return _backend is not None


def store_name() -> str:
    """Where saved keys go, in words for the Settings page ("" when there is nowhere)."""
    return getattr(_backend, "NAME", "") if _backend is not None else ""


def _target(provider: str) -> str:
    return TARGET_PREFIX + provider


def get(provider: str) -> Optional[str]:
    """The stored key for *provider*, or None. A store that cannot be read counts as no key."""
    if _backend is None or not provider:
        return None
    try:
        key = _backend.get(_target(provider))
    except OSError:
        return None
    return key.strip() if key else None


def set(provider: str, key: str) -> None:  # noqa: A001 - mirrors get/delete
    if _backend is None:
        raise KeyStoreUnavailable(
            "There is no key store on this system to save keys in. On Linux, install secret-tool "
            "(sudo apt install libsecret-tools) and run a keyring such as GNOME Keyring, or set the "
            "provider's api_key_env environment variable instead."
        )
    _backend.set(_target(provider), key)


def delete(provider: str) -> bool:
    if _backend is None:
        return False
    try:
        return _backend.delete(_target(provider))
    except OSError:
        return False


def hint(key: Optional[str]) -> str:
    """The last four characters of a key, enough to tell keys apart and no more."""
    return ("…" + key[-4:]) if key and len(key) >= 12 else ""
