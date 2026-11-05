"""/init, reply styles, notebooks, and the status line command."""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def test_init_gathers_the_facts_first():
    from coder.projinit import prompt, scan

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"))
        os.makedirs(os.path.join(tmp, "ignored"))
        files = {
            "package.json": json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}),
            "pnpm-lock.yaml": "", "src/app.ts": "export {}", "src/b.ts": "", "tool.py": "",
            "Makefile": "build:\n\tgo build\ntest: build\n\tgo test\n", ".gitignore": "ignored/\n",
            "ignored/x.ts": "", "CLAUDE.md": "Always use tabs.", "README.md": "# Demo\nA demo app.",
        }
        for rel, body in files.items():
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        facts = scan(tmp)
        assert facts["languages"][0].startswith("TypeScript (2 files)")       # the ignored folder is not counted
        assert "pnpm run test  ->  vitest run" in facts["commands"] and "make build" in facts["commands"]
        assert facts["rule_files"] == {"CLAUDE.md": "Always use tabs."}
        text = prompt(tmp, "the build")
        assert "It does not exist yet." in text and "Always use tabs." in text and "# Demo" in text
        assert text.rstrip().endswith("Pay special attention to: the build")
    print("PASS test_init_gathers_the_facts_first")


def test_reply_styles_reach_the_system_prompt():
    from coder.styles import BUILT_IN, instruction, load
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".codemonkey", "styles"))
        with open(os.path.join(tmp, ".codemonkey", "styles", "pirate.md"), "w") as fh:
            fh.write("---\nname: pirate\ndescription: Arr.\n---\nTalk like a pirate.")
        styles = load(os.path.join(tmp, "none"), tmp)
        assert set(BUILT_IN) <= set(styles) and styles["pirate"]["origin"] == "project"
        assert instruction(styles, "default") == "" and "Talk like a pirate." in instruction(styles, "pirate")
        app, server, base = start_server(tmp)
        server.shutdown()
        session = app.create_session(tmp)
        assert "[Reply style" not in session.loop.build_system_prompt("")
        app.update_settings({"output_style": "concise"})
        assert "[Reply style: concise]" in session.loop.build_system_prompt("")
        session.settings["output_style"] = "pirate"               # a session's own choice wins
        assert "Talk like a pirate." in session.loop.build_system_prompt("")
        session.close()
    print("PASS test_reply_styles_reach_the_system_prompt")


def test_notebooks_read_as_cells_and_change_one_cell():
    from coder.tools import _read, coder_tools

    with tempfile.TemporaryDirectory() as tmp:
        nb = {"cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n"]},
            {"cell_type": "code", "metadata": {}, "execution_count": 3, "source": ["print(1+1)\n"],
             "outputs": [{"output_type": "stream", "name": "stdout", "text": ["2\n"]},
                         {"output_type": "display_data", "data": {"image/png": PNG}, "metadata": {}}]}],
            "metadata": {"kernelspec": {"language": "python"}}, "nbformat": 4, "nbformat_minor": 5}
        with open(os.path.join(tmp, "n.ipynb"), "w") as fh:
            json.dump(nb, fh)
        shown = _read("n.ipynb", project_dir=tmp)
        assert "## Cell 1 [code] (run 3)" in shown and "print(1+1)" in shown and "[[cm-image " in shown
        tool = {t.name: t for t in coder_tools(tmp, auto_approve=True)}["notebook_edit"]
        assert tool.needs_approval
        assert tool.func("n.ipynb", 1, "print(2+2)").startswith("Replaced cell 1")
        assert tool.func("n.ipynb", 2, "x = 1", "", "insert").startswith("Inserted")
        assert tool.func("n.ipynb", 0, mode="delete").startswith("Deleted")
        assert tool.func("n.ipynb", 7, "x").startswith("ERROR: there is no cell 7")
        assert tool.func("other.txt", 0, "x").startswith("ERROR: not a notebook")
        after = json.load(open(os.path.join(tmp, "n.ipynb")))
        assert [c["cell_type"] for c in after["cells"]] == ["code", "code"]
        assert after["cells"][0]["outputs"] == [] and after["cells"][0]["execution_count"] is None
        assert after["metadata"]["kernelspec"]["language"] == "python"
    print("PASS test_notebooks_read_as_cells_and_change_one_cell")


def test_statusline_command_gets_the_session_and_phones_cannot_set_it():
    from test_webui import http, start_server

    from webui.server import WebHandler

    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            session = app.create_session(tmp)
            assert app.statusline(session) == ""
            py = sys.executable.replace("\\", "/")
            app.update_settings({"statusline_command": f'"{py}" -c "import json,sys; print(\'s:\' + json.load(sys.stdin)[\'session_id\'])"'})
            assert app.statusline(session) == "s:" + session.id
            assert http("GET", base + f"/api/sessions/{session.id}/statusline")[1]["text"] == "s:" + session.id
            real = WebHandler._remote, WebHandler._has_key
            WebHandler._remote = lambda self: True
            WebHandler._has_key = lambda self: True
            try:
                http("POST", base + "/api/settings", {"statusline_command": "calc.exe"})
            finally:
                WebHandler._remote, WebHandler._has_key = real
            assert app.settings["statusline_command"].endswith("session_id\'])\"")
            session.close()
        finally:
            server.shutdown()
    print("PASS test_statusline_command_gets_the_session_and_phones_cannot_set_it")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
