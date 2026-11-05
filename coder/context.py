"""Project context: auto-load AGENTS.md / CLAUDE.md / .coderules etc.

These files are the project's standing instructions for the coding agent,
analogous to a system prompt written by the repo owner. When present in the
project root they are injected into the agent's system prompt.
"""

from __future__ import annotations

import os
from typing import List


def load_project_context(
    project_dir: str, filenames: List[str]
) -> str:
    """Return the concatenated contents of present context files.

    Each file becomes a labeled section. Missing files are skipped
    silently; binary-looking files are skipped too.
    """
    if isinstance(filenames, str):
        filenames = [filenames]
    sections: List[str] = []
    for name in filenames:
        path = os.path.join(os.path.abspath(project_dir), name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read(200_000)
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if len(raw) >= 200_000:
            text += "\n... [truncated]"
        sections.append(f"--- {name} ---\n{text}")
    return "\n\n".join(sections)
