"""Git worktrees: a session or an agent works on its own branch, in its own folder.

Several agents changing one checkout step on each other. A worktree is a second working folder
of the same repository on a branch of its own (``codemonkey/<name>``), made from where the
repository is now: work there touches nothing in the main checkout until it is merged.

Worktrees live under <workspace>/worktrees/<repo>/<name>; what each one is (its repository,
branch, the branch it came from and when) is kept in <workspace>/worktrees/worktrees.json.

Finishing one:
  merge    commit what is not committed yet, then merge the branch into the branch it came
           from, in the main checkout (which must be on that branch, without conflicting
           changes). A conflict is undone and reported, never left half-merged.
  discard  remove the folder and delete the branch (its work is lost).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional, Tuple

_LOCK = threading.Lock()


class WorktreeError(RuntimeError):
    pass


def _git(cwd: str, *args: str, timeout: int = 60) -> Tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _must(cwd: str, *args: str) -> str:
    code, out = _git(cwd, *args)
    if code != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {out[:400]}")
    return out


def repo_root(folder: str) -> Optional[str]:
    code, out = _git(folder, "rev-parse", "--show-toplevel")
    return os.path.normpath(out) if code == 0 and out else None


class Worktrees:
    def __init__(self, workspace: str) -> None:
        self.root = os.path.join(workspace, "worktrees")
        self.index_path = os.path.join(self.root, "worktrees.json")

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.index_path)

    def get(self, path: str) -> Optional[Dict[str, Any]]:
        return self._load().get(os.path.normcase(os.path.normpath(path)))

    def containing(self, folder: str) -> Optional[Dict[str, Any]]:
        """The worktree *folder* is in (it may be a sub-folder of one), or None."""
        here = os.path.normcase(os.path.normpath(folder))
        for key, entry in self._load().items():
            if here == key or here.startswith(key + os.sep):
                return entry
        return None

    @staticmethod
    def back_in_repo(entry: Dict[str, Any], folder: str) -> str:
        """Where *folder* (inside the worktree) is in the main checkout."""
        sub = os.path.relpath(os.path.realpath(folder), os.path.realpath(entry["path"]))
        target = os.path.normpath(os.path.join(entry["repo"], sub)) if sub != "." else entry["repo"]
        return target if os.path.isdir(target) else entry["repo"]

    # ------------------------------------------------------------- create ---

    def create(self, folder: str, name: str = "", owner: str = "") -> Dict[str, Any]:
        """A new worktree of the repository *folder* is in, on a new branch from its HEAD."""
        repo = repo_root(folder)
        if repo is None:
            raise WorktreeError("this folder is not in a git repository")
        if self.get(repo):
            raise WorktreeError("this folder is already a worktree; finish it before starting another from it")
        head = _git(repo, "rev-parse", "HEAD")
        if head[0] != 0:
            raise WorktreeError("the repository has no commits yet; make a first commit before using a worktree")
        base = _must(repo, "rev-parse", "--abbrev-ref", "HEAD")
        slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")[:30] or \
            datetime.datetime.now().strftime("%m%d-%H%M%S")
        with _LOCK:
            data = self._load()
            branch, n = f"codemonkey/{slug}", 1
            while _git(repo, "rev-parse", "--verify", "--quiet", branch)[0] == 0:
                n += 1
                branch = f"codemonkey/{slug}-{n}"
            path = os.path.join(self.root, os.path.basename(repo), branch.split("/", 1)[1])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _must(repo, "worktree", "add", "-b", branch, path, "HEAD")
            entry = {"path": os.path.normpath(path), "repo": repo, "branch": branch, "base": base,
                     "base_commit": head[1].strip(), "created": datetime.datetime.now().isoformat(timespec="seconds"),
                     "owner": owner}
            data[os.path.normcase(os.path.normpath(path))] = entry
            self._save(data)
        # the rest of the session (relative paths in its files) works from here as it did there
        sub = os.path.relpath(os.path.realpath(folder), os.path.realpath(repo))
        entry = dict(entry, folder=os.path.normpath(os.path.join(path, sub)) if sub != "." else entry["path"])
        return entry

    # ------------------------------------------------------------- status ---

    def status(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        path, repo = entry["path"], entry["repo"]
        if not os.path.isdir(path):
            return dict(entry, exists=False, changed=[], ahead=0, behind=0)
        code, short = _git(path, "status", "--short")
        changed = [line[3:] for line in short.splitlines() if line.strip()] if code == 0 else []
        ahead = behind = 0
        code, counts = _git(repo, "rev-list", "--left-right", "--count", f"{entry['base']}...{entry['branch']}")
        if code == 0 and counts:
            parts = counts.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
        code, stat = _git(repo, "diff", "--shortstat", f"{entry['base_commit']}..{entry['branch']}")
        return dict(entry, exists=True, changed=changed[:50], uncommitted=len(changed), ahead=ahead, behind=behind,
                    stat=stat if code == 0 else "")

    def listing(self) -> List[Dict[str, Any]]:
        return [self.status(e) for e in self._load().values()]

    # ------------------------------------------------------------- finish ---

    def commit_all(self, entry: Dict[str, Any], message: str) -> bool:
        """Commit everything not yet committed in the worktree. False when there was nothing."""
        path = entry["path"]
        _must(path, "add", "-A")
        if _git(path, "diff", "--cached", "--quiet")[0] == 0:
            return False
        code, out = _git(path, "-c", "user.name=CodeMonkey", "-c", "user.email=codemonkey@localhost",
                         "commit", "-m", message) if not _has_identity(path) else _git(path, "commit", "-m", message)
        if code != 0:
            raise WorktreeError("could not commit the worktree's changes: " + out[:300])
        return True

    def merge(self, path: str, message: str = "") -> Dict[str, Any]:
        entry = self.get(path)
        if entry is None:
            raise WorktreeError("not a worktree CodeMonkey made")
        repo, branch, base = entry["repo"], entry["branch"], entry["base"]
        self.commit_all(entry, message or f"Work from {branch}")
        current = _must(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != base:
            raise WorktreeError(f"the main checkout is on {current!r}, not {base!r}: switch it to {base} first")
        if _git(repo, "rev-list", "--count", f"{base}..{branch}")[1].strip() == "0":
            return dict(self.status(entry), merged=False, note="nothing to merge: the branch has no new commits")
        code, out = _git(repo, "merge", "--no-ff", "-m", f"Merge {branch} into {base}", branch)
        if code != 0:
            conflicts = _git(repo, "diff", "--name-only", "--diff-filter=U")[1].splitlines()
            _git(repo, "merge", "--abort")
            if conflicts:
                raise WorktreeError("the merge conflicts in " + ", ".join(conflicts[:10]) +
                                    "; nothing was changed. Resolve it on the branch (merge "
                                    f"{base} into {branch} there) and merge again.")
            raise WorktreeError("the merge did not go through, and nothing was changed: " + out[:400])
        return dict(self.status(entry), merged=True, note=f"merged {branch} into {base}")

    def discard(self, path: str, keep_branch: bool = False) -> None:
        entry = self.get(path)
        if entry is None:
            raise WorktreeError("not a worktree CodeMonkey made")
        repo = entry["repo"]
        code, out = _git(repo, "worktree", "remove", "--force", entry["path"])
        if code != 0 and os.path.isdir(entry["path"]):
            raise WorktreeError("could not remove the worktree folder: " + out[:300])
        _git(repo, "worktree", "prune")
        if not keep_branch:
            _git(repo, "branch", "-D", entry["branch"])
        with _LOCK:
            data = self._load()
            data.pop(os.path.normcase(os.path.normpath(entry["path"])), None)
            self._save(data)


def _has_identity(path: str) -> bool:
    return _git(path, "config", "user.email")[0] == 0 and _git(path, "config", "user.name")[0] == 0
