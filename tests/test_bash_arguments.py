"""A tool call that leaves out an optional argument still runs.

The web UI replaces bash's function with a closure (webui/app.py) whose signature is
``run_bash(cmd, timeout=60, description="")``. The argument filter kept only the names
the model actually sent, so a call carrying just ``timeout`` lost ``cmd`` and failed as:

    ERROR: bad arguments for tool 'bash': ... run_bash() missing 1 required positional
    argument: 'cmd'

Six of those are recorded in agent-workspace/webui/codemonkey.db. Run: python tests/test_bash_arguments.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import Tool, ToolRegistry  # noqa: E402


def run_bash(cmd: str, timeout: int = 60, description: str = "") -> str:
    return f"{cmd}|{timeout}|{description}"


def _registry() -> ToolRegistry:
    reg = ToolRegistry(auto_approve=True)
    reg.register(Tool(name="bash", description="run a command",
                      parameters={"type": "object",
                                  "properties": {"cmd": {"type": "string"},
                                                 "timeout": {"type": "integer"},
                                                 "description": {"type": "string"}},
                                  "required": ["cmd"]},
                      func=run_bash))
    return reg


def test_an_optional_argument_left_out_uses_its_default():
    reg = _registry()
    # The shape that broke: an optional argument sent without cmd reaching the call.
    got = reg.execute("bash", {"cmd": "ls", "timeout": 5})
    assert got == "ls|5|", got
    # An omitted optional argument now arrives as its own default instead of vanishing.
    got = reg.execute("bash", {"cmd": "ls"})
    assert got == "ls|60|", got


def test_a_missing_required_argument_is_still_reported():
    reg = _registry()
    # Nothing for cmd: its absence is reported, and no default is invented for it.
    got = reg.execute("bash", {"timeout": 5})
    assert "bad arguments" in got and "cmd" in got, got

    # A required argument with no default stays missing: that is the model's error to fix.
    def needs(cmd: str) -> str:
        return cmd

    reg.register(Tool(name="needs", description="d", parameters={"type": "object",
                                                                 "properties": {"cmd": {"type": "string"}}},
                      func=needs))
    got = reg.execute("needs", {})
    assert "bad arguments" in got, got


def test_a_name_the_tool_does_not_take_is_reported_not_passed_on():
    reg = _registry()
    got = reg.execute("bash", {"cmd": "ls", "workdir": "/tmp"})
    assert got.startswith("ls|60|") and "ignored arguments" in got, got


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok: a left-out optional argument uses its default; a required one still reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
