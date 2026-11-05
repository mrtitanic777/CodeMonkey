"""One local store for everything the agent does (SQLite, standard library only).

Before this, state was scattered over JSON files. Here every part of the system writes to one
database so that anything can be traced afterwards:

    sessions, agents      who worked: a root agent per session, sub-agents under it
    items                 the append-only record of a session: numbered messages, tool calls,
                          tool results, notices. A reopened session is rebuilt from it.
    compactions           checkpoints where older items were replaced by a summary
    activity              the ledger of things done (file changed, command run, task finished)
    memory_claims         memory as claims, each with a standing, a source and a history

A memory claim's ``state`` is one of the MSHMOE kernel's states: what you told the agent or what
it read is KNOWN or DERIVED; what the model merely concluded is CONFABULATED until something
supports it. Nothing is deleted when a claim is replaced: the old one is marked superseded.

Thread-safe: one connection per thread, write-ahead logging.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    project_dir  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    archived_at  TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id         TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    parent_agent_id  TEXT,
    kind             TEXT NOT NULL DEFAULT 'root',     -- root | subagent | worker
    agent_type       TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'idle',     -- idle | running | done | failed | stopped
    depth            INTEGER NOT NULL DEFAULT 0,
    prompt           TEXT,
    final_response   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_session ON agents (session_id);
CREATE INDEX IF NOT EXISTS agents_parent ON agents (parent_agent_id);

CREATE TABLE IF NOT EXISTS items (
    agent_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- user | assistant | tool | notice | plan | subagent | ...
    source      TEXT NOT NULL DEFAULT 'chat',   -- what produced it: chat, scheduler, background...
    tool_name   TEXT,
    success     INTEGER,
    text        TEXT,                   -- searchable text of the item
    data_json   TEXT NOT NULL,          -- the full record the interface shows
    created_at  TEXT NOT NULL,
    PRIMARY KEY (agent_id, seq)
);

CREATE TABLE IF NOT EXISTS compactions (
    compaction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    first_kept_seq  INTEGER,
    checkpoint_seq  INTEGER,
    tokens_before   INTEGER,
    tokens_after    INTEGER,
    trigger         TEXT NOT NULL DEFAULT 'auto',      -- auto | manual
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    activity_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    agent_id       TEXT,
    activity_type  TEXT NOT NULL,       -- file_created | file_updated | command_run | task | ...
    title          TEXT NOT NULL,
    subtitle       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'success',    -- success | error | pending | stopped
    details_json   TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_session ON activity (session_id, activity_id);

CREATE TABLE IF NOT EXISTS reviews (
    day           TEXT PRIMARY KEY,
    summary_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id  TEXT PRIMARY KEY,
    session_id   TEXT,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- page | document
    filename     TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    idea_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_dir  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    dedup_key    TEXT NOT NULL,
    title        TEXT NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL DEFAULT '',
    source_path  TEXT,
    source_line  INTEGER,
    score        REAL NOT NULL DEFAULT 0.5,
    status       TEXT NOT NULL DEFAULT 'new',       -- new | dismissed | retired | started
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (project_dir, kind, dedup_key)
);

CREATE TABLE IF NOT EXISTS goals (
    goal_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    project_dir   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',     -- active | completed
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS goal_updates (
    update_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL,
    note        TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT 'agent',        -- agent | you
    session_id  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id               TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    task_text            TEXT NOT NULL,
    schedule_expr        TEXT NOT NULL,                 -- five-field cron
    project_dir          TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    session_id           TEXT,                          -- where its reports are delivered
    next_run_at          TEXT,
    last_run_at          TEXT,
    last_status          TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    blocked_reason       TEXT,                          -- set when the job pauses itself
    blocked_until        TEXT,                          -- when it tries again
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    trigger         TEXT NOT NULL DEFAULT 'schedule',   -- schedule | manual
    scheduled_for   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',    -- running | reported | silent | error | skipped
    result_summary  TEXT NOT NULL DEFAULT '',
    error_text      TEXT NOT NULL DEFAULT '',
    steps           INTEGER NOT NULL DEFAULT 0,
    delivered       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs (job_id, run_id);

CREATE TABLE IF NOT EXISTS bench_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    TEXT NOT NULL DEFAULT '',       -- the catalog entry it is, when known
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    machine     TEXT NOT NULL DEFAULT '',
    seconds     REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_claims (
    claim_id            TEXT PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'fact',  -- fact | preference | convention | person
    claim_text          TEXT NOT NULL,
    state               TEXT NOT NULL,                 -- a kernel state: KNOWN, DERIVED, CONFABULATED...
    basis               TEXT NOT NULL DEFAULT '',      -- in plain words, why it has that standing
    confidence          REAL NOT NULL DEFAULT 1.0,
    salience            REAL NOT NULL DEFAULT 0.5,
    quote               TEXT,
    source_path         TEXT,
    source_line         INTEGER,
    session_id          TEXT,
    project_dir         TEXT,                          -- NULL: applies everywhere
    supersedes_claim_id TEXT,
    status              TEXT NOT NULL DEFAULT 'active',   -- active | superseded | retracted
    first_seen          TEXT NOT NULL,
    reinforced_at       TEXT NOT NULL,
    reinforced_count    INTEGER NOT NULL DEFAULT 0,
    valid_until         TEXT
);
CREATE INDEX IF NOT EXISTS claims_status ON memory_claims (status);
"""

_WORD = re.compile(r"[a-z0-9_]{2,}")
_STOP = frozenset(
    "the a an and or of to in on for with is are was were be it this that at by as from "
    "use uses used run runs always never should must".split()
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _words(text: str) -> set:
    return {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._local = threading.local()
        self._write = threading.RLock()
        self._fts = True
        with self._write:
            db = self._db()
            db.executescript(_SCHEMA)
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(claim_id UNINDEXED, claim_text)"
                )
            except sqlite3.OperationalError:
                self._fts = False   # this SQLite has no full-text search; plain matching is used
            for statement in (   # columns added after the first version of a table
                "ALTER TABLE jobs ADD COLUMN watch_cmd TEXT",
                "ALTER TABLE jobs ADD COLUMN watch_state TEXT",
                "ALTER TABLE memory_claims ADD COLUMN subject TEXT",   # the person a claim is about
            ):
                try:
                    db.execute(statement)
                except sqlite3.OperationalError:
                    pass   # already there
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        self.close()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            self._local.db = db
        return db

    def close(self) -> None:
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None

    def get_meta(self, key: str) -> Optional[str]:
        row = self._db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            db.commit()

    # ------------------------------------------------- sessions, agents ---

    def touch_session(self, session_id: str, title: str, project_dir: str) -> None:
        now = _now()
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO sessions (session_id, title, project_dir, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "title = excluded.title, project_dir = excluded.project_dir, updated_at = excluded.updated_at",
                (session_id, title, project_dir, now, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO agents (agent_id, session_id, kind, status, created_at, updated_at) "
                "VALUES (?, ?, 'root', 'idle', ?, ?)",
                (session_id, session_id, now, now),
            )
            db.commit()

    def delete_session(self, session_id: str) -> None:
        with self._write:
            db = self._db()
            agents = [r["agent_id"] for r in db.execute(
                "SELECT agent_id FROM agents WHERE session_id = ?", (session_id,))]
            for agent_id in agents + [session_id]:
                db.execute("DELETE FROM items WHERE agent_id = ?", (agent_id,))
                db.execute("DELETE FROM compactions WHERE agent_id = ?", (agent_id,))
            db.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM activity WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            db.commit()

    def spawn_agent(
        self, session_id: str, parent_agent_id: str, prompt: str,
        kind: str = "subagent", agent_type: str = "", model: str = "", depth: int = 1,
    ) -> str:
        agent_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO agents (agent_id, session_id, parent_agent_id, kind, agent_type, model, "
                "status, depth, prompt, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (agent_id, session_id, parent_agent_id, kind, agent_type, model, depth, prompt, now, now),
            )
            db.commit()
        return agent_id

    def finish_agent(self, agent_id: str, status: str, final_response: str = "") -> None:
        with self._write:
            db = self._db()
            db.execute(
                "UPDATE agents SET status = ?, final_response = ?, updated_at = ? WHERE agent_id = ?",
                (status, final_response, _now(), agent_id),
            )
            db.commit()

    def agents(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM agents WHERE session_id = ? ORDER BY created_at, agent_id", (session_id,)
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ items ---

    def append_item(self, agent_id: str, entry: Dict[str, Any], source: str = "chat") -> int:
        """Append one record to an agent's log and return its sequence number."""
        text = entry.get("text")
        if text is None:
            text = entry.get("result") or entry.get("plan") or entry.get("summary") or ""
        with self._write:
            db = self._db()
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM items WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            seq = int(row["next"])
            success = None
            if entry.get("kind") == "tool":
                success = 0 if str(entry.get("result", "")).lstrip().startswith("ERROR") else 1
            db.execute(
                "INSERT INTO items (agent_id, seq, kind, source, tool_name, success, text, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, seq, str(entry.get("kind", "")), source, entry.get("name"), success,
                 str(text)[:20000], json.dumps(entry), _now()),
            )
            db.commit()
        return seq

    def truncate_items(self, agent_id: str, keep: int) -> None:
        """Keep only an agent's first *keep* records (rewinding a conversation)."""
        with self._write:
            db = self._db()
            row = db.execute("SELECT seq FROM items WHERE agent_id = ? ORDER BY seq LIMIT 1 OFFSET ?",
                             (agent_id, max(0, keep))).fetchone()
            if row is not None:
                db.execute("DELETE FROM items WHERE agent_id = ? AND seq >= ?", (agent_id, row["seq"]))
                db.commit()

    def items(self, agent_id: str, after_seq: int = 0, limit: int = 2000) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT seq, data_json FROM items WHERE agent_id = ? AND seq > ? ORDER BY seq DESC LIMIT ?",
            (agent_id, after_seq, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            try:
                out.append(json.loads(r["data_json"]))
            except json.JSONDecodeError:
                continue
        return out

    def last_seq(self, agent_id: str) -> int:
        row = self._db().execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM items WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return int(row["seq"])

    def replace_items(self, agent_id: str, entries: Iterable[Dict[str, Any]]) -> None:
        """Seed an agent's log (used when a session from before the store is reopened)."""
        if self.last_seq(agent_id):
            return
        for entry in entries:
            self.append_item(agent_id, entry)

    def add_compaction(
        self, agent_id: str, summary: str, tokens_before: int, tokens_after: int, trigger: str,
    ) -> int:
        seq = self.last_seq(agent_id)
        with self._write:
            db = self._db()
            cur = db.execute(
                "INSERT INTO compactions (agent_id, summary, first_kept_seq, checkpoint_seq, tokens_before, "
                "tokens_after, trigger, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, summary, seq + 1, seq, tokens_before, tokens_after, trigger, _now()),
            )
            db.commit()
            return int(cur.lastrowid)

    def compactions(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM compactions WHERE agent_id = ? ORDER BY compaction_id", (agent_id,)
        )
        return [dict(r) for r in rows]

    # --------------------------------------------------------- activity ---

    def add_activity(
        self, session_id: Optional[str], activity_type: str, title: str, subtitle: str = "",
        status: str = "success", details: Optional[Dict[str, Any]] = None, agent_id: Optional[str] = None,
    ) -> int:
        with self._write:
            db = self._db()
            cur = db.execute(
                "INSERT INTO activity (session_id, agent_id, activity_type, title, subtitle, status, "
                "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, agent_id, activity_type, title[:200], subtitle[:300], status,
                 json.dumps(details or {}), _now()),
            )
            db.commit()
            return int(cur.lastrowid)

    def recent_activity(
        self, session_id: Optional[str] = None, activity_type: Optional[str] = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM activity WHERE 1=1", []
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        if activity_type:
            sql += " AND activity_type = ?"
            args.append(activity_type)
        sql += " ORDER BY activity_id DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        out = []
        for r in self._db().execute(sql, args):
            row = dict(r)
            try:
                row["details"] = json.loads(row.pop("details_json"))
            except json.JSONDecodeError:
                row["details"] = {}
            out.append(row)
        return out

    # ---------------------------------------------------------- reviews ---

    def items_between(self, since: str, until: str) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT agent_id, seq, kind, tool_name, success, text, data_json, created_at FROM items "
            "WHERE created_at >= ? AND created_at <= ? ORDER BY agent_id, seq", (since, until))
        return [dict(r) for r in rows]

    def activity_between(self, since: str, until: str) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM activity WHERE created_at >= ? AND created_at <= ? ORDER BY activity_id",
            (since, until))
        return [dict(r) for r in rows]

    def save_review(self, day: str, summary: Dict[str, Any]) -> None:
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO reviews (day, summary_json, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET summary_json = excluded.summary_json, created_at = excluded.created_at",
                (day, json.dumps(summary), _now()),
            )
            db.commit()

    def review(self, day: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute("SELECT summary_json FROM reviews WHERE day = ?", (day,)).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def reviews(self, limit: int = 14) -> List[Dict[str, Any]]:
        rows = self._db().execute("SELECT day, summary_json FROM reviews ORDER BY day DESC LIMIT ?", (limit,))
        return [dict(json.loads(r["summary_json"]), day=r["day"]) for r in rows]

    # -------------------------------------------------------- artifacts ---

    def put_artifact(self, artifact_id: Optional[str], session_id: Optional[str], title: str, kind: str,
                     filename: str, size_bytes: int) -> Dict[str, Any]:
        now = _now()
        artifact_id = artifact_id or uuid.uuid4().hex[:12]
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO artifacts (artifact_id, session_id, title, kind, filename, size_bytes, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(artifact_id) DO UPDATE SET "
                "title = excluded.title, size_bytes = excluded.size_bytes, updated_at = excluded.updated_at",
                (artifact_id, session_id, title[:160], kind, filename, int(size_bytes), now, now),
            )
            db.commit()
        return self.artifact(artifact_id)

    def artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def artifacts(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._db().execute("SELECT * FROM artifacts ORDER BY updated_at DESC")]

    def delete_artifact(self, artifact_id: str) -> bool:
        with self._write:
            db = self._db()
            cur = db.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------ ideas ---

    def sync_ideas(self, project_dir: str, found: List[Dict[str, Any]]) -> None:
        """Add ideas that are new, refresh the ones still true, retire those whose cause is gone."""
        now = _now()
        keys = {(i["kind"], i["key"]) for i in found}
        with self._write:
            db = self._db()
            for i in found:
                db.execute(
                    "INSERT INTO ideas (project_dir, kind, dedup_key, title, rationale, prompt, source_path, "
                    "source_line, score, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_dir, kind, dedup_key) DO UPDATE SET title = excluded.title, "
                    "rationale = excluded.rationale, prompt = excluded.prompt, source_line = excluded.source_line, "
                    "updated_at = excluded.updated_at, "
                    "status = CASE WHEN status = 'retired' THEN 'new' ELSE status END",
                    (project_dir, i["kind"], i["key"], i["title"], i["rationale"], i["prompt"],
                     i["source_path"], i["source_line"], i["score"], now, now),
                )
            for row in db.execute(
                "SELECT idea_id, kind, dedup_key FROM ideas WHERE project_dir = ? AND status = 'new'",
                (project_dir,),
            ).fetchall():
                if (row["kind"], row["dedup_key"]) not in keys:
                    db.execute("UPDATE ideas SET status = 'retired', updated_at = ? WHERE idea_id = ?",
                               (now, row["idea_id"]))
            db.commit()

    def ideas(self, project_dir: Optional[str] = None, status: str = "new") -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM ideas WHERE status = ?", [status]
        if project_dir:
            sql += " AND project_dir = ?"
            args.append(project_dir)
        sql += " ORDER BY score DESC, idea_id DESC"
        return [dict(r) for r in self._db().execute(sql, args)]

    def set_idea_status(self, idea_id: int, status: str) -> bool:
        with self._write:
            db = self._db()
            cur = db.execute("UPDATE ideas SET status = ?, updated_at = ? WHERE idea_id = ?",
                             (status, _now(), int(idea_id)))
            db.commit()
            return cur.rowcount > 0

    def search_items(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Past conversation: user and agent messages that mention every word of *query*."""
        words = [w for w in _WORD.findall(str(query or "").lower()) if w not in _STOP][:6]
        if not words:
            return []
        sql = ("SELECT i.agent_id, i.seq, i.kind, i.text, i.created_at, s.title FROM items i "
               "LEFT JOIN agents a ON a.agent_id = i.agent_id LEFT JOIN sessions s ON s.session_id = a.session_id "
               "WHERE i.kind IN ('user', 'assistant')")
        args: List[Any] = []
        for w in words:
            sql += " AND LOWER(i.text) LIKE ?"
            args.append(f"%{w}%")
        sql += " ORDER BY i.created_at DESC LIMIT ?"
        args.append(max(1, min(30, int(limit))))
        return [dict(r) for r in self._db().execute(sql, args)]

    # ------------------------------------------------------------ goals ---

    def add_goal(self, title: str, description: str, project_dir: str) -> Dict[str, Any]:
        title = " ".join(str(title or "").split())
        if not title:
            raise ValueError("a goal needs a title")
        goal_id = uuid.uuid4().hex[:10]
        now = _now()
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO goals (goal_id, title, description, project_dir, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, title[:160], str(description or "").strip(), project_dir, now, now),
            )
            db.commit()
        return self.goal(goal_id)

    def goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        row = db.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        if row is None:
            return None
        goal = dict(row)
        goal["updates"] = [dict(r) for r in db.execute(
            "SELECT * FROM goal_updates WHERE goal_id = ? ORDER BY update_id DESC LIMIT 30", (goal_id,))]
        return goal

    def goals(self, project_dir: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
        rows = [dict(r) for r in self._db().execute("SELECT goal_id, project_dir, status FROM goals ORDER BY created_at")]
        out = []
        for r in rows:
            if project_dir is not None and r["project_dir"] != project_dir:
                continue
            if active_only and r["status"] != "active":
                continue
            out.append(self.goal(r["goal_id"]))
        return out

    def log_goal(self, goal_id: str, note: str, author: str = "agent", session_id: Optional[str] = None) -> bool:
        note = str(note or "").strip()
        if not note or self.goal(goal_id) is None:
            return False
        now = _now()
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO goal_updates (goal_id, note, author, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (goal_id, note[:2000], author, session_id, now),
            )
            db.execute("UPDATE goals SET updated_at = ? WHERE goal_id = ?", (now, goal_id))
            db.commit()
        return True

    def set_goal_status(self, goal_id: str, status: str) -> bool:
        now = _now()
        with self._write:
            db = self._db()
            cur = db.execute(
                "UPDATE goals SET status = ?, completed_at = ?, updated_at = ? WHERE goal_id = ?",
                (status, now if status == "completed" else None, now, goal_id),
            )
            db.commit()
            return cur.rowcount > 0

    def delete_goal(self, goal_id: str) -> bool:
        with self._write:
            db = self._db()
            cur = db.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
            db.execute("DELETE FROM goal_updates WHERE goal_id = ?", (goal_id,))
            db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- jobs ---

    _JOB_FIELDS = (
        "title", "task_text", "schedule_expr", "project_dir", "enabled", "session_id", "next_run_at",
        "last_run_at", "last_status", "consecutive_failures", "blocked_reason", "blocked_until",
        "watch_cmd", "watch_state",
    )

    def add_job(self, title: str, task_text: str, schedule_expr: str, project_dir: str,
                next_run_at: Optional[str]) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:10]
        now = _now()
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO jobs (job_id, title, task_text, schedule_expr, project_dir, next_run_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, title[:120], task_text, schedule_expr, project_dir, next_run_at, now, now),
            )
            db.commit()
        return self.job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        sets, args = [], []
        for key, value in fields.items():
            if key in self._JOB_FIELDS:
                sets.append(f"{key} = ?")
                args.append(int(value) if key == "enabled" else value)
        if sets:
            with self._write:
                db = self._db()
                db.execute(
                    f"UPDATE jobs SET {', '.join(sets)}, updated_at = ? WHERE job_id = ?",
                    args + [_now(), job_id],
                )
                db.commit()
        return self.job(job_id)

    def delete_job(self, job_id: str) -> bool:
        with self._write:
            db = self._db()
            cur = db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            db.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
            db.commit()
            return cur.rowcount > 0

    def job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def jobs(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._db().execute("SELECT * FROM jobs ORDER BY created_at, job_id")]

    def start_run(self, job_id: str, trigger: str, scheduled_for: Optional[str]) -> int:
        with self._write:
            db = self._db()
            cur = db.execute(
                "INSERT INTO job_runs (job_id, trigger, scheduled_for, started_at) VALUES (?, ?, ?, ?)",
                (job_id, trigger, scheduled_for, _now()),
            )
            db.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, result_summary: str = "", error_text: str = "",
                   steps: int = 0, delivered: bool = False) -> None:
        with self._write:
            db = self._db()
            db.execute(
                "UPDATE job_runs SET status = ?, result_summary = ?, error_text = ?, steps = ?, "
                "delivered = ?, finished_at = ? WHERE run_id = ?",
                (status, result_summary[:20000], error_text[:2000], steps, int(delivered), _now(), run_id),
            )
            db.commit()

    def runs(self, job_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM job_runs WHERE job_id = ? ORDER BY run_id DESC LIMIT ?",
            (job_id, max(1, min(100, int(limit)))),
        )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- memory ---

    def add_claim(
        self, text: str, state: str = "KNOWN", basis: str = "", kind: str = "fact",
        confidence: float = 1.0, salience: float = 0.5, quote: Optional[str] = None,
        source_path: Optional[str] = None, source_line: Optional[int] = None,
        session_id: Optional[str] = None, project_dir: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save a claim. A near-duplicate is reinforced; a claim it replaces is superseded.

        *subject* is the person the claim is about, if it is about one. Claims about different
        people never replace each other, however alike they read.

        Returns {"claim": row, "outcome": "added" | "reinforced" | "superseded", "replaced": id?}.
        """
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("cannot remember an empty claim")
        now = _now()
        new_words = _words(text)
        with self._write:
            db = self._db()
            closest, overlap = None, 0.0
            for row in db.execute("SELECT * FROM memory_claims WHERE status = 'active'"):
                if (row["project_dir"] or None) != (project_dir or None):
                    continue
                if (row["kind"] == "lesson") != (kind == "lesson"):
                    continue   # a lesson never replaces a fact, nor a fact a lesson
                if (row["subject"] or "").lower() != (subject or "").lower():
                    continue   # "Sam prefers tabs" says nothing about Alex
                old_words = _words(row["claim_text"])
                union = new_words | old_words
                score = len(new_words & old_words) / len(union) if union else 0.0
                if score > overlap:
                    closest, overlap = row, score
            if closest is not None and (overlap >= 0.85 or closest["claim_text"].lower() == text.lower()):
                # The same thing again: stronger, not duplicated. A better standing is kept.
                better = state if _rank(state) > _rank(closest["state"]) else closest["state"]
                db.execute(
                    "UPDATE memory_claims SET reinforced_at = ?, reinforced_count = reinforced_count + 1, "
                    "state = ?, basis = CASE WHEN ? != state THEN ? ELSE basis END, "
                    "confidence = MAX(confidence, ?) WHERE claim_id = ?",
                    (now, better, better, basis, confidence, closest["claim_id"]),
                )
                db.commit()
                return {"claim": self.claim(closest["claim_id"]), "outcome": "reinforced"}
            claim_id = uuid.uuid4().hex[:12]
            replaced = None
            if closest is not None and overlap >= 0.5:
                # About the same subject but saying something different: the newer claim wins,
                # the older one is kept as history.
                replaced = closest["claim_id"]
                db.execute(
                    "UPDATE memory_claims SET status = 'superseded', valid_until = ? WHERE claim_id = ?",
                    (now, replaced),
                )
                self._fts_remove(db, replaced)
            db.execute(
                "INSERT INTO memory_claims (claim_id, kind, claim_text, state, basis, confidence, salience, "
                "quote, source_path, source_line, session_id, project_dir, supersedes_claim_id, status, "
                "first_seen, reinforced_at, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (claim_id, kind, text, state, basis, confidence, salience, quote, source_path, source_line,
                 session_id, project_dir, replaced, now, now, subject or None),
            )
            if self._fts:
                db.execute("INSERT INTO memory_fts (claim_id, claim_text) VALUES (?, ?)", (claim_id, text))
            db.commit()
        out = {"claim": self.claim(claim_id), "outcome": "superseded" if replaced else "added"}
        if replaced:
            out["replaced"] = replaced
        return out

    def _fts_remove(self, db: sqlite3.Connection, claim_id: str) -> None:
        if self._fts:
            db.execute("DELETE FROM memory_fts WHERE claim_id = ?", (claim_id,))

    def claim(self, claim_id: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute("SELECT * FROM memory_claims WHERE claim_id = ?", (claim_id,)).fetchone()
        return dict(row) if row else None

    def retract_claim(self, claim_id: str) -> bool:
        """Forget a claim: it leaves memory and search, and its text is erased from the record."""
        with self._write:
            db = self._db()
            cur = db.execute(
                "UPDATE memory_claims SET status = 'retracted', claim_text = '', quote = NULL, "
                "valid_until = ? WHERE claim_id = ? AND status != 'retracted'",
                (_now(), claim_id),
            )
            self._fts_remove(db, claim_id)
            db.commit()
            return cur.rowcount > 0

    def update_claim_dates(self, claim_id: str, reinforced_at: str) -> None:
        """Set when a claim was last reinforced (used when importing, and by tests)."""
        with self._write:
            db = self._db()
            db.execute("UPDATE memory_claims SET reinforced_at = ? WHERE claim_id = ?", (reinforced_at, claim_id))
            db.commit()

    def set_claim_state(self, claim_id: str, state: str, basis: str) -> bool:
        with self._write:
            db = self._db()
            cur = db.execute(
                "UPDATE memory_claims SET state = ?, basis = ?, reinforced_at = ? WHERE claim_id = ? "
                "AND status = 'active'", (state, basis, _now(), claim_id),
            )
            db.commit()
            return cur.rowcount > 0

    def claims(self, project_dir: Optional[str] = None, include_history: bool = False) -> List[Dict[str, Any]]:
        """Active claims that apply here (global ones plus this folder's), most solid first."""
        sql = "SELECT * FROM memory_claims"
        if not include_history:
            sql += " WHERE status = 'active'"
        rows = [dict(r) for r in self._db().execute(sql)]
        if project_dir is not None:
            rows = [r for r in rows if not r["project_dir"] or r["project_dir"] == project_dir]
        rows.sort(key=lambda r: (-_rank(r["state"]), -r["salience"], r["first_seen"]))
        return rows

    def people(self) -> List[Dict[str, Any]]:
        """Everyone there are notes about: [{"name", "claims": [...]}], most recently touched first."""
        groups: Dict[str, Dict[str, Any]] = {}
        for row in self.claims():
            name = row.get("subject")
            if not name:
                continue
            group = groups.setdefault(name.lower(), {"name": name, "claims": [], "touched": ""})
            group["claims"].append(row)
            group["touched"] = max(group["touched"], row["reinforced_at"])
        return sorted(groups.values(), key=lambda g: g["touched"], reverse=True)

    def rename_person(self, old: str, new: str) -> int:
        """Give a person another name (or fold them into one already there). Returns claims moved."""
        with self._write:
            db = self._db()
            cur = db.execute(
                "UPDATE memory_claims SET subject = ? WHERE lower(subject) = lower(?)", (new, old))
            db.execute("UPDATE memory_claims SET subject = ? WHERE lower(subject) = lower(?)", (new, new))
            db.commit()
            return cur.rowcount

    def forget_person(self, name: str) -> int:
        """Forget everything about one person. Returns how many claims were erased."""
        ids = [r["claim_id"] for r in self._db().execute(
            "SELECT claim_id FROM memory_claims WHERE lower(subject) = lower(?) AND status != 'retracted'", (name,))]
        for claim_id in ids:
            self.retract_claim(claim_id)
        with self._write:   # the name goes too, not only the text
            db = self._db()
            db.execute("UPDATE memory_claims SET subject = NULL WHERE lower(subject) = lower(?)", (name,))
            db.commit()
        return len(ids)

    def search_claims(self, query: str, project_dir: Optional[str] = None, limit: int = 8) -> List[Dict[str, Any]]:
        words = sorted(_words(query))
        if not words:
            return []
        ids: List[str] = []
        if self._fts:
            match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            try:
                ids = [r["claim_id"] for r in self._db().execute(
                    "SELECT claim_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 50", (match,))]
            except sqlite3.OperationalError:
                ids = []
        active = {r["claim_id"]: r for r in self.claims(project_dir)}
        if ids:
            found = [active[i] for i in ids if i in active]
        else:
            scored = [(len(set(words) & _words(r["claim_text"])), r) for r in active.values()]
            found = [r for score, r in sorted(scored, key=lambda x: -x[0]) if score]
        # Two rankings, fused by reciprocal rank: how well the words match, and how much the
        # claim can be relied on (its standing, how often it was reinforced, how recently). A
        # well-matched guess does not outrank a slightly weaker match you confirmed three times.
        by_strength = sorted(found, key=lambda r: r["reinforced_at"], reverse=True)
        by_strength.sort(key=lambda r: (-_rank(r["state"]), -r["reinforced_count"]))
        strength = {r["claim_id"]: n for n, r in enumerate(by_strength)}
        fused = sorted(
            enumerate(found),
            # on a tie, the claim that can be relied on more comes first
            key=lambda pair: (-round(1.0 / (RRF_K + pair[0]) + 1.0 / (RRF_K + strength[pair[1]["claim_id"]]), 9),
                              strength[pair[1]["claim_id"]]),
        )
        return [r for _, r in fused][:limit]

    def add_bench(self, model_id: str, provider: str, model: str, summary: str, details: Dict[str, Any],
                  machine: str = "", seconds: float = 0.0) -> None:
        with self._write:
            db = self._db()
            db.execute(
                "INSERT INTO bench_runs (model_id, provider, model, summary, details_json, machine, seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (model_id or "", provider, model, summary, json.dumps(details), machine, float(seconds), _now()),
            )
            db.commit()

    def bench_results(self, model_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """Checks run on this computer, newest first; for one catalog model, or all of them."""
        sql, args = "SELECT * FROM bench_runs", []
        if model_id:
            sql += " WHERE model_id = ?"
            args.append(model_id)
        sql += " ORDER BY run_id DESC LIMIT ?"
        args.append(max(1, min(100, int(limit))))
        out = []
        for r in self._db().execute(sql, args):
            row = dict(r)
            try:
                row["details"] = json.loads(row.pop("details_json"))
            except ValueError:
                row["details"] = {}
            row["day"] = row["created_at"][:10]
            out.append(row)
        return out

    def recent_tasks(self, project_dir: str, exclude_session: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        """The last finished tasks in a folder, across sessions, newest first."""
        rows = self._db().execute(
            "SELECT a.title, a.subtitle, a.status, a.created_at FROM activity a "
            "JOIN sessions s ON s.session_id = a.session_id "
            "WHERE a.activity_type = 'task' AND s.project_dir = ? AND a.session_id != ? "
            "ORDER BY a.activity_id DESC LIMIT ?",
            (project_dir, exclude_session, max(1, min(20, int(limit)))),
        )
        return [dict(r) for r in rows]


_STATE_RANK = {"KNOWN": 3, "DERIVED": 2, "CONFABULATED": 1}
RRF_K = 60   # the usual constant for reciprocal rank fusion


def _rank(state: str) -> int:
    return _STATE_RANK.get(state, 0)


def _closing(fn):
    """Close this thread's connection when the outermost store call returns.

    No handle stays open between calls, so the database file is never locked on Windows (it can
    be moved, backed up or deleted while the app runs). Opening SQLite costs microseconds.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._local.depth = depth
            if depth == 0:
                self.close()

    return wrapper


for _name, _fn in list(vars(Store).items()):
    if callable(_fn) and not _name.startswith("_") and _name != "close":
        setattr(Store, _name, _closing(_fn))
