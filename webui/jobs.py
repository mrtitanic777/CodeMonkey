"""Scheduled work: jobs that run by themselves, with a history you can trust.

A job is a task and a five-field cron schedule. When it is due, a fresh worker agent does the
task alone and ends with a report, or with NOTHING_TO_REPORT when there is nothing worth your
attention. A report is delivered into the job's own session, where you can reply to it.

What this is honest about (the rules come from how larger agent runtimes describe scheduling):

- It polls. A run starts around its time, not at the exact second, and only while CodeMonkey
  is running. Nothing watches in between.
- The schedule is not proof that anything ran: the run history is.
- A worker runs unattended, so it can only look: read and search files, plus commands you have
  already chosen to always allow in that folder. It cannot edit anything.
- A job can have a check command. It runs first, without the model; the agent is woken only
  when its output has changed since the last run. Most checks then cost nothing.
- A job that keeps failing pauses itself, says why, and tries again later.
- It will not start a run when free memory is below your limit.
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Any, Dict, List, Optional

from coder import CoderLoop, coder_tools
from scheduler import cron_matches
from tools.registry import Tool, ToolRegistry

from . import machine
from .epistemic import Ledger, command_key

SILENT = "NOTHING_TO_REPORT"
FAILURES_BEFORE_PAUSE = 3
RETRY_AFTER_MINUTES = 60

_WORKER_PROMPT = (
    "You are running a scheduled task by yourself; nobody is watching and you cannot ask "
    "questions. You can read and search files and run only commands that are already allowed. "
    "Do the task below, then end with a short report for the user: what you checked, what you "
    "found, and what (if anything) needs their attention, with the file or output behind each "
    f"point. If nothing is new or worth their attention, reply with exactly {SILENT} and "
    "nothing else.\n\nTask:\n"
)

PRESETS = {
    "*/30 * * * *": "Every 30 minutes",
    "0 * * * *": "Every hour",
    "0 9 * * *": "Every day at 09:00",
    "0 18 * * *": "Every day at 18:00",
    "0 9 * * 1-5": "Weekdays at 09:00",
    "0 9 * * 1": "Mondays at 09:00",
}


def describe(expr: str) -> str:
    """A schedule in plain words where it has an obvious reading."""
    if expr in PRESETS:
        return PRESETS[expr]
    parts = expr.split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"] and parts[0].isdigit() and parts[1].isdigit():
        return f"Every day at {int(parts[1]):02d}:{int(parts[0]):02d}"
    if len(parts) == 5 and parts[1:] == ["*", "*", "*", "*"] and parts[0].startswith("*/"):
        return f"Every {parts[0][2:]} minutes"
    return expr


def valid_schedule(expr: str) -> bool:
    try:
        cron_matches(str(expr), datetime.datetime.now())
        return True
    except (ValueError, TypeError):
        return False


def next_run(expr: str, after: datetime.datetime) -> Optional[datetime.datetime]:
    """The first minute after *after* that the schedule matches (looking 40 days ahead)."""
    when = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(40 * 24 * 60):
        if cron_matches(expr, when):
            return when
        when += datetime.timedelta(minutes=1)
    return None


def _iso(when: Optional[datetime.datetime]) -> Optional[str]:
    return when.isoformat(timespec="seconds") if when else None


class Jobs:
    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._running: set = set()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ manage ---

    def public(self, job: Dict[str, Any]) -> Dict[str, Any]:
        runs = self.app.store.runs(job["job_id"], 1)
        with self._lock:
            running = job["job_id"] in self._running
        return {
            "id": job["job_id"],
            "title": job["title"],
            "task": job["task_text"],
            "schedule": job["schedule_expr"],
            "schedule_text": describe(job["schedule_expr"]),
            "project_dir": job["project_dir"],
            "folder": os.path.basename(job["project_dir"].rstrip("\\/")) or job["project_dir"],
            "enabled": bool(job["enabled"]),
            "watch": job.get("watch_cmd") or "",
            "running": running,
            "session_id": job["session_id"],
            "next_run_at": job["next_run_at"] if job["enabled"] else None,
            "last_run_at": job["last_run_at"],
            "last_status": job["last_status"],
            "blocked_reason": job["blocked_reason"],
            "blocked_until": job["blocked_until"],
            "last_run": runs[0] if runs else None,
        }

    def listing(self) -> List[Dict[str, Any]]:
        return [self.public(j) for j in self.app.store.jobs()]

    def _check_watch(self, watch: str, project_dir: str) -> str:
        from .app import simple_command

        watch = str(watch or "").strip()
        if watch and not (simple_command(watch) and self.app.command_allowed(command_key(watch), project_dir)):
            raise ValueError(
                f"the check command must be one plain command you already always allow in that folder "
                f"({command_key(watch) or watch!r} is not on the list)"
            )
        return watch

    def add(self, title: str, task: str, schedule: str, project_dir: str, watch: str = "") -> Dict[str, Any]:
        title, task, schedule = str(title or "").strip(), str(task or "").strip(), str(schedule or "").strip()
        if not title or not task:
            raise ValueError("a scheduled job needs a title and a task")
        if not valid_schedule(schedule):
            raise ValueError("the schedule must be five cron fields, for example: 0 9 * * *")
        project_dir = os.path.abspath(os.path.expanduser(project_dir or self.app.default_project))
        if not os.path.isdir(project_dir):
            raise ValueError(f"folder not found: {project_dir}")
        watch = self._check_watch(watch, project_dir)
        job = self.app.store.add_job(
            title, task, schedule, project_dir, _iso(next_run(schedule, datetime.datetime.now()))
        )
        if watch:
            job = self.app.store.update_job(job["job_id"], watch_cmd=watch)
        return self.public(job)

    def update(self, job_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        job = self.app.store.job(job_id)
        if job is None:
            raise KeyError(job_id)
        fields: Dict[str, Any] = {}
        if "title" in patch and str(patch["title"]).strip():
            fields["title"] = str(patch["title"]).strip()[:120]
        if "task" in patch and str(patch["task"]).strip():
            fields["task_text"] = str(patch["task"]).strip()
        schedule = job["schedule_expr"]
        if "schedule" in patch:
            if not valid_schedule(patch["schedule"]):
                raise ValueError("the schedule must be five cron fields, for example: 0 9 * * *")
            schedule = fields["schedule_expr"] = str(patch["schedule"]).strip()
        if "watch" in patch:
            fields["watch_cmd"] = self._check_watch(patch["watch"], job["project_dir"]) or None
            fields["watch_state"] = None
        if "enabled" in patch:
            fields["enabled"] = bool(patch["enabled"])
        if "schedule" in patch or patch.get("enabled"):
            # resuming or rescheduling clears a self-pause: you looked at it
            fields.update(
                next_run_at=_iso(next_run(schedule, datetime.datetime.now())),
                blocked_reason=None, blocked_until=None, consecutive_failures=0,
            )
        return self.public(self.app.store.update_job(job_id, **fields))

    def remove(self, job_id: str) -> bool:
        return self.app.store.delete_job(job_id)

    # --------------------------------------------------------------- run ---

    def tick(self, now: Optional[datetime.datetime] = None, wait: bool = False) -> List[str]:
        """Start every job that is due. Returns the ids started."""
        now = now or datetime.datetime.now()
        started = []
        for job in self.app.store.jobs():
            if not job["enabled"] or not job["next_run_at"]:
                continue
            if job["blocked_until"] and job["blocked_until"] > _iso(now):
                continue
            if job["next_run_at"] <= _iso(now):
                if self.run_now(job["job_id"], "schedule", now=now, wait=wait):
                    started.append(job["job_id"])
        return started

    def run_now(self, job_id: str, trigger: str = "manual",
                now: Optional[datetime.datetime] = None, wait: bool = False) -> bool:
        job = self.app.store.job(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._lock:
            if job_id in self._running:
                return False
            self._running.add(job_id)
        now = now or datetime.datetime.now()
        scheduled_for = job["next_run_at"] if trigger == "schedule" else None
        # the next time is fixed before the run, so a slow run never causes a pile-up
        self.app.store.update_job(job_id, next_run_at=_iso(next_run(job["schedule_expr"], now)))
        thread = threading.Thread(
            target=self._run, args=(job, trigger, scheduled_for), name=f"job-{job_id[:6]}", daemon=True
        )
        thread.start()
        if wait:
            thread.join()
        return True

    def _worker_registry(self, job: Dict[str, Any], ledger: Ledger, counter: Dict[str, int]) -> ToolRegistry:
        from .app import _ANSI_RE, simple_command

        project = job["project_dir"]
        registry = ToolRegistry(auto_approve=False)
        tools = {t.name: t for t in coder_tools(project, auto_approve=False)}
        for name in ("read", "glob", "grep", "bash"):
            registry.register(tools[name])

        def approver(name: str, args: Dict[str, Any], preview: str):
            cmd = str((args or {}).get("cmd", ""))
            if name == "bash" and simple_command(cmd) and self.app.command_allowed(command_key(cmd), project):
                return True, None
            return False, ("this is an unattended run: only commands the user has chosen to always "
                           "allow in this folder can run. Report that you could not run it.")

        registry.approver = approver
        original = registry.execute

        def traced(name: str, arguments: Dict[str, Any]) -> str:
            text = _ANSI_RE.sub("", str(original(name, arguments) or ""))
            ledger.note_tool(name, arguments, text)
            counter["steps"] += 1
            return text

        registry.execute = traced  # type: ignore[method-assign]
        return registry

    def _client(self, job: Dict[str, Any]):
        """The model client for a run (separate so tests can script it)."""
        provider = self.app.settings.get("default_provider") or self.app.config.provider
        model = self.app.settings.get("default_model") or self.app.config.model
        p, _, m = str(self.app.settings.get("helper_model") or "").partition("/")
        if p and m and p in (self.app.config.providers or {}):
            provider, model = p, m   # background work has a model of its own
        return self.app.client_for(provider, model)

    def _run(self, job: Dict[str, Any], trigger: str, scheduled_for: Optional[str]) -> None:
        store, job_id = self.app.store, job["job_id"]
        run_id = store.start_run(job_id, trigger, scheduled_for)
        counter = {"steps": 0}
        status, report, error = "error", "", ""
        try:
            floor = float(self.app.settings.get("min_free_ram_gb") or 0)
            free = machine.memory()["free_gb"]
            if floor and free is not None and free < floor:
                status, error = "skipped", f"only {free} GB of memory was free (your limit is {floor:g} GB)"
            elif not os.path.isdir(job["project_dir"]):
                error = f"the folder {job['project_dir']} no longer exists"
            elif job.get("watch_cmd") and self._unchanged(job):
                status, report = "silent", ""
                error = ""
                counter["steps"] = 0
                self._quiet_note = "The check command's output has not changed, so the agent was not woken."
            else:
                ledger = Ledger(job["project_dir"])
                loop = CoderLoop(
                    config=self.app.config, project_dir=job["project_dir"],
                    registry=self._worker_registry(job, ledger, counter), memory=self.app.memory,
                    skills=self.app.skills, client=self._client(job), auto_approve=False, depth=1,
                )
                task = job["task_text"]
                if job.get("watch_cmd"):
                    task += (f"\n\nThis run was started because the output of `{job['watch_cmd']}` changed. "
                             f"Its output now:\n{self._watch_output[:3000]}")
                reply = str(loop.run(_WORKER_PROMPT + task) or "").strip()
                if reply.startswith("[Error"):
                    error = reply
                elif reply.upper().startswith(SILENT) or not reply:
                    status = "silent"
                else:
                    status, report = "reported", reply
                    self._deliver(job, report, ledger)
        except Exception as exc:  # noqa: BLE001 - a run must always end with a record
            error = f"{type(exc).__name__}: {exc}"
        finally:
            quiet = getattr(self, "_quiet_note", "")
            self._quiet_note = ""
            store.finish_run(run_id, status, report or (quiet if status == "silent" else ""), error,
                             counter["steps"], delivered=status == "reported")
            self._settle(job_id, status, error)
            with self._lock:
                self._running.discard(job_id)

    def _unchanged(self, job: Dict[str, Any]) -> bool:
        """Run the job's check command (no model). True when its output is the same as last time."""
        import hashlib

        from procutil import run_shell

        self._watch_output = ""
        try:
            done = run_shell(job["watch_cmd"], cwd=job["project_dir"], timeout=120)
            output = "[stopped: it ran for over two minutes]" if done.timed_out else f"[exit {done.code}]\n{done.output}"
        except OSError as exc:
            output = f"[could not run: {exc}]"
        self._watch_output = output
        digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
        if digest == job.get("watch_state"):
            return True
        self.app.store.update_job(job["job_id"], watch_state=digest)
        return False

    def _settle(self, job_id: str, status: str, error: str) -> None:
        """Update the job after a run; pause it when it keeps failing."""
        store = self.app.store
        job = store.job(job_id)
        if job is None:
            return
        now = datetime.datetime.now()
        fields: Dict[str, Any] = {"last_run_at": _iso(now), "last_status": status}
        if status == "error":
            failures = job["consecutive_failures"] + 1
            fields["consecutive_failures"] = failures
            if failures >= FAILURES_BEFORE_PAUSE:
                fields["blocked_reason"] = f"it failed {failures} times in a row: {error[:200]}"
                fields["blocked_until"] = _iso(now + datetime.timedelta(minutes=RETRY_AFTER_MINUTES))
        elif status != "skipped":
            fields.update(consecutive_failures=0, blocked_reason=None, blocked_until=None)
        store.update_job(job_id, **fields)
        if status in ("reported", "error"):
            store.add_activity(
                job.get("session_id"), "scheduled_run", "Scheduled: " + job["title"],
                "reported" if status == "reported" else error[:160],
                "success" if status == "reported" else "error", {"job_id": job_id},
            )

    def _deliver(self, job: Dict[str, Any], report: str, ledger: Ledger) -> None:
        """Put the report in the job's own session, labelled like any other reply."""
        app = self.app
        session = app.get_session(job["session_id"]) if job["session_id"] else None
        if session is None and job["session_id"]:
            session = app.reopen_session(job["session_id"])
        if session is None:
            session = app.create_session(job["project_dir"], title=("Scheduled: " + job["title"])[:80])
            app.store.update_job(job["job_id"], session_id=session.id)
        claims = None
        if app.settings.get("claim_labels", True):
            try:
                claims = ledger.label_reply(report)
            except Exception:  # noqa: BLE001
                claims = None
        stamp = datetime.datetime.now().strftime("%A %H:%M")
        session._record({"kind": "notice", "text": f"Scheduled run, {stamp}"}, source="scheduler")
        session._record({"kind": "assistant", "text": report, "claims": claims}, source="scheduler")
        with session._lock:
            session.history.append({"role": "assistant", "content": f"[Scheduled report, {stamp}]\n{report}"})
            session.session_mgr.save_history(session.history)
        session._save_transcript()
        session.last_active = datetime.datetime.now().isoformat(timespec="seconds")
        app.refresh_index_entry(session)
        session.bus.emit("notice", {"text": f"Scheduled run, {stamp}"})
        session.bus.emit("token", {"text": report, "final": True})
        if claims is not None:
            session.bus.emit("claims", claims)
        session.bus.emit("turn_end", {})

    # ----------------------------------------------------------- service ---

    def start(self, interval: float = 30.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the scheduler must never die
                    pass

        self._thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._thread.start()

    # ------------------------------------------------------- agent tools ---

    def tools(self, session) -> List[Tool]:
        text = {"type": "string"}

        def add(title: str, task: str, schedule: str, watch: str = "") -> str:
            try:
                job = self.add(title, task, schedule, session.project_dir, watch)
            except ValueError as exc:
                return f"ERROR: {exc}"
            return (f"Scheduled as job {job['id']}: {job['schedule_text']}, next run around "
                    f"{job['next_run_at']}. It runs unattended and read-only, only while CodeMonkey "
                    "is running. Tell the user it checks at intervals, not the moment something happens.")

        def listing() -> str:
            jobs = self.listing()
            if not jobs:
                return "No scheduled jobs."
            return "\n".join(
                f"{j['id']}  {'on ' if j['enabled'] else 'off'} {j['schedule_text']}  {j['title']}"
                f"  last: {j['last_status'] or 'never run'}"
                + (f"  PAUSED ITSELF: {j['blocked_reason']}" if j["blocked_reason"] else "")
                for j in jobs
            )

        def update(job_id: str, enabled: str = "", title: str = "", task: str = "", schedule: str = "") -> str:
            patch: Dict[str, Any] = {}
            if enabled:
                patch["enabled"] = str(enabled).lower() in ("true", "1", "yes", "on")
            for key, value in (("title", title), ("task", task), ("schedule", schedule)):
                if value:
                    patch[key] = value
            try:
                job = self.update(job_id, patch)
            except KeyError:
                return f"ERROR: no job {job_id}."
            except ValueError as exc:
                return f"ERROR: {exc}"
            return f"Updated job {job['id']}: {'on' if job['enabled'] else 'paused'}, {job['schedule_text']}."

        def remove(job_id: str) -> str:
            return f"Removed job {job_id}." if self.remove(job_id) else f"ERROR: no job {job_id}."

        def run(job_id: str) -> str:
            try:
                ok = self.run_now(job_id)
            except KeyError:
                return f"ERROR: no job {job_id}."
            return ("Started. This means the run began, not that it finished; check cron_runs for the result."
                    if ok else "It is already running.")

        def runs(job_id: str) -> str:
            rows = self.app.store.runs(job_id, 8)
            if not rows:
                return "This job has never run. A schedule is not a run."
            return "\n".join(
                f"{r['started_at']}  {r['status']:9} {r['trigger']:8} {r['steps']} steps  "
                + (r["error_text"] or r["result_summary"])[:160].replace("\n", " ")
                for r in rows
            )

        return [
            Tool(
                name="cron_add",
                description=(
                    "Schedule a task to run by itself on a five-field cron schedule (minute hour "
                    "day month weekday), for example '0 9 * * 1-5'. Runs are unattended and can only "
                    "read files and run commands the user already always allows. The report is "
                    "delivered to its own session. Use for recurring checks and digests."
                ),
                parameters={"type": "object", "properties": {
                    "title": dict(text, description="Short name shown to the user."),
                    "task": dict(text, description="Complete instructions for the unattended worker."),
                    "schedule": dict(text, description="Five cron fields."),
                    "watch": dict(text, description=(
                        "Optional check command (must be one the user always allows here). It runs "
                        "first without the model; the worker is woken only when its output changed.")),
                }, "required": ["title", "task", "schedule"]},
                func=add, needs_approval=True,
                preview=lambda a: f"Schedule: {describe(str(a.get('schedule', '')))}\n\n{a.get('task', '')}",
            ),
            Tool(name="cron_list", description="List scheduled jobs with their state.",
                 parameters={"type": "object", "properties": {}}, func=listing),
            Tool(name="cron_update",
                 description="Change a scheduled job: enabled ('true'/'false'), title, task or schedule.",
                 parameters={"type": "object", "properties": {
                     "job_id": text, "enabled": text, "title": text, "task": text, "schedule": text},
                     "required": ["job_id"]}, func=update),
            Tool(name="cron_remove", description="Delete a scheduled job and its history.",
                 parameters={"type": "object", "properties": {"job_id": text}, "required": ["job_id"]},
                 func=remove, needs_approval=True),
            Tool(name="cron_run", description="Run a scheduled job now. Returns when it has started, not finished.",
                 parameters={"type": "object", "properties": {"job_id": text}, "required": ["job_id"]}, func=run),
            Tool(name="cron_runs",
                 description="The run history of a job: the only proof of what ran and what it found.",
                 parameters={"type": "object", "properties": {"job_id": text}, "required": ["job_id"]}, func=runs),
        ]
