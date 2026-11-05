---
name: debug
description: Run the systematic debugging workflow on a described symptom.
---

Debug this symptom: $ARGUMENTS

Follow the debugging loop — do not guess at fixes:

1. **Reproduce.** Find the smallest command, test, or input that triggers
   it. Use `bash` to run it and `read`/`grep` to inspect. Write down the
   exact repro.
2. **Isolate.** Binary-search the code path: which half still fails?
   Check values at function boundaries.
3. **Hypothesize.** State ONE falsifiable mechanism for what's wrong.
4. **Fix.** The smallest change addressing that mechanism, then re-run
   the repro.
5. **Verify.** Repro passes, related test suites pass. If this was a real
   bug (not user error), add a regression test so it stays fixed.

Report as you go: what you expected, what you observed, and which
hypotheses the evidence kills.
