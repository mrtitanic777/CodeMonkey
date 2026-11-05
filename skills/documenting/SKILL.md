---
name: documenting
description: Use when writing or updating documentation, docstrings, or README sections, or when deciding whether a change needs docs.
keywords: [docs, documentation, docstring, readme, comments]
---

# Documenting

Docs are a user interface. Write them for the reader who arrives confused
at 2am, not for the author who just finished the code.

## Docstrings

Every public module, class, and function gets a docstring:

- **Module:** one paragraph — what lives here and why it exists.
- **Function:** what it does, its arguments' shapes, what it returns, and
  what it raises. Document the *contract*, not the implementation steps.
- **Tricky internals** get a short comment explaining *why*, never *what*
  (the code already says what).

Match the repo's voice: plain sentences, no jargon without definition,
examples where the shape isn't obvious.

## README updates

The README is the project's front door. Update it when you:

- add a subsystem, entry point, or config section,
- change how something is run or configured,
- add a test suite or change what the suites cover.

Keep the architecture tables honest: if the code changed, the table
changed. A stale README is worse than no README — it teaches wrong
things confidently.

## When docs are required with a change

Ask: "Could someone use or debug this without reading the code?" If no,
it needs docs. Concretely:

- New tool → docstring + registry description (the description *is* the
  model's documentation; write it as instructions).
- New config key → `config.yaml` comment + README section.
- New event/hook point → event catalog entry.
- New test suite → how to run it, in the README and the file's docstring.

## What not to document

Implementation details that will rot: line-by-line walkthroughs of
stable code, exhaustive option lists duplicated from `--help`, and
aspirational "planned features" sections. Document what *is*, not what
might be. Delete docs for removed features in the same change that
removes them.
