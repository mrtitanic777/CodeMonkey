"""Tests for breadth round 2: new skills and slash commands.

Run from the project root:
    python3 tests/test_breadth2.py

Stdlib only. Verifies the new markdown files through the REAL loaders
(skills.load_skills, coder.commands.load_commands) — no wiring needed
because both are auto-discovered.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coder.commands import (  # noqa: E402
    expand_command,
    find_command,
    load_commands,
)
from skills import load_skills  # noqa: E402

NEW_SKILLS = ["security-review", "performance-profiling", "api-design"]
NEW_COMMANDS = ["changelog", "deps", "todo-scan"]

EXISTING_SKILLS = {
    "code-review", "commit", "debugging", "deep-research",
    "documenting", "notes", "tdd", "writing-plans",
}
EXISTING_COMMANDS = {
    "commit", "debug", "document", "explain", "refactor", "review", "test",
}


def test_new_skills_load():
    skills = load_skills(os.path.join(ROOT, "skills"))
    by_name = {s.name: s for s in skills}
    for name in NEW_SKILLS:
        assert name in by_name, f"skill {name!r} not discovered by load_skills()"
        skill = by_name[name]
        assert skill.description, f"skill {name!r} has empty description"
        # Bodies must be readable (lazy load path works).
        assert len(skill.body) > 200, f"skill {name!r} body suspiciously short"
    # New names are unique against the known inventory.
    assert not (set(NEW_SKILLS) & EXISTING_SKILLS), "skill name collision"


def test_skill_descriptions_use_when():
    skills = load_skills(os.path.join(ROOT, "skills"))
    by_name = {s.name: s for s in skills}
    for name in NEW_SKILLS:
        desc = by_name[name].description
        assert desc.startswith("Use when"), (
            f"skill {name!r} description must start with 'Use when', got: {desc[:60]!r}"
        )


def test_skill_keywords_not_stuffed():
    # Keywords stay minimal: short terms, no sentence stuffing. The loader
    # keeps flow-style `[a, b]` as one string (house format, same as every
    # existing skill), so normalize before checking.
    skills = load_skills(os.path.join(ROOT, "skills"))
    by_name = {s.name: s for s in skills}
    for name in NEW_SKILLS:
        # the loader reads `[a, b]` as a list of terms (it used to keep the line as one string)
        terms = [t.strip(" []") for kw in by_name[name].keywords for t in str(kw).split(",")]
        terms = [t for t in terms if t]
        assert 1 <= len(terms) <= 6, f"skill {name!r} has {len(terms)} keywords"
        for term in terms:
            assert len(term.split()) <= 2, (
                f"skill {name!r} keyword too long: {term!r}"
            )


def test_new_commands_load():
    commands = load_commands(os.path.join(ROOT, "commands"))
    by_name = {c.name: c for c in commands}
    for name in NEW_COMMANDS:
        assert name in by_name, f"command {name!r} not discovered by load_commands()"
        cmd = by_name[name]
        assert cmd.description, f"command {name!r} has empty description"
        assert "$ARGUMENTS" in cmd.body, (
            f"command {name!r} body must support $ARGUMENTS"
        )
    assert not (set(NEW_COMMANDS) & EXISTING_COMMANDS), "command name collision"
    # find_command resolves each new command.
    for name in NEW_COMMANDS:
        assert find_command(commands, name) is not None


def test_commands_expand_arguments():
    commands = load_commands(os.path.join(ROOT, "commands"))
    by_name = {c.name: c for c in commands}
    expanded = expand_command(by_name["changelog"], "v1.2.0..HEAD")
    assert "v1.2.0..HEAD" in expanded
    assert "$ARGUMENTS" not in expanded
    expanded = expand_command(by_name["deps"], "python")
    assert "python" in expanded
    assert "$ARGUMENTS" not in expanded
    expanded = expand_command(by_name["todo-scan"], "src/")
    assert "src/" in expanded
    assert "$ARGUMENTS" not in expanded


def test_no_duplicate_names_overall():
    skills = load_skills(os.path.join(ROOT, "skills"))
    names = [s.name for s in skills]
    assert len(names) == len(set(names)), "duplicate skill names on disk"
    commands = load_commands(os.path.join(ROOT, "commands"))
    cnames = [c.name for c in commands]
    assert len(cnames) == len(set(cnames)), "duplicate command names on disk"


if __name__ == "__main__":
    test_fns = [v for k, v in sorted(globals().items())
                if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in test_fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    total = len(test_fns)
    print(f"{total - failed}/{total} tests passed.")
    sys.exit(1 if failed else 0)
