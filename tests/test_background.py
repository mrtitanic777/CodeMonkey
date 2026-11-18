"""A new message while a command runs: the command goes on in the background, the message is
answered, and the agent reports back when the command ends (coder/processes.py, webui/app.py)."""

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coder.processes import ProcessManager  # noqa: E402
from test_webui import (  # noqa: E402
    SSEReader,
    http,
    make_session_with_fake,
    start_server,
    text_msg,
    tool_call_msg,
)

EXE = sys.executable.replace("\\", "/")   # forward slashes: the shell may be bash, even on Windows
PY = f'"{EXE}"' if " " in EXE else EXE
SLOW = PY + " -c \"import time; print('starting', flush=True); time.sleep(3); print('all done', flush=True)\""


def test_a_command_moved_to_the_background_says_when_it_ends():
    with tempfile.TemporaryDirectory() as tmp:
        pm = ProcessManager(tmp)
        ended = []
        pm.on_finish = ended.append
        detach = threading.Event()
        threading.Timer(0.8, detach.set).start()
        said = pm.run_or_detach(SLOW, 60, detach, lambda: "Moved for a test")
        assert said.startswith("Moved for a test as 'bash-1'") and "You will be told when it finishes" in said, said
        deadline = time.time() + 15
        while time.time() < deadline and not ended:
            time.sleep(0.1)
        assert ended and ended[0].state() == "exited with code 0"
        assert "all done" in "\n".join(ended[0].tail(10, False))

        # one stopped on purpose is not reported
        detach.clear()
        threading.Timer(0.8, detach.set).start()
        pm.run_or_detach(SLOW, 60, detach)
        pm.stop("bash-2")
        time.sleep(1.5)
        assert len(ended) == 1
        pm.stop_all()


def test_a_new_message_moves_the_command_to_the_background_and_it_reports_back():
    with tempfile.TemporaryDirectory() as tmp:
        script = [
            tool_call_msg("bash", {"cmd": SLOW, "timeout": 60}, "c1"),
            tool_call_msg("bash", {"cmd": "echo should not run"}, "c2"),    # refused: the user is waiting
            text_msg("The build is still going in the background; I'll tell you when it's done."),
            text_msg("Paris."),                                              # the new question
            text_msg("The command finished fine."),                          # the report
        ]
        app, server, base = start_server(tmp)
        sid, fake = make_session_with_fake(base, tmp, script)
        seen = []
        real = fake.chat
        fake.chat = lambda messages, tools: (seen.append(list(messages)), real(messages, tools))[1]
        http("POST", f"{base}/api/sessions/{sid}/settings", {"permission_mode": "bypass"})
        reader = SSEReader(f"{base}/api/sessions/{sid}/events")
        reader.start()
        reader.wait_for("state")
        try:
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "run the build"})
            reader.wait_for("tool_call")
            time.sleep(0.8)                                  # the command is running now
            http("POST", f"{base}/api/sessions/{sid}/message", {"text": "what is the capital of France?"})
            deadline = time.time() + 40
            while time.time() < deadline and sum(1 for t, _ in reader.events if t == "turn_end") < 3:
                time.sleep(0.2)
            ends = sum(1 for t, _ in reader.events if t == "turn_end")
            assert ends == 3, [t for t, _ in reader.events]
            results = [d["result"] for t, d in reader.events if t == "tool_result"]
            assert "new message, so this command moved to the background as 'bash-1'" in results[0], results[0]
            assert "call no more tools" in results[0]
            assert results[1].startswith("Not run."), results[1]
            notices = [d.get("text", "") for t, d in reader.events if t == "notice"]
            assert any("carries on in the background" in n for n in notices), notices
            # the question was answered before the command ended, and the report came after
            assert "capital of France" in str(seen[3][-1])
            report = str(seen[4])
            assert "bash-1" in report and "exited with code 0" in report and "all done" in report, report[-600:]
        finally:
            reader.stop() if hasattr(reader, "stop") else None
            server.shutdown()
