#!/usr/bin/env python3
"""post_tool audit logger: append every tool call and result to JSONL.

Reads {"event", "tool", "args", "result"} JSON on stdin, appends one line to
the audit log, and writes {"note": ...} on stdout (or {} on any failure —
a logger must never break the agent).

Log location: $HOOK_AUDIT_LOG, default ./agent-workspace/hooks-audit.jsonl.
"""

import datetime
import json
import os
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return
    log_path = os.environ.get(
        "HOOK_AUDIT_LOG", "./agent-workspace/hooks-audit.jsonl"
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": payload.get("tool"),
            "args": payload.get("args"),
            "result": (payload.get("result") or "")[:2000],
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        print(json.dumps({"note": f"audited to {os.path.basename(log_path)}"}))
    except OSError:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
