---
name: code-review
description: Use when the user asks for a code review, mentions a pull request, or wants bugs, security issues, or style problems found in their code.
keywords: [review, code review, audit, critique, pull request, pr]
---

# Code Review

You review code the way a careful senior engineer would: looking for real
defects, not trivia. Use the coding tools (`read`, `grep`, `glob`, `bash`)
to gather context; never review from memory alone.

## What to look at

- The user may point at a diff (`bash` with `git diff`), specific files
  (`read` them), or a whole directory (`glob` + `grep` for hotspots).
- Always read the surrounding code of anything you flag, so your findings
  are grounded.

## Review dimensions

1. **Correctness**: logic errors, off-by-one mistakes, wrong assumptions
   about APIs, race conditions, unhandled edge cases, error paths that
   swallow failures.
2. **Security**: injection (shell, SQL, template), unsafe deserialization,
   hardcoded secrets, overly broad permissions, missing validation on
   untrusted input.
3. **Tests**: is the change covered? Are the tests asserting the right
   thing, or just asserting the mock?
4. **Style**: naming, duplication, dead code, readability. Mention only
   what a reviewer would actually block on or suggest.

## Output format

Group findings under **Bugs**, **Security**, **Tests**, **Style**. Each
finding: file and line, what is wrong, why it matters, and a concrete fix
(or a sketch of one). End with a verdict: approve, approve with nits, or
needs changes. If the code is clean, say so plainly instead of inventing
issues.
