"""What each turn changed, file by file, so you can keep or undo every file on its own.

Changes are made as the agent works (nothing waits on you mid-turn). Before a file's first change
in a turn, its content is kept; at the end of the turn the files it touched are listed with their
added and removed lines. For each one you can:

  keep   the change stays (it is marked reviewed);
  undo   the file goes back to how it was before the turn (a file the turn created is removed).

An undo is refused when the file was changed again after the turn (by you or by a later turn),
so it never throws away work that is not the turn's. Kept on disk with the session, so a turn can
be reviewed after the app restarts. Files over 2 MB are listed but cannot be undone.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional

MAX_BYTES = 2_000_000
KEEP_TURNS = 30


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _digest(data: Optional[bytes]) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _lines(data: Optional[bytes]) -> List[str]:
    return [] if data is None else data.decode("utf-8", "replace").splitlines(keepends=True)


class TurnChanges:
    def __init__(self, project_dir: str, path: str) -> None:
        self.project_dir = os.path.abspath(project_dir)
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self.turn: Optional[str] = None
        self._before: Dict[str, Optional[bytes]] = {}     # rel -> content before the turn (None: new)
        self.turns: List[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self.turns = [t for t in saved if isinstance(t, dict)] if isinstance(saved, list) else []
        except (OSError, ValueError):
            self.turns = []

    def _abs(self, rel: str) -> str:
        full = os.path.abspath(os.path.join(self.project_dir, rel))
        if not (full == self.project_dir or full.startswith(self.project_dir + os.sep)):
            raise ValueError("outside the project")
        return full

    def _rel(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.project_dir, path))
        return os.path.relpath(full, self.project_dir).replace(os.sep, "/")

    # --------------------------------------------------------------- a turn ---

    def begin(self, turn_id: str) -> None:
        with self._lock:
            self.turn, self._before = turn_id, {}

    def before_change(self, path: str) -> None:
        """Called just before a tool changes *path*: keep what it was, once per turn."""
        if not self.turn or not path:
            return
        try:
            rel = self._rel(path)
            full = self._abs(rel)
        except ValueError:
            return
        with self._lock:
            if rel not in self._before:
                self._before[rel] = _read(full) if os.path.isfile(full) else None

    def end(self) -> Optional[Dict[str, Any]]:
        """Close the turn: the files it really changed, or None when it changed nothing."""
        with self._lock:
            turn, before, self.turn, self._before = self.turn, self._before, None, {}
        if not turn:
            return None
        files = []
        for rel, old in sorted(before.items()):
            new = _read(self._abs(rel)) if os.path.isfile(self._abs(rel)) else None
            if old == new:
                continue
            diff = list(difflib.unified_diff(_lines(old), _lines(new), n=0))
            added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            big = old is not None and len(old) > MAX_BYTES
            files.append({
                "path": rel, "added": added, "removed": removed, "created": old is None,
                "deleted": new is None, "state": "pending", "undoable": not big,
                "before": None if old is None or big else base64.b64encode(old).decode(),
                "after_hash": _digest(new),
            })
        if not files:
            return None
        record = {"turn": turn, "files": files}
        with self._lock:
            self.turns = [t for t in self.turns if t.get("turn") != turn] + [record]
            del self.turns[:-KEEP_TURNS]
            self._save()
        return self.public(record)

    # ------------------------------------------------------------- review ---

    @staticmethod
    def public(record: Dict[str, Any]) -> Dict[str, Any]:
        """A turn as the page sees it (no stored contents)."""
        return {"turn": record["turn"], "files": [
            {k: v for k, v in f.items() if k not in ("before", "after_hash")} for f in record["files"]]}

    def _find(self, turn: str, path: str):
        for record in self.turns:
            if record.get("turn") == turn:
                for f in record["files"]:
                    if f["path"] == path:
                        return record, f
        raise KeyError(f"{path} is not among that turn's changes")

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.public(self.turns[-1]) if self.turns else None

    def get(self, turn: str) -> Optional[Dict[str, Any]]:
        found = next((t for t in self.turns if t.get("turn") == turn), None)
        return self.public(found) if found else None

    def diff(self, turn: str, path: str) -> str:
        """The turn's change to one file, as a unified diff (before the turn against now)."""
        _record, f = self._find(turn, path)
        old = base64.b64decode(f["before"]) if f.get("before") else None
        full = self._abs(path)
        new = _read(full) if os.path.isfile(full) else None
        return "".join(difflib.unified_diff(_lines(old), _lines(new), "a/" + path, "b/" + path, n=3))

    def decide(self, turn: str, path: str, action: str) -> Dict[str, Any]:
        """keep or undo one file's change. Returns the file's new state. Raises ValueError."""
        with self._lock:
            record, f = self._find(turn, path)
            if f["state"] != "pending":
                raise ValueError(f"{path} was already {'kept' if f['state'] == 'kept' else 'undone'}")
            if action == "keep":
                f["state"] = "kept"
            elif action == "undo":
                if not f.get("undoable"):
                    raise ValueError(f"{path} is too large to undo here")
                full = self._abs(path)
                now = _read(full) if os.path.isfile(full) else None
                if _digest(now) != f["after_hash"]:
                    raise ValueError(f"{path} was changed again after this turn; undo it by hand or rewind")
                if f["created"]:
                    if now is not None:
                        os.remove(full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "wb") as fh:
                        fh.write(base64.b64decode(f["before"]))
                f["state"] = "undone"
            else:
                raise ValueError("action is keep or undo")
            self._save()
            return {k: v for k, v in f.items() if k not in ("before", "after_hash")}

    def restore_from(self, turn: str) -> Dict[str, List[str]]:
        """Put every file back as it was before *turn*, undoing that turn and all later ones
        (rewind). Their records are dropped. Returns what was restored, removed or could not be."""
        with self._lock:
            index = next((i for i, t in enumerate(self.turns) if t.get("turn") == turn), None)
            if index is None:
                return {"restored": [], "removed": [], "skipped": []}
            earliest: Dict[str, Dict[str, Any]] = {}
            for record in self.turns[index:]:
                for f in record["files"]:
                    earliest.setdefault(f["path"], f)   # the state before the first of them
            out: Dict[str, List[str]] = {"restored": [], "removed": [], "skipped": []}
            for rel, f in sorted(earliest.items()):
                full = self._abs(rel)
                if f["created"]:
                    if os.path.isfile(full):
                        os.remove(full)
                        out["removed"].append(rel)
                elif f.get("before"):
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "wb") as fh:
                        fh.write(base64.b64decode(f["before"]))
                    out["restored"].append(rel)
                else:
                    out["skipped"].append(rel)   # too large to have been kept
            del self.turns[index:]
            self._save()
            return out

    def turns_from(self, turn_ids: List[str]) -> Dict[str, int]:
        """For each turn id, how many files restoring from it would put back."""
        out = {}
        for i, record in enumerate(self.turns):
            if record.get("turn") in turn_ids:
                out[record["turn"]] = len({f["path"] for r in self.turns[i:] for f in r["files"]})
        return out

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.turns, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass
