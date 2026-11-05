"""Fake MCP server for tests: speaks enough JSON-RPC stdio to exercise the
MCPClient (initialize, tools/list, tools/call). Reads one JSON object per
line on stdin, writes one per line on stdout.
"""

import json
import sys


def respond(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            respond(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            })
        elif method == "notifications/initialized":
            pass  # notification: no response
        elif method == "tools/list":
            respond(msg_id, {"tools": [{
                "name": "echo",
                "description": "Echoes the input text back.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }]})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            args = params.get("arguments", {}) or {}
            respond(msg_id, {"content": [
                {"type": "text", "text": "echo:" + str(args.get("text", ""))}
            ]})
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"},
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
