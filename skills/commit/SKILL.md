---
name: commit
description: Use when the user wants to create a git commit, stage changes, or needs help writing a good commit message.
keywords: [commit, git commit, commit message, staging]
---

# Commit

You help the user make commits that are easy to review and safe to share.
Use `bash` for git operations and `read`/`grep` to understand what changed.

## Message format

- Subject: imperative mood, <= 72 characters, no trailing period.
  Good: `Add retry with backoff to the fetcher`
  Bad: `fixed stuff`, `WIP`, `Adds retries.`
- Blank line, then a body explaining *why* the change exists. The diff
  shows *what*; the message should answer *why*.
- Reference issue numbers when the user gives them.

## Hygiene rules

- One logical change per commit. If the working tree holds unrelated
  changes, stage only the related files (`git add <paths>`) and leave the
  rest alone. Say what you left unstaged.
- Run the relevant tests (`bash`) before committing when the change is
  behavioral.
- Never commit secrets: scan the diff for API keys, tokens, passwords,
  `.env` files, and private keys. If you find any, stop and warn the user
  instead of committing.
- Never use `git commit -a` blindly and never `git push --force` unless the
  user explicitly asks.
- If there is nothing to commit, say so instead of creating an empty one.
