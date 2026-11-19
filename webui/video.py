"""Watching a video: frames picked with ffmpeg and shown to the model as pictures.

A model that sees pictures (Strata, Claude, ...) does not take video, so a video is shown to it as
a handful of frames: one a second for a short clip, and for a longer one a frame at each change of
scene plus frames spread evenly between them, so nothing long goes unseen. Each frame costs the
model about 150 tokens at 512 pixels wide, which is why it is never every frame (a minute of video
at 30 frames a second would fill a 256K window); the model asks again for a closer look at a part
(start and end).
What is said is read by whisper.cpp when it is set up (speech_dir()), as lines with times.

    watch(project_dir, "videos/clip.mp4", frames_dir)       -> text with picture markers (images.py)
    save_video(project_dir, "https://example.com/clip.mp4")  -> videos/clip.mp4
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .speech import whisper

MAX_BYTES = 500 * 1024 * 1024
FOLDER = "videos"
DEFAULT_FRAMES = 24
MOST_FRAMES = 64
WIDTH = 512
_UA = "Mozilla/5.0 (CodeMonkey; +https://github.com/mrtitanic777/CodeMonkey)"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIDEO_TYPES = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".ogv", ".gif")


def ffmpeg(name: str = "ffmpeg") -> Optional[str]:
    """ffmpeg or ffprobe: on the PATH, or where winget puts it (its PATH entry needs a new shell)."""
    found = shutil.which(name)
    if found:
        return found
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    for hit in sorted(glob.glob(os.path.join(base, "*FFmpeg*", "*", "bin", name + ".exe")), reverse=True):
        return hit
    return None


def _run(args: List[str], timeout: float = 600, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                          creationflags=_NO_WINDOW, env=env)


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def probe(path: str) -> dict:
    out = _run([ffmpeg("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 60)
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("there is no video in this file")
    duration = float((info.get("format") or {}).get("duration") or video.get("duration") or 0)
    return {"duration": duration, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}


def scene_changes(path: str, start: float, end: float, threshold: float = 0.3) -> List[float]:
    """Times (seconds from the start of the video) where the picture changes a lot."""
    out = _run([ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", path,
                "-an", "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], 900)
    return [start + float(t) for t in re.findall(r"pts_time:([0-9.]+)", out.stderr)]


def pick_times(start: float, end: float, most: int, scenes: List[float]) -> List[Tuple[float, str]]:
    """Up to *most* moments to look at, in order, each with why it was picked."""
    span = max(0.0, end - start)
    if span <= 0:
        return [(start, "")]
    if span <= most:                                   # short: one a second, from the middle of each second
        return [(start + i + 0.5, "") for i in range(int(span))] or [(start + span / 2, "")]
    even_count = max(2, most // 3) if scenes else most
    picked = [(start + span * (i + 0.5) / even_count, "") for i in range(even_count)]
    room = most - len(picked)
    if scenes and room > 0:                            # the changes of scene, thinned evenly if there are many
        step = max(1.0, len(scenes) / room)
        picked += [(scenes[int(i * step)] + 0.15, "scene change") for i in range(min(room, len(scenes)))]
    picked.sort()
    out: List[Tuple[float, str]] = []
    for t, why in picked:                              # two frames less than half a second apart: keep one
        if out and t - out[-1][0] < 0.5:
            if why and not out[-1][1]:
                out[-1] = (t, why)
            continue
        out.append((min(t, end - 0.05), why))
    return out


def _inside(project_dir: str, rel: str) -> str:
    root = os.path.realpath(project_dir)
    full = os.path.realpath(os.path.join(root, str(rel or "").replace("\\", "/").lstrip("/")))
    if os.path.commonpath([root, full]) != root:
        raise ValueError("the video must be inside the project")
    return full


def watch(project_dir: str, path: str, frames_dir: str, start: float = 0, end: float = 0,
          frames: int = DEFAULT_FRAMES) -> str:
    """Frames of the project's video at *path* for the model: a line per frame with its picture marker."""
    import images

    if not ffmpeg() or not ffmpeg("ffprobe"):
        return "ERROR: watching a video needs ffmpeg, which is not installed (Windows: winget install Gyan.FFmpeg)"
    try:
        full = _inside(project_dir, path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not os.path.isfile(full):
        return f"ERROR: no such file: {path} (save a video from the web with video_save first)"
    try:
        info = probe(full)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return f"ERROR: could not read {path} as a video: {exc}"
    duration = info["duration"]
    start = max(0.0, float(start or 0))
    end = min(duration, float(end or 0) or duration) if duration else float(end or 0)
    if duration and start >= duration:
        return f"ERROR: the video is only {clock(duration)} long"
    most = max(1, min(MOST_FRAMES, int(frames or DEFAULT_FRAMES)))
    scenes = scene_changes(full, start, end) if end - start > most else []
    times = pick_times(start, end, most, scenes)

    os.makedirs(frames_dir, exist_ok=True)
    for old in glob.glob(os.path.join(frames_dir, "*.jpg")):
        try:
            os.remove(old)
        except OSError:
            pass
    lines = []
    for n, (t, why) in enumerate(times, 1):
        out = os.path.join(frames_dir, f"frame-{n:03d}-{int(t * 1000):09d}.jpg")
        _run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", full, "-frames:v", "1",
              "-vf", f"scale='min({WIDTH},iw)':-2", "-q:v", "4", out], 120)
        if os.path.isfile(out):
            lines.append(f"Frame {n} at {clock(t)}" + (f" ({why})" if why else "") + "\n" + images.mark(out))
    if not lines:
        return f"ERROR: no frames could be taken from {path}"
    part = f" from {clock(start)} to {clock(end)}" if start > 0 or (duration and end < duration - 0.5) else ""
    how = ("one a second" if end - start <= most else
           f"{len(scenes)} change(s) of scene found; frames at those and spread evenly between")
    head = [f"{path}: {clock(duration)} long, {info['width']}×{info['height']}, "
            f"{'has sound' if info['audio'] else 'no sound'}. {len(lines)} frames{part} ({how}), in order:"]
    said: List[str] = []
    if info["audio"]:
        said = heard(full, os.path.dirname(frames_dir.rstrip("/\\")), start, end)
    tail = ["To look closer at a part, call again with start and end (seconds)."] if end - start > most else []
    return "\n".join(head + lines + said + tail)


# ------------------------------------------------------------------ sound ---

#: The longest transcript given back in one go; a longer one says to ask for a part.
MOST_SAID = 12000


def transcribe(video: str, work_dir: str) -> List[Tuple[float, float, str]]:
    """What is said in *video*, as (from, to, text) in seconds. Kept beside the frames by the file's
    size and time, so asking about a part again does not transcribe it all again."""
    import hashlib

    tool = whisper()
    if tool is None:
        raise RuntimeError("speech-to-text is not set up")
    st = os.stat(video)
    key = hashlib.sha1(f"{os.path.realpath(video)}|{st.st_size}|{st.st_mtime}|{os.path.basename(tool[1])}".encode()).hexdigest()[:16]
    cache = os.path.join(work_dir, "transcripts", key + ".json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as fh:
            return [tuple(x) for x in json.load(fh)]
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    wav, out = cache[:-5] + ".wav", cache[:-5] + "-whisper"
    _run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", wav], 900)
    try:
        from .speech import program_env, threads

        _run([tool[0], "-m", tool[1], "-f", wav, "-t", threads(), "-np", "-oj", "-of", out], 3600,
             env=program_env(tool[0]))
        with open(out + ".json", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    finally:
        for leftover in (wav, out + ".json"):
            try:
                os.remove(leftover)
            except OSError:
                pass
    pieces = [(s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0, str(s.get("text") or "").strip())
              for s in data.get("transcription") or []]
    pieces = [p for p in pieces if p[2] and not re.fullmatch(r"[\[(][^\])]*[\])]", p[2])]   # [BLANK_AUDIO], (music)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(pieces, fh)
    return pieces


def heard(video: str, work_dir: str, start: float, end: float) -> List[str]:
    """Lines saying what is said between *start* and *end*, for the model."""
    if whisper() is None:
        return ["The sound is not heard (speech-to-text is not set up): you see the pictures only. "
                "Say so if the answer depends on what is said."]
    try:
        pieces = [p for p in transcribe(video, work_dir) if p[1] > start and p[0] < end]
    except Exception as exc:  # noqa: BLE001 - the pictures still stand
        return [f"The sound could not be transcribed ({exc}): you see the pictures only."]
    if not pieces:
        return ["What is said: nothing (no speech in this part)."]
    lines, size = ["What is said (transcribed by a small speech-to-text model: names and rare words may be wrong):"], 0
    for a, _, text in pieces:
        line = f"[{clock(a)}] {text}"
        size += len(line)
        if size > MOST_SAID:
            lines.append(f"... (more is said after {clock(a)}: call again with start={int(a)} to read on)")
            break
        lines.append(line)
    return lines


def save_video(project_dir: str, url: str, path: str = "") -> str:
    """Save the video file at *url* into the project (a direct link to the file; pages are not searched)."""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return "ERROR: give an http(s) address of a video file"
    host = urllib.parse.urlsplit(url).hostname or ""
    if re.search(r"(^|\.)(youtube\.com|youtu\.be|vimeo\.com|tiktok\.com|instagram\.com|facebook\.com|x\.com|twitter\.com)$", host):
        return ("ERROR: that site does not offer the video as a file to save (and its terms do not allow it). "
                "Put the link in your reply instead: YouTube and Vimeo links show as a card the user can play.")
    root = os.path.realpath(project_dir)
    if path:
        rel = path.replace("\\", "/").lstrip("/")
    else:
        name = os.path.basename(urllib.parse.urlsplit(url).path)
        stem, ext = os.path.splitext(urllib.parse.unquote(name))
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:60] or "video"
        rel = f"{FOLDER}/{stem}{ext.lower() if ext.lower() in VIDEO_TYPES else '.mp4'}"
    try:
        full = _inside(root, rel)
    except ValueError as exc:
        return f"ERROR: {exc}"
    stem, ext = os.path.splitext(full)
    n = 2
    while os.path.exists(full):
        full = f"{stem}-{n}{ext}"
        n += 1
    os.makedirs(os.path.dirname(full), exist_ok=True)
    part = full + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "video/*,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
            kind = resp.headers.get("Content-Type", "")
            if "html" in kind.lower():
                raise ValueError("that address is a page, not a video file (look in the page for the file's own link)")
            got = 0
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                got += len(block)
                if got > MAX_BYTES:
                    raise ValueError(f"the video is bigger than {MAX_BYTES // (1024 * 1024)} MB")
                fh.write(block)
        if ffmpeg("ffprobe"):
            info = probe(part)
        else:
            info = None
        os.replace(part, full)
    except Exception as exc:  # noqa: BLE001 - reported to the model
        try:
            os.remove(part)
        except OSError:
            pass
        return f"ERROR: could not save the video: {exc}"
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    about = (f"{clock(info['duration'])}, {info['width']}×{info['height']}, "
             f"{'has sound' if info['audio'] else 'no sound'}, ") if info else ""
    return f"Saved {rel} ({about}{max(1, got // (1024 * 1024))} MB) from {url}. Watch it with video_watch."
