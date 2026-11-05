"""A local record of what the agent used: one line per finished turn, never sent anywhere.

Token counts are estimates (characters / 4), the same estimate the rest of the web UI uses.
"""

from __future__ import annotations

import datetime
import json
import threading
from typing import Any, Dict, List


class UsageLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def add(self, model: str, tokens_in: int, tokens_out: int, elapsed_ms: int) -> None:
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "in": int(tokens_in),
            "out": int(tokens_out),
            "ms": int(elapsed_ms),
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")

    def _records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return records

    def summary(self, rates: Dict[str, float]) -> Dict[str, Any]:
        today = datetime.date.today()
        week_ago = today - datetime.timedelta(days=6)
        totals = {"tasks": 0, "in": 0, "out": 0, "ms": 0}
        by_model: Dict[str, Dict[str, int]] = {}
        # the last 14 days, oldest first, every day present (a quiet day is a zero, not a gap)
        days = {today - datetime.timedelta(days=i): {"in": 0, "out": 0, "tasks": 0} for i in range(13, -1, -1)}
        for rec in self._records():
            try:
                day = datetime.datetime.fromisoformat(rec["ts"]).date()
            except (KeyError, ValueError):
                continue
            if day in days:
                days[day]["tasks"] += 1
                days[day]["in"] += rec.get("in", 0)
                days[day]["out"] += rec.get("out", 0)
            if day == today:
                totals["tasks"] += 1
                totals["in"] += rec.get("in", 0)
                totals["out"] += rec.get("out", 0)
                totals["ms"] += rec.get("ms", 0)
            if day >= week_ago:
                slot = by_model.setdefault(str(rec.get("model", "?")), {"tasks": 0, "in": 0, "out": 0})
                slot["tasks"] += 1
                slot["in"] += rec.get("in", 0)
                slot["out"] += rec.get("out", 0)
        grand = sum(m["in"] + m["out"] for m in by_model.values()) or 1
        rate_in = float((rates or {}).get("input") or 0)
        rate_out = float((rates or {}).get("output") or 0)
        models = [
            {
                "model": name,
                "tasks": m["tasks"],
                "tokens": m["in"] + m["out"],
                "share": round((m["in"] + m["out"]) / grand * 100),
                "cost": round(m["in"] / 1e6 * rate_in + m["out"] / 1e6 * rate_out, 4),
            }
            for name, m in sorted(by_model.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
        ]
        daily = [dict(v, day=d.isoformat()) for d, v in sorted(days.items())]
        return {"today": totals, "by_model": models, "days": daily, "estimated": True}
