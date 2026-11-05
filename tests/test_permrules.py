"""Permission rules: the pattern language, and how the web session applies them."""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coder.permrules import check, decide, merged, project_rules  # noqa: E402

RULES = {
    "allow": ["bash(npm test:*)", "bash(git status)", "read(src/**)", "web_fetch(domain:github.com)", "mcp__gh__*"],
    "ask": ["bash(git push:*)"],
    "deny": ["bash(rm -rf*)", "read(**/.env)", "edit(secrets/**)"],
}


def test_patterns():
    p = "C:/proj"
    d = lambda tool, **args: decide(RULES, tool, args, p)[0]  # noqa: E731
    assert d("bash", cmd="npm test -- -x") == "allow"
    assert d("bash", cmd="npm testing") is None                      # a prefix rule is per word
    assert d("bash", cmd="npm test && rm -rf /") == "deny"           # any denied part denies the chain
    assert d("bash", cmd="git status && npm test") == "allow"        # every part allowed
    assert d("bash", cmd="git status && curl x") is None             # one part not allowed
    assert d("bash", cmd="git push origin main") == "ask"
    assert d("read", path=".env") == "deny" and d("read", path="a/b/.env") == "deny"
    assert d("read", path="src/x/y.py") == "allow"
    if os.name == "nt":   # a Windows path is only a path on Windows
        assert d("read", path="C:/proj/src/z.py") == "allow"
    assert d("read", path="docs/a.md") is None
    assert d("edit", path="secrets/k.txt") == "deny"
    assert d("web_fetch", url="https://api.github.com/x") == "allow"
    assert d("web_fetch", url="https://evilgithub.com") is None      # a domain, not a suffix of letters
    assert d("mcp__gh__list") == "allow" and d("mcp__other__x") is None
    assert check("bash(") and check("bash()") and not check("read(**)") and not check("mcp__*")
    print("PASS test_patterns")


def test_project_rules_and_the_session():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".codemonkey"))
        with open(os.path.join(tmp, ".codemonkey", "settings.json"), "w") as fh:
            json.dump({"permissions": {"deny": ["read(**/*.key)", "not a rule ((("]}}, fh)
        with open(os.path.join(tmp, ".codemonkey", "settings.local.json"), "w") as fh:
            json.dump({"permissions": {"allow": ["bash(make:*)"]}}, fh)
        assert project_rules(tmp) == {"deny": ["read(**/*.key)"], "ask": [], "allow": ["bash(make:*)"]}
        assert merged({"deny": ["a"]}, {"deny": ["a", "b"]})["deny"] == ["a", "b"]

        app, server, base = start_server(tmp)
        try:
            app.update_settings({"permission_rules": {"deny": ["bash(rm *)"], "ask": ["read(notes/**)"], "allow": ["bash(echo:*)"]}})
            session = app.create_session(tmp)
            with open(os.path.join(tmp, "a.key"), "w") as fh:
                fh.write("secret")
            reg = session.loop.registry
            # deny reaches even a tool that never asks, from the project's own file
            assert "blocked by the permission rule read(**/*.key)" in reg.execute("read", {"path": "a.key"})
            # deny refuses without asking, in Auto too
            session.settings["permission_mode"] = "auto"
            assert session.gate("bash", {"cmd": "rm x"}, "") == (False, "blocked by the permission rule bash(rm *)")
            # allow runs without asking, in Ask mode
            session.settings["permission_mode"] = "ask"
            assert session.gate("bash", {"cmd": "echo hi"}, "") == (True, None)
            assert session.gate("bash", {"cmd": "make all"}, "") == (True, None)   # the project's local rule
            # ask asks, even in Auto: the request carries the rule
            session.settings["permission_mode"] = "auto"
            seen = []
            import threading

            def answer():
                for _ in range(100):
                    req = session.gate.current
                    if req:
                        seen.append(req)
                        session.gate.resolve(req["request_id"], "deny")
                        return
                    threading.Event().wait(0.05)
            threading.Thread(target=answer, daemon=True).start()
            os.makedirs(os.path.join(tmp, "notes"), exist_ok=True)
            with open(os.path.join(tmp, "notes", "n.txt"), "w") as fh:
                fh.write("x")
            out = reg.execute("read", {"path": "notes/n.txt"})
            assert seen and seen[0]["rule"] == "read(notes/**)" and out.startswith("ERROR: the user denied"), (seen, out)
            # the tester in Settings
            r = app.check_permission("read", "x/y.key", tmp)
            assert r["decision"] == "deny" and r["from_project"] is True
            assert app.check_permission("bash", "echo 1", tmp)["decision"] == "allow"
            session.close()
        finally:
            server.shutdown()
    print("PASS test_project_rules_and_the_session")


if __name__ == "__main__":
    test_patterns()
    test_project_rules_and_the_session()
