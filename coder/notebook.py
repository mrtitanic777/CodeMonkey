"""Jupyter notebooks: read them as cells, change them a cell at a time.

A notebook is JSON; reading it raw buries the code in metadata and base64, and editing it with a
text edit easily leaves it unloadable. Here ``read`` shows it as numbered cells with their
outputs (text cut short, pictures shown as pictures), and ``notebook_edit`` replaces, inserts or
deletes one cell and writes the file back the way Jupyter does, keeping its metadata. A code
cell that is changed loses its old outputs, which no longer match it.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from typing import Any, Dict, List

_OUT_CHARS = 1500


def _source(cell: Dict[str, Any]) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


def _lines(text: str) -> List[str]:
    parts = text.splitlines(keepends=True)
    return parts


def render(path: str, max_chars: int = 60_000) -> str:
    import images

    try:
        with open(path, "r", encoding="utf-8") as fh:
            nb = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"ERROR: not a readable notebook: {exc}"
    cells = nb.get("cells") or []
    lang = ((nb.get("metadata") or {}).get("kernelspec") or {}).get("language") or \
        ((nb.get("metadata") or {}).get("language_info") or {}).get("name") or "python"
    out = [f"Notebook {os.path.basename(path)}: {len(cells)} cells, {lang}. Cell numbers start at 0 "
           "(notebook_edit uses them)."]
    for i, cell in enumerate(cells):
        kind = cell.get("cell_type", "code")
        count = cell.get("execution_count")
        out.append(f"\n## Cell {i} [{kind}]" + (f" (run {count})" if kind == "code" and count else ""))
        out.append(_source(cell).rstrip() or "(empty)")
        for output in cell.get("outputs") or []:
            out.append(_output(output, images))
        if sum(len(x) for x in out) > max_chars:
            out.append(f"\n... ({len(cells) - i - 1} more cells not shown; read the file again with a smaller notebook "
                       "or ask for a cell)")
            break
    return "\n".join(x for x in out if x)


def _output(output: Dict[str, Any], images) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        text = "".join(output.get("text") or []) if isinstance(output.get("text"), list) else str(output.get("text") or "")
        return "--- output ---\n" + _cut(text)
    if kind == "error":
        return f"--- error: {output.get('ename')}: {output.get('evalue')} ---"
    data = output.get("data") or {}
    for mime, ext in (("image/png", ".png"), ("image/jpeg", ".jpg")):
        if mime in data:
            raw = data[mime]
            raw = "".join(raw) if isinstance(raw, list) else str(raw)
            path = os.path.join(tempfile.gettempdir(), "codemonkey-nb", f"out-{time.time_ns()}{ext}")
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(raw))
                return "--- output: an image ---\n" + images.mark(path)
            except (OSError, ValueError):
                return "--- output: an image that could not be decoded ---"
    text = data.get("text/plain")
    if text is not None:
        return "--- output ---\n" + _cut("".join(text) if isinstance(text, list) else str(text))
    if data:
        return "--- output: " + ", ".join(sorted(data)) + " ---"
    return ""


def _cut(text: str) -> str:
    text = text.rstrip()
    return text if len(text) <= _OUT_CHARS else text[:_OUT_CHARS] + f"\n... ({len(text):,} characters in all)"


def edit(path: str, cell: Any, source: str = "", cell_type: str = "", mode: str = "replace") -> str:
    """Replace, insert (before *cell*; at the end when cell equals the count) or delete one cell."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            nb = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"ERROR: not a readable notebook: {exc}"
    cells = nb.setdefault("cells", [])
    try:
        index = int(cell)
    except (TypeError, ValueError):
        return "ERROR: cell must be a cell number (see read)"
    mode = str(mode or "replace").lower()
    if mode not in ("replace", "insert", "delete"):
        return "ERROR: mode is replace, insert or delete"
    limit = len(cells) if mode == "insert" else len(cells) - 1
    if not 0 <= index <= limit:
        return f"ERROR: there is no cell {index}; the notebook has {len(cells)} (numbered from 0)"
    kind = str(cell_type or "").lower()
    if kind and kind not in ("code", "markdown", "raw"):
        return "ERROR: cell_type is code, markdown or raw"
    if mode == "delete":
        removed = cells.pop(index)
        what = f"Deleted cell {index} ({removed.get('cell_type', 'code')})."
    elif mode == "insert":
        kind = kind or "code"
        new: Dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": _lines(source)}
        if kind == "code":
            new.update(execution_count=None, outputs=[])
        if nb.get("nbformat", 4) >= 4 and nb.get("nbformat_minor", 0) >= 5:
            new["id"] = os.urandom(4).hex()
        cells.insert(index, new)
        what = f"Inserted a {kind} cell at {index}."
    else:
        target = cells[index]
        old_kind = target.get("cell_type", "code")
        kind = kind or old_kind
        target["cell_type"] = kind
        target["source"] = _lines(source)
        if kind == "code":
            target["outputs"] = []           # the old outputs belong to the old code
            target["execution_count"] = None
        else:
            target.pop("outputs", None)
            target.pop("execution_count", None)
        what = f"Replaced cell {index} ({kind})."
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(nb, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return what + f" The notebook now has {len(cells)} cells."
