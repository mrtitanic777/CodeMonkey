"""A voice call: hearing what was said (whisper.cpp), and messages said aloud (webui/speech.py)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui import speech  # noqa: E402


def _said(folder, sentence):
    """A WAV of *sentence* in Windows' own voice at 16 kHz mono, or None elsewhere."""
    if os.name != "nt":
        return None
    wav = os.path.join(folder, "said.wav")
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
              "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
              f"$s.SetOutputToWaveFile('{wav}', $f); $s.Speak('{sentence}'); $s.Dispose()")
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    return wav if done.returncode == 0 and os.path.isfile(wav) else None


def test_what_a_call_can_do_here(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEMONKEY_SPEECH", str(tmp_path))
    assert speech.status() == {"hear": False, "voice": "system"}
    with pytest.raises(RuntimeError):
        speech.hear(b"RIFF")
    with pytest.raises(RuntimeError):
        speech.speak("hello")


def test_hearing_a_sentence():
    if speech.whisper() is None:
        pytest.skip("speech-to-text is not set up")
    with tempfile.TemporaryDirectory() as tmp:
        wav = _said(tmp, "Please open the settings page.")
        if wav is None:
            pytest.skip("no voice to say a sentence with")
        heard = speech.hear(open(wav, "rb").read()).lower()
        assert "settings" in heard and "open" in heard, heard


def test_a_message_said_aloud_is_answered_the_way_people_talk():
    from test_webui import start_server

    from webui.app import VOICE_NOTE

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            s = app.create_session(tmp)
            sent = []
            real = s.loop.run

            def run(text, *a, **k):
                sent.append(text)
                return real(text, *a, **k)

            s.loop.run = run
            req = urllib.request.Request(f"{base}/api/sessions/{s.id}/message", method="POST",
                                         data=json.dumps({"text": "what time is it", "voice": True}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            deadline = time.time() + 30
            while time.time() < deadline and not (sent and s.status == "idle"):
                time.sleep(0.1)
            assert sent and sent[0].endswith(VOICE_NOTE), "the model is told it is a call"
            users = [e for e in s.transcript if e.get("kind") == "user"]
            assert users[-1]["text"] == "what time is it" and users[-1].get("voice") is True
            assert VOICE_NOTE not in json.dumps(s.transcript), "the transcript shows what was said, nothing else"
            s._voice = True
            assert s._turn_effort_for("Add a settings page with a dark mode switch") == "low", "a call answers quickly"
            assert s._turn_effort_for("Think hard about how the renderer should be split up") == "high"
            s.close()
        finally:
            server.shutdown()


def test_the_hearing_endpoint():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            s = app.create_session(tmp)
            with urllib.request.urlopen(f"{base}/api/speech") as r:
                assert {"hear", "voice", "voices", "chosen"} <= set(json.load(r))
            if speech.whisper() is None:
                return
            wav = _said(tmp, "Hello there.")
            if wav is None:
                return
            req = urllib.request.Request(f"{base}/api/sessions/{s.id}/hear", data=open(wav, "rb").read(),
                                         method="POST", headers={"Content-Type": "audio/wav"})
            with urllib.request.urlopen(req) as r:
                assert "hello" in json.load(r)["text"].lower()
            try:
                urllib.request.urlopen(urllib.request.Request(f"{base}/api/sessions/{s.id}/hear", data=b"",
                                                              method="POST"))
                raise AssertionError("an empty recording was taken")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            s.close()
        finally:
            server.shutdown()


def test_the_call_voice_speaks():
    if speech.kokoro_files() is None:
        pytest.skip("Kokoro is not set up")
    import wave
    import io

    data = speech.speak("Hello, this is a test.", "am_michael", 1.1)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 24000 and w.getnframes() / w.getframerate() > 0.8
    assert speech.speak("Unknown voice falls back.", "zz_nobody")[:4] == b"RIFF"
    assert "af_heart" in speech.voices() and speech.status()["voice"] == "kokoro"


def test_call_voice_settings_are_kept_in_range():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.update_settings({"call_voice": "bf_emma", "call_speed": 9})
            assert app.settings["call_voice"] == "bf_emma" and app.settings["call_speed"] == 2.0
            app.update_settings({"call_speed": "fast"})
            assert app.settings["call_speed"] == 1.0
        finally:
            server.shutdown()


def test_written_shorthand_is_said_in_words():
    assert speech.spoken("Use a dict, e.g. a map - about 40% faster 🚀") == "Use a dict, for example, a map, about 40 percent faster"
    assert speech.spoken("**Done** in `app.py` — all tests pass…") == "Done in app.py, all tests pass..."
    assert speech.spoken("Python vs. Rust & Go") == "Python versus Rust and Go"


def test_a_blend_of_two_voices():
    if speech.kokoro_files() is None:
        pytest.skip("Kokoro is not set up")
    engine = speech._kokoro()
    seen = []
    real = engine.create
    engine.create = lambda text, voice, **k: (seen.append(voice), real(text, voice=voice, **k))[1]
    try:
        assert speech.speak("A short test.", "af_heart", 1.0, "af_bella", 30)[:4] == b"RIFF"
        speech.speak("A short test.", "af_heart", 1.0, "af_heart", 30)
    finally:
        engine.create = real
    heart, bella = engine.get_voice_style("af_heart"), engine.get_voice_style("af_bella")
    assert abs(seen[0] - (heart * 0.7 + bella * 0.3)).max() < 1e-6, "30% of the second voice"
    assert seen[1] == "af_heart", "a voice blended with itself is just itself"


def test_blend_settings_are_kept_in_range():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            app.update_settings({"call_blend": "af_bella", "call_blend_pct": 99})
            assert app.settings["call_blend"] == "af_bella" and app.settings["call_blend_pct"] == 70
        finally:
            server.shutdown()
