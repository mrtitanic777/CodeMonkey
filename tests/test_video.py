"""Watching a video as frames, and saving one from a direct link (webui/video.py)."""

import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui.video import clock, ffmpeg, pick_times, save_video, watch  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not (ffmpeg() and ffmpeg("ffprobe")), reason="ffmpeg is not installed")


def make_video(path, colors=("red", "blue", "green"), each=10):
    """A video of solid colours, *each* seconds apart: a change of scene at every colour."""
    args = [ffmpeg(), "-loglevel", "error", "-y"]
    for c in colors:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=320x180:d={each}:r=10"]
    args += ["-filter_complex", "".join(f"[{i}]" for i in range(len(colors))) + f"concat=n={len(colors)}:v=1[v]",
             "-map", "[v]", "-pix_fmt", "yuv420p", path]
    subprocess.run(args, check=True, capture_output=True)


def test_moments_to_look_at():
    assert [t for t, _ in pick_times(0, 5, 24, [])] == [0.5, 1.5, 2.5, 3.5, 4.5]      # short: one a second
    long = pick_times(0, 600, 24, [100.0, 300.0])
    assert len(long) <= 24 and [w for _, w in long].count("scene change") == 2
    assert all(a[0] < b[0] for a, b in zip(long, long[1:])) and long[-1][0] < 600
    assert len(pick_times(0, 600, 10, [float(i) for i in range(5, 595, 5)])) <= 10      # many changes are thinned
    assert clock(75) == "1:15" and clock(3725) == "1:02:05"


@needs_ffmpeg
def test_watching_a_short_and_a_long_video():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "videos"))
        make_video(os.path.join(tmp, "videos", "short.mp4"), each=4)
        frames = os.path.join(tmp, "frames")
        said = watch(tmp, "videos/short.mp4", frames, frames=24)
        assert "0:12 long" in said and "no sound" in said and "one a second" in said, said
        assert len(re.findall(r"\[\[cm-image ", said)) == 12 and len(os.listdir(frames)) == 12

        make_video(os.path.join(tmp, "videos", "long.mp4"), each=20)
        said = watch(tmp, "videos/long.mp4", frames, frames=12)
        assert "2 change(s) of scene" in said and said.count("(scene change)") == 2, said
        assert len(re.findall(r"\[\[cm-image ", said)) <= 12 and len(os.listdir(frames)) <= 12, "old frames go"
        assert "start and end" in said

        closer = watch(tmp, "videos/long.mp4", frames, start=18, end=24)
        assert "from 0:18 to 0:24" in closer and len(re.findall(r"\[\[cm-image ", closer)) == 6, closer

        assert watch(tmp, "../outside.mp4", frames).startswith("ERROR")
        assert watch(tmp, "videos/none.mp4", frames).startswith("ERROR")
        open(os.path.join(tmp, "notes.mp4"), "w").write("not a video")
        assert watch(tmp, "notes.mp4", frames).startswith("ERROR")


@needs_ffmpeg
def test_saving_a_video_from_a_direct_link():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        make_video(src, each=2)
        data = open(src, "rb").read()

        class Site(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body, kind = (data, "video/mp4") if self.path == "/clip.mp4" else (b"<html>hi</html>", "text/html")
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        project = os.path.join(tmp, "project")
        os.makedirs(project)
        try:
            said = save_video(project, base + "/clip.mp4")
            assert said.startswith("Saved videos/clip.mp4 (0:06, 320×180, no sound") and "video_watch" in said, said
            assert open(os.path.join(project, "videos", "clip.mp4"), "rb").read() == data
            assert "videos/clip-2.mp4" in save_video(project, base + "/clip.mp4"), "never overwrites"
            assert save_video(project, base + "/page").startswith("ERROR"), "a page is not a video file"
            assert "card" in save_video(project, "https://www.youtube.com/watch?v=aqz-KE-bpKQ")
            assert save_video(project, base + "/clip.mp4", "../out.mp4").startswith("ERROR")
            assert not [f for f in os.listdir(os.path.join(project, "videos")) if f.endswith(".part")]
        finally:
            server.shutdown()


def test_the_tools_ask_like_their_kind():
    from webui.app import TOOL_GROUPS, tool_kind

    assert tool_kind("video_watch") == "read", "watching a file in the project reads it"
    assert tool_kind("video_save") == "network", "saving from the web asks like the other web tools"
    assert set(TOOL_GROUPS["video"][1]) == {"video_watch", "video_save"}


def test_a_session_shows_pictures_to_the_model_chosen_in_the_menu():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.config.providers["seeing"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "",
                                              "models": ["m"], "vision": True}
            s = app.create_session(tmp)
            assert not s.loop.sees_images()
            s.settings["provider"], s.settings["model"] = "seeing", "m"
            s.enqueue_message("hi", [])          # the turn fails (nothing listens there); the switch is what counts
            import time
            deadline = time.time() + 30
            while time.time() < deadline and (s.status != "idle" or not s.history):
                time.sleep(0.2)
            assert s.loop.sees_images(), "the model picked in the menu decides, not the session's first one"
            s.close()
        finally:
            server.shutdown()


def test_what_is_said_comes_with_times(monkeypatch, tmp_path):
    import webui.video as video

    monkeypatch.setattr(video, "whisper", lambda: None)
    assert "not set up" in video.heard("x.mp4", str(tmp_path), 0, 10)[0]
    monkeypatch.setattr(video, "whisper", lambda: ("whisper-cli", "ggml-base.en.bin"))
    monkeypatch.setattr(video, "transcribe", lambda v, w: [(0.0, 2.0, "Welcome."), (65.0, 70.0, "Add salt.")])
    lines = video.heard("x.mp4", str(tmp_path), 0, 80)
    assert lines[1:] == ["[0:00] Welcome.", "[1:05] Add salt."]
    assert video.heard("x.mp4", str(tmp_path), 60, 80)[1:] == ["[1:05] Add salt."], "only the part asked about"
    assert "nothing" in video.heard("x.mp4", str(tmp_path), 10, 20)[0]


def _spoken_video(folder):
    """A clip with a sentence said by Windows' own voice (no download), or None elsewhere."""
    if os.name != "nt":
        return None
    wav, mp4 = os.path.join(folder, "say.wav"), os.path.join(folder, "say.mp4")
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('The secret ingredient is a spoonful of honey.'); $s.Dispose()")
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    if done.returncode or not os.path.isfile(wav):
        return None
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=10", "-i", wav,
                    "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", mp4], check=True, capture_output=True)
    return mp4


@needs_ffmpeg
def test_a_spoken_video_is_heard():
    import webui.video as video

    if video.whisper() is None:
        pytest.skip("speech-to-text is not set up")
    with tempfile.TemporaryDirectory() as tmp:
        clip = _spoken_video(tmp)
        if clip is None:
            pytest.skip("no voice to make a spoken clip with")
        work = os.path.join(tmp, "work")
        said = watch(tmp, "say.mp4", os.path.join(work, "frames"))
        assert "has sound" in said and "What is said" in said and "honey" in said.lower(), said
        assert os.listdir(os.path.join(work, "transcripts")), "kept, so a closer look does not transcribe again"
