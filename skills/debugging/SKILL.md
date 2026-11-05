---
name: debugging
description: Use when something is broken, a test fails, there's a traceback to read, or the user describes a bug or unexpected behavior.
keywords: [debug, bug, traceback, error, failing, reproduce, stack trace]
---

# Debugging

You debug systematically. Never guess at fixes; follow the loop.

## The loop

1. **Reproduce.** Get the failure to happen on demand with the smallest
   possible trigger: one command, one test, one input. If you can't
   reproduce it, you can't verify a fix. Write the repro down.
2. **Isolate.** Narrow where the wrongness lives. Binary-search the code:
   disable or stub half the path and see which half still fails. Add
   temporary prints at boundaries (inputs vs outputs of each function),
   not in the middle of logic.
3. **Hypothesize.** State ONE specific mechanism: "X is None here because
   Y never ran", not "something's wrong with the data". A hypothesis must
   be falsifiable by a single observation.
4. **Fix.** Change the smallest thing that addresses the mechanism. One
   hypothesis, one change, then re-run the repro.
5. **Verify.** The repro passes, the full relevant test suite passes, and
   you can explain *why* the fix works, not just that it does.

## Reading tracebacks

Read bottom-up: the last frame is where it blew up, the frames above are
how it got there. The exception line tells you *what* broke
(`KeyError: 'x'`); the frames tell you *whose* assumption broke. Check
arguments at each frame boundary — most bugs are a wrong value crossing a
boundary, not broken logic inside a function.

## Tactics

- **Bisect the input**, not just the code: halve the failing input until
  the minimal trigger remains.
- **Check the recent:** `git log` / `git diff` on the area. Most bugs were
  introduced recently, by someone, on purpose (a change with a reason).
- **Rubber-duck the invariant:** for each function on the path, state what
  it promises about its inputs and outputs. The bug is where a promise is
  broken.
- **Distinguish once vs always:** fails every time (logic bug) vs
  intermittently (state, ordering, timing, shared mutable data).
- **Temporary instrumentation is fine;** leaving it in is not. Remove
  debug prints before you finish, or convert the useful ones into a
  regression test.

## When stuck

Explain the mechanism out loud (in your reply): what you expected at each
step, what you observed instead, and which hypothesis the evidence kills.
If two hypotheses remain, design the single cheapest observation that
distinguishes them.
