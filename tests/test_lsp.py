"""Code intelligence: the LSP client against a fake server, the built-in checks, and errors shown
right after an edit."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

from coder.lsp import LSP, fallback_check  # noqa: E402

FAKE = [sys.executable, os.path.join(TESTS, "fake_lsp_server.py")]


def test_the_client_speaks_lsp():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.py")
        with open(path, "w") as fh:
            fh.write("class Thing:\n    def run(self):\n        BAD = 1\n")
        lsp = LSP(tmp, {"python": FAKE})
        try:
            report = lsp.diagnostics("m.py")
            assert "m.py:3:9 error [F1]: BAD is not allowed" in report and "(" + FAKE[0] in report, report
            with open(path, "w") as fh:          # the change is sent, and the new report waited for
                fh.write("class Thing:\n    def run(self):\n        ok = 1\n")
            assert "no problems" in lsp.diagnostics("m.py")
            assert lsp.definition("m.py", 2, "run").startswith("m.py:1  class Thing:")
            assert lsp.references("m.py", 2, "run").startswith("2 reference(s) to run")
            assert lsp.hover("m.py", 2, "run") == "col 8"            # the column comes from the name
            assert lsp.symbols("m.py") == "class Thing  (line 1)\n  method run  (line 2)"
            assert "is not on line 2" in str(_err(lambda: lsp.hover("m.py", 2, "nope")))
            assert "outside the project" in str(_err(lambda: lsp.diagnostics("../x.py")))
            assert [s for s in lsp.status() if s["language"] == "python"][0]["running"]
        finally:
            lsp.close()
    print("PASS test_the_client_speaks_lsp")


def _err(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("expected an error")


def test_built_in_checks_without_a_server():
    with tempfile.TemporaryDirectory() as tmp:
        files = {"a.py": "import os\nprint(undefined_x)\n", "b.py": "def f(:\n", "c.json": '{"a": 1,}', "d.txt": "x"}
        for rel, body in files.items():
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        a = fallback_check(os.path.join(tmp, "a.py"))
        assert any(i["severity"] == "error" and "undefined_x" in i["message"] for i in a)
        assert fallback_check(os.path.join(tmp, "b.py"))[0]["code"] == "syntax"
        assert fallback_check(os.path.join(tmp, "c.json"))[0]["source"] == "json"
        assert fallback_check(os.path.join(tmp, "d.txt")) is None
        lsp = LSP(tmp, {"python": ["no-such-server-anywhere"]})
        assert "built-in check" in lsp.diagnostics("a.py") or "no-such" in lsp.diagnostics("a.py")
    print("PASS test_built_in_checks_without_a_server")


def test_python_definitions_and_outline_without_a_server():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pkg"))
        with open(os.path.join(tmp, "pkg", "util.py"), "w") as fh:
            fh.write("LIMIT = 3\n\n\nclass Box:\n    def open(self):\n        pass\n\n\ndef helper():\n    pass\n")
        with open(os.path.join(tmp, "main.py"), "w") as fh:
            fh.write("from pkg.util import helper\n\nhelper()\n")
        lsp = LSP(tmp, {"python": ["no-such-server-anywhere"]})
        found = lsp.definition("main.py", 3, "helper")
        assert found.startswith("pkg/util.py:9  function helper"), found
        assert lsp.definition("main.py", 3, "").startswith("pkg/util.py:9")      # the first name on the line
        with open(os.path.join(tmp, "main.py"), "a") as fh:
            fh.write("print(missing_name)\n")
        assert lsp.definition("main.py", 4, "missing_name").startswith("No definition of missing_name")
        assert lsp.symbols("pkg/util.py") == ("variable LIMIT  (line 1)\nclass Box  (line 4)\n"
                                              "  method open  (line 5)\nfunction helper  (line 9)")
    print("PASS test_python_definitions_and_outline_without_a_server")


def test_an_edit_shows_the_errors_it_made():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "m.py"), "w") as fh:
            fh.write("x = 1\n")
        app, server, base = start_server(tmp)
        server.shutdown()
        session = app.create_session(tmp)
        session.settings["permission_mode"] = "auto"
        app.update_settings({"code_gate": "off"})
        session.lsp.overrides = {"python": FAKE}
        session.loop.registry.execute("read", {"path": "m.py"})   # the guard asks before editing an unread file
        out = session.loop.registry.execute("edit", {"path": "m.py", "old_text": "x = 1", "new_text": "BAD = 1"})
        assert "[Diagnostics after this change] m.py: 1 error" in out, out
        clean = session.loop.registry.execute("edit", {"path": "m.py", "old_text": "BAD = 1", "new_text": "y = 2"})
        assert "Diagnostics" not in clean, clean
        assert "diagnostics" in session.loop.registry.names() and "lsp_definition" in session.loop.registry.names()
        session.close()
    print("PASS test_an_edit_shows_the_errors_it_made")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
