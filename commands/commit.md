---
name: commit
description: Draft a commit message from the working tree and create a focused commit.
---

Review the working tree and create one focused commit.

1. Run `bash` with `git status --short` to see what changed.
2. Run `git diff --stat`, then `git diff` for the full picture (use `read` on individual files if the diff is huge).
3. Draft the message: a short imperative subject line (<= 72 chars), a blank line, then a body explaining *why* the change exists, not just what it does.
4. Stage exactly the files belonging to one logical change (`git add <paths>`). Leave unrelated changes unstaged.
5. Commit with `git commit -m`. Never commit secrets, `.env` files, or credentials.

Extra instructions from the user: $ARGUMENTS
