"""Git and GitHub for the open project, as Claude Code's desktop app does it: the branch and what it
changed, Commit, Push, and Create PR through the GitHub CLI (gh), then the pull request's checks.

Every command runs in the session's folder with a fixed argument list (no shell), so a message or
branch name is never read as a command.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional


class GitError(RuntimeError):
    """A git or gh command failed; the message is what it said."""


def run(folder: str, *args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(args), cwd=folder, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(said[-1] if said else f"{args[0]} {args[1] if len(args) > 1 else ''} failed")
    return proc


def git(folder: str, *args: str, **kw) -> str:
    return run(folder, "git", *args, **kw).stdout.strip()


def _numstat(text: str) -> List[int]:
    added = removed = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    return [added, removed]


def base_branch(folder: str) -> str:
    """The branch pull requests go into: the remote's default, else main or master."""
    head = git(folder, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for name in ("main", "master"):
        if run(folder, "git", "rev-parse", "--verify", "--quiet", name, check=False).returncode == 0:
            return name
    return ""


def status(folder: str) -> Dict[str, Any]:
    """Where the project stands: the branch, what is not committed yet, what the branch adds to its
    base, and whether it can go to GitHub."""
    if run(folder, "git", "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        return {"repo": False}
    branch = git(folder, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    has_commits = run(folder, "git", "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0
    porcelain = git(folder, "status", "--porcelain", check=False).splitlines()
    untracked = [l[3:] for l in porcelain if l.startswith("??")]
    work = _numstat(git(folder, "diff", "HEAD", "--numstat", check=False)) if has_commits else [0, 0]
    for path in untracked[:200]:   # new files count as added lines, as a diff would show them
        try:
            with open(os.path.join(folder, path), "rb") as fh:
                work[0] += fh.read(2_000_000).count(b"\n")
        except OSError:
            pass
    base = base_branch(folder)
    ahead_of_base = [0, 0]
    if base and branch and branch != base and has_commits:
        ahead_of_base = _numstat(git(folder, "diff", "--numstat", f"{base}...HEAD", check=False))
    upstream = git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    ahead = behind = 0
    if upstream and not upstream.startswith("fatal"):
        counts = git(folder, "rev-list", "--left-right", "--count", f"{upstream}...HEAD", check=False).split()
        if len(counts) == 2:
            behind, ahead = int(counts[0]), int(counts[1])
    else:
        upstream = ""
    remote = git(folder, "remote", "get-url", "origin", check=False)
    remote = "" if remote.startswith("error") or remote.startswith("fatal") else remote
    return {
        "repo": True, "branch": branch, "base": base, "upstream": upstream, "ahead": ahead, "behind": behind,
        "files": len(porcelain), "added": work[0], "removed": work[1],
        "branch_added": ahead_of_base[0], "branch_removed": ahead_of_base[1],
        "remote": remote, "github": "github.com" in remote, "gh": bool(shutil.which("gh")),
    }


def pull_request(folder: str, branch: str) -> Optional[Dict[str, Any]]:
    """The open pull request for *branch*, with its checks counted, or None."""
    if not shutil.which("gh") or not branch:
        return None
    proc = run(folder, "gh", "pr", "view", branch, "--json", "number,url,state,title,isDraft,statusCheckRollup",
               timeout=20, check=False)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    passed = failed = pending = 0
    for c in data.get("statusCheckRollup") or []:
        verdict = str(c.get("conclusion") or c.get("state") or "").upper()
        if verdict in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        elif verdict in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            failed += 1
        else:
            pending += 1
    return {"number": data.get("number"), "url": data.get("url"), "state": data.get("state"),
            "title": data.get("title"), "draft": bool(data.get("isDraft")),
            "checks": {"passed": passed, "failed": failed, "pending": pending}}


def changes_for_message(folder: str, base: str = "", limit: int = 24000) -> str:
    """What a commit or pull request message is written from: the diff, new files, the branch's commits."""
    parts = []
    if base:
        log = git(folder, "log", "--oneline", f"{base}..HEAD", check=False)
        if log:
            parts.append("Commits on this branch:\n" + log)
        parts.append(git(folder, "diff", f"{base}...HEAD", check=False))
    parts.append(git(folder, "diff", "HEAD", check=False))
    untracked = [l[3:] for l in git(folder, "status", "--porcelain", check=False).splitlines() if l.startswith("??")]
    if untracked:
        parts.append("New files:\n" + "\n".join(untracked[:100]))
    return "\n\n".join(p for p in parts if p)[:limit]


def commit(folder: str, message: str) -> Dict[str, Any]:
    message = str(message or "").strip()
    if not message:
        raise GitError("write a commit message first")
    git(folder, "add", "-A")
    if run(folder, "git", "diff", "--cached", "--quiet", check=False).returncode == 0:
        raise GitError("nothing to commit")
    git(folder, "commit", "-m", message)
    return {"commit": git(folder, "rev-parse", "--short", "HEAD"), "summary": message.splitlines()[0]}


_BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def new_branch(folder: str, name: str) -> str:
    name = str(name or "").strip()
    if not _BRANCH_OK.match(name) or ".." in name or name.endswith((".lock", "/", ".")):
        raise GitError(f"not a usable branch name: {name or '(empty)'}")
    git(folder, "switch", "-c", name)
    return name


def push(folder: str, branch: str) -> str:
    return run(folder, "git", "push", "-u", "origin", branch, timeout=180).stderr.strip()


def create_pr(folder: str, title: str, body: str, base: str, draft: bool = False) -> str:
    if not shutil.which("gh"):
        raise GitError("the GitHub CLI (gh) is not installed: get it from cli.github.com, then sign in with: gh auth login")
    args = ["gh", "pr", "create", "--title", str(title or "").strip() or "Changes", "--body", str(body or "")]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    out = run(folder, *args, timeout=120).stdout.strip()
    return out.splitlines()[-1] if out else ""


def failing_checks(folder: str, number: int) -> List[Dict[str, str]]:
    """The pull request's failing checks, each with the end of its log when GitHub Actions ran it."""
    proc = run(folder, "gh", "pr", "checks", str(number), "--json", "name,state,link", timeout=30, check=False)
    try:
        checks = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    out = []
    for c in checks:
        if str(c.get("state") or "").upper() not in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            continue
        log = ""
        found = re.search(r"/runs/(\d+)", str(c.get("link") or ""))
        if found:
            log = run(folder, "gh", "run", "view", found.group(1), "--log-failed", timeout=60, check=False).stdout[-4000:]
        out.append({"name": str(c.get("name") or "check"), "link": str(c.get("link") or ""), "log": log})
    return out


def review_comments(folder: str, number: int) -> List[Dict[str, str]]:
    """Comments and reviews on the pull request by anyone but you, oldest first."""
    me = run(folder, "gh", "api", "user", "--jq", ".login", timeout=20, check=False).stdout.strip()
    proc = run(folder, "gh", "pr", "view", str(number), "--json", "comments,reviews", timeout=30, check=False)
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return []
    out = []
    for item in (data.get("comments") or []) + (data.get("reviews") or []):
        author = str(((item.get("author") or {}).get("login")) or "")
        body = str(item.get("body") or "").strip()
        if not body or (me and author == me):
            continue
        out.append({"id": str(item.get("id") or item.get("url") or body[:40]), "author": author, "body": body[:3000],
                    "at": str(item.get("createdAt") or item.get("submittedAt") or "")})
    return sorted(out, key=lambda c: c["at"])


def merge_when_ready(folder: str, number: int) -> str:
    """GitHub's auto-merge, squashing, as Claude Code's Merge when ready asks for."""
    return run(folder, "gh", "pr", "merge", str(number), "--auto", "--squash", timeout=60).stdout.strip()
