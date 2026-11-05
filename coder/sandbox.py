"""The command sandbox: guard rails around the commands the agent runs (bash, process_start,
test_focus). Your own hooks and CodeMonkey's internal checks are not affected.

Levels (Settings > Permissions, config.yaml coder.sandbox, or headless --sandbox):

  off       commands run as they are (the approval rules still apply).
  guarded   before a command runs it is read, and refused when it would
              - write, move or delete outside the project folder (or the temp folder),
              - wipe a disk or a home folder, shut the computer down, raise privileges
                (sudo, runas), change the machine part of the registry,
              - download something and run it in the same breath (curl ... | sh, iwr ... | iex),
              - force-push over a remote branch.
  strict    everything guarded does, and also
              - no network commands (curl, wget, ssh, git fetch/pull/push/clone, package installs),
              - the command's environment has no secrets: variables named like keys, tokens,
                passwords and credentials are removed, so nothing it runs can read your API keys,
              - web traffic is sent to a proxy that does not exist (tools that honour
                HTTP(S)_PROXY, such as pip, npm, git over https and Python, cannot reach the
                internet), package managers are told to stay offline,
              - on Windows the command runs in a job object: at most 64 processes and 4 GB of
                memory between them, and everything it started is ended with it.

What this is not: an operating-system sandbox. A determined program can open a raw socket or
write a file by a path the reading above does not recognise. It stops the common accidents and
the obvious attempts; for code you do not trust at all, use a virtual machine or a container.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from typing import Callable, Dict, List, Optional

LEVELS = ("off", "guarded", "strict")
_source: Callable[[], str] = lambda: os.environ.get("CODEMONKEY_SANDBOX", "off")   # noqa: E731

_CHAIN = re.compile(r"&&|\|\||;|\n|(?<![|])\|(?![|])")
_DANGER = [
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rRf][a-zA-Z]*\s+(?:-[a-zA-Z]*\s+)*(?:/\*?|~/?|\$HOME/?|[A-Za-z]:[\\/]?\*?)(?:\s|$)"),
     "deletes a whole disk or home folder"),
    (re.compile(r"\b(mkfs(\.\w+)?|diskpart|format\s+[A-Za-z]:|shutdown|reboot|halt|poweroff|Stop-Computer|Restart-Computer)\b", re.I),
     "formats a disk or turns the computer off"),
    (re.compile(r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh|python\d*|node|iex|Invoke-Expression|powershell|pwsh)\b", re.I),
     "downloads something and runs it straight away"),
    (re.compile(r"(^|\s)(sudo|runas|doas)\s"), "asks for administrator rights"),
    (re.compile(r"\breg(\.exe)?\s+(delete|add|import)\s+\"?HK(LM|EY_LOCAL_MACHINE|CR|EY_CLASSES_ROOT)", re.I),
     "changes the machine part of the registry"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "is a fork bomb"),
    (re.compile(r"\bgit\s+push\b[^\n]*(\s--force(?!-with-lease)\b|\s-f\b|\s\+\S)"), "force-pushes over a remote branch"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)"), "opens up the whole file system"),
]
_NETWORK = [
    re.compile(r"(^|\s)(curl|wget|nc|ncat|netcat|telnet|ssh|scp|sftp|ftp|rsync)(\.exe)?(\s|$)", re.I),
    re.compile(r"\b(Invoke-WebRequest|Invoke-RestMethod|iwr|irm|Start-BitsTransfer)\b", re.I),
    re.compile(r"\bgit\s+(push|pull|fetch|clone|ls-remote|remote\s+update|submodule\s+update)\b"),
    re.compile(r"\b(pip3?|uv|poetry|conda|npm|pnpm|yarn|bun|cargo|gem|composer|nuget|dotnet)\s+(install|add|i|update|upgrade|get|fetch|restore|publish)\b"),
    re.compile(r"\b(python\d*|py)\s+-m\s+pip\s+install\b"),
    re.compile(r"\bgo\s+(get|install|mod\s+download)\b"),
]
_WRITERS = {"rm", "rmdir", "del", "erase", "rd", "mv", "move", "cp", "copy", "xcopy", "robocopy", "touch", "mkdir",
            "md", "tee", "ln", "chmod", "chown", "truncate", "shred", "remove-item", "move-item", "copy-item",
            "new-item", "set-content", "out-file", "add-content", "clear-content", "rename-item", "ri", "rni"}
_REDIRECT = re.compile(r"(?<![<>&0-9-])(?:[12]?>>?|&>)\s*(\"[^\"]+\"|'[^']+'|[^\s&|;<>]+)")
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)", re.I)
_KEEP_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS",
             "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "COMPUTERNAME", "LANG", "TERM", "SHELL", "PWD"}


def set_source(fn: Callable[[], str]) -> None:
    """Where the current level comes from (the web UI points this at its settings)."""
    global _source
    _source = fn


def level() -> str:
    try:
        value = str(_source() or "off").lower()
    except Exception:  # noqa: BLE001
        value = "off"
    return value if value in LEVELS else "off"


def _unquote(text: str) -> str:
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'" else text


def _outside(path: str, project_dir: str) -> bool:
    """True when *path* (as a command writes it) points outside the project and the temp folder."""
    p = _unquote(path.strip())
    if not p or p.startswith("-") or p in ("/dev/null", "NUL", "nul", "/dev/stdout", "/dev/stderr", "$null"):
        return False
    p = re.sub(r"^(~|\$HOME|\$env:USERPROFILE|%USERPROFILE%)(?=[\\/]|$)", os.path.expanduser("~").replace("\\", "/"), p, flags=re.I)
    m = re.match(r"^/([A-Za-z])(/.*)?$", p)                   # /c/Users/x, as Git Bash writes it
    if m and sys.platform == "win32":
        p = f"{m.group(1)}:{m.group(2) or '/'}"
    if p.startswith("$") or p.startswith("%"):
        return False                                            # a variable: cannot tell
    full = os.path.normcase(os.path.normpath(p if os.path.isabs(p) or re.match(r"^[A-Za-z]:", p)
                                             else os.path.join(project_dir, p)))
    roots = [os.path.normcase(os.path.normpath(project_dir)), os.path.normcase(os.path.normpath(tempfile.gettempdir()))]
    if sys.platform != "win32":
        roots.append("/tmp")
    return not any(full == r or full.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _words(part: str) -> List[str]:
    try:
        # POSIX splitting eats the backslashes of a Windows path; keep them when there are any
        return shlex.split(part, posix="\\" not in part)
    except ValueError:
        return part.split()


def check(command: str, project_dir: str, at: Optional[str] = None) -> str:
    """Why *command* is refused at this level ("" when it may run)."""
    lvl = at or level()
    if lvl == "off":
        return ""
    text = str(command or "")
    for pattern, why in _DANGER:
        if pattern.search(text):
            return f"it {why}"
    for part in _CHAIN.split(text):
        words = _words(part.strip())
        if not words:
            continue
        name = os.path.basename(words[0]).lower()
        name = name[:-4] if name.endswith(".exe") else name
        if name in _WRITERS:
            targets = [w for w in words[1:] if not w.startswith("-")]
            if name in ("cp", "copy", "xcopy", "robocopy", "copy-item", "ln") and targets:
                targets = targets[-1:]                        # copying from outside is fine; to outside is not
            for t in targets:
                if _outside(t, project_dir):
                    return f"it writes outside the project ({_unquote(t)})"
        for w in words[1:]:
            if w.lower().startswith(("of=", "-outfile=", "-path=")) and _outside(w.split("=", 1)[1], project_dir):
                return f"it writes outside the project ({w})"
    for m in _REDIRECT.finditer(text):
        if _outside(m.group(1), project_dir):
            return f"it writes outside the project ({_unquote(m.group(1))})"
    if lvl == "strict":
        for pattern in _NETWORK:
            if pattern.search(text):
                return "it uses the network, which the strict sandbox does not allow"
    return ""


def environment(at: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The environment a command runs with at this level (None: unchanged)."""
    if (at or level()) != "strict":
        return None
    env = {k: v for k, v in os.environ.items()   # CODEMONKEY_* are settings ("monKEY"), not secrets
           if k.upper() in _KEEP_ENV or k.upper().startswith("CODEMONKEY_") or not _SECRET_NAME.search(k)}
    dead = "http://127.0.0.1:9"
    env.update({"HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead, "http_proxy": dead, "https_proxy": dead,
                "all_proxy": dead, "NO_PROXY": "", "no_proxy": "", "npm_config_offline": "true", "PIP_NO_INDEX": "1",
                "GIT_TERMINAL_PROMPT": "0", "CODEMONKEY_SANDBOX": "strict"})
    return env


def contain(proc, at: Optional[str] = None) -> None:
    """On Windows at the strict level: put *proc* in a job object (process and memory limits; all of
    it ends together). The job handle is kept on the process object, which ends the job when dropped."""
    if (at or level()) != "strict" or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes as wt

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED()
        # kill on close | active process limit | job memory limit
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0008 | 0x0200
        info.BasicLimitInformation.ActiveProcessLimit = 64
        info.JobMemoryLimit = 4 * 1024 ** 3
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        if not k32.AssignProcessToJobObject(job, wt.HANDLE(int(proc._handle))):
            k32.CloseHandle(job)
            return

        class _Job:
            def __init__(self, handle):
                self.handle = handle

            def __del__(self):
                try:
                    k32.CloseHandle(self.handle)
                except Exception:  # noqa: BLE001
                    pass
        proc._cm_job = _Job(job)
    except (OSError, AttributeError, ValueError):
        pass


def describe(at: Optional[str] = None) -> str:
    lvl = at or level()
    return {"off": "Off: commands run as they are.",
            "guarded": "Guarded: commands that would write outside the project or do lasting damage are refused.",
            "strict": "Strict: guarded, plus no network, no secrets in the environment, and process limits."}[lvl]
