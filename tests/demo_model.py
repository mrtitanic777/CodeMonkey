"""A scripted stand-in for a model server, for looking at the interface without loading a model.

    python design/demo_model.py [port]          (default 8799)

It speaks just enough of the OpenAI chat-completions API for CodeMonkey: every user message gets
the same short piece of work (search, read, edit, run the tests, answer). It reasons about nothing.
Point a provider at http://127.0.0.1:8799/v1 to use it; see design/README for the demo folder.
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STEPS = [
    ("grep", {"pattern": "def ordinal", "path": "."}),
    ("read", {"path": "inflection/__init__.py"}),
    ("edit", {"path": "inflection/__init__.py",
              "old_text": "    if number % 100 < (11, 12, 13):",
              "new_text": "    if number % 100 in (11, 12, 13):"}),
    ("bash", {"cmd": "python -m pytest -q"}),
]

ANSWER = """I found it. In `ordinal`, the membership check was written as a comparison:

```python
if number % 100 < (11, 12, 13):   # compares an int with a tuple
    return "th"
```

I changed `<` to `in` in `inflection/__init__.py`. **3 passed** and 1 failed; the failure left is in `ordinalize`:

| Test | Before | After |
|---|---|---|
| `test_ordinal` | failing | passing |
| `test_ordinalize` | failing | failing |

That one is a separate bug in how the suffix is joined, probably in `helpers/suffix.py`. Want me to fix it too?"""

PLAN = """1. **Fix the membership check in `ordinal`.** Change `<` to `in` in `inflection/__init__.py`.
2. **Fix the suffix join in `ordinalize`.** It drops the suffix for 11, 12 and 13.
3. **Run the full suite** with `python -m pytest -q` and report what still fails.

2 files, about 6 lines, no new dependencies."""


def reply_for(messages):
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    text = str(messages[last_user].get("content") or "") if last_user >= 0 else ""
    done = sum(1 for m in messages[last_user + 1:] if m.get("role") == "tool")
    if "implementation plan" in text:
        return {"role": "assistant", "content": PLAN}
    if "say hi" in text.lower():
        return {"role": "assistant", "content": "Hi. I have not looked at anything yet, so this is only me talking."}
    if done < len(STEPS):
        name, args = STEPS[done]
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{done}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}
    return {"role": "assistant", "content": ANSWER}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "demo-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        time.sleep(0.8)  # long enough to see the working state
        message = reply_for(body.get("messages", []))
        if not body.get("stream"):
            return self._send({"choices": [{"message": message}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(delta):
            line = "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"
            self.wfile.write(line.encode())
            self.wfile.flush()

        try:
            if message.get("tool_calls"):
                call = message["tool_calls"][0]
                emit({"tool_calls": [{"index": 0, "id": call["id"], "function": call["function"]}]})
            else:
                words = message["content"].split(" ")
                for i, word in enumerate(words):
                    emit({"content": word + (" " if i < len(words) - 1 else "")})
                    time.sleep(0.03)
            usage = {"prompt_tokens": 900, "completion_tokens": len((message.get("content") or "").split())}
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()
        except OSError:
            pass   # the app stopped listening (Stop was pressed)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"demo model on http://127.0.0.1:{port}/v1")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
