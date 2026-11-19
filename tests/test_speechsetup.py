"""Setting up hearing and the call voice: downloads checked against their checksums, unpacked safely
(webui/speechsetup.py). A local server stands in for the real download sites."""

import hashlib
import io
import os
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from webui import speech, speechsetup  # noqa: E402


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _tgz(files, link=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link[0])
            info.type, info.linkname = tarfile.SYMTYPE, link[1]
            t.addfile(info)
    return buf.getvalue()


@pytest.fixture
def site(tmp_path, monkeypatch):
    served = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = served.get(self.path.lstrip("/"))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    monkeypatch.setenv("CODEMONKEY_SPEECH", str(tmp_path / "speech"))
    monkeypatch.setattr(speechsetup.Setup, "_pip", lambda self, package: None)   # never a real pip install
    yield served, {"whisper": base, "model": base, "kokoro": base}
    server.shutdown()


def _serve(served, name, data):
    served[name] = data
    return (name, len(data), hashlib.sha256(data).hexdigest())


def _wait(setup):
    deadline = time.time() + 20
    while time.time() < deadline and setup.snapshot()["running"]:
        time.sleep(0.05)
    return setup.snapshot()


def test_hearing_is_fetched_checked_and_unpacked(site, monkeypatch):
    served, bases = site
    exe = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    program = _serve(served, "w.zip", _zip({f"Release/{exe}": b"program", "Release/whisper.dll": b"lib"}))
    model = _serve(served, "ggml-base.en-q5_1.bin", b"model bytes")
    monkeypatch.setattr(speechsetup, "PROGRAMS", {speechsetup.this_platform(): program})
    monkeypatch.setattr(speechsetup, "HEARING_MODEL", model)
    monkeypatch.setattr(speech.shutil, "which", lambda name: None)
    items = speechsetup.plan(["hear"], bases)
    assert [i["kind"] for i in items] == ["archive", "file"] and sum(i["size"] for i in items) == program[1] + model[1]
    setup = speechsetup.Setup()
    done = _wait_start(setup, ["hear"], bases)
    assert done["done"] and not done["error"] and done["got"] == done["total"], done
    assert speech.whisper() is not None, "the program sits in bin/ without the archive's top folder"
    assert os.path.basename(speech.whisper()[0]) == exe
    assert speechsetup.plan(["hear"], bases) == [], "nothing left to fetch"


def _wait_start(setup, parts, bases):
    setup.start(parts, bases)
    return _wait(setup)


def test_a_download_that_does_not_match_its_checksum_is_not_used(site, monkeypatch):
    served, bases = site
    name, size, _ = _serve(served, "ggml-base.en-q5_1.bin", b"tampered")
    monkeypatch.setattr(speechsetup, "HEARING_MODEL", (name, size, "0" * 64))
    monkeypatch.setattr(speechsetup, "PROGRAMS", {})
    done = _wait_start(speechsetup.Setup(), ["hear"], bases)
    assert not done["done"] and "checksum" in done["error"], done
    assert not os.listdir(speech.speech_dir()) or not any(f.startswith("ggml-") for f in os.listdir(speech.speech_dir()))


def test_an_archive_that_points_outside_its_folder_is_refused(site, monkeypatch):
    served, bases = site
    bad = _serve(served, "bad.tgz", _tgz({"top/whisper-cli": b"x"}, link=("top/escape", "../../../outside")))
    monkeypatch.setattr(speechsetup, "PROGRAMS", {speechsetup.this_platform(): bad})
    monkeypatch.setattr(speechsetup, "HEARING_MODEL", _serve(served, "ggml-base.en-q5_1.bin", b"m"))
    monkeypatch.setattr(speech.shutil, "which", lambda name: None)
    done = _wait_start(speechsetup.Setup(), ["hear"], bases)
    assert not done["done"] and done["error"], done
    assert not os.path.exists(os.path.join(speech.speech_dir(), "..", "outside"))


def test_the_voice_files_are_fetched(site, monkeypatch):
    served, bases = site
    files = [_serve(served, "kokoro-v1.0.onnx", b"onnx"), _serve(served, "voices-v1.0.bin", b"voices")]
    monkeypatch.setattr(speechsetup, "VOICE_FILES", files)
    monkeypatch.setattr(speech, "kokoro_files", lambda: None)
    items = speechsetup.plan(["voice"], bases)
    assert {i["what"] for i in items} >= {"Kokoro voice model", "Kokoro voice pack"}
    done = _wait_start(speechsetup.Setup(), ["voice"], bases)
    assert done["done"], done
    folder = os.path.join(speech.speech_dir(), "kokoro")
    assert sorted(os.listdir(folder)) == ["kokoro-v1.0.onnx", "voices-v1.0.bin"]
