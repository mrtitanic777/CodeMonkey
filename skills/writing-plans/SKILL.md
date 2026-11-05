---
name: writing-plans
description: Use when breaking a multi-step task into bite-sized pieces, drafting a work breakdown, or structuring implementation steps before coding.
keywords: [breakdown, tasks, roadmap, work breakdown, sequencing]
---

# Writing Plans

A plan is a contract between the planner and the executor (which may be a
different model, a future session, or a future you). Write it so the
executor never has to guess.

## Anatomy of a good plan

Every task in the plan has four parts:

1. **What** — one sentence: the behavior change or artifact produced.
2. **Where** — exact file paths. Not "the config module": `config.py`,
   `Config` dataclass, `resolve_model_endpoint()`.
3. **How to verify** — the exact command that proves it worked:
   `python3 tests/test_x.py`, `python3 -c "import ..."`, never "run the
   tests" vaguely.
4. **Size** — each task is 2–5 minutes of work. If a task is bigger, split
   it. If it's smaller than 2 minutes, merge it with a neighbor.

## Structure

- **Order by dependency**, not by importance: scaffolding before logic,
  logic before wiring, wiring before docs. Number the tasks; later tasks
  may reference earlier ones by number.
- **Start with a spike** if the shape is unknown: "Task 1: read X and
  report the integration points (no code)." Plans written before
  understanding are fiction.
- **End with verification**: the last task is always "run the full
  relevant suites; all green."
- **Call out risks explicitly**: "Risk: the YAML subset parser doesn't
  support flow-style lists — check before using them in config."

## Example (good)

```
3. Add a --version flag to the command line (cli.py, build_parser).
   - Read the version from version.py; print it and exit 0 when the flag
     is given, before any other argument is handled.
   - Verify: python3 -m pytest tests/test_cli.py -k version
```

## Example (bad)

```
3. Make planning use the big model.
```

The bad version forces the executor to re-derive the what, where, and
how — which is exactly the work planning was supposed to do once.

## Complementing plan mode

This skill is the methodology; `/plan` is the mechanism. When writing a
plan for the agent, follow this anatomy in the plan text itself — the
executor reads `plan.md` literally and has no access to your reasoning
unless you wrote it down.
