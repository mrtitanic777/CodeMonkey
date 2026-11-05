---
name: tdd
description: Use when implementing a new feature, fixing a bug that needs a regression test, or the user asks for test-driven development.
keywords: [tdd, test-driven, red-green-refactor, unit test, regression test]
---

# Test-Driven Development

## Red → Green → Refactor

1. **Red.** Write a failing test that describes the behavior you want.
   Run it; watch it fail for the *right* reason (not an import error).
   The test is a specification — name it after the behavior:
   `test_edit_fuzzy_fallback`, not `test_edit2`.
2. **Green.** Write the smallest implementation that passes. Duplication
   and ugliness are allowed; correctness is the only goal.
3. **Refactor.** Clean up the implementation with the test as a safety
   net. If a refactor breaks the test, the test was right.

For bug fixes, step 0 is **reproduce as a test**: the regression test
fails before the fix and passes after. No repro test, no fix.

## What makes a good test in this repo

- **Plain asserts, stdlib only.** Tests run via `python3 tests/test_x.py`;
  no pytest, no fixtures framework. A test is a function; failure is an
  exception.
- **Fake the boundaries.** Scripted fake clients stand in for models;
  `tempfile.TemporaryDirectory` stands in for the filesystem; tiny
  `http.server` scripts stand in for network servers. Never hit the real
  network or require an API key in a test.
- **One behavior per test.** If the name needs "and", split it.
- **Test the contract, not the internals.** Assert on return values and
  observable effects (files written, messages sent), not private method
  calls — internals change, contracts shouldn't.
- **Failure messages matter.** End asserts with a payload:
  `assert x == y, (x, y)` — the next person debugging at 3am thanks you.

## Test pyramid for an agent harness

- **Unit:** pure functions (parsers, trimmers, matchers). Cheap, many.
- **Integration:** loop + fake client + real registry (does the tool
  actually execute and feed back?). Fewer, but they catch the real bugs.
- **End-to-end over HTTP:** only for servers (web UI, adapters). Slowest;
  keep the count low.

## When NOT to TDD

Spikes and explorations: when you don't know what the shape should be
yet, write the code first, then characterize the good parts with tests.
UI copy, prompt wording, and config examples are reviewed by reading,
not by tests.
