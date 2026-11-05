"""Tests for procutil: the one way a shell command is run.

Run with:  python tests/test_procutil.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import procutil  # noqa: E402
from hooks import Hook, HookManager  # noqa: E402
from speculation.policy import is_speculatable  # noqa: E402

PY = '"' + sys.executable.replace("\\", "/") + '"'


def test_runs_and_reports_exit_and_output():
    done = procutil.run_shell(f"{PY} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\"")
    assert done.code == 3 and not done.timed_out, done
    assert "out" in done.stdout and "err" in done.stderr and "out" in done.output and "err" in done.output
    print("PASS test_runs_and_reports_exit_and_output")


def test_runs_in_the_given_folder_and_takes_input():
    with tempfile.TemporaryDirectory() as tmp:
        done = procutil.run_shell(f"{PY} -c \"import os; print(os.path.realpath(os.getcwd()))\"", cwd=tmp)
        assert os.path.realpath(tmp).lower() in done.stdout.lower(), done
    done = procutil.run_shell(f"{PY} -c \"import sys; print(sys.stdin.read().upper())\"", stdin_text="piped in")
    assert "PIPED IN" in done.stdout, done
    print("PASS test_runs_in_the_given_folder_and_takes_input")


def test_a_timeout_really_stops_the_command_and_what_it_started():
    # the command starts a second process that would run for 30 seconds
    with tempfile.TemporaryDirectory() as tmp:
        marker = os.path.join(tmp, "still-running.txt").replace("\\", "/")
        child = f"import time; time.sleep(4); open(r'{marker}', 'w').write('x')"
        parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
        script = os.path.join(tmp, "parent.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(parent)
        started = time.monotonic()
        done = procutil.run_shell(f"{PY} \"{script.replace(chr(92), '/')}\"", timeout=1.0)
        elapsed = time.monotonic() - started
        assert done.timed_out and done.code is None, done
        assert elapsed < 10, f"a timeout must not wait for the command to finish (took {elapsed:.1f}s)"
        time.sleep(5)
        assert not os.path.exists(marker), "what the command started was left running"
    print("PASS test_a_timeout_really_stops_the_command_and_what_it_started")


def test_shell_is_named_truthfully():
    name = procutil.shell_name()
    assert name in ("bash (Git Bash)", "cmd.exe", "sh"), name
    if name != "cmd.exe":   # a POSIX shell: single quotes and && mean what a model expects
        done = procutil.run_shell("echo 'a  b' && echo done")
        assert done.stdout.splitlines() == ["a  b", "done"], done
    print("PASS test_shell_is_named_truthfully")


def test_a_hook_that_hangs_does_not_hang_the_agent():
    started = time.monotonic()
    decision = HookManager([Hook("pre_tool", f"{PY} -c \"import time; time.sleep(30)\"", timeout=0.5)]).run(
        "pre_tool", {"event": "pre_tool", "tool": "bash", "args": {}})
    assert decision["allow"] is True and time.monotonic() - started < 10, time.monotonic() - started
    print("PASS test_a_hook_that_hangs_does_not_hang_the_agent")


def test_rooted_paths_are_outside_the_project_on_every_system():
    for path in ("/etc/passwd", "\\Windows\\win.ini", "C:/Windows/win.ini", "C:\\Windows\\win.ini", "../x.py"):
        assert is_speculatable("read", {"path": path}, "/proj") is False, path
    assert is_speculatable("read", {"path": "a/b.py"}, "/proj") is True
    print("PASS test_rooted_paths_are_outside_the_project_on_every_system")


def main():
    tests = [
        test_runs_and_reports_exit_and_output,
        test_runs_in_the_given_folder_and_takes_input,
        test_a_timeout_really_stops_the_command_and_what_it_started,
        test_shell_is_named_truthfully,
        test_a_hook_that_hangs_does_not_hang_the_agent,
        test_rooted_paths_are_outside_the_project_on_every_system,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
