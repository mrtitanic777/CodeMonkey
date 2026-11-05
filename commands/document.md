---
name: document
description: Generate or update documentation for the given paths.
---

Write or update documentation for: $ARGUMENTS

1. **Read first.** Use `read`/`glob`/`grep` to understand what the code
   actually does — never document from memory or from names alone.
2. **Docstrings.** Every public module/class/function touched gets a
   docstring covering the contract: what it does, argument shapes, return
   value, what it raises. Match the repo's plain-spoken voice.
3. **README.** If this adds a subsystem, entry point, config key, or test
   suite, update the README's architecture tables and setup docs in the
   same change. A stale README teaches wrong things confidently.
4. **Prune.** Delete docs for anything you removed. Don't document
   internals that will rot or features that don't exist yet.

Keep it tight: document what *is*, for the reader arriving confused.
