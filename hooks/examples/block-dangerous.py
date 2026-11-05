#!/usr/bin/env python3
"""pre_tool guard: block destructive commands and absolute-path escapes.

Reads {"event", "tool", "args"} JSON on stdin, writes a decision on stdout:
  {"allow": true}  or  {"allow": false, "message": "reason"}
"""

import json
import os
import re
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({"allow": True}))
        return
    tool = payload.get("tool", "")
    args = payload.get("args", {}) or {}

    deny = None
    if tool == "bash":
        cmd = args.get("cmd", "") or args.get("command", "")
        if re.search(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b", cmd):
            deny = "refusing recursive rm; delete files individually instead"
        elif re.search(r"\b(mkfs|dd\s+.*of=/dev)\b", cmd):
            deny = "refusing disk-destructive command"
    if tool in ("write_file", "edit"):
        path = args.get("path", "")
        if os.path.isabs(path):
            deny = (
                f"refusing absolute path {path!r}; "
                "all file tools are sandboxed to the project directory"
            )

    if deny:
        print(json.dumps({"allow": False, "message": deny}))
    else:
        print(json.dumps({"allow": True}))


if __name__ == "__main__":
    main()
