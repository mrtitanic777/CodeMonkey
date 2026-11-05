"""A fuller fake MCP server for tests: paged tools, an image result, resources, a prompt, a tool
that changes the tool list, a slow tool, a ping the server sends us, and tools that ask the client
for an answer from its model (sampling) and for answers from the user (elicitation).

Run as a script it speaks stdio; ``Fake`` is the same logic for the HTTP test server.
"""

import itertools
import json
import sys
import threading
import time

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Fake:
    def __init__(self, send):
        self.send = send          # send(message): to the client
        self.grown = False
        self.pong = False
        self.cancelled = []
        self.client_caps = {}
        self.waiting = {}         # our requests to the client: id -> [event, its answer]
        self._ids = itertools.count(1)

    def ask(self, method, params):
        rid = f"q{next(self._ids)}"
        self.waiting[rid] = [threading.Event(), None]
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        self.waiting[rid][0].wait(10)
        return self.waiting.pop(rid)[1] or {"error": {"message": "no answer"}}

    def tools(self):
        names = ["echo", "picture", "slow", "grow", "pinged", "ask_model", "ask_user"] + (["extra"] if self.grown else [])
        return [{"name": n, "description": f"the {n} tool",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}} for n in names]

    def handle(self, msg):
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:                       # a response to our ping
            if mid == "s1":
                self.pong = "result" in msg
            elif mid in self.waiting:
                self.waiting[mid][1] = msg
                self.waiting[mid][0].set()
            return None
        if method == "notifications/initialized":
            self.send({"jsonrpc": "2.0", "id": "s1", "method": "ping"})
            return None
        if method == "notifications/cancelled":
            self.cancelled.append(params.get("requestId"))
            sys.stderr.write(f"cancelled {params.get('requestId')}\n")
            sys.stderr.flush()
            return None
        if method == "initialize":
            self.client_caps = params.get("capabilities") or {}
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fullfake", "version": "1"},
                      "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}}}
        elif method == "tools/list":
            all_tools = self.tools()
            if params.get("cursor") == "p2":
                result = {"tools": all_tools[2:]}
            else:
                result = {"tools": all_tools[:2], "nextCursor": "p2"}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                result = {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}
            elif name == "picture":
                result = {"content": [{"type": "text", "text": "here it is"},
                                      {"type": "image", "data": PNG, "mimeType": "image/png"}]}
            elif name == "slow":
                time.sleep(3)
                result = {"content": [{"type": "text", "text": "late"}]}
            elif name == "grow":
                self.grown = True
                self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                result = {"content": [{"type": "text", "text": "grown"}]}
            elif name == "ask_model":
                got = self.ask("sampling/createMessage", {"systemPrompt": "Be brief.", "maxTokens": 20, "messages": [
                    {"role": "user", "content": {"type": "text", "text": str(args.get("text") or "say hi")}}]})
                text = got["result"]["content"]["text"] if "result" in got else "refused: " + got["error"]["message"]
                result = {"content": [{"type": "text", "text": text}]}
            elif name == "ask_user":
                got = self.ask("elicitation/create", {"message": "Who is asking?", "requestedSchema": {
                    "type": "object", "required": ["name"], "properties": {
                        "name": {"type": "string", "title": "Your name"}, "age": {"type": "integer"},
                        "tea": {"type": "boolean"}, "size": {"type": "string", "enum": ["S", "L"]}}}})
                result = {"content": [{"type": "text", "text": json.dumps(got.get("result") or got.get("error"))}]}
            elif name == "pinged":
                result = {"content": [{"type": "text", "text": "pong" if self.pong else "no pong"}]}
            else:
                result = {"content": [{"type": "text", "text": "no such tool"}], "isError": True}
        elif method == "resources/list":
            result = {"resources": [{"uri": "mem://notes", "name": "notes"}]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": [{"uriTemplate": "mem://item/{id}", "name": "item"}]}
        elif method == "resources/read":
            result = {"contents": [{"uri": params.get("uri"), "mimeType": "text/plain", "text": "hello notes"}]}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "review", "description": "Review code",
                                   "arguments": [{"name": "focus", "required": True}]}]}
        elif method == "prompts/get":
            focus = (params.get("arguments") or {}).get("focus", "")
            result = {"messages": [{"role": "user", "content": {"type": "text",
                                                                "text": "Please review with a focus on " + focus}}]}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    out = threading.Lock()

    def send(message):
        with out:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    fake = Fake(send)
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)

        def work(m=msg):
            reply = fake.handle(m)
            if reply is not None:
                send(reply)
        # each request on its own thread, so a slow tool does not hold up the rest
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    main()
