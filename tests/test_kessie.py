"""Tests for the KeSSie backend adapter (kessie_client.py).

Uses a tiny fake KeSSie HTTP server (stdlib http.server in a thread) that
records requests and replays canned responses. No GPU, no real server.
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import AgentLoop  # noqa: E402
from config import Config  # noqa: E402
from kessie_client import (  # noqa: E402
    KeSSieClient,
    client_from_config,
    parse_kessie_tool_calls,
    trim_history,
)
from tools import Tool, ToolRegistry  # noqa: E402


class FakeKeSSieHandler(BaseHTTPRequestHandler):
    """Records the last payload; replays queued responses."""

    received = []
    script = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        FakeKeSSieHandler.received.append(payload)
        body = FakeKeSSieHandler.script.pop(0)
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_server():
    server = HTTPServer(("127.0.0.1", 0), FakeKeSSieHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def kessie_msg(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def make_config(tmp, provider_entry=None):
    providers = {
        "default": {"base_url": "http://localhost:11434/v1", "api_key_env": ""}
    }
    provider = "default"
    if provider_entry is not None:
        providers["kessie"] = provider_entry
        provider = "kessie"
    return Config(
        workspace=tmp,
        agent_name="TestAgent",
        max_iterations=6,
        skills_dir=os.path.join(ROOT, "skills"),
        provider=provider,
        providers=providers,
    )


def test_parse_well_formed_block():
    text = 'Thinking...\n<tool_call>\n{"name": "read", "arguments": {"path": "a.py"}}\n</tool_call>\nDone.'
    clean, calls = parse_kessie_tool_calls(text)
    assert len(calls) == 1, calls
    assert calls[0]["function"]["name"] == "read", calls
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}
    assert "<tool_call>" not in clean and "a.py" not in clean, clean
    assert "Thinking" in clean and "Done" in clean, clean
    print("PASS test_parse_well_formed_block")


def test_parse_multiple_and_unterminated():
    text = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call> mid '
        '<tool_call>{"name": "b", "arguments": {"x": 1}}'
    )
    clean, calls = parse_kessie_tool_calls(text)
    assert [c["function"]["name"] for c in calls] == ["a", "b"], calls
    assert "mid" in clean, clean
    print("PASS test_parse_multiple_and_unterminated")


def test_parse_args_as_string_and_malformed_dropped():
    text = (
        '<tool_call>{"name": "s", "arguments": "{\\"k\\": \\"v\\"}"}</tool_call>'
        '<tool_call>not json at all</tool_call>'
        "plain text"
    )
    clean, calls = parse_kessie_tool_calls(text)
    assert len(calls) == 1, calls
    assert json.loads(calls[0]["function"]["arguments"]) == {"k": "v"}
    assert "not json" not in clean and "plain text" in clean, clean
    print("PASS test_parse_args_as_string_and_malformed_dropped")


def test_parse_no_blocks():
    clean, calls = parse_kessie_tool_calls("Just a normal reply.")
    assert calls == [] and clean == "Just a normal reply.", (clean, calls)
    print("PASS test_parse_no_blocks")


def test_trim_history_keeps_system_and_recent():
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(10):
        msgs.append({"role": "user", "content": "x" * 100})
    trimmed = trim_history(msgs, 350)
    assert trimmed[0]["content"] == "SYS", trimmed
    # system (3) + at least the newest message must survive
    assert trimmed[-1]["content"] == "x" * 100, trimmed
    total = sum(len(m["content"]) for m in trimmed)
    assert total <= 350 + 100, total  # newest may overflow the budget alone
    assert len(trimmed) < len(msgs), trimmed
    print("PASS test_trim_history_keeps_system_and_recent")


def test_conversation_id_and_tools_sent():
    server = start_server()
    try:
        FakeKeSSieHandler.received = []
        FakeKeSSieHandler.script = [kessie_msg("hello")]
        client = KeSSieClient(
            f"http://127.0.0.1:{server.server_port}/v1",
            "",
            "test-model",
            conversation_id="sess-123",
        )
        tools = [{"type": "function", "function": {"name": "echo"}}]
        resp = client.chat([{"role": "user", "content": "hi"}], tools)
        payload = FakeKeSSieHandler.received[0]
        assert payload["conversation_id"] == "sess-123", payload
        assert payload["tools"] == tools, payload
        assert payload["model"] == "test-model", payload
        assert resp["choices"][0]["message"]["content"] == "hello", resp
        assert resp["choices"][0]["message"]["tool_calls"] == [], resp
    finally:
        server.shutdown()
    print("PASS test_conversation_id_and_tools_sent")


def test_client_from_config_selection():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_config(
            tmp,
            {
                "base_url": "http://localhost:8200/v1",
                "api_key_env": "",
                "client": "kessie",
            },
        )
        client = client_from_config(cfg)
        assert isinstance(client, KeSSieClient), type(client)
        assert client.base_url == "http://localhost:8200/v1", client.base_url

        from agent import OpenAICompatClient

        cfg2 = make_config(tmp)
        client2 = client_from_config(cfg2)
        assert isinstance(client2, OpenAICompatClient), type(client2)
    print("PASS test_client_from_config_selection")


def test_end_to_end_tool_loop_over_http():
    server = start_server()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            received = {}

            def echo(text):
                received["text"] = text
                return f"echo:{text}"

            reg = ToolRegistry(auto_approve=True)
            reg.register(
                Tool(
                    name="echo",
                    description="Echoes text.",
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                    func=echo,
                )
            )
            FakeKeSSieHandler.received = []
            FakeKeSSieHandler.script = [
                kessie_msg(
                    'On it.\n<tool_call>{"name": "echo", '
                    '"arguments": {"text": "via-kessie"}}</tool_call>'
                ),
                kessie_msg("All done!"),
            ]
            cfg = make_config(
                tmp,
                {
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key_env": "",
                    "client": "kessie",
                    "kessie": {"max_history_chars": 50000},
                },
            )
            agent = AgentLoop(
                config=cfg, registry=reg, client=None, skills=[]
            )
            reply = agent.run("please test the echo tool")
            assert reply == "All done!", reply
            assert received["text"] == "via-kessie", received
            # second request must carry the tool result back as a user message
            second = FakeKeSSieHandler.received[1]
            contents = [m["content"] for m in second["messages"]]
            assert any("echo:via-kessie" in c for c in contents), contents
            assert all(m["role"] != "tool" for m in second["messages"]), second
            assert second["conversation_id"], second
    finally:
        server.shutdown()
    print("PASS test_end_to_end_tool_loop_over_http")


if __name__ == "__main__":
    test_parse_well_formed_block()
    test_parse_multiple_and_unterminated()
    test_parse_args_as_string_and_malformed_dropped()
    test_parse_no_blocks()
    test_trim_history_keeps_system_and_recent()
    test_conversation_id_and_tools_sent()
    test_client_from_config_selection()
    test_end_to_end_tool_loop_over_http()
    print("\n8/8 KeSSie adapter tests passed.")
