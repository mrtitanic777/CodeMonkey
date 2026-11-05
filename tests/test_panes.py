"""The file pane and the terminal pane: their endpoints, and that they stay inside the session's folder."""

import os
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import http, make_session_with_fake, start_server  # noqa: E402


def test_file_pane_reads_saves_and_refuses_what_it_should():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"))
        with open(os.path.join(tmp, "src", "a.py"), "w", encoding="utf-8", newline="") as fh:
            fh.write("x = 1\n")
        with open(os.path.join(tmp, ".mcp.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(tmp, "blob.bin"), "wb") as fh:
            fh.write(b"\0\1\2")
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            url = f"{base}/api/sessions/{sid}/file"
            status, f = http("GET", url + "?path=" + urllib.parse.quote("src/a.py"))
            assert status == 200 and f["text"] == "x = 1\n" and f["path"] == "src/a.py" and not f["view_only"], f

            status, saved = http("POST", url, {"path": "src/a.py", "text": "x = 2\n", "mtime": f["mtime"]})
            assert status == 200, saved
            with open(os.path.join(tmp, "src", "a.py"), encoding="utf-8", newline="") as fh:
                assert fh.read() == "x = 2\n"

            # it changed on disk after it was opened: refused, unless forced
            time.sleep(0.05)
            with open(os.path.join(tmp, "src", "a.py"), "w", encoding="utf-8", newline="") as fh:
                fh.write("x = 3\n")
            os.utime(os.path.join(tmp, "src", "a.py"), (saved["mtime"] + 5, saved["mtime"] + 5))
            assert http("POST", url, {"path": "src/a.py", "text": "x = 4\n", "mtime": saved["mtime"]})[0] == 409
            assert http("POST", url, {"path": "src/a.py", "text": "x = 4\n", "mtime": saved["mtime"], "force": True})[0] == 200

            assert http("GET", url + "?path=" + urllib.parse.quote("../outside.txt"))[0] == 400
            assert http("POST", url, {"path": "../outside.txt", "text": "no"})[0] == 400
            assert not os.path.exists(os.path.join(os.path.dirname(tmp), "outside.txt"))
            assert http("GET", url + "?path=missing.py")[0] == 404
            assert http("GET", url + "?path=blob.bin")[0] == 400
            status, mcp = http("GET", url + "?path=.mcp.json")
            assert status == 200 and mcp["view_only"]
            assert http("POST", url, {"path": ".mcp.json", "text": "{\"x\": 1}"})[0] == 403
        finally:
            server.shutdown()


def test_terminal_runs_in_the_folder_follows_cd_and_stops():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "sub"))
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, tmp, [])
            url = f"{base}/api/sessions/{sid}/terminal"

            def wait_idle():
                for _ in range(100):
                    r = http("GET", url + "?after=0")[1]
                    if not r["running"]:
                        return r
                    time.sleep(0.1)
                raise AssertionError("the command did not finish")

            assert http("POST", url, {"command": "echo hello-term"})[0] == 200
            r = wait_idle()
            texts = [l["text"] for l in r["lines"]]
            assert "echo hello-term" in texts and "hello-term" in texts and any(t.startswith("exit 0") for t in texts), texts

            assert http("POST", url, {"command": "cd sub"})[1]["cwd"] == os.path.realpath(os.path.join(tmp, "sub"))
            http("POST", url, {"command": "cd nowhere"})
            assert any("no such folder" in l["text"] for l in http("GET", url + "?after=0")[1]["lines"])

            # a long command can be stopped
            long_cmd = "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"
            assert http("POST", url, {"command": long_cmd})[0] == 200
            time.sleep(0.5)
            assert http("POST", url, {"command": "echo too soon"})[0] == 409
            assert http("POST", url, {"stop": True})[1]["stopped"] is True
            wait_idle()
        finally:
            server.shutdown()


def test_at_mention_ranks_names_first_then_contains_then_letters_in_order():
    from webui.server import _list_files
    with tempfile.TemporaryDirectory() as tmp:
        for rel in ("utils/retry.ts", "src/retry_helper.py", "docs/how-to-retry.md", "README.md",
                    "src/really_easy_test_runner.py", "node_modules/x/retry.js", ".git/retry"):
            path = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        got = _list_files(tmp, "retry")
        assert got[:2] == ["utils/retry.ts", "src/retry_helper.py"], got   # names that start with it
        assert "docs/how-to-retry.md" in got and got.index("docs/how-to-retry.md") > 1
        assert not any(g.startswith(("node_modules", ".git")) for g in got)
        fuzzy = _list_files(tmp, "rtr")   # r..t..r in order, in the name
        assert "utils/retry.ts" in fuzzy and "src/really_easy_test_runner.py" in fuzzy and "README.md" not in fuzzy, fuzzy
        assert _list_files(tmp, "zzz") == []
        assert len(_list_files(tmp, "", limit=3)) == 3


def test_bash_takes_a_description_that_is_shown_not_run():
    from coder.tools import coder_tools
    with tempfile.TemporaryDirectory() as tmp:
        bash = next(t for t in coder_tools(tmp) if t.name == "bash")
        assert "description" in bash.parameters["properties"]
        out = bash.func(cmd="echo hello-desc", description="Said hello")
        assert "hello-desc" in out and "Said hello" not in out
