"""Setting up hearing and the call voice from Settings: official downloads, checked, unpacked.

Hearing is whisper.cpp (its program from the project's own GitHub release, and the base.en model
from its model page). The call voice is Kokoro (the kokoro-onnx Python package, and its model and
voices from that project's release). Every file is checked against its published SHA-256 before it
is used, and everything lands in speech_dir() (webui/speech.py), outside the project and the app.

The person asks for it (a button that lists the downloads and their sizes first); nothing is
fetched otherwise. A Mac has no whisper.cpp program to download: Homebrew's `whisper-cpp` is found
on the PATH instead.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from typing import Dict, List, Optional

from . import speech

_WHISPER = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/"
_MODEL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
_KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"

#: whisper.cpp's program for each platform it publishes one for: (file, size, sha256)
PROGRAMS = {
    ("windows", "x64"): ("whisper-bin-x64.zip", 8928640, "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb"),
    ("windows", "arm64"): ("whisper-bin-win-cpu-arm64.zip", 4371193,
                           "28c37e7b598c3d9bbfef94f3bd67f2ee6f12b7c86f3da5edcf5e4308d615ff6d"),
    ("linux", "x64"): ("whisper-bin-ubuntu-x64.tar.gz", 10364195,
                       "a72becf15d7917f990f6313867a52638b82b7f9ef237fb0c980dac56a135781c"),
    ("linux", "arm64"): ("whisper-bin-ubuntu-arm64.tar.gz", 4608377,
                         "6b95ebfc60447df48e70ef00a73bdc3f41679ed2d01ef465827206a2ff325149"),
}
HEARING_MODEL = ("ggml-base.en-q5_1.bin", 59721011, "4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f")
VOICE_FILES = [
    ("kokoro-v1.0.onnx", 325505369, "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"),
    ("voices-v1.0.bin", 28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
]
#: about what pip fetches for kokoro-onnx (onnxruntime and the phonemizer), for the size shown first
VOICE_PACKAGE_BYTES = 45_000_000


def this_platform() -> tuple:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "mac"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("amd64", "x86_64", "x64") else machine
    return system, arch


def plan(parts: List[str], bases: Optional[Dict[str, str]] = None) -> List[dict]:
    """What would be fetched for *parts* ("hear", "voice") on this computer, skipping what is there."""
    bases = {"whisper": _WHISPER, "model": _MODEL, "kokoro": _KOKORO, **(bases or {})}
    base = speech.speech_dir()
    items: List[dict] = []
    if "hear" in parts and speech.whisper() is None:
        program = PROGRAMS.get(this_platform())
        if program and not _found_program():
            name, size, digest = program
            items.append({"what": "whisper.cpp (hearing)", "url": bases["whisper"] + name, "size": size,
                          "sha256": digest, "kind": "archive", "dest": os.path.join(base, "bin")})
        if not any(f.startswith("ggml-") for f in _listdir(base)):
            name, size, digest = HEARING_MODEL
            items.append({"what": "Speech-to-text model (base.en)", "url": bases["model"] + name, "size": size,
                          "sha256": digest, "kind": "file", "dest": os.path.join(base, name)})
    if "voice" in parts and speech.kokoro_files() is None:
        import importlib.util

        if importlib.util.find_spec("kokoro_onnx") is None:
            items.append({"what": "kokoro-onnx Python package", "size": VOICE_PACKAGE_BYTES, "kind": "pip",
                          "package": "kokoro-onnx"})
        folder = os.path.join(base, "kokoro")
        for name, size, digest in VOICE_FILES:
            if not os.path.isfile(os.path.join(folder, name)):
                items.append({"what": "Kokoro voice " + ("model" if name.endswith(".onnx") else "pack"),
                              "url": bases["kokoro"] + name, "size": size, "sha256": digest, "kind": "file",
                              "dest": os.path.join(folder, name)})
    return items


def _listdir(path: str) -> List[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _found_program() -> bool:
    return speech.whisper_program() is not None


class Setup:
    """One setup at a time, in the background; the page asks how far it is."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict = {"running": False, "done": False, "error": "", "step": "", "got": 0, "total": 0}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, parts: List[str], bases: Optional[Dict[str, str]] = None) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            items = plan(parts, bases)
            self.state = {"running": bool(items), "done": not items, "error": "", "step": "",
                          "got": 0, "total": sum(i["size"] for i in items)}
        if items:
            threading.Thread(target=self._run, args=(items,), name="speech-setup", daemon=True).start()
        return self.snapshot()

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def _run(self, items: List[dict]) -> None:
        try:
            for item in items:
                self._set(step=item["what"])
                if item["kind"] == "pip":
                    self._pip(item["package"])
                    self._add(item["size"])
                else:
                    self._fetch(item)
            self._set(running=False, done=True, step="")
        except Exception as exc:  # noqa: BLE001 - said on the page
            self._set(running=False, done=False, error=f"{self.state.get('step')}: {exc}")

    def _add(self, n: int) -> None:
        with self.lock:
            self.state["got"] += n

    def _fetch(self, item: dict) -> None:
        dest = item["dest"]
        os.makedirs(os.path.dirname(dest) if item["kind"] == "file" else os.path.dirname(dest.rstrip("/\\")), exist_ok=True)
        part = (dest if item["kind"] == "file" else dest.rstrip("/\\") + "-download") + ".part"
        digest = hashlib.sha256()
        req = urllib.request.Request(item["url"], headers={"User-Agent": "CodeMonkey speech setup"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    fh.write(block)
                    digest.update(block)
                    self._add(len(block))
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("the download did not match its published checksum, so it was not used")
            if item["kind"] == "file":
                os.replace(part, dest)
            else:
                _unpack(part, dest)
        finally:
            if os.path.exists(part):
                os.remove(part)

    def _pip(self, package: str) -> None:
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        args = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", package]
        if not in_venv:
            args.insert(4, "--user")
        done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=1800,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            raise RuntimeError("pip could not install " + package + ": " + (done.stderr or done.stdout)[-400:])
        import importlib
        import site

        # a user folder made by this install was not there when Python started, so it is not looked in yet
        user_site = site.getusersitepackages() if not in_venv else ""
        if user_site and os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.append(user_site)
        importlib.invalidate_caches()


def _unpack(archive: str, dest: str) -> None:
    """An archive's files into *dest*, without the one top folder an archive often has, and never
    anywhere outside *dest*."""
    tmp = dest.rstrip("/\\") + "-unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    root = os.path.realpath(tmp)
    if archive.endswith((".zip", ".zip.part")) or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if os.path.commonpath([root, os.path.realpath(os.path.join(tmp, name))]) != root:
                    raise ValueError("the archive points outside its folder")
            z.extractall(tmp)
    else:
        with tarfile.open(archive) as t:
            # the "data" filter refuses members (and links) that point outside the folder
            t.extractall(tmp, filter="data")
    inside = os.listdir(tmp)
    top = os.path.join(tmp, inside[0]) if len(inside) == 1 and os.path.isdir(os.path.join(tmp, inside[0])) else tmp
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(top, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    if os.name != "nt":
        for name in os.listdir(dest):
            path = os.path.join(dest, name)
            if os.path.isfile(path) and not name.endswith((".so", ".txt", ".md")) and "." not in name:
                os.chmod(path, 0o755)
