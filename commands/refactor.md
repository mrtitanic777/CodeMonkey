---
name: refactor
description: Safely refactor code with test verification at each step.
---

Refactor the target safely. No behavior changes — structure only.

Target: $ARGUMENTS

1. **Characterize first.** Before touching anything, make sure the
   behavior is pinned down: run the existing tests covering this code
   (`python3 tests/test_*.py`). If coverage is thin, write a
   characterization test that captures current behavior (it should pass
   *before* your change).
2. **Small steps.** One transformation at a time: extract a function,
   rename, move code — never two at once. After each step, re-run the
   relevant tests.
3. **No opportunistic fixes.** If you spot a bug, note it and keep
   refactoring; fix it in a separate change.
4. **Verify.** Full relevant test suites green at the end, plus a final
   read-through of the diff: does it do the same thing, only cleaner?

If a step breaks tests, revert that step (don't press forward) and pick a
smaller transformation.
