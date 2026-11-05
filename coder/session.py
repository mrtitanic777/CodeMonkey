"""Sessions and rewind: history persistence plus git-based checkpoints.

A session records every file the agent edits or creates (the *session log*).
A checkpoint marks a position in that log; ``/rewind`` undoes everything
after the last checkpoint:

- files the agent *edited* are restored with ``git checkout -- <path>``
  (requires the project to be a git repo),
- files the agent *created* are deleted,
- files the user changed outside the session log are never touched.

Without a git repo, rewind can only delete agent-created files; edits are
reported as not restorable. A checkpoint is taken automatically when the
session starts; ``/checkpoint`` takes one manually.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from typing import Any, Dict, List


def _git(project_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", os.path.abspath(project_dir), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


class SessionManager:
    """Tracks one coding session: history, file-change log, checkpoints."""

    def __init__(self, project_dir: str, sessions_root: str):
        self.project_dir = os.path.abspath(project_dir)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_id = stamp
        self.session_dir = os.path.join(
            os.path.abspath(sessions_root), stamp
        )
        os.makedirs(self.session_dir, exist_ok=True)
        self.log_path = os.path.join(self.session_dir, "session_log.jsonl")
        self.history_path = os.path.join(self.session_dir, "history.json")
        self.todos_path = os.path.join(self.session_dir, "todos.json")
        self.checkpoint_path = os.path.join(
            self.session_dir, "checkpoint.json"
        )
        # Auto-checkpoint at session start (position 0 in the log).
        self.checkpoint()

    # -------------------------------------------------- change log ---

    def _append(self, entry: Dict[str, Any]) -> None:
        entry = dict(entry)
        entry["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def record_edit(self, rel_path: str, existed: bool = True) -> None:
        """Log that the agent edited *rel_path* (project-relative)."""
        self._append({"action": "edit", "path": rel_path, "existed": existed})

    def record_write(self, rel_path: str, existed: bool = False) -> None:
        """Log that the agent wrote *rel_path* (project-relative)."""
        self._append({"action": "write", "path": rel_path, "existed": existed})

    def _entries(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.log_path):
            return []
        entries: List[Dict[str, Any]] = []
        with open(self.log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    # -------------------------------------------------- checkpoints ---

    def checkpoint(self) -> int:
        """Mark the current end of the change log. Returns entry count."""
        count = len(self._entries())
        with open(self.checkpoint_path, "w", encoding="utf-8") as fh:
            json.dump({"entries": count}, fh)
        return count

    def _checkpoint_index(self) -> int:
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as fh:
                return int(json.load(fh).get("entries", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return 0

    # -------------------------------------------------------- git ---

    def is_git_repo(self) -> bool:
        try:
            proc = _git(self.project_dir, "rev-parse", "--is-inside-work-tree")
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    # ------------------------------------------------------ rewind ---

    def rewind(self) -> str:
        """Undo session changes after the last checkpoint. Returns a report."""
        entries = self._entries()
        idx = self._checkpoint_index()
        pending = entries[idx:]
        if not pending:
            return "Nothing to rewind: no agent changes since the last checkpoint."
        git_ok = self.is_git_repo()
        restored: List[str] = []
        deleted: List[str] = []
        unrestorable: List[str] = []
        checkout_paths: List[str] = []
        for entry in reversed(pending):
            rel = entry.get("path", "")
            action = entry.get("action", "")
            existed = entry.get("existed", True)
            abs_path = os.path.join(self.project_dir, rel)
            if action == "write" and not existed:
                if os.path.isfile(abs_path):
                    try:
                        os.remove(abs_path)
                        deleted.append(rel)
                    except OSError as exc:
                        unrestorable.append(f"{rel} (delete failed: {exc})")
                continue
            # edits (or overwrites of pre-existing files): restore via git
            if git_ok:
                if rel not in checkout_paths:
                    checkout_paths.append(rel)
            else:
                unrestorable.append(rel)
        if checkout_paths:
            proc = _git(self.project_dir, "checkout", "--", *checkout_paths)
            if proc.returncode == 0:
                restored = checkout_paths
            else:
                unrestorable.extend(checkout_paths)
                err = (proc.stderr or "").strip().splitlines()
                if err:
                    unrestorable.append(f"git: {err[-1]}")
        # Truncate the log back to the checkpoint so a second rewind is a no-op.
        kept = entries[:idx]
        with open(self.log_path, "w", encoding="utf-8") as fh:
            for entry in kept:
                fh.write(json.dumps(entry) + "\n")
        parts = [f"Rewound {len(pending)} change(s) to the last checkpoint."]
        if restored:
            parts.append(f"Restored via git: {', '.join(restored)}")
        if deleted:
            parts.append(f"Deleted agent-created files: {', '.join(deleted)}")
        if unrestorable:
            parts.append(
                "Could NOT restore (not a git repo or git failed): "
                + ", ".join(unrestorable)
            )
        return "\n".join(parts)

    # ------------------------------------------------------ history ---

    def load_history(self) -> List[Dict[str, Any]]:
        if os.path.isfile(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return []

    def save_history(self, history: List[Dict[str, Any]]) -> None:
        with open(self.history_path, "w", encoding="utf-8") as fh:
            json.dump(history[-100:], fh, indent=2)
