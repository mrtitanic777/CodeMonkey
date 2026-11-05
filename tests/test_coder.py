"""Tests for the coding harness (stdlib only).

Run from the project root:
    python tests/test_coder.py

Uses a FAKE model client, so no API key or network is needed. Every test is
a plain assert; any failure raises and the script exits non-zero.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coder import (  # noqa: E402
    CoderLoop,
    SessionManager,
    TodoState,
    coder_tools,
    expand_command,
    find_command,
    load_commands,
    load_project_context,
)
from coder.mcp import MCPClient, load_mcp_tools  # noqa: E402
from config import Config  # noqa: E402
from tools import ToolRegistry  # noqa: E402

GIT = shutil.which("git")


# ------------------------------------------------------- fake client ---


class FakeClient:
    """Scripted stand-in for a model: pops one response per chat() call."""

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, tools):
        assert self.script, "FakeClient ran out of scripted responses"
        return self.script.pop(0)


def tool_call_msg(name, args, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


def text_msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def make_config(tmp):
    return Config(
        workspace=tmp,
        agent_name="TestCoder",
        max_iterations=6,
        skills_dir=os.path.join(ROOT, "skills"),
    )


def make_registry(project_dir, auto_approve=True, tracker=None, **kw):
    reg = ToolRegistry(auto_approve=auto_approve)
    for tool in coder_tools(project_dir, auto_approve=auto_approve,
                            tracker=tracker, **kw):
        reg.register(tool)
    return reg


def write_files(project_dir, files):
    for rel, content in files.items():
        abs_path = os.path.join(project_dir, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(content)


# ------------------------------------------------------------- tests ---


def test_coder_edit_roundtrip_with_diff():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {"a.py": "x = 1\nprint(x)\n"})
        reg = make_registry(tmp)
        out = reg.execute(
            "edit",
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        )
        assert out.startswith("Edited"), out
        # unified diff preview must be present in the result
        assert "---" in out and "+++" in out, out
        assert "-x = 1" in out and "+x = 2" in out, out
        with open(os.path.join(tmp, "a.py")) as fh:
            assert fh.read() == "x = 2\nprint(x)\n"
        # old_text missing -> ERROR, file untouched
        out = reg.execute(
            "edit",
            {"path": "a.py", "old_text": "nope", "new_text": "x"},
        )
        assert out.startswith("ERROR:"), out
    print("PASS test_coder_edit_roundtrip_with_diff")


def test_coder_read_grep_glob():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {
            "src/main.py": "def hello():\n    return 'hi'\n",
            "src/util.py": "# nothing\n",
            "README.md": "hello project\n",
            ".git/objects/pack": "hello in git\n",
        })
        reg = make_registry(tmp)
        out = reg.execute("read", {"path": "src/main.py"})
        assert "1\tdef hello():" in out, out
        out = reg.execute("read", {"path": "src/main.py", "offset": 2, "limit": 1})
        assert out.strip() == "2\t    return 'hi'", out
        out = reg.execute("glob", {"pattern": "**/*.py"})
        assert "src/main.py" in out and "src/util.py" in out, out
        out = reg.execute("grep", {"pattern": "hello"})
        assert "src/main.py" in out and "README.md" in out, out
        # .git must be skipped
        assert ".git" not in out, out
        # gitignore honored
        write_files(tmp, {".gitignore": "*.log\n"})
        write_files(tmp, {"debug.log": "hello log\n"})
        out = reg.execute("grep", {"pattern": "hello"})
        assert "debug.log" not in out, out
    print("PASS test_coder_read_grep_glob")


def test_coder_sandbox_escape():
    with tempfile.TemporaryDirectory() as tmp:
        reg = make_registry(tmp)
        for name, args in [
            ("read", {"path": "../../etc/passwd"}),
            ("edit", {"path": "../evil.py", "old_text": "a", "new_text": "b"}),
            ("write_file", {"path": "../../tmp/x.txt", "content": "x"}),
        ]:
            out = reg.execute(name, args)
            assert "escapes the project" in out, (name, out)
    print("PASS test_coder_sandbox_escape")


def test_coder_bash():
    with tempfile.TemporaryDirectory() as tmp:
        reg = make_registry(tmp)
        # the command runs in the project folder (asked of Python, so it reads the same in any shell)
        out = reg.execute("bash", {"cmd": 'python -c "import os; print(os.path.realpath(os.getcwd()))"'})
        assert os.path.realpath(tmp).lower() in out.lower(), out
        out = reg.execute("bash", {"cmd": "echo hello"})
        assert "[exit 0]" in out and "hello" in out, out
        out = reg.execute("bash", {"cmd": "exit 3"})
        assert "[exit 3]" in out, out
    print("PASS test_coder_bash")


def test_slash_commands():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {"commands/greet.md": (
            "---\nname: greet\ndescription: Say hi.\n---\n"
            "Hello, $ARGUMENTS! Welcome."
        )})
        cmds = load_commands(os.path.join(tmp, "commands"))
        assert len(cmds) == 1, cmds
        cmd = find_command(cmds, "greet")
        assert cmd is not None and cmd.description == "Say hi."
        expanded = expand_command(cmd, "Ada")
        assert expanded == "Hello, Ada! Welcome.", expanded
        assert find_command(cmds, "missing") is None
        # missing dir -> empty list, no crash
        assert load_commands(os.path.join(tmp, "nope")) == []
    print("PASS test_slash_commands")


def test_plan_mode_is_read_only():
    with tempfile.TemporaryDirectory() as tmp:
        reg = make_registry(tmp, plan_mode=True)
        names = set(reg.names())
        assert {"read", "glob", "grep", "todo_write", "todo_list"} <= names, names
        assert "edit" not in names, names
        assert "write_file" not in names, names
        assert "bash" not in names, names
        # the loop itself must not smuggle write tools back in
        loop = CoderLoop(
            config=make_config(tmp), project_dir=tmp, plan_mode=True,
            registry=reg, client=FakeClient([text_msg("plan")]),
        )
        assert loop.registry.get("edit") is None
        assert loop.registry.get("delegate") is None, "delegate could write"
        assert loop.registry.get("read") is not None
    print("PASS test_plan_mode_is_read_only")


def test_project_context_loading():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {
            "AGENTS.md": "# Rules\nAlways run tests.\n",
            "notes.txt": "ignored\n",
        })
        ctx = load_project_context(tmp, ["AGENTS.md", "CLAUDE.md", ".coderules"])
        assert "AGENTS.md" in ctx and "Always run tests." in ctx, ctx
        assert "notes.txt" not in ctx
        assert load_project_context(tmp, ["MISSING.md"]) == ""
    print("PASS test_project_context_loading")


def test_todo_tools():
    state = TodoState()
    reg = ToolRegistry(auto_approve=True)
    for tool in coder_tools(".", todo_state=state):
        if tool.name in ("todo_write", "todo_list"):
            reg.register(tool)
    out = reg.execute("todo_write", {"todos": [
        {"title": "Explore", "status": "completed"},
        {"title": "Implement", "status": "in_progress"},
        {"title": "Test", "status": "pending"},
    ]})
    assert "[x] 1. Explore" in out, out
    assert "[>] 2. Implement" in out, out
    assert "[ ] 3. Test" in out, out
    out = reg.execute("todo_list", {})
    assert "Implement" in out, out
    # bad status -> ERROR
    out = reg.execute("todo_write", {"todos": [{"title": "x", "status": "bogus"}]})
    assert out.startswith("ERROR:"), out
    # persistence roundtrip
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "todos.json")
        s1 = TodoState(save_path=path)
        s1.write([{"title": "remembered", "status": "pending"}])
        s2 = TodoState(save_path=path)
        assert any(i["title"] == "remembered" for i in s2.items), s2.items
    print("PASS test_todo_tools")


def test_checkpoint_rewind():
    if GIT is None:
        print("SKIP test_checkpoint_rewind (git not available)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([GIT, "init", "-q"], cwd=tmp, check=True)
        subprocess.run([GIT, "config", "user.email", "t@t"], cwd=tmp, check=True)
        subprocess.run([GIT, "config", "user.name", "t"], cwd=tmp, check=True)
        write_files(tmp, {"a.txt": "original\n"})
        subprocess.run([GIT, "add", "."], cwd=tmp, check=True)
        subprocess.run([GIT, "commit", "-qm", "init"], cwd=tmp, check=True)

        sessions_root = os.path.join(tmp, "sessions")
        session = SessionManager(tmp, sessions_root)
        reg = make_registry(tmp, tracker=session)

        # agent edits a tracked file and creates a new one
        out = reg.execute("edit", {"path": "a.txt", "old_text": "original",
                                   "new_text": "changed"})
        assert out.startswith("Edited"), out
        out = reg.execute("write_file", {"path": "new.txt", "content": "new"})
        assert out.startswith("Wrote"), out
        with open(os.path.join(tmp, "a.txt")) as fh:
            assert fh.read() == "changed\n"

        report = session.rewind()
        assert "Rewound" in report, report
        with open(os.path.join(tmp, "a.txt")) as fh:
            assert fh.read() == "original\n", "edited file must be restored"
        assert not os.path.exists(os.path.join(tmp, "new.txt")), \
            "agent-created file must be deleted"
        # second rewind is a no-op
        assert "Nothing to rewind" in session.rewind()
    print("PASS test_checkpoint_rewind")


def test_rewind_degrades_without_git():
    with tempfile.TemporaryDirectory() as tmp:
        # no git init here
        sessions_root = os.path.join(tmp, "sessions")
        session = SessionManager(tmp, sessions_root)
        assert not session.is_git_repo()
        reg = make_registry(tmp, tracker=session)
        reg.execute("write_file", {"path": "new.txt", "content": "x"})
        reg.execute("edit", {"path": "new.txt", "old_text": "x", "new_text": "y"})
        report = session.rewind()
        # new file deleted; the edit can't be restored without git
        assert not os.path.exists(os.path.join(tmp, "new.txt"))
        assert "Could NOT restore" in report or "Rewound" in report, report
    print("PASS test_rewind_degrades_without_git")


def test_mcp_fake_server_roundtrip():
    server = os.path.join(ROOT, "tests", "fake_mcp_server.py")
    client = MCPClient("fake", sys.executable, [server])
    try:
        client.connect()
        tools = client.list_tools()
        assert len(tools) == 1 and tools[0]["name"] == "echo", tools
        out = client.call_tool("echo", {"text": "ping"})
        assert out == "echo:ping", out
    finally:
        client.close()
    print("PASS test_mcp_fake_server_roundtrip")


def test_mcp_tool_registration():
    with tempfile.TemporaryDirectory() as tmp:
        server = os.path.join(ROOT, "tests", "fake_mcp_server.py")
        cfg_path = os.path.join(tmp, "mcp.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"servers": {"fake": {
                "command": sys.executable, "args": [server]}}}, fh)
        tools, clients = load_mcp_tools(cfg_path)
        try:
            assert len(tools) == 1, [t.name for t in tools]
            assert tools[0].name == "mcp__fake__echo", tools[0].name
            assert tools[0].needs_approval, "MCP tools must be gated"
            reg = ToolRegistry(auto_approve=True)
            reg.register(tools[0])
            out = reg.execute("mcp__fake__echo", {"text": "hi"})
            assert out == "echo:hi", out
        finally:
            for client in clients:
                client.close()
        # missing config -> empty, no crash
        tools, clients = load_mcp_tools(os.path.join(tmp, "nope.json"))
        assert tools == [] and clients == []
    print("PASS test_mcp_tool_registration")


def test_coder_loop_tool_call_and_plan_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {"AGENTS.md": "Be careful.\n", "main.py": "x=1\n"})
        reg = make_registry(tmp)
        client = FakeClient([
            tool_call_msg("read", {"path": "main.py"}),
            text_msg("Done reading."),
        ])
        loop = CoderLoop(
            config=make_config(tmp), project_dir=tmp, registry=reg,
            client=client,
        )
        reply = loop.run("look at main.py")
        assert reply == "Done reading.", reply
        prompt = loop.build_system_prompt("look at main.py")
        assert "Be careful." in prompt, "project context must be injected"
        assert str(tmp) in prompt
        # plan mode prompt mentions read-only
        plan_loop = CoderLoop(
            config=make_config(tmp), project_dir=tmp, plan_mode=True,
            client=FakeClient([text_msg("p")]),
        )
        plan_prompt = plan_loop.build_system_prompt("plan this")
        assert "PLAN MODE" in plan_prompt, plan_prompt
    print("PASS test_coder_loop_tool_call_and_plan_prompt")


def test_new_coder_skills_load():
    from skills import load_skills, skill_search  # noqa: E402
    skills = load_skills(os.path.join(ROOT, "skills"))
    names = {s.name for s in skills}
    assert {"code-review", "commit"} <= names, names
    hits = skill_search("please review this pull request", skills)
    assert hits and hits[0].name == "code-review", [s.name for s in hits]
    hits = skill_search("help me write a git commit message", skills)
    assert hits and hits[0].name == "commit", [s.name for s in hits]
    print("PASS test_new_coder_skills_load")


def test_edit_fuzzy_fallback():
    # Aider-style: SEARCH block differs in whitespace/indentation.
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {"a.py": "def f():\n    x = 1\n    return x\n"})
        reg = make_registry(tmp)
        # "x=1" is not an exact substring of "    x = 1" -> fuzzy path
        out = reg.execute(
            "edit",
            {"path": "a.py", "old_text": "x=1", "new_text": "x=2"},
        )
        assert out.startswith("Edited"), out
        assert "fuzzy" in out.lower(), out
        with open(os.path.join(tmp, "a.py")) as fh:
            body = fh.read()
        # indentation of the file is preserved; the model's replacement
        # text is used verbatim otherwise ("x=2", not "x = 2")
        assert body == "def f():\n    x=2\n    return x\n", body

        # multi-line block with trailing-whitespace differences
        write_files(tmp, {"b.py": "a = 1   \nb = 2\n"})
        out = reg.execute(
            "edit",
            {"path": "b.py", "old_text": "a = 1\nb = 2", "new_text": "a = 10\nb = 20"},
        )
        assert out.startswith("Edited"), out
        with open(os.path.join(tmp, "b.py")) as fh:
            assert fh.read() == "a = 10\nb = 20\n"
    print("PASS test_edit_fuzzy_fallback")


def test_edit_no_match_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        write_files(tmp, {"a.py": "x = 1\n"})
        reg = make_registry(tmp)
        out = reg.execute(
            "edit",
            {"path": "a.py", "old_text": "completely unrelated zebra",
             "new_text": "y = 2"},
        )
        assert out.startswith("ERROR:"), out
        assert "fuzzy" in out.lower(), out  # reports that fuzzy was tried
        with open(os.path.join(tmp, "a.py")) as fh:
            assert fh.read() == "x = 1\n", "file must be untouched"
    print("PASS test_edit_no_match_at_all")


def test_provider_config_resolution():
    from config import resolve_model_endpoint  # noqa: E402
    from agent import OpenAICompatClient  # noqa: E402

    cfg = Config(
        workspace=tempfile.mkdtemp(),
        provider="ollama",
        providers={
            "ollama": {"base_url": "http://localhost:11434/v1",
                       "api_key_env": ""},
            "cloud": {"base_url": "https://api.example.com/v1",
                      "api_key_env": "TEST_CLOUD_KEY"},
        },
        model="mymodel",
    )
    # keyless local provider -> api_key None, no key required
    base_url, api_key, model = resolve_model_endpoint(cfg)
    assert base_url == "http://localhost:11434/v1", base_url
    assert api_key is None, api_key
    assert model == "mymodel"
    client = OpenAICompatClient.from_config(cfg)
    assert client.api_key is None
    assert client.base_url == "http://localhost:11434/v1"

    # provider naming an env var -> key read from it
    cfg2 = Config(workspace=cfg.workspace, provider="cloud",
                  providers=cfg.providers, model="m")
    os.environ["TEST_CLOUD_KEY"] = "sk-test-123"
    try:
        base_url, api_key, _ = resolve_model_endpoint(cfg2)
        assert base_url == "https://api.example.com/v1", base_url
        assert api_key == "sk-test-123", api_key
    finally:
        del os.environ["TEST_CLOUD_KEY"]

    # unknown provider -> falls back to top-level base_url + default key
    cfg3 = Config(workspace=cfg.workspace, provider="nope",
                  base_url="http://fallback:1/v1")
    old = {k: os.environ.pop(k, None) for k in ("CODEMONKEY_API_KEY", "PERSONAL_AGENT_API_KEY")}
    try:
        try:
            resolve_model_endpoint(cfg3)
            raise AssertionError("expected RuntimeError for missing key")
        except RuntimeError as exc:
            assert "CODEMONKEY_API_KEY" in str(exc), exc
        os.environ["PERSONAL_AGENT_API_KEY"] = "sk-old-name"   # the old name still works
        assert resolve_model_endpoint(cfg3)[1] == "sk-old-name"
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    print("PASS test_provider_config_resolution")


def test_skill_use_when_descriptions():
    from skills import load_skills, skill_search  # noqa: E402
    skills = load_skills(os.path.join(ROOT, "skills"))
    for skill in skills:
        assert skill.description.lower().startswith("use when"), (
            skill.name, skill.description,
        )
    # routing still works through the "use when" phrasing
    hits = skill_search("use when I want to save a quick note", skills)
    assert hits and hits[0].name == "notes", [s.name for s in hits]
    print("PASS test_skill_use_when_descriptions")



def main():
    tests = [
        test_coder_edit_roundtrip_with_diff,
        test_coder_read_grep_glob,
        test_coder_sandbox_escape,
        test_coder_bash,
        test_slash_commands,
        test_plan_mode_is_read_only,
        test_project_context_loading,
        test_todo_tools,
        test_checkpoint_rewind,
        test_rewind_degrades_without_git,
        test_mcp_fake_server_roundtrip,
        test_mcp_tool_registration,
        test_coder_loop_tool_call_and_plan_prompt,
        test_new_coder_skills_load,
        test_edit_fuzzy_fallback,
        test_edit_no_match_at_all,
        test_provider_config_resolution,
        test_skill_use_when_descriptions,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
