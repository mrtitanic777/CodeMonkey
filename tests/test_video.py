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
