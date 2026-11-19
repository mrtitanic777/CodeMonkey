"""Hearing and speaking, on this computer: whisper.cpp turns speech into text, and a voice turns a
reply into speech: Kokoro when it is set up (lifelike, and quick enough on a CPU), else Piper,
else the page speaks with the voices the system has.

Both live outside the project and the app, in speech_dir(), so neither grows by them:

    <speech_dir>/bin/Release/whisper-cli.exe   whisper.cpp (or bin/whisper-cli)
    <speech_dir>/ggml-*.bin                    its model
    <speech_dir>/piper/piper(.exe)             Piper
    <speech_dir>/voices/*.onnx                 Piper voices (with their .onnx.json)
    <speech_dir>/kokoro/kokoro-*.onnx          Kokoro, with voices-*.bin (and the kokoro-onnx package)
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from typing import List, Optional, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: The longest piece of speech heard in one go (a voice call sends a sentence or a few).
MOST_HEARD_BYTES = 20 * 1024 * 1024
#: What whisper writes for a stretch with no words in it: not something said.
_NOT_SPEECH = re.compile(r"^\s*([\[(][^\])]*[\])]\s*)+$")


def speech_dir() -> str:
    """CODEMONKEY_SPEECH, or the CodeMonkey folder in the user's local app data."""
    return os.environ.get("CODEMONKEY_SPEECH") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "CodeMonkey", "speech")


def whisper_program() -> Optional[str]:
    """whisper.cpp's whisper-cli: in the speech folder, or on the PATH (Homebrew's whisper-cpp on a Mac)."""
    base = speech_dir()
    return next((p for p in (os.path.join(base, "bin", "Release", "whisper-cli.exe"),
                             os.path.join(base, "bin", "whisper-cli.exe"), os.path.join(base, "bin", "whisper-cli"))
                 if os.path.isfile(p)), None) or shutil.which("whisper-cli")


def whisper() -> Optional[Tuple[str, str]]:
    """(whisper.cpp's whisper-cli, its model file), or None when hearing is not set up."""
    exe = whisper_program()
    models = sorted(glob.glob(os.path.join(speech_dir(), "ggml-*.bin")))
    return (exe, models[0]) if exe and models else None


def program_env(exe: str) -> dict:
    """The environment to run *exe* in: on Linux its own folder holds the libraries it loads."""
    env = dict(os.environ)
    if os.name != "nt":
        folder = os.path.dirname(os.path.abspath(exe))
        env["LD_LIBRARY_PATH"] = folder + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def piper() -> Optional[Tuple[str, str]]:
    """(Piper, a voice's .onnx file), or None when no voice is set up."""
    base = speech_dir()
    exe = next((p for p in (os.path.join(base, "piper", "piper.exe"), os.path.join(base, "piper", "piper"))
                if os.path.isfile(p)), None)
    voices = sorted(glob.glob(os.path.join(base, "voices", "*.onnx")))
    return (exe, voices[0]) if exe and voices else None


#: The voice a call speaks with when none is chosen in Settings (Kokoro's best-rated one).
DEFAULT_VOICE = "af_heart"
_KOKORO = {"engine": None, "error": "", "from": "", "lock": threading.Lock()}


def packages_dir() -> str:
    """Where Settings installs the voice's Python packages: in the speech folder, not the app."""
    return os.path.join(speech_dir(), "python")


def use_packages() -> None:
    """Let Python find the packages Settings installed (the folder is added once, when it exists)."""
    import sys

    folder = packages_dir()
    if os.path.isdir(folder) and folder not in sys.path:
        sys.path.append(folder)


def kokoro_files() -> Optional[Tuple[str, str]]:
    """(Kokoro's model, its voices file), when both are there and the kokoro-onnx package is installed."""
    import importlib.util

    use_packages()

    base = os.path.join(speech_dir(), "kokoro")
    models = sorted(glob.glob(os.path.join(base, "kokoro-*.onnx")), key=lambda p: (".int8" in p, ".fp16" in p, p))
    voices = sorted(glob.glob(os.path.join(base, "voices-*.bin")))
    if not models or not voices or importlib.util.find_spec("kokoro_onnx") is None:
        return None
    return models[0], voices[0]


def _kokoro():
    """Kokoro, loaded once and kept (a second or two to load; then a sentence takes a fraction of that).

    The cache remembers which speech folder it was loaded from. Pointing CODEMONKEY_SPEECH
    somewhere else, or removing the folder, must not keep a voice that is no longer set up.
    """
    with _KOKORO["lock"]:
        base = speech_dir()
        if _KOKORO["engine"] is not None and _KOKORO["from"] != base:
            _KOKORO["engine"], _KOKORO["error"] = None, ""
        if _KOKORO["engine"] is None:
            files = kokoro_files()
            if files is None:
                return None
            import numpy as np
            import kokoro_onnx

            longest = getattr(kokoro_onnx, "MAX_PHONEME_LENGTH", 510)
            rate = getattr(kokoro_onnx, "SAMPLE_RATE", 24000)

            class Kokoro(kokoro_onnx.Kokoro):
                """The model is run with the input names and types it declares itself: model files and
                package versions disagree on them (the speed is a float in some, an int in others)."""

                def _create_audio(self, phonemes, voice, speed):
                    phonemes = phonemes[:longest]
                    tokens = np.array(self.tokenizer.tokenize(phonemes), dtype=np.int64)
                    style = np.array(voice[len(tokens)], dtype=np.float32)
                    kinds = {i.name: i.type for i in self.sess.get_inputs()}
                    ids = "input_ids" if "input_ids" in kinds else "tokens"
                    pace = np.array([speed], dtype=np.float32 if "float" in kinds.get("speed", "float") else np.int32)
                    audio = self.sess.run(None, {ids: [[0, *tokens, 0]], "style": style, "speed": pace})[0]
                    return audio, rate

            try:
                _KOKORO["engine"] = Kokoro(*files)
                _KOKORO["from"] = base
            except Exception as exc:  # noqa: BLE001 - the page falls back to the system's voices
                _KOKORO["error"] = f"{type(exc).__name__}: {exc}"
                return None
        return _KOKORO["engine"]


def warm() -> None:
    """Load the voice in the background (a call is starting), so its first sentence is not kept waiting."""
    if kokoro_files() is not None and _KOKORO["engine"] is None:
        threading.Thread(target=_kokoro, name="voice-warm", daemon=True).start()


def voices() -> List[str]:
    engine = _kokoro() if _KOKORO["engine"] is not None else None
    if engine is not None:
        return sorted(engine.get_voices())
    files = kokoro_files()
    if files is None:
        return []
    try:
        import numpy as np

        with np.load(files[1]) as data:
            return sorted(data.files)
    except Exception:  # noqa: BLE001
        return []


def status() -> dict:
    voice = piper()
    if kokoro_files() is not None:
        kind = "kokoro"
    elif voice:
        kind = "piper:" + os.path.basename(voice[1])[:-5]
    else:
        kind = "system"
    return {"hear": whisper() is not None, "voice": kind}


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
                              timeout=120, creationflags=_NO_WINDOW, env=program_env(tool[0]))
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    lines = [ln.strip() for ln in (done.stdout or "").splitlines() if ln.strip() and not _NOT_SPEECH.match(ln)]
    return " ".join(lines).strip()


def _wav(samples, rate: int) -> bytes:
    import io

    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


#: Written shorthand a voice would read letter by letter, or stumble over, and what is said instead.
_SAID = [
    (r"\be\.g\.,?", "for example,"), (r"\bi\.e\.,?", "that is,"), (r"\betc\.", "and so on."),
    (r"\bvs\.?(?=\s)", "versus"), (r"\s&\s", " and "), (r"\s*(->|\u2192)\s*", " to "),
    (r"\s+[\u2014\u2013-]\s+", ", "), (r"\u2026", "..."), (r"(?<=\d)\s*%", " percent"),
    (r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", ""),        # emoji: nothing to say
    (r"[*_#`~|<>\[\]{}]+", " "),
]


def spoken(text: str) -> str:
    """*text* as it should be said: shorthand spelled out, symbols and emoji left out, spaces tidied."""
    text = str(text or "")
    for pattern, said in _SAID:
        text = re.sub(pattern, said, text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([,.!?;:])", r"\1", text).strip()


def speak(text: str, voice: str = "", speed: float = 1.0, blend: str = "", blend_pct: int = 0) -> bytes:
    """*text* said by the voice set up (Kokoro, else Piper), as a WAV file's bytes. With Kokoro, *blend*
    mixes a second voice in (*blend_pct* of it): blends often sound warmer than either voice alone.
    RuntimeError when no voice is set up, so the page speaks with the system's voices."""
    engine = _kokoro()
    if engine is not None:
        text = spoken(text)[:2000]
        if not text:
            return b""
        names = engine.get_voices()
        chosen = voice if voice in names else (DEFAULT_VOICE if DEFAULT_VOICE in names else names[0])
        lang = "en-gb" if chosen[:1] == "b" else "en-us"
        style = chosen
        share = max(0, min(70, int(blend_pct or 0))) / 100.0
        if blend in names and blend != chosen and share > 0:
            style = engine.get_voice_style(chosen) * (1 - share) + engine.get_voice_style(blend) * share
        with _KOKORO["lock"]:
            samples, rate = engine.create(text, voice=style, speed=max(0.5, min(2.0, float(speed or 1.0))), lang=lang)
        return _wav(samples, rate)
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
