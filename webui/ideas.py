"""Ideas: things worth doing next in a folder, each with the evidence it came from.

Larger agent runtimes generate idea cards with a model and score them for fit and novelty. Here
every idea is read off something real, with no model call, so it can always say why it exists:

    a TODO or FIXME left in the code                 (the file and line)
    a command whose last run failed                   (the activity ledger)
    a goal with no progress for a week                (the goal's log)
    something the agent concluded but never verified  (memory)
    a file the agent changed that has no test file    (the ledger and the folder)

An idea you dismiss stays dismissed. An idea whose cause has gone (the TODO was removed, the
command passes again) retires by itself.
"""

from __future__ import annotations

import datetime
import os
import re
from typing import Any, Dict, List, Optional

_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode",
         "agent-workspace", ".mypy_cache", ".pytest_cache", "site-packages"}
_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".cs", ".rb", ".php")
_MARK = re.compile(r"(?:#|//|/\*|<!--)\s*(TODO|FIXME|BUG|HACK|XXX)\b[:\s-]*(.{4,160})")
MAX_FILES = 2500
MAX_BYTES = 300_000
SCAN_EVERY_SECONDS = 600


def _in_string(before: str) -> bool:
    """True when the text before a mark leaves a quote open: the mark is inside a string literal."""
    before = re.sub(r"\\.", "", before)   # escaped quotes do not count
    return any(before.count(q) % 2 for q in ('"', "'", "`"))


def _todos(project_dir: str) -> List[Dict[str, Any]]:
    found, seen = [], 0
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for name in files:
            if not name.endswith(_EXT):
                continue
            seen += 1
            if seen > MAX_FILES:
                return found
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, start=1):
                        m = _MARK.search(line)
                        if m and _in_string(line[:m.start()]):
                            m = None   # "# FIXME" written inside a string (a test's sample code), not a note
                        if m:
                            rel = os.path.relpath(path, project_dir).replace(os.sep, "/")
                            text = m.group(2).strip().rstrip("*/-> ").strip()
                            found.append({"tag": m.group(1), "text": text, "path": rel, "line": number,
                                          "mtime": os.path.getmtime(path)})
            except OSError:
                continue
    return found


def _has_test(project_dir: str, rel: str) -> bool:
    base = os.path.splitext(os.path.basename(rel))[0]
    if base.startswith("test_") or base.endswith("_test") or ".test" in rel or ".spec" in rel:
        return True
    wanted = {f"test_{base}", f"{base}_test", f"{base}.test", f"{base}.spec"}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for name in files:
            if os.path.splitext(name)[0] in wanted:
                return True
    return False


def gather(app, project_dir: str) -> List[Dict[str, Any]]:
    """Every idea the evidence supports right now, for one folder."""
    store = app.store
    out: List[Dict[str, Any]] = []
    todos = sorted(_todos(project_dir), key=lambda t: -t["mtime"])[:15]
    for t in todos:
        urgent = t["tag"] in ("FIXME", "BUG")
        out.append({
            "kind": "todo", "key": f"{t['path']}:{t['text'][:60]}",
            "title": f"{'Fix' if urgent else 'Finish'}: {t['text'][:90]}",
            "rationale": f"A {t['tag']} note left in the code.",
            "source_path": t["path"], "source_line": t["line"], "score": 0.7 if urgent else 0.5,
            "prompt": f"There is a {t['tag']} in {t['path']} at line {t['line']}: \"{t['text']}\". "
                      "Read the code around it, then resolve it.",
        })
    # commands whose most recent run failed
    last: Dict[str, Dict[str, Any]] = {}
    for a in reversed(store.recent_activity(activity_type="command_run", limit=200)):
        if a["details"].get("project_dir") != project_dir:
            continue
        last[a["title"]] = a
    for cmd, a in last.items():
        if a["status"] == "error" and cmd.strip():   # a run with no command named is no idea
            out.append({
                "kind": "failed_command", "key": cmd[:120], "title": f"`{cmd[:70]}` failed last time",
                "rationale": "Its most recent run ended with an error" + (f": {a['subtitle'][:120]}" if a["subtitle"] else "."),
                "source_path": None, "source_line": None, "score": 0.9,
                "prompt": f"`{cmd}` failed the last time it ran ({a['subtitle'][:200]}). Run it again, find the cause and fix it.",
            })
    week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    for g in store.goals(project_dir, active_only=True):
        if g["updated_at"] < week_ago:
            out.append({
                "kind": "stale_goal", "key": g["goal_id"], "title": f"Pick up “{g['title'][:70]}”",
                "rationale": f"This goal has had no progress since {g['updated_at'][:10]}.",
                "source_path": None, "source_line": None, "score": 0.6,
                "prompt": f"Let's make progress on the goal \"{g['title']}\". Look at where it stands and propose the next concrete step.",
            })
    for c in store.claims(project_dir):
        if c["state"] == "CONFABULATED":
            out.append({
                "kind": "unverified_memory", "key": c["claim_id"], "title": f"Check: {c['claim_text'][:80]}",
                "rationale": "The agent remembered this but nothing it saw states it.",
                "source_path": None, "source_line": None, "score": 0.4,
                "prompt": f"You remembered \"{c['claim_text']}\" without evidence. Check whether it is true in this "
                          "project and tell me what you find.",
            })
    changed = []
    for a in store.recent_activity(activity_type="file_updated", limit=60):
        if a["details"].get("project_dir") != project_dir or a["status"] != "success":
            continue
        rel = str(a["details"].get("path") or "")
        if rel.endswith(_EXT) and rel not in changed and os.path.isfile(os.path.join(project_dir, rel)):
            changed.append(rel)
    for rel in changed[:6]:
        if not _has_test(project_dir, rel):
            out.append({
                "kind": "untested_change", "key": rel, "title": f"Write tests for {rel}",
                "rationale": "The agent changed this file and no test file for it was found.",
                "source_path": rel, "source_line": None, "score": 0.65,
                "prompt": f"{rel} was changed recently and has no tests. Read it and write tests for its behaviour.",
            })
    return out


def refresh(app, project_dir: str, force: bool = False) -> None:
    """Bring a folder's ideas in line with the evidence (at most every few minutes)."""
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        return
    key = "ideas_scanned:" + project_dir
    now = datetime.datetime.now()
    last = app.store.get_meta(key)
    if not force and last:
        try:
            if (now - datetime.datetime.fromisoformat(last)).total_seconds() < SCAN_EVERY_SECONDS:
                return
        except ValueError:
            pass
    app.store.set_meta(key, now.isoformat(timespec="seconds"))
    app.store.sync_ideas(project_dir, gather(app, project_dir))


def listing(app, project_dir: Optional[str], limit: int = 30) -> List[Dict[str, Any]]:
    if project_dir:
        refresh(app, project_dir)
    rows = app.store.ideas(os.path.abspath(project_dir) if project_dir else None)
    for r in rows:
        r["folder"] = os.path.basename(r["project_dir"].rstrip("\\/")) or r["project_dir"]
    return rows[:limit]
