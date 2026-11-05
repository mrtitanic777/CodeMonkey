"""Plugins: install from a folder, a zip or a git repository, switched off until switched on, and
everything they bring arriving (and leaving) with them."""

import json
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import keystore  # noqa: E402

keystore.use_memory()

from coder.plugins import PluginError, PluginManager  # noqa: E402


def _make_plugin(base, name="demo"):
    p = os.path.join(base, name)
    for d in ("skills/greet", "commands", "agents", "styles"):
        os.makedirs(os.path.join(p, d))
    files = {
        "plugin.json": json.dumps({"name": name, "version": "1.2.0", "description": "A demo."}),
        "skills/greet/SKILL.md": "---\nname: greet\ndescription: Use when greeting someone warmly.\n---\nSay hello.",
        "commands/hello.md": "---\nname: hello\ndescription: Say hello\n---\nSay hello to $ARGUMENTS.",
        "agents/helper.md": "---\nname: demo-helper\ndescription: Helps.\ntools: read\n---\nHelp out.",
        "styles/shout.md": "---\nname: shout\ndescription: Loud.\n---\nWRITE IN CAPITALS.",
        "hooks.yaml": "hooks:\n  - event: turn_end\n    command: echo done\n",
        "mcp.json": json.dumps({"servers": {"full": {"command": sys.executable,
                                                     "args": [os.path.join(TESTS, "fake_mcp_full.py")]}}}),
    }
    for rel, body in files.items():
        with open(os.path.join(p, rel), "w") as fh:
            fh.write(body)
    return p


def test_install_from_folder_zip_and_git():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_plugin(os.path.join(tmp, "src"))
        pm = PluginManager(os.path.join(tmp, "plugins"))
        s = pm.install(src)
        assert s["name"] == "demo" and s["enabled"] is False and s["commands"] == ["demo:hello"]
        assert s["skills"] == ["greet"] and s["agents"] == ["helper"] and s["hooks"][0]["command"] == "echo done"
        assert s["mcp"][0]["name"] == "demo-full"
        assert pm.dirs("commands") == [] and pm.mcp_servers() == {}          # nothing until switched on
        try:
            pm.install(src)
            raise AssertionError("a second copy must be refused")
        except PluginError as exc:
            assert "installed already" in str(exc)
        pm.set_enabled("demo", True)
        assert pm.dirs("agents")[0][1] == "plugin:demo" and "demo-full" in pm.mcp_servers()
        assert pm.mcp_servers()["demo-full"]["cwd"].endswith("demo")
        pm.update("demo")
        assert pm.summary("demo")["enabled"] is True                         # an update keeps it on
        pm.remove("demo")
        assert pm.listing() == []

        zpath = os.path.join(tmp, "demo.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            for here, _d, names in os.walk(src):
                for n in names:
                    full = os.path.join(here, n)
                    zf.write(full, os.path.join("demo-main", os.path.relpath(full, src)))
        assert pm.install(zpath)["name"] == "demo"                           # a zip with one folder inside
        pm.remove("demo")
        bad = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "x")
        try:
            pm.install(bad)
            raise AssertionError("a zip path escaping the folder must be refused")
        except PluginError as exc:
            assert "escapes" in str(exc)

        repo = os.path.join(tmp, "repo")
        os.rename(src, repo)
        g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True)  # noqa: E731
        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "t")
        g("add", "-A")
        g("commit", "-qm", "plugin")
        assert pm.install("file:///" + repo.replace("\\", "/") + "/.git")["name"] == "demo"
        assert not os.path.isdir(os.path.join(tmp, "plugins", "demo", ".git"))
    print("PASS test_install_from_folder_zip_and_git")


def test_a_switched_on_plugin_reaches_every_part_of_the_app():
    from test_webui import start_server

    from webui.mcphub import MCPHub

    with tempfile.TemporaryDirectory() as tmp:
        src = _make_plugin(os.path.join(tmp, "src"))
        app, server, base = start_server(tmp)
        server.shutdown()
        try:
            app.plugins = PluginManager(os.path.join(tmp, "plugins"))
            app.mcp = MCPHub(os.path.join(tmp, "mcp.json"))
            app.plugins.install(src)
            app.plugins.set_enabled("demo", True)
            app.plugins_changed()
            assert any(s.name == "greet" for s in app.skills)
            assert any(c.name == "demo:hello" for c in app.commands)
            assert app.agents(tmp)["demo-helper"].origin == "plugin:demo"
            assert "shout" in app.styles(tmp)
            assert "demo-full" in app.mcp.servers and "demo-full" in app.mcp.external
            import time

            end = time.time() + 10
            while app.mcp.servers["demo-full"].status != "connected" and time.time() < end:
                time.sleep(0.05)
            assert app.mcp.servers["demo-full"].status == "connected"
            assert "demo-full" not in json.dumps(json.load(open(os.path.join(tmp, "mcp.json"))) if
                                                 os.path.isfile(os.path.join(tmp, "mcp.json")) else {})
            try:
                app.mcp.remove("demo-full")
                raise AssertionError("a plugin's server is changed only through the plugin")
            except ValueError:
                pass
            session = app.create_session(tmp)
            assert session._hook_manager is not None and any(h.command == "echo done" for h in session._hook_manager.hooks)
            assert "mcp__demo-full__echo" in session.loop.registry.names()
            session.close()
            app.plugins.set_enabled("demo", False)
            app.plugins_changed()
            assert not any(s.name == "greet" for s in app.skills) and "demo-full" not in app.mcp.servers
            assert "demo-helper" not in app.agents(tmp)
        finally:
            app.mcp.close_all()
    print("PASS test_a_switched_on_plugin_reaches_every_part_of_the_app")


def test_phones_cannot_install_plugins():
    from test_webui import http, start_server

    from webui.server import WebHandler

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        real = WebHandler._remote, WebHandler._has_key
        WebHandler._remote = lambda self: True
        WebHandler._has_key = lambda self: True
        try:
            assert http("POST", base + "/api/plugins/install", {"source": tmp})[0] == 403
            assert http("POST", base + "/api/plugins/x/enabled", {"enabled": True})[0] == 403
            assert http("DELETE", base + "/api/plugins/x")[0] == 403
        finally:
            WebHandler._remote, WebHandler._has_key = real
            server.shutdown()
    print("PASS test_phones_cannot_install_plugins")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
