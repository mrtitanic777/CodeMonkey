"""Commands that can destroy work, and why, in plain words.

The permission gate stops before one of these in every mode (even "Never asks"), shows the reason,
and offers no "always allow". Settings > Permissions can turn the check off. It is a list of the
usual ways to lose files, history or data, not a sandbox: anything not on it runs as the mode says.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_RULES: List[Tuple[str, str]] = [
    # deleting folders
    (r"\brm\s+(?:-\w*\s+)*-\w*[rR]\w*\b|\brm\s+(?:-\w*\s+)*--recursive\b",
     "it deletes a folder and everything in it"),
    (r"\bRemove-Item\b[^|;&]*-Recurse\b|\b(?:rd|rmdir)\s+/s\b",
     "it deletes a folder and everything in it"),
    (r"\bdel\s+(?:/\w\s+)*/s\b", "it deletes matching files in every folder below this one"),
    # throwing away work in git
    (r"\bgit\s+reset\s+(?:\S+\s+)*--hard\b", "it throws away every change that isn't committed"),
    (r"\bgit\s+clean\s+(?:\S+\s+)*-\w*f", "it deletes files git isn't tracking, for good"),
    (r"\bgit\s+(?:checkout|restore)\s+(?:\S+\s+)*(?:--\s+)?\.(?:\s|$)", "it throws away uncommitted changes to every file"),
    (r"\bgit\s+push\s+(?:\S+\s+)*(?:--force\b|-f\b|--force-with-lease\b)", "it overwrites history on the remote"),
    (r"\bgit\s+branch\s+(?:\S+\s+)*-D\b", "it deletes a branch even if its work isn't merged"),
    # disks
    (r"\b(?:mkfs(?:\.\w+)?|diskpart|format\s+[a-zA-Z]:)|\bdd\s+[^|;&]*\bof=/dev/", "it can wipe a whole disk"),
    # databases
    (r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", "it deletes data in a database"),
    # running something straight from the internet
    (r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|]*\|\s*iex\b",
     "it downloads a script and runs it straight away"),
]
_COMPILED = [(re.compile(p, re.I if "DROP" in p or "Remove-Item" in p else 0), why) for p, why in _RULES]


def risky_reason(command: str) -> str:
    """Why a shell command could destroy work, or "" when it is not one of the risky kinds."""
    text = str(command or "")
    for rx, why in _COMPILED:
        if rx.search(text):
            return why
    return ""
