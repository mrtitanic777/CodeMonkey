"""Tests for skillsmith (mines case store -> proposed SKILL.md files).

Run from the project root:
    python tests/test_skillsmith.py

Stdlib only; no network, no API key, no model calls.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from skillsmith import (  # noqa: E402
    mine_patterns,
    propose_skill,
    run,
    write_proposals,
)
from skills import load_skills  # noqa: E402


def make_case(task, tools, success=True, strategies=None, pitfalls=None):
    return {
        "task": task,
        "plan_summary": "",
        "trace": [
            {"tool": t, "args_summary": f"path='{task[:8]}'", "ok": True}
            for t in tools
        ],
        "outcome": {"success": success, "tests_passed": success,
                    "turns": 3, "latency_s": 5.0},
        "project_kind": "python-cli",
        "strategies": strategies or [],
        "pitfalls": pitfalls or [],
    }


def workflow_cases():
    return [
        make_case("add --version flag to cli", ["glob", "read", "edit", "bash"]),
        make_case("add --help flag to cli", ["read", "edit", "bash"]),
        make_case("fix off-by-one in mean()", ["grep", "read", "edit", "bash"]),
        make_case("fix crash on empty input", ["read", "read", "edit", "bash"]),
    ]


# ---------------------------------------------------------------- mining ---

def test_mine_finds_repeated_pattern():
    patterns = mine_patterns(workflow_cases(), min_support=3)
    found = [p for p in patterns if p["tools"] == ("read", "edit", "bash")]
    assert found, [p["tools"] for p in patterns]
    assert found[0]["support"] >= 3, found[0]
    assert found[0]["example_tasks"], "examples recorded"
    assert len(found[0]["arg_shapes"]) == 3, "one shape per position"
    print("PASS test_mine_finds_repeated_pattern")


def test_mine_ignores_singletons():
    cases = [
        make_case("unique task one", ["read", "delegate"]),
        make_case("unique task two", ["grep", "todo_write"]),
    ]
    patterns = mine_patterns(cases, min_support=2)
    assert patterns == [], patterns
    print("PASS test_mine_ignores_singletons")


def test_mine_only_successes():
    cases = [
        make_case("failed attempt one", ["read", "edit", "bash"], success=False),
        make_case("failed attempt two", ["read", "edit", "bash"], success=False),
        make_case("failed attempt three", ["read", "edit", "bash"], success=False),
    ]
    patterns = mine_patterns(cases, min_support=3)
    assert all(p["tools"] != ("read", "edit", "bash") for p in patterns), patterns
    print("PASS test_mine_only_successes")


def test_mine_maximal_prefers_longer():
    # (read, edit) appears in exactly the same 3 cases as (read, edit, bash)
    cases = [
        make_case("t1", ["read", "edit", "bash"]),
        make_case("t2", ["read", "edit", "bash"]),
        make_case("t3", ["read", "edit", "bash"]),
    ]
    patterns = mine_patterns(cases, min_support=3)
    tools = [p["tools"] for p in patterns]
    assert ("read", "edit", "bash") in tools, tools
    assert ("read", "edit") not in tools, tools
    assert ("edit", "bash") not in tools, tools
    print("PASS test_mine_maximal_prefers_longer")


def test_mine_keeps_shorter_when_more_general():
    # (read, edit) in 4 cases, (read, edit, bash) in only 3 of them
    cases = [
        make_case("t1", ["read", "edit", "bash"]),
        make_case("t2", ["read", "edit", "bash"]),
        make_case("t3", ["read", "edit", "bash"]),
        make_case("t4", ["read", "edit"]),
    ]
    patterns = mine_patterns(cases, min_support=3)
    tools = [p["tools"] for p in patterns]
    assert ("read", "edit", "bash") in tools, tools
    assert ("read", "edit") in tools, tools
    print("PASS test_mine_keeps_shorter_when_more_general")


def test_mine_empty_cases():
    assert mine_patterns([], min_support=3) == []
    print("PASS test_mine_empty_cases")


# --------------------------------------------------------------- propose ---

def test_propose_valid_frontmatter():
    patterns = mine_patterns(workflow_cases(), min_support=3)
    name, skill_md = propose_skill(patterns[0], taken=set())
    assert name, "name generated"
    assert skill_md.startswith("---\n"), skill_md[:20]
    # description must follow the house rule
    lines = skill_md.splitlines()
    desc_line = next(
        l for l in lines if l.startswith("description:")
    )
    assert "use when" in desc_line.lower(), desc_line
    print("PASS test_propose_valid_frontmatter")


def test_propose_no_collision_with_installed():
    patterns = mine_patterns(workflow_cases(), min_support=3)
    taken = {"read-edit-bash"}  # pretend it's already installed
    name, _ = propose_skill(patterns[0], taken)
    assert name != "read-edit-bash", name
    assert name.startswith("read-edit-bash-"), name
    print("PASS test_propose_no_collision_with_installed")


def test_propose_filesystem_safe_name():
    pattern = {
        "tools": ("mcp__weird server__tool!", "read"),
        "support": 3,
        "example_tasks": ["do a thing"],
        "arg_shapes": ["", ""],
        "strategies": [],
        "pitfalls": [],
    }
    name, _ = propose_skill(pattern, taken=set())
    assert name and "/" not in name and " " not in name, name
    assert all(c.isalnum() or c == "-" for c in name), name
    print("PASS test_propose_filesystem_safe_name")


def test_propose_carries_strategies_and_pitfalls():
    cases = [
        make_case(
            "add flag one", ["read", "edit", "bash"],
            strategies=["Explore before editing."],
            pitfalls=["Edit failed — check old_text exists first."],
        )
        for _ in range(3)
    ]
    patterns = mine_patterns(cases, min_support=3)
    assert patterns[0]["strategies"] == ["Explore before editing."], patterns[0]
    assert patterns[0]["pitfalls"], patterns[0]
    _, skill_md = propose_skill(patterns[0], taken=set())
    assert "Explore before editing." in skill_md, skill_md
    assert "old_text" in skill_md, skill_md
    print("PASS test_propose_carries_strategies_and_pitfalls")


# ----------------------------------------------------------------- write ---

def test_write_proposals_roundtrip():
    patterns = mine_patterns(workflow_cases(), min_support=3)
    proposals = [propose_skill(p, set()) for p in patterns[:1]]
    with tempfile.TemporaryDirectory() as tmp:
        written = write_proposals(proposals, tmp)
        assert len(written) == 1
        assert os.path.isfile(written[0]), written
        assert written[0].endswith(os.path.join(proposals[0][0], "SKILL.md"))
        skills = load_skills(tmp)
        assert len(skills) == 1, [s.name for s in skills]
        assert skills[0].name == proposals[0][0]
        assert skills[0].body, "body non-empty"
    print("PASS test_write_proposals_roundtrip")


def test_run_end_to_end():
    import json

    with tempfile.TemporaryDirectory() as tmp:
        store_path = os.path.join(tmp, "cases.jsonl")
        with open(store_path, "w", encoding="utf-8") as fh:
            for c in workflow_cases():
                fh.write(json.dumps(c) + "\n")
        proposals_dir = os.path.join(tmp, "proposals")
        proposals = run(store_path, proposals_dir, min_support=3,
                        skills_dir=os.path.join(tmp, "no-such-skills"))
        assert proposals, "proposals generated"
        names = [n for n, _ in proposals]
        assert any("read" in n and "bash" in n for n in names), names
        for name, _ in proposals:
            assert os.path.isfile(
                os.path.join(proposals_dir, name, "SKILL.md")
            ), name
    print("PASS test_run_end_to_end")


def test_run_empty_store():
    with tempfile.TemporaryDirectory() as tmp:
        proposals = run(
            os.path.join(tmp, "missing.jsonl"),
            os.path.join(tmp, "proposals"),
            skills_dir=os.path.join(tmp, "no-skills"),
        )
        assert proposals == [], proposals
    print("PASS test_run_empty_store")


def main():
    tests = [
        test_mine_finds_repeated_pattern,
        test_mine_ignores_singletons,
        test_mine_only_successes,
        test_mine_maximal_prefers_longer,
        test_mine_keeps_shorter_when_more_general,
        test_mine_empty_cases,
        test_propose_valid_frontmatter,
        test_propose_no_collision_with_installed,
        test_propose_filesystem_safe_name,
        test_propose_carries_strategies_and_pitfalls,
        test_write_proposals_roundtrip,
        test_run_end_to_end,
        test_run_empty_store,
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
