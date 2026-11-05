"""Background programs the agent starts, reads and stops; and running only the tests that cover
what changed."""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coder import testfocus  # noqa: E402
from coder.processes import ProcessManager  # noqa: E402

SERVER = ("import time\nprint('booting')\ntime.sleep(0.5)\nprint('listening on 1234')\n"
          "while True:\n    print('tick'); time.sleep(0.3)\n")


def test_a_server_is_started_waited_for_read_and_stopped():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "srv.py"), "w") as fh:
            fh.write(SERVER)
        pm = ProcessManager(tmp)
        out = pm.start(f'"{sys.executable}" srv.py', "web", "listening on", 15)
        assert "Ready: listening on 1234" in out and "booting" in out, out
        assert "already running" in pm.start(f'"{sys.executable}" srv.py', "web")
        time.sleep(0.8)
        new = pm.logs("web")
        assert "tick" in new and "booting" not in new        # only what is new since the last look
        assert "booting" in pm.logs("web", new_only=False)
        # an old "ready" line does not count when waiting again
        assert "Nothing matched" in pm.logs("web", wait_for="listening on", wait_seconds=1)
        assert "web: running" in pm.listing()
        assert pm.stop("web").startswith("Stopped 'web' (it was running)")
        assert "exited" in pm.listing()
        crashed = pm.start(f'"{sys.executable}" -c "print(1/0)"', "bad", "never", 10)
        assert "exited with code 1 before printing" in crashed and "ZeroDivisionError" in crashed
        assert pm.logs("nope").startswith("ERROR: no background program named 'nope'")
        pm.stop_all()
    print("PASS test_a_server_is_started_waited_for_read_and_stopped")


def test_focused_tests_are_the_ones_that_cover_the_change():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pkg"))
        os.makedirs(os.path.join(tmp, "tests"))
        files = {
            "pkg/__init__.py": "",
            "pkg/calc.py": "def add(a, b):\n    return a + b\n",
            "tools.py": "X = 1\n",
            "tests/test_calc.py": "from pkg.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            "tests/test_other.py": "from tools import X\n\ndef test_x():\n    assert X == 1\n",
        }
        for rel, body in files.items():
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        picked = dict(testfocus.pick_python(tmp, ["pkg/calc.py"]))
        assert list(picked) == ["tests/test_calc.py"], picked   # not test_other's unrelated "tools"
        assert dict(testfocus.pick_python(tmp, ["tools.py"])) == {"tests/test_other.py": "imports tools.py"}
        assert testfocus.run(tmp).startswith("ERROR: not a git repository")
        run = lambda *a: subprocess.run(["git", "-C", tmp, *a], capture_output=True, check=True)  # noqa: E731
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        run("add", "-A")
        run("commit", "-qm", "one")
        assert "Nothing has changed" in testfocus.run(tmp)
        with open(os.path.join(tmp, "pkg/calc.py"), "a") as fh:
            fh.write("\ndef sub(a, b):\n    return a - b\n")
        out = testfocus.run(tmp)
        assert "tests/test_calc.py (" in out and "test_other" not in out, out
        assert "1 passed" in out or "exit 0" in out, out
        with open(os.path.join(tmp, "notes.txt"), "w") as fh:
            fh.write("x")
        assert "No tests could be matched" in testfocus.run(tmp, "notes.txt")
    print("PASS test_focused_tests_are_the_ones_that_cover_the_change")


if __name__ == "__main__":
    test_a_server_is_started_waited_for_read_and_stopped()
    test_focused_tests_are_the_ones_that_cover_the_change()
