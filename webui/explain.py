"""Why a command failed, in plain words (the terminal's "Fix it for me" card).

``explain(command, output, code)`` reads what a failed build or run printed and returns a short
title, one or two plain sentences, and the file and line when the error names one. It is a set of
rules for the usual compilers and languages (C/C++, MSVC, Python, JavaScript, Rust, Go, C#, the
shell); anything else gets a general explanation. No model is asked: it has to be instant.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_BUILD_WORDS = ("make", "cmake", "gcc", "g++", "clang", "cl ", "cl.exe", "msbuild", "cargo build",
                "go build", "dotnet build", "npm run build", "tsc", "javac", "ninja", "build")

# a file and line as compilers print them: src/game.cpp:42:5:  game.cpp(42,5):  File "x.py", line 42
_WHERE = [
    re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)'),
    re.compile(r"(?P<file>[\w./\\:-]+\.\w{1,6})\((?P<line>\d+)(?:,\d+)?\)\s*:\s*(?:fatal )?error"),
    re.compile(r"(?P<file>[\w./\\:-]*[\w-]+\.\w{1,6}):(?P<line>\d+)(?::\d+)?:\s*(?:fatal )?error"),
    re.compile(r"--> (?P<file>[\w./\\-]+\.rs):(?P<line>\d+)"),
    re.compile(r"at .*?\(?(?P<file>[\w./\\:-]+\.[cm]?js):(?P<line>\d+):\d+\)?"),
]

# (pattern, title, explanation); groups in the pattern can be used as {1}, {2} in the explanation
_RULES: List[tuple] = [
    # names
    (r"'([^']+)' was not declared in this scope|use of undeclared identifier '([^']+)'|"
     r"error C2065: '([^']+)': undeclared identifier|NameError: name '([^']+)' is not defined|"
     r"ReferenceError: (\S+) is not defined|cannot find value `([^`]+)`|undefined: (\S+)|"
     r"The name '([^']+)' does not exist in the current context",
     "A name the code uses doesn't exist",
     "The code uses \"{0}\", but nothing by that name was made. Often it's a typo, or something used before it's set up."),
    (r"has no member named '([^']+)'|no member named '([^']+)'|is not a member of|"
     r"AttributeError: .*has no attribute '([^']+)'",
     "Something doesn't have the part the code asks for",
     "The code asks for \"{0}\" on something that doesn't have it. It may be misspelled or belong to something else."),
    (r"(?:fatal error: |fatal error C1083: Cannot open include file: ')([\w./\\-]+\.h\w*)'?:? No such file|"
     r"Cannot open include file: '([^']+)'",
     "A file the code includes can't be found",
     "The code includes \"{0}\", but the compiler can't find it. The file may be missing, misnamed, or its folder isn't set up in the build."),
    (r"ModuleNotFoundError: No module named '([^']+)'|ImportError: No module named '?([\w.]+)",
     "A Python package is missing",
     "The code uses the package \"{0}\", which isn't installed on this computer (or in this project's environment)."),
    (r"Cannot find module '([^']+)'|Module not found: Error: Can't resolve '([^']+)'",
     "A JavaScript package or file is missing",
     "The code needs \"{0}\", which isn't installed or isn't where the code expects. Installing the project's packages (npm install) often fixes it."),
    # punctuation and syntax
    (r"expected ';'|error C2143: syntax error: missing ';'|SyntaxError: |IndentationError: |"
     r"expected expression|error: expected|Unexpected token|syntax error",
     "The code breaks the language's rules",
     "Something is written in a way the language doesn't allow: a missing ; or bracket, a stray character, or wrong indentation."),
    # linking
    (r"undefined reference to [`']([^'`]+)'|unresolved external symbol \"?([^\"\s]+)|"
     r"Undefined symbols for architecture|LNK2019|LNK2001",
     "The pieces don't join up",
     "Each file compiled, but \"{0}\" is used without its code being found when the program is put together. A file or library may be missing from the build."),
    (r"multiple definition of [`']([^'`]+)'|LNK2005|already defined in",
     "Something is defined twice",
     "\"{0}\" is defined in more than one place, so the program can't be put together."),
    # types
    (r"cannot convert|invalid conversion|incompatible types?|mismatched types|TypeError: |"
     r"error C2440|error C2664|expected .* found",
     "Two kinds of value don't fit together",
     "The code uses one kind of value where another is needed, for example text where a number should be."),
    (r"too (?:few|many) arguments|takes \d+ positional arguments? but \d+ (?:were|was) given|"
     r"missing \d+ required positional argument|error C2660",
     "A function got the wrong number of values",
     "A function is run with more or fewer values than it asks for."),
    # running
    (r"Segmentation fault|SIGSEGV|Access violation|0xC0000005|core dumped|exit code (?:-1073741819|3221225477|139|-11)\b",
     "The program crashed",
     "It tried to use memory it doesn't own, usually something used after it's gone or a list read past its end."),
    (r"IndexError: |index out of range|out of bounds|ArrayIndexOutOfBounds|subscript out of range",
     "A list was read past its end",
     "The program asked for an item that isn't in a list, like the 11th of 10."),
    (r"KeyError: '?([^'\n]+)",
     "A lookup found nothing",
     "The program looked up \"{0}\" in a table that doesn't have it."),
    (r"ZeroDivisionError|division by zero|divide by zero",
     "A division by zero",
     "The program divided a number by zero, which has no answer."),
    (r"Cannot read propert(?:y|ies) of (?:undefined|null)|NoneType' object|NullReferenceException|"
     r"null pointer|nil pointer dereference",
     "Something was used before it existed",
     "The program used a value that was empty (nothing had been put in it yet)."),
    (r"stack overflow|RecursionError|Maximum call stack",
     "The program went round in circles",
     "A function kept calling itself without stopping, until it ran out of room."),
    (r"panicked at",
     "The program stopped itself",
     "The program hit a check that said something was wrong and stopped (a Rust panic)."),
    # the computer
    (r"is not recognized as (?:the name of a cmdlet|an internal or external command)|command not found|"
     r"No such file or directory.*(?:bin|exec)|CommandNotFoundException",
     "That command isn't on this computer",
     "The computer doesn't know the command. The tool may not be installed, or it isn't on the PATH."),
    (r"No such file or directory|cannot find the (?:file|path) specified|FileNotFoundError|ENOENT",
     "A file couldn't be found",
     "The program or build looked for a file that isn't there, or is in a different folder."),
    (r"Permission denied|Access is denied|EACCES|PermissionError",
     "Not allowed to do that",
     "The computer refused access to a file or folder. It may be open in another program, or need different permissions."),
    (r"Address already in use|EADDRINUSE|Only one usage of each socket address",
     "That port is already taken",
     "Another program (often an earlier copy of this one) is already using that port."),
    (r"out of memory|MemoryError|std::bad_alloc|OutOfMemory",
     "It ran out of memory",
     "The program asked for more memory than the computer could give."),
]
_COMPILED = [(re.compile(p, re.I), t, e) for p, t, e in _RULES]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")   # colours and cursor moves in program output
_SUGGEST = re.compile(r"[Dd]id you mean:? [`'\"]?([\w.:]+)[`'\"]?\??")


def _where(output: str) -> Dict[str, Any]:
    for i, rx in enumerate(_WHERE):
        found = [m for m in rx.finditer(output)
                 if not re.search(r"[\\/](site-packages|node_modules|lib[\\/]python\d*)[\\/]|^<", m.group("file"))]
        if found:
            m = found[-1] if i == 0 else found[0]   # a Python traceback ends at the line that failed
            return {"file": m.group("file"), "line": int(m.group("line"))}
    return {}


def _is_build(command: str) -> bool:
    c = " " + command.lower() + " "
    return any((" " + w) in c for w in _BUILD_WORDS)


def explain(command: str, output: str, code: Optional[int] = None) -> Dict[str, Any]:
    """{"title", "plain", "file"?, "line"?, "kind": "build"|"run"} for a command that failed."""
    output = ANSI.sub("", output or "") + (f"\n(exit code {code})" if code not in (None, 0) else "")
    kind = "build" if _is_build(command or "") else "run"
    result: Dict[str, Any] = {"kind": kind}
    for rx, title, text in _COMPILED:
        m = rx.search(output)
        if not m:
            continue
        name = next((g for g in m.groups() if g), "") if m.groups() else ""
        result["title"] = title
        result["plain"] = text.format(name.strip()[:80]) if name else text.replace('"{0}"', "something")
        hint = _SUGGEST.search(output)
        if hint and hint.group(1) != name:
            result["plain"] += f' It may mean "{hint.group(1)}".'
        break
    else:
        verb = "didn't build" if kind == "build" else "stopped with an error"
        result["title"] = "It " + verb
        result["plain"] = ("The command ended with an error" + (f" (code {code})" if code not in (None, 0) else "")
                           + ". The full error below says what went wrong.")
    result.update(_where(output))
    if result.get("file"):
        result["plain"] += f" It points at line {result['line']} of {re.split(r'[\\/]', result['file'])[-1]}."
    return result
