"""Slash commands: markdown files with frontmatter, expanded before the run.

A command is ``commands/<name>.md``::

    ---
    name: commit
    description: Draft a commit message and commit the changes.
    ---
    Review the diff and commit. Extra instructions: $ARGUMENTS

``load_commands`` parses them; ``expand_command`` substitutes ``$ARGUMENTS``
with the user's trailing text. The expanded text is then sent to the agent
as the user message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import mini_yaml


@dataclass
class Command:
    """One slash command definition."""

    name: str
    description: str
    body: str
    path: str


def _split_frontmatter(text: str) -> tuple[Dict, str]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                try:
                    front = mini_yaml.parse("\n".join(lines[1:i])) or {}
                except ValueError:
                    front = {}
                return front, "\n".join(lines[i + 1 :]).strip()
    return {}, text.strip()


def load_commands(commands_dir: str) -> List[Command]:
    """Load every ``*.md`` command file found directly under *commands_dir*."""
    commands: List[Command] = []
    if not os.path.isdir(commands_dir):
        return commands
    for entry in sorted(os.listdir(commands_dir)):
        if not entry.endswith(".md"):
            continue
        path = os.path.join(commands_dir, entry)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        front, body = _split_frontmatter(text)
        name = str(front.get("name", "")).strip() or entry[: -len(".md")]
        description = str(front.get("description", "")).strip()
        if not body:
            continue
        commands.append(
            Command(name=name, description=description, body=body, path=path)
        )
    return commands


def find_command(commands: List[Command], name: str) -> Optional[Command]:
    for cmd in commands:
        if cmd.name == name:
            return cmd
    return None


def expand_command(cmd: Command, arguments: str) -> str:
    """Substitute ``$ARGUMENTS`` in the command body with *arguments*."""
    return cmd.body.replace("$ARGUMENTS", arguments.strip())
