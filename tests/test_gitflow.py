"""The branch bar's git work, against real repositories in a temporary folder."""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from webui import gitflow  # noqa: E402


def _git(folder, *args):
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)


def _repo(tmp):
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    with open(os.path.join(work, "a.py"), "w", newline="") as fh:
        fh.write("x = 1\ny = 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "start")
    return work


def test_status_counts_what_is_not_committed_and_what_the_branch_adds():
    with tempfile.TemporaryDirectory() as tmp:
        assert gitflow.status(tmp) == {"repo": False}
        work = _repo(tmp)
        s = gitflow.status(work)
        assert s["repo"] and s["branch"] == "main" and s["base"] == "main" and s["files"] == 0
        assert not s["github"] and s["remote"] == ""

        with open(os.path.join(work, "a.py"), "w", newline="") as fh:
            fh.write("x = 10\ny = 2\nz = 3\n")
        with open(os.path.join(work, "new.py"), "w", newline="") as fh:
            fh.write("a\nb\n")
        s = gitflow.status(work)
        assert s["files"] == 2 and s["added"] == 2 + 2 and s["removed"] == 1, s

        gitflow.new_branch(work, "feature/one")
        gitflow.commit(work, "Change x and add new.py")
        s = gitflow.status(work)
        assert s["branch"] == "feature/one" and s["files"] == 0
        assert s["branch_added"] == 4 and s["branch_removed"] == 1, s


def test_commit_push_and_the_refusals():
    with tempfile.TemporaryDirectory() as tmp:
        work = _repo(tmp)
        try:
            gitflow.commit(work, "nothing here")
            raise AssertionError("an empty commit was made")
        except gitflow.GitError as exc:
            assert "nothing to commit" in str(exc)
        try:
            gitflow.commit(work, "  ")
            raise AssertionError("a commit with no message was made")
        except gitflow.GitError as exc:
            assert "message" in str(exc)
        for bad in ("", "-x", "a..b", "bad name", "x.lock"):
            try:
                gitflow.new_branch(work, bad)
                raise AssertionError("accepted the branch name " + repr(bad))
            except gitflow.GitError:
                pass

        remote = os.path.join(tmp, "remote.git")
        _git(tmp, "init", "-q", "--bare", remote)
        _git(work, "remote", "add", "origin", remote)
        with open(os.path.join(work, "b.py"), "w") as fh:
            fh.write("b = 1\n")
        done = gitflow.commit(work, "Add b")
        assert done["summary"] == "Add b" and len(done["commit"]) >= 7
        gitflow.push(work, "main")
        s = gitflow.status(work)
        assert s["upstream"] == "origin/main" and s["ahead"] == 0 and not s["github"]
        assert "b.py" in subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=remote,
                                        capture_output=True, text=True).stdout


def test_create_pr_needs_a_github_remote():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_webui import http, make_session_with_fake, start_server

    with tempfile.TemporaryDirectory() as tmp:
        work = _repo(tmp)
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, work, [])
            status, s = http("GET", f"{base}/api/sessions/{sid}/git")
            assert status == 200 and s["repo"] and s["pr"] is None
            status, r = http("POST", f"{base}/api/sessions/{sid}/git/pr", {"title": "x", "branch": "codemonkey/x"})
            assert status == 400 and "GitHub remote" in r["error"], r
            status, r = http("POST", f"{base}/api/sessions/{sid}/git/commit", {"message": "nothing"})
            assert status == 400 and "nothing to commit" in r["error"], r
        finally:
            server.shutdown()


def test_autofix_hands_new_failures_and_comments_to_the_agent_once():
    from test_webui import make_session_with_fake, start_server
    from webui import gitflow as gf

    with tempfile.TemporaryDirectory() as tmp:
        work = _repo(tmp)
        app, server, base = start_server(tmp)
        try:
            sid, _ = make_session_with_fake(base, work, [])
            session = app.get_session(sid)
            pr = {"number": 7, "url": "https://github.com/x/y/pull/7", "state": "OPEN", "checks": {"failed": 1}}
            session.git_state = lambda fresh=False: {"repo": True, "pr": pr}
            comments = [{"id": "old", "author": "rev", "body": "earlier note", "at": "1"}]
            fails = [{"name": "tests", "link": "", "log": "AssertionError: 1 != 2"}]
            saved = (gf.review_comments, gf.failing_checks)
            gf.review_comments = lambda folder, n: list(comments)
            gf.failing_checks = lambda folder, n: list(fails)
            sent = []
            session.enqueue_message = lambda text, att, cid="": sent.append(text) or 1
            try:
                session._autofix = {"on": True, "fails": set(), "comments": set(), "primed": False}
                session._autofix_check()
                assert len(sent) == 1 and 'check "tests" failed' in sent[0] and "1 != 2" in sent[0]
                assert "earlier note" not in sent[0], "comments from before Auto-fix are not new"
                session._autofix_check()
                assert len(sent) == 1, "the same failure is not sent twice"
                comments.append({"id": "new", "author": "rev", "body": "please rename x", "at": "2"})
                session._autofix_check()
                assert len(sent) == 2 and "please rename x" in sent[1] and "failed" not in sent[1]
                pr["state"] = "MERGED"
                session._autofix_check()
                assert session._autofix["on"] is False
            finally:
                gf.review_comments, gf.failing_checks = saved
        finally:
            server.shutdown()
