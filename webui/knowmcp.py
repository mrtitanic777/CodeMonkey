"""CodeMonkey's knowledge tools (webui/knowtools.py) as an MCP server, over stdio.

Any MCP client can use them: another CodeMonkey, Claude Code, an editor. Run:

    python -m webui.knowmcp [--project DIR] [--kb DIR]

--project is the folder whose files the tools read (default: the current folder); --kb is where the
packs are (default: CodeMonkey's agent-workspace/codekb, built by the app). Messages are JSON-RPC 2.0,
one per line. Every tool only reads, except code_build, which is marked so a client asks before it
runs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROTOCOL = "2025-06-18"


class _App:
    """What CodeKnowledge needs of the app: a workspace and settings."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.settings = {}


def serve(project: str, kb_home: str, stdin=sys.stdin, stdout=sys.stdout) -> None:
    from webui.codeknow import CodeKnowledge
    from webui.knowtools import specs

    app = _App(os.path.dirname(kb_home))
    knowledge = CodeKnowledge(app)
    tools = {s["name"]: s for s in specs(knowledge, lambda: project)}

    def reply(mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        msg["error" if error else "result"] = error or result
        stdout.write(json.dumps(msg) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:
            continue   # a notification (initialized, cancelled): nothing to answer
        if method == "initialize":
            reply(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "codemonkey-knowledge", "version": "0.3.0"},
                        "instructions": ("Tools over what this computer knows for certain: C, C++ and assembly "
                                         "checked against the installed SDKs, compilers and assemblers, and the "
                                         "MSHMOE kernel. Look a name up before writing it; check and compile after.")})
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": [{"name": s["name"], "description": s["description"], "inputSchema": s["parameters"],
                                   "annotations": {"readOnlyHint": s.get("read_only", not s.get("approval"))}}
                                  for s in tools.values()]})
        elif method == "tools/call":
            spec = tools.get(params.get("name"))
            if spec is None:
                reply(mid, error={"code": -32602, "message": f"no tool {params.get('name')!r}"})
                continue
            try:
                text, failed = spec["func"](**(params.get("arguments") or {})), False
            except (ValueError, OSError, KeyError, TypeError) as exc:
                text, failed = f"ERROR: {exc}", True
            except Exception:  # noqa: BLE001 - report it to the client rather than dying
                text, failed = "ERROR: " + traceback.format_exc(limit=3), True
            reply(mid, {"content": [{"type": "text", "text": str(text)}], "isError": failed})
        else:
            reply(mid, error={"code": -32601, "message": f"no method {method!r}"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--kb", default=os.path.join(ROOT, "agent-workspace", "codekb"))
    args = ap.parse_args()
    serve(os.path.abspath(args.project), os.path.abspath(args.kb))


if __name__ == "__main__":
    main()
