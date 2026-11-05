"""A tiny language server for tests: reports an error on every line containing BAD, and answers
definition, references, hover and documentSymbol from the open text. It also asks the client for
its configuration, as real servers do."""

import json
import sys

texts = {}


def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


def publish(uri, version):
    diags = [{"range": {"start": {"line": i, "character": line.index("BAD")}, "end": {"line": i, "character": 99}},
              "severity": 1, "message": "BAD is not allowed", "source": "fake", "code": "F1"}
             for i, line in enumerate(texts[uri].splitlines()) if "BAD" in line]
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
          "params": {"uri": uri, "version": version, "diagnostics": diags}})


while True:
    msg = read()
    if msg is None:
        break
    method, mid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": "cfg", "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "textDocument/didOpen":
        doc = p["textDocument"]
        texts[doc["uri"]] = doc["text"]
        publish(doc["uri"], doc["version"])
    elif method == "textDocument/didChange":
        texts[p["textDocument"]["uri"]] = p["contentChanges"][-1]["text"]
        publish(p["textDocument"]["uri"], p["textDocument"]["version"])
    elif method in ("textDocument/definition", "textDocument/references"):
        uri = p["textDocument"]["uri"]
        loc = {"uri": uri, "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}}}
        result = loc if method.endswith("definition") else [loc, dict(loc, range={"start": {"line": 2, "character": 0},
                                                                                       "end": {"line": 2, "character": 3}})]
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": "plaintext",
                                                                   "value": f"col {p['position']['character']}"}}})
    elif method == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": mid, "result": [
            {"name": "Thing", "kind": 5, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 3, "character": 0}},
             "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 11}},
             "children": [{"name": "run", "kind": 6, "range": {"start": {"line": 1, "character": 4}, "end": {"line": 2, "character": 0}},
                           "selectionRange": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 11}}}]}]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
