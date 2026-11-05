"""Fix it for me: failed commands explained in plain words (webui/explain.py), and the terminal's "fail" line."""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from webui.explain import explain  # noqa: E402
from webui.terminal import Terminal  # noqa: E402


def test_compiler_errors_get_plain_words_and_a_place():
    gcc = "src/game.cpp: In function 'int main()':\nsrc/game.cpp:42:5: error: 'scroe' was not declared in this scope\n"
    why = explain("make", gcc, 2)
    assert why["kind"] == "build" and why["title"] == "A name the code uses doesn't exist", why
    assert '"scroe"' in why["plain"] and why["file"] == "src/game.cpp" and why["line"] == 42
    msvc = r"C:\proj\main.cpp(17,9): error C2065: 'lives': undeclared identifier"
    why = explain("msbuild game.sln", msvc, 1)
    assert '"lives"' in why["plain"] and why["line"] == 17 and why["file"].endswith("main.cpp"), why
    why = explain("g++ a.o b.o", "a.o: undefined reference to `draw_hud()'\ncollect2: error: ld returned 1 exit status", 1)
    assert why["title"] == "The pieces don't join up" and "draw_hud()" in why["plain"]


def test_running_errors_and_the_fallback():
    trace = ('Traceback (most recent call last):\n  File "/usr/lib/python3.12/runpy.py", line 9, in run\n'
             '  File "game.py", line 3, in <module>\n    main()\n  File "game.py", line 12, in main\n'
             "    print(scroe)\nNameError: name 'scroe' is not defined\n")
    why = explain("python game.py", trace, 1)
    assert why["kind"] == "run" and why["file"] == "game.py" and why["line"] == 12, why   # where it failed, not where it began
    coloured = ('  File \x1b[35m"C:\\proj\\game.py"\x1b[0m, line \x1b[35m8\x1b[0m, in \x1b[35m<module>\x1b[0m\n'
                "\x1b[1;35mNameError\x1b[0m: \x1b[35mname 'scroe' is not defined. Did you mean: 'score'?\x1b[0m")
    why = explain("python game.py", coloured, 1)   # Python 3.13+ colours its errors
    assert why["line"] == 8 and '"scroe"' in why["plain"] and 'It may mean "score"' in why["plain"], why
    assert explain("python x.py", "ModuleNotFoundError: No module named 'pygame'", 1)["title"] == "A Python package is missing"
    assert explain("./game", "", 3221225477)["title"] == "The program crashed"
    assert explain("pip", "'pip' is not recognized as an internal or external command", 1)["title"] == "That command isn't on this computer"
    blah = explain("mytool --go", "something odd happened", 4)
    assert blah["title"] == "It stopped with an error" and "(code 4)" in blah["plain"] and "file" not in blah
    assert explain("cmake --build .", "oops", 1)["title"] == "It didn't build"


def _wait(term, kind, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        found = [l for l in term.read()["lines"] if l["kind"] == kind]
        if found and not term.running:
            return found
        time.sleep(0.1)
    return [l for l in term.read()["lines"] if l["kind"] == kind]


def test_terminal_adds_a_fail_line_only_for_failures():
    with tempfile.TemporaryDirectory() as tmp:
        term = Terminal(tmp)
        py = '"' + sys.executable + '"' if sys.platform != "win32" else "& '" + sys.executable + "'"
        with open(os.path.join(tmp, "game.py"), "w") as fh:
            fh.write("import sys\nprint(\"NameError: name 'scroe' is not defined\")\nsys.exit(3)\n")
        with open(os.path.join(tmp, "ok.py"), "w") as fh:
            fh.write("print('fine')\n")
        term.run(py + " game.py")
        fails = _wait(term, "fail")
        assert len(fails) == 1, term.read()["lines"]
        f = fails[0]
        assert f["code"] == 3 and "scroe" in f["output"] and f["why"]["title"] == "A name the code uses doesn't exist", f
        assert f["command"].endswith("game.py") and f["why"]["kind"] == "run"
        term.run(py + " ok.py")
        _wait(term, "end")
        time.sleep(0.3)
        assert len([l for l in term.read()["lines"] if l["kind"] == "fail"]) == 1   # success adds no card
