"""Running a shell command: one way, used everywhere a command is run.

Two things were wrong with running commands through ``subprocess.run(cmd, shell=True, timeout=...)``,
and both were repeated in every place that did it (the bash tool, hooks, watch commands, the
code gate's test run, scheduled jobs):

1. On Windows a timeout did not stop the command. The shell was killed, but what it had started
   kept running and kept the output pipes open, so the caller waited until that finished anyway.
   A hook or a test that hung froze the agent for as long as it liked. Here the command runs in
   its own process group and, on a timeout, the whole tree is killed.

2. On Windows the command ran in cmd.exe, while the tool is called "bash" and models write bash
   (single quotes, ``&&``, ``ls``, ``grep``). When Git for Windows is installed its bash is used
   instead, so a command means the same on every system. ``shell_name()`` says which shell is in
   use, and the tool tells the model.

Set CODEMONKEY_SHELL=system to keep the system's own shell.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from typing import Dict, List, NamedTuple, Optional, Union

_BASH: Optional[str] = None
_LOOKED = False


class Done(NamedTuple):
    code: Optional[int]     # None when it was stopped for running too long
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def _git_bash() -> Optional[str]:
    """Git for Windows' bash, or None. Never the WSL launcher that Windows also calls bash.exe."""
    global _BASH, _LOOKED
    if _LOOKED:
        return _BASH
    _LOOKED = True
    if sys.platform != "win32" or os.environ.get("CODEMONKEY_SHELL", "").lower() == "system":
        return None
    candidates: List[str] = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LocalAppData")):
        if base:
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
            candidates.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
    for part in os.environ.get("PATH", "").split(os.pathsep):
        low = part.lower().rstrip("\\/")
        if low.endswith(("git\\cmd", "git\\bin", "git\\mingw64\\bin")):   # found through where git lives
            root = part
            while root and os.path.basename(root.rstrip("\\/")).lower() != "git":
                parent = os.path.dirname(root.rstrip("\\/"))
                if parent == root:
                    break
                root = parent
            candidates.append(os.path.join(root, "bin", "bash.exe"))
    for path in candidates:
        if os.path.isfile(path):
            _BASH = path
            break
    return _BASH


def shell_name() -> str:
    """The shell commands run in, for telling the model: "bash" or "cmd.exe" or "sh"."""
    if sys.platform == "win32":
        return "bash (Git Bash)" if _git_bash() else "cmd.exe"
    return "sh"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


_OWN_CD = re.compile(r"""^\s*cd\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*(?:&&|;)\s*""")


def _same_folder(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        p = p.replace("\\", "/").rstrip("/")
        m = re.match(r"^/([A-Za-z])(/.*)?$", p)        # /c/Users/x, as Git Bash writes it
        if m:
            p = m.group(1) + ":" + (m.group(2) or "")
        return os.path.normcase(os.path.normpath(p))

    return norm(a) == norm(b)


def strip_own_cd(command: str, cwd: str) -> str:
    """Drop a leading `cd <the folder the command already runs in> &&`.

    Models often begin a command by changing into the project folder. It is where the command
    runs anyway, a Windows path written with backslashes does not survive the shell, and a
    chained command is not covered by a rule that allows the plain one.
    """
    text = str(command or "")
    m = _OWN_CD.match(text)
    if not m or not cwd:
        return text
    target = m.group(1) or m.group(2) or m.group(3) or ""
    if target in (".", "./") or _same_folder(target, cwd):
        return text[m.end():]
    shell_root = re.match(r"^/(tmp|usr|etc|home|dev|proc|var|bin|opt|mnt|root|[A-Za-z])(/|$)", target)
    if re.match(r"^(/|[A-Za-z]:[\\/])", target) and not shell_root and not os.path.isdir(target):
        # a rooted folder that is not there (a model's guess at where the project is, such as
        # /workspace/project): the command could only fail at the cd, and the project is here
        return text[m.end():]
    return text


def start_shell(command: str, cwd: Optional[str] = None, sandbox: str = "off") -> subprocess.Popen:
    """Start *command* in the shell and return at once: a server, a watcher, anything long-lived.

    Same shell as run_shell, in a process group of its own so stop_tree() ends everything it
    started; stdout and stderr come out as one stream of lines.
    """
    bash = _git_bash()
    args: Union[str, List[str]] = [bash, "-c", command] if bash else command
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    from coder import sandbox as _sandbox

    env = dict(_sandbox.environment(sandbox) or os.environ, PYTHONUNBUFFERED="1")   # output arrives as printed
    proc = subprocess.Popen(
        args, shell=not bash, cwd=cwd, text=True, encoding="utf-8", errors="replace", bufsize=1,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **kwargs,
    )
    _sandbox.contain(proc, sandbox)
    return proc


def stop_tree(proc: subprocess.Popen) -> None:
    """Stop a process started with start_shell and everything it started."""
    if proc.poll() is None:
        _kill_tree(proc)


def run_shell(command: str, cwd: Optional[str] = None, timeout: Optional[float] = 60,
              stdin_text: Optional[str] = None, sandbox: str = "off",
              extra_env: Optional[Dict[str, str]] = None) -> Done:
    """Run *command* in the shell. On a timeout everything it started is stopped."""
    bash = _git_bash()
    args: Union[str, List[str]] = [bash, "-c", command] if bash else command
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    from coder import sandbox as _sandbox

    env = _sandbox.environment(sandbox)
    if extra_env:
        env = dict(env if env is not None else os.environ, **extra_env)
    proc = subprocess.Popen(
        args, shell=not bash, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **kwargs,
    )
    _sandbox.contain(proc, sandbox)   # strict, on Windows: process and memory limits
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
        return Done(proc.returncode, out or "", err or "", False)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out, err = "", ""
        return Done(None, out or "", err or "", True)
