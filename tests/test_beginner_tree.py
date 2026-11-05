"""The Files pane's tree (/api/sessions/<id>/tree) and Beginner mode's settings and instructions."""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_webui import http, start_server  # noqa: E402

from webui.app import BEGINNER_TEXT  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def test_tree_lists_files_and_what_git_sees_changed():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(proj, "src"))
        for name in ("src/game.cpp", "src/old.cpp", "README.md"):
            with open(os.path.join(proj, name), "w") as fh:
                fh.write("x\n")
        _git(proj, "init", "-q")
        _git(proj, "add", ".")
        _git(proj, "commit", "-qm", "first")
        with open(os.path.join(proj, "src", "game.cpp"), "a") as fh:
            fh.write("y\n")
        with open(os.path.join(proj, "notes.txt"), "w") as fh:
            fh.write("new\n")
        _git(proj, "mv", "src/old.cpp", "src/renamed.cpp")
        app, server, base = start_server(tmp)
        try:
            sid = http("POST", base + "/api/sessions", {"project_dir": proj})[1]["id"]
            status, tree = http("GET", f"{base}/api/sessions/{sid}/tree")
        finally:
            server.shutdown()
    assert status == 200 and tree["folder"] == "proj" and not tree["truncated"]
    assert {"src/game.cpp", "README.md", "notes.txt", "src/renamed.cpp"} <= set(tree["files"]), tree
    assert tree["changed"].get("src/game.cpp") == "M" and tree["changed"].get("notes.txt") == "?", tree["changed"]
    assert "src/old.cpp" not in tree["changed"] or tree["changed"]["src/old.cpp"] != "?"   # a rename's old name is not a file of its own


def test_beginner_mode_adds_its_instructions_and_keeps_learned_words():
    with tempfile.TemporaryDirectory() as tmp:
        app, server, base = start_server(tmp)
        try:
            sid = http("POST", base + "/api/sessions", {"project_dir": tmp})[1]["id"]
            session = app.get_session(sid)
            assert BEGINNER_TEXT not in app.style_text(session)
            status, st = http("POST", base + "/api/settings", {"beginner": True, "ui": {"learned": {"Variable": "2026-10-28"}}})
            assert status == 200 and st["beginner"] is True and st["ui"]["learned"] == {"variable": "2026-10-28"}, st
            assert "```choices" in app.style_text(session) and "[[word|" in app.style_text(session)
            http("POST", base + "/api/settings", {"beginner": False})
            assert BEGINNER_TEXT not in app.style_text(session)
        finally:
            server.shutdown()


def test_folder_chooser_marks_projects_and_makes_folders():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "game", ".git"))
        os.makedirs(os.path.join(tmp, "photos"))
        os.makedirs(os.path.join(tmp, ".cache"))
        app, server, base = start_server(tmp)
        try:
            status, d = http("GET", base + "/api/browse?path=" + tmp.replace("\\", "/"))
            names = {x["name"]: x.get("project") for x in d["dirs"]}
            assert status == 200 and names.get("game") is True and names.get("photos") is False and ".cache" not in names, d
            assert any(p["name"] == "Home" for p in d["places"]) and "recent" in d
            status, made = http("POST", base + "/api/browse/new", {"parent": tmp, "name": "New game"})
            assert status == 200 and os.path.isdir(made["path"])
            for bad in ("", "a/b", "..", 'x"y'):
                assert http("POST", base + "/api/browse/new", {"parent": tmp, "name": bad})[0] == 400, bad
            assert http("POST", base + "/api/browse/new", {"parent": tmp, "name": "New game"})[0] == 400   # already there
        finally:
            server.shutdown()
