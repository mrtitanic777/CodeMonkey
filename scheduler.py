"""Background job scheduler.

Jobs live in ``jobs.yaml``. Each job has a name, a schedule, and a prompt::

    jobs:
      - name: morning-briefing
        schedule: {cron: "0 8 * * *"}
        prompt: "Write a morning briefing."

      - name: hourly-checkin
        schedule: {interval_seconds: 3600}
        prompt: "Do a quick self-check."

``python scheduler.py --once`` runs due jobs a single time; without flags it
loops forever, checking every 30 seconds. Results are appended to
``<workspace>/runs.log``. Jobs run non-interactively (auto-approve), so keep
their prompts read-mostly and let the approval gate live in chat.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import time
import traceback

import mini_yaml
from agent import AgentLoop
from config import Config, load_config, resolve_workspace
from memory import MemoryManager

STATE_FILE = "scheduler_state.json"
RUNS_LOG = "runs.log"
POLL_SECONDS = 30


# ------------------------------------------------------------ cron ---


def _parse_field(field: str, lo: int, hi: int) -> set:
    """Parse one cron field into a set of matching ints.

    Supports ``*``, integers, ``*/n`` steps, ``a,b`` lists, ``a-b`` ranges.
    """
    values: set = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    if not all(lo <= v <= hi for v in values):
        raise ValueError(f"Cron field {field!r} out of range {lo}-{hi}")
    return values


def cron_matches(expr: str, when: datetime.datetime) -> bool:
    """True if the 5-field cron expression matches *when* (minute precision)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Cron expression needs 5 fields, got: {expr!r}")
    minute, hour, dom, month, dow = fields
    checks = [
        (when.minute, _parse_field(minute, 0, 59)),
        (when.hour, _parse_field(hour, 0, 23)),
        (when.day, _parse_field(dom, 1, 31)),
        (when.month, _parse_field(month, 1, 12)),
        # cron Sunday is 0 or 7; Python Monday is 0.
        ((when.weekday() + 1) % 7, _parse_field(dow, 0, 7) | _dow_sunday_fix(dow)),
    ]
    return all(value in allowed for value, allowed in checks)


def _dow_sunday_fix(dow: str) -> set:
    """Accept ``7`` as Sunday too."""
    try:
        vals = _parse_field(dow, 0, 7)
    except ValueError:
        return set()
    return {0} if 7 in vals else set()


# ------------------------------------------------------------ jobs ---


def load_jobs(path: str = "jobs.yaml") -> list:
    """Load and validate the job list."""
    data = mini_yaml.parse_file(path) if os.path.exists(path) else {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    for job in jobs:
        if not job.get("name") or not job.get("prompt"):
            raise ValueError(f"Job needs a name and prompt: {job!r}")
        sched = job.get("schedule") or {}
        if not isinstance(sched, dict) or not (
            "interval_seconds" in sched or "cron" in sched
        ):
            raise ValueError(
                f"Job {job['name']!r} needs schedule.interval_seconds "
                f"or schedule.cron"
            )
    return jobs


def _load_state(workspace: str) -> dict:
    path = os.path.join(workspace, STATE_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_state(workspace: str, state: dict) -> None:
    path = os.path.join(workspace, STATE_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def is_due(job: dict, now: datetime.datetime, last_run: float | None) -> bool:
    """Decide whether *job* should fire at *now*."""
    sched = job["schedule"]
    if "interval_seconds" in sched:
        interval = int(sched["interval_seconds"])
        return last_run is None or (now.timestamp() - last_run) >= interval
    # Cron: fire when the current minute matches and we haven't fired
    # already within this minute.
    if not cron_matches(str(sched["cron"]), now):
        return False
    if last_run is None:
        return True
    return last_run < now.replace(second=0, microsecond=0).timestamp()


def run_job(job: dict, config: Config, workspace: str) -> str:
    """Execute one job's prompt through the agent. Returns the result text."""
    agent = AgentLoop(
        config=config,
        memory=MemoryManager(workspace),
        auto_approve=True,  # non-interactive; prompts are read-mostly
    )
    return agent.run(f"[Scheduled job: {job['name']}]\n\n{job['prompt']}")


def log_run(workspace: str, job_name: str, result: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"\n{'=' * 60}\n[{stamp}] job: {job_name}\n{'-' * 60}\n"
        f"{result[:4000]}\n"
    )
    with open(os.path.join(workspace, RUNS_LOG), "a", encoding="utf-8") as fh:
        fh.write(entry)


def check_once(config: Config, workspace: str) -> list:
    """Run every due job once. Returns the names of jobs that fired."""
    jobs = load_jobs()
    state = _load_state(workspace)
    now = datetime.datetime.now()
    fired = []
    for job in jobs:
        name = job["name"]
        last = state.get(name)
        try:
            due = is_due(job, now, last)
        except ValueError as exc:
            print(f"[{name}] bad schedule: {exc}")
            continue
        if not due:
            continue
        print(f"[{now:%H:%M:%S}] running job: {name}")
        try:
            result = run_job(job, config, workspace)
        except Exception:  # noqa: BLE001 - logged, scheduler continues
            result = "JOB FAILED:\n" + traceback.format_exc(limit=5)
        log_run(workspace, name, result)
        state[name] = now.timestamp()
        fired.append(name)
    _save_state(workspace, state)
    return fired


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scheduled agent jobs.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check due jobs a single time, then exit.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    workspace = resolve_workspace(config)

    if args.once:
        fired = check_once(config, workspace)
        print("Fired:", ", ".join(fired) if fired else "(none due)")
        return

    print(f"Scheduler running. Jobs file: jobs.yaml. Ctrl-C to stop.")
    while True:
        try:
            check_once(config, workspace)
        except Exception:  # noqa: BLE001 - keep the loop alive
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
