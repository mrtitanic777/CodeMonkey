"""Worktrees: a branch and folder of its own for a session or an agent."""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

from coder.worktrees import WorktreeError, Worktrees  # noqa: E402


def _repo(tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "src"))
    g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True)  # noqa: E731
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    with open(os.path.join(repo, "src", "a.txt"), "w") as fh:
        fh.write("one\n")
    g("add", "-A")
    g("commit", "-qm", "first")
    return repo, g


def test_create_merge_conflict_discard():
    with tempfile.TemporaryDirectory() as tmp:
        repo, g = _repo(tmp)
        wt = Worktrees(os.path.join(tmp, "ws"))
        e = wt.create(os.path.join(repo, "src"), "Fix bug")
        assert e["branch"] == "codemonkey/fix-bug" and e["folder"].endswith(os.path.join("fix-bug", "src"))
        assert wt.containing(e["folder"])["branch"] == "codemonkey/fix-bug"
        # the same folder by its long or its short name (C:\Users\RUNNER~1)
        same = lambda a, b: os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))  # noqa: E731
        assert same(wt.back_in_repo(e, e["folder"]), os.path.join(repo, "src"))
        with open(os.path.join(e["folder"], "a.txt"), "a") as fh:
            fh.write("two\n")
        st = wt.status(wt.get(e["path"]))
        assert st["uncommitted"] == 1 and st["ahead"] == 0
        r = wt.merge(e["path"], "Fix it")
        assert r["merged"] and open(os.path.join(repo, "src", "a.txt")).read() == "one\ntwo\n"
        assert wt.merge(e["path"])["merged"] is False            # nothing new since
        wt.discard(e["path"])
        assert not os.path.isdir(e["path"]) and wt.listing() == []

        e2 = wt.create(repo, "fix bug")
        assert e2["branch"] == "codemonkey/fix-bug-2" or e2["branch"] == "codemonkey/fix-bug"
        with open(os.path.join(e2["path"], "src", "a.txt"), "w") as fh:
            fh.write("branch\n")
        wt.commit_all(wt.get(e2["path"]), "b")
        with open(os.path.join(repo, "src", "a.txt"), "w") as fh:
            fh.write("main\n")
        g("commit", "-qam", "m")
        try:
            wt.merge(e2["path"])
            raise AssertionError("a conflict must be reported")
        except WorktreeError as exc:
            assert "conflicts in src/a.txt" in str(exc)
        assert g("status", "--short").stdout == ""                # nothing left half-merged
        g("checkout", "-q", "-b", "other")
        try:
            wt.merge(e2["path"])
            raise AssertionError("merging into the wrong branch must be refused")
        except WorktreeError as exc:
            assert "switch it to main" in str(exc)
        wt.discard(e2["path"])
        try:
            wt.create(tmp)
            raise AssertionError("not a repository")
        except WorktreeError:
            pass
    print("PASS test_create_merge_conflict_discard")


def test_a_web_session_moves_into_one_and_back():
    from test_webui import start_server

    with tempfile.TemporaryDirectory() as tmp:
        repo, g = _repo(tmp)
        app, server, base = start_server(repo)
        server.shutdown()
        session = app.create_session(repo)
        entry = session.move_to_worktree("try it")
        assert session.project_dir == entry["folder"] and session.snapshot()["worktree"]["branch"] == entry["branch"]
        with open(os.path.join(session.project_dir, "new.txt"), "w") as fh:
            fh.write("x\n")
        listed = app.worktree_list()
        assert listed[0]["sessions"][0]["id"] == session.id and listed[0]["uncommitted"] == 1
        result = app.worktree_merge(entry["path"])
        assert result["merged"] and result["removed"] and os.path.isfile(os.path.join(repo, "new.txt"))
        # moved back to the main checkout (by its long or its short name: the same folder)
        assert os.path.normcase(os.path.realpath(session.project_dir)) == os.path.normcase(os.path.realpath(repo))
        assert app.worktree_list() == []
        session.close()
    print("PASS test_a_web_session_moves_into_one_and_back")




if __name__ == "__main__":
    test_create_merge_conflict_discard()
    test_a_web_session_moves_into_one_and_back()
