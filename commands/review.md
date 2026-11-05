---
name: review
description: Review the working-tree diff for bugs, security issues, and style.
---

Review the current uncommitted changes carefully.

1. Run `bash` with `git diff` to get the full diff (use `git diff --stat` first if it might be large).
2. For context on anything unclear, use `read` and `grep` to look at the surrounding code.
3. Report your findings grouped as:
   - **Bugs**: logic errors, off-by-ones, unhandled edge cases, broken error paths.
   - **Security**: injection risks, unsafe shell use, secrets in code, weak validation.
   - **Style**: naming, duplication, dead code, anything that hurts readability.
4. For each finding give the file and line, what is wrong, and a concrete fix.
5. End with a one-line verdict: approve, approve with nits, or needs changes.

Focus on real issues, not trivia. Extra instructions from the user: $ARGUMENTS
