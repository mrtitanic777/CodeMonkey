"""Model servers that start with CodeMonkey (webui/autostart.py), with a small pretend server."""

import os
import socket
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SERVER = (
    "import json, sys\n"
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def log_message(self, *a): pass\n"
    "    def do_GET(self):\n"
    "        body = json.dumps({'data': [{'id': 'tiny-model'}]}).encode()\n"
    "        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "s = HTTPServer(('127.0.0.1', int(sys.argv[1])), H)\n"
    "s.timeout = 1\n"
    "import time; end = time.time() + 40\n"
    "while time.time() < end: s.handle_request()\n"
)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_server_with_a_start_command_is_started_once_and_noticed():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "pretend_server.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(SERVER)
        port = _free_port()
        app, server, base = start_server(tmp)
        pid = None
        try:
            app.providers.save("pretend", f"http://127.0.0.1:{port}/v1", ["tiny-model"])
            try:
                app.providers.set_start("ollama-not-added-here", "x")
                raise AssertionError("only servers added in Settings take a start command")
            except KeyError:
                pass
            app.providers.set_start("pretend", f'"{sys.executable}" "{script}" {port}')
            assert app.config_snapshot()["providers"]["pretend"]["start_command"].endswith(str(port))
            app.autostart.start_all()
            assert app.autostart.status()["pretend"]["state"] == "starting", app.autostart.status()
            deadline = time.time() + 30
            while time.time() < deadline and app.autostart.status()["pretend"]["state"] == "starting":
                time.sleep(0.3)
            assert app.autostart.status()["pretend"]["state"] == "running", app.autostart.status()
            # already answering: not started a second time
            pid = app.autostart.status()["pretend"]["pid"]
            app.autostart.start_all()
            assert app.autostart.status()["pretend"]["state"] == "already"
            # a re-save of the server keeps its start command
            app.providers.save("pretend", f"http://127.0.0.1:{port}/v1", ["tiny-model"])
            assert app.providers.custom["pretend"]["start_command"]
        finally:
            server.shutdown()
            if pid:   # the pretend server and the shell that started it
                import subprocess
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    import signal
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except OSError:
                        pass
                time.sleep(1)


if __name__ == "__main__":
    test_a_server_with_a_start_command_is_started_once_and_noticed()
    print("PASS")
