"""Hearing and speaking, on this computer: whisper.cpp turns speech into text, and a voice (Piper,
when it is set up) turns a reply into speech. Without a voice the page speaks with the voices the
system has.

Both live outside the project and the app, in speech_dir(), so neither grows by them:

    <speech_dir>/bin/Release/whisper-cli.exe   whisper.cpp (or bin/whisper-cli)
    <speech_dir>/ggml-*.bin                    its model
    <speech_dir>/piper/piper(.exe)             Piper
    <speech_dir>/voices/*.onnx                 Piper voices (with their .onnx.json)
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: The longest piece of speech heard in one go (a voice call sends a sentence or a few).
MOST_HEARD_BYTES = 20 * 1024 * 1024
#: What whisper writes for a stretch with no words in it: not something said.
_NOT_SPEECH = re.compile(r"^\s*([\[(][^\])]*[\])]\s*)+$")


def speech_dir() -> str:
    """CODEMONKEY_SPEECH, or the CodeMonkey folder in the user's local app data."""
    return os.environ.get("CODEMONKEY_SPEECH") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "CodeMonkey", "speech")


def whisper() -> Optional[Tuple[str, str]]:
    """(whisper.cpp's whisper-cli, its model file), or None when hearing is not set up."""
    base = speech_dir()
    exe = next((p for p in (os.path.join(base, "bin", "Release", "whisper-cli.exe"),
                            os.path.join(base, "bin", "whisper-cli.exe"), os.path.join(base, "bin", "whisper-cli"))
                if os.path.isfile(p)), None) or shutil.which("whisper-cli")
    models = sorted(glob.glob(os.path.join(base, "ggml-*.bin")))
    return (exe, models[0]) if exe and models else None


def piper() -> Optional[Tuple[str, str]]:
    """(Piper, a voice's .onnx file), or None when no voice is set up."""
    base = speech_dir()
    exe = next((p for p in (os.path.join(base, "piper", "piper.exe"), os.path.join(base, "piper", "piper"))
                if os.path.isfile(p)), None)
    voices = sorted(glob.glob(os.path.join(base, "voices", "*.onnx")))
    return (exe, voices[0]) if exe and voices else None


def status() -> dict:
    voice = piper()
    return {"hear": whisper() is not None,
            "voice": ("piper:" + os.path.basename(voice[1])[:-5]) if voice else "system"}


def threads() -> str:
    return str(max(2, min(8, (os.cpu_count() or 4) // 2)))


def hear(audio: bytes) -> str:
    """What is said in *audio* (a WAV file's bytes, best at 16 kHz mono): the words, or "" for none."""
    tool = whisper()
    if tool is None:
        raise RuntimeError("hearing is not set up (whisper.cpp in " + speech_dir() + ")")
    if not audio or len(audio) > MOST_HEARD_BYTES:
        raise ValueError("no audio, or too much at once")
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="cm-heard-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        done = subprocess.run([tool[0], "-m", tool[1], "-f", wav, "-t", threads(), "-np", "-nt"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, creationflags=_NO_WINDOW)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    lines = [ln.strip() for ln in (done.stdout or "").splitlines() if ln.strip() and not _NOT_SPEECH.match(ln)]
    return " ".join(lines).strip()


def speak(text: str) -> bytes:
    """*text* said by the Piper voice, as a WAV file's bytes. RuntimeError when no voice is set up."""
    tool = piper()
    if tool is None:
        raise RuntimeError("no voice is set up")
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:2000]
    if not text:
        return b""
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="cm-said-")
    os.close(fd)
    try:
        subprocess.run([tool[0], "--model", tool[1], "--output_file", wav], input=text, capture_output=True,
                       text=True, encoding="utf-8", timeout=60, creationflags=_NO_WINDOW,
                       cwd=os.path.dirname(tool[0]))
        with open(wav, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
