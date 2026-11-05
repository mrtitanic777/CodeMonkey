"""API keys saved in the app, providers added in the app, and the Claude adapter.

Keys are kept in memory here (keystore.use_memory), so nothing touches Windows Credential Manager.
The Claude adapter is driven with a stand-in for the SDK's stream: no network.
"""

import json
import os
import sys
import tempfile
from dataclasses import replace
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keystore  # noqa: E402

keystore.use_memory()

from config import Config, resolve_model_endpoint  # noqa: E402
from test_webui import http, start_server  # noqa: E402


def test_stored_key_is_used_when_the_environment_has_none():
    keystore.use_memory()
    cfg = Config(provider="svc", providers={"svc": {"base_url": "https://x.test/v1", "api_key_env": "CM_TEST_KEY"}})
    os.environ.pop("CM_TEST_KEY", None)
    assert resolve_model_endpoint(cfg)[1] is None
    keystore.set("svc", "sk-stored-0123456789")
    assert resolve_model_endpoint(cfg)[1] == "sk-stored-0123456789"
    os.environ["CM_TEST_KEY"] = "sk-from-env-0123456789"
    try:
        assert resolve_model_endpoint(cfg)[1] == "sk-from-env-0123456789"   # the environment wins
    finally:
        os.environ.pop("CM_TEST_KEY")
    assert keystore.hint("sk-stored-0123456789") == "…6789"
    assert keystore.delete("svc") and keystore.get("svc") is None
    print("PASS test_stored_key_is_used_when_the_environment_has_none")


def test_keys_and_added_servers_through_the_api():
    keystore.use_memory()
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            # a server added in the app, with its key
            status, body = http("POST", base + "/api/providers", {
                "name": "router", "base_url": "https://router.test/v1/", "models": "m-one, m-two",
                "key": "sk-router-0123456789"})
            assert status == 200 and body["models"] == ["m-one", "m-two"], body
            assert body["base_url"] == "https://router.test/v1" and body["key"]["source"] == "stored"
            cfg = http("GET", base + "/api/config")[1]
            p = cfg["providers"]["router"]
            assert p["custom"] and p["key"]["hint"] == "…6789" and "sk-router" not in json.dumps(cfg)
            assert resolve_model_endpoint(replace(app.config, provider="router"))[1] == "sk-router-0123456789"
            saved = open(os.path.join(app.webui_dir, "providers.json"), encoding="utf-8").read()
            assert "router.test" in saved and "sk-router" not in saved   # no secret in the file

            # moving it to another address drops the key: it goes only where it was entered for
            http("POST", base + "/api/providers", {"name": "router", "base_url": "https://elsewhere.test/v1", "models": "m-one"})
            assert keystore.get("router") is None

            # a key for a provider from config.yaml; bad keys and unknown providers are refused
            assert http("POST", base + "/api/providers/fake/key", {"key": "sk-ant-0123456789"})[0] == 200
            assert keystore.get("fake") == "sk-ant-0123456789"
            assert http("POST", base + "/api/providers/fake/key", {"key": "has spaces in it"})[0] == 400
            assert http("POST", base + "/api/providers/nope/key", {"key": "sk-0123456789"})[0] == 404
            assert http("POST", base + "/api/providers", {"name": "fake", "base_url": "https://x.test"})[0] == 400
            assert http("POST", base + "/api/providers", {"name": "Bad Name", "base_url": "https://x.test"})[0] == 400
            assert http("POST", base + "/api/providers", {"name": "ok", "base_url": "file:///etc"})[0] == 400
            assert http("DELETE", base + "/api/providers/fake/key")[0] == 200 and keystore.get("fake") is None
            assert http("DELETE", base + "/api/providers/fake")[0] == 404   # from config.yaml: stays

            # a paired phone may not set keys or servers
            from webui.server import WebHandler

            real = WebHandler._remote, WebHandler._has_key
            WebHandler._remote = lambda self: True
            WebHandler._has_key = lambda self: True   # paired
            try:
                assert http("POST", base + "/api/providers/fake/key", {"key": "sk-ant-0123456789"})[0] == 403
                assert http("POST", base + "/api/providers", {"name": "evil", "base_url": "https://evil.test"})[0] == 403
                assert http("DELETE", base + "/api/providers/router")[0] == 403
            finally:
                WebHandler._remote, WebHandler._has_key = real
            assert keystore.get("fake") is None and "evil" not in app.config.providers

            # removing an added server removes its key too, and it is not loaded again
            http("POST", base + "/api/providers/router/key", {"key": "sk-router-0123456789"})
            assert http("DELETE", base + "/api/providers/router")[0] == 200
            assert keystore.get("router") is None and "router" not in app.config.providers
        finally:
            server.shutdown()
    print("PASS test_keys_and_added_servers_through_the_api")


def test_added_servers_survive_a_restart():
    keystore.use_memory()
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        server.shutdown()
        app.providers.save("local2", "http://127.0.0.1:9999/v1", ["a"], "openai")
        from webui.app import WebApp

        again = WebApp(app.config_path, default_project=tmp)
        assert again.config.providers["local2"]["models"] == ["a"]
        assert again.config_snapshot()["providers"]["local2"]["custom"]
    print("PASS test_added_servers_survive_a_restart")


# ------------------------------------------------------------ Claude adapter ---


def test_conversation_is_translated_for_the_messages_api():
    from anthropic_client import to_anthropic_messages, to_anthropic_tools

    convo = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
        {"role": "assistant", "content": "Reading.", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}},
            {"id": "t2", "type": "function", "function": {"name": "read", "arguments": '{"path": "b.py"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "print(1)"},
        # t2 never got a result; and a result for a call that is not in the history
        {"role": "tool", "tool_call_id": "gone", "content": "old output"},
        {"role": "user", "content": "and now?"},
    ]
    system, msgs = to_anthropic_messages(convo)
    assert system == "Be brief."
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user"]
    assert msgs[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a.py"}}
    results = msgs[2]["content"]
    assert results[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "print(1)"}
    assert results[1]["type"] == "text" and "old output" in results[1]["text"]
    assert results[2]["tool_use_id"] == "t2" and results[2]["type"] == "tool_result"
    assert all("_results" not in m for m in msgs)
    tools = to_anthropic_tools([{"type": "function", "function": {"name": "read", "description": "Read",
                                                                  "parameters": {"type": "object"}}}])
    assert tools == [{"name": "read", "description": "Read", "input_schema": {"type": "object"}}]
    print("PASS test_conversation_is_translated_for_the_messages_api")


class _FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


def _fake_client(final, deltas=("Hel", "lo")):
    sent = []

    def stream(**kwargs):
        sent.append(kwargs)
        events = [NS(type="content_block_delta", delta=NS(type="text_delta", text=t)) for t in deltas]
        return _FakeStream(events, final)

    api = NS(stream=stream)
    return NS(beta=NS(messages=api), messages=api), sent


def test_claude_answers_stream_and_keep_their_thinking_for_the_next_request():
    from anthropic_client import BLOCKS, AnthropicClient

    final = NS(
        content=[NS(type="thinking", thinking="plan", signature="sig1"),
                 NS(type="text", text="Hello"),
                 NS(type="tool_use", id="toolu_9", name="read", input={"path": "x"})],
        stop_reason="tool_use", stop_details=None,
        usage=NS(input_tokens=10, cache_read_input_tokens=90, cache_creation_input_tokens=0, output_tokens=7))
    client = AnthropicClient("sk-ant-test-0123456789", "claude-opus-5-5")
    client._client, sent = _fake_client(final)
    client.effort = "high"
    seen = []
    client.on_delta = seen.append
    reply = client.chat([{"role": "user", "content": "hi"}], [])
    msg = reply["choices"][0]["message"]
    assert seen == ["Hel", "lo"] and msg["content"] == "Hello"
    assert msg["tool_calls"][0]["id"] == "toolu_9" and json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "x"}
    assert reply["choices"][0]["finish_reason"] == "tool_calls"
    req = sent[0]
    assert req["output_config"] == {"effort": "high"}
    assert req["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert "thinking-binding-controls-2026-08-01" in req["betas"] and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["extra_body"]["fallbacks"] == "default" and req["extra_body"]["cache_control"] == {"type": "ephemeral"}
    assert client.usage_total["in"] == 100 and client.usage_total["out"] == 7 and client.usage_total["cache_n"] == 90

    # the next request sends the turn back with its thinking block, as Claude wrote it
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": msg["content"], "tool_calls": msg["tool_calls"]},
               {"role": "tool", "tool_call_id": "toolu_9", "content": "data"}]
    client.chat(history, [])
    blocks = sent[1]["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "plan", "signature": "sig1"}
    assert BLOCKS.recall(history[1]) is not None

    # "auto" leaves the effort to the model; a model without fallbacks gets none
    other = AnthropicClient("sk-ant-test-0123456789", "claude-haiku-5-5")
    other._client, sent2 = _fake_client(final)
    other.chat([{"role": "user", "content": "hi"}], [])
    assert "output_config" not in sent2[0] and "fallbacks" not in sent2[0]["extra_body"]
    print("PASS test_claude_answers_stream_and_keep_their_thinking_for_the_next_request")


def test_a_refusal_runs_no_tools_and_says_so():
    from anthropic_client import AnthropicClient

    final = NS(content=[NS(type="text", text="I can't"), NS(type="tool_use", id="toolu_x", name="bash", input={})],
               stop_reason="refusal", stop_details=NS(category="cyber"),
               usage=NS(input_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=1))
    client = AnthropicClient("sk-ant-test-0123456789", "claude-sonnet-5-5")
    client._client, _ = _fake_client(final, deltas=())
    msg = client.chat([{"role": "user", "content": "x"}], [])["choices"][0]["message"]
    assert msg["tool_calls"] is None and "declined" in msg["content"] and "cyber" in msg["content"]
    print("PASS test_a_refusal_runs_no_tools_and_says_so")


def test_claude_provider_picks_the_adapter_and_needs_a_key():
    from anthropic_client import AnthropicClient
    from kessie_client import client_from_config

    keystore.use_memory()
    os.environ.pop("CM_CLAUDE_KEY", None)
    cfg = Config(provider="claude", model="claude-opus-5-5",
                 providers={"claude": {"base_url": "https://api.anthropic.com", "api_key_env": "CM_CLAUDE_KEY",
                                       "client": "anthropic"}})
    try:
        client_from_config(cfg)
        raise AssertionError("a Claude provider without a key must say so")
    except RuntimeError as exc:
        assert "Settings > Providers" in str(exc)
    keystore.set("claude", "sk-ant-test-0123456789")
    assert isinstance(client_from_config(cfg), AnthropicClient)
    print("PASS test_claude_provider_picks_the_adapter_and_needs_a_key")


def test_claude_works_without_the_anthropic_package():
    # the installers leave the package out: Claude is then reached through its OpenAI-compatible endpoint
    import anthropic_client
    from agent import OpenAICompatClient
    from kessie_client import client_from_config

    keystore.use_memory()
    keystore.set("claude", "sk-ant-test-0123456789")
    cfg = Config(provider="claude", model="claude-sonnet-5-5",
                 providers={"claude": {"base_url": "https://api.anthropic.com", "api_key_env": "CM_CLAUDE_KEY",
                                       "client": "anthropic"}})
    real = anthropic_client.sdk_available
    anthropic_client.sdk_available = lambda: False
    try:
        client = client_from_config(cfg)
    finally:
        anthropic_client.sdk_available = real
    assert isinstance(client, OpenAICompatClient), client
    assert client.base_url == "https://api.anthropic.com/v1" and client.model == "claude-sonnet-5-5"
    assert anthropic_client.compat_base_url("https://proxy.test/v1/") == "https://proxy.test/v1"
    print("PASS test_claude_works_without_the_anthropic_package")


def test_terminal_and_headless_runs_see_providers_added_in_the_app():
    from config import app_default_model, merge_app_providers

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "webui"))
        with open(os.path.join(tmp, "webui", "providers.json"), "w") as fh:
            json.dump({"mine": {"base_url": "https://x.example/v1", "models": ["m1"], "client": "evil"},
                       "local": {"base_url": "http://elsewhere/v1", "models": ["z"]}}, fh)
        with open(os.path.join(tmp, "webui", "settings.json"), "w") as fh:
            json.dump({"default_provider": "mine", "default_model": "m1"}, fh)
        cfg = merge_app_providers(Config(workspace=tmp, providers={"local": {"base_url": "http://localhost:1/v1"}}))
        assert cfg.providers["mine"] == {"base_url": "https://x.example/v1", "api_key_env": "", "client": "openai",
                                         "models": ["m1"], "vision": False}
        assert cfg.providers["local"]["base_url"] == "http://localhost:1/v1"     # config.yaml wins
        assert app_default_model(cfg) == "mine/m1"
        assert app_default_model(Config(workspace=tmp)) == ""                    # an unknown provider is ignored
    print("PASS test_terminal_and_headless_runs_see_providers_added_in_the_app")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
