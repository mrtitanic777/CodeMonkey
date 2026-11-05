"""Images in tool results.

A tool that has an image for the model (read on a .png, a browser screenshot) puts a marker line
in its text result: ``mark(path)``. Results pass through hooks, truncation and checks as plain
text, which would lose anything else; the marker survives them. The agent loop then turns each
marker into an image the model sees, sent once in a user message right after the tool results
(the one place every OpenAI-compatible server and the Claude adapter both accept images), and
replaced by a short note in later requests so the history stays small.

The marker carries a token made fresh for each run of the program, so text a command prints, or
a file the agent reads, cannot pass itself off as one and have some other file sent.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
from typing import Dict, List, Optional, Tuple

TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp"}
#: Larger images are described, not sent (the Claude API takes up to 5 MB per image).
MAX_BYTES = 3_750_000

_TOKEN = secrets.token_hex(6)
_MARK_RE = re.compile(r"\[\[cm-image " + _TOKEN + r" ([^\]\n]+)\]\]")


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TYPES


def mark(path: str) -> str:
    """The marker line for the image at *path* (an absolute path)."""
    return f"[[cm-image {_TOKEN} {os.path.abspath(path)}]]"


def extract(text: str, sees_images: bool) -> Tuple[str, List[Dict]]:
    """*text* with its markers replaced by a word on each image, and the image parts to send.

    For a model that cannot view images, each is described instead and nothing is sent.
    """
    if not isinstance(text, str) or "[[cm-image " not in text:
        return text, []
    parts: List[Dict] = []

    def swap(match: "re.Match") -> str:
        path = match.group(1)
        if not sees_images:
            return describe(path)
        part = image_part(path)
        if part is None:
            return f"(image {os.path.basename(path)} could not be attached: unreadable or over {MAX_BYTES // 1_000_000} MB)"
        parts.append(part)
        return f"(image {os.path.basename(path)}: shown below)"

    return _MARK_RE.sub(swap, text), parts


def image_part(path: str) -> Optional[Dict]:
    """An OpenAI-style image content part for *path*, or None if it cannot be sent."""
    media = TYPES.get(os.path.splitext(path)[1].lower())
    try:
        if media is None or os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}


def without_images(content) -> object:
    """A message's content with its image parts replaced by a line saying one was shown."""
    if not isinstance(content, list):
        return content
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            out.append({"type": "text", "text": "(an image was shown here; it is no longer attached)"})
        else:
            out.append(part)
    return out


def describe(path: str) -> str:
    """What the model is told about an image file it cannot be shown."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return f"(image file {os.path.basename(path)}, {size:,} bytes; this model cannot view images)"
