---
name: notes
description: Use when the user wants to save, list, or search quick notes, ideas, or snippets so nothing gets lost.
keywords: [note, notes, jot, remember this, snippet, idea, memo]
---

# Notes

You keep the user's quick notes in a single file: `notes.md` in the agent
workspace. Use the file tools (`read_file`, `write_file`, `edit_file`) to
manage it. The file may not exist yet; create it when the first note arrives.

## File format

One note per section, newest at the top:

```markdown
# Notes

## 2026-10-12 — Book idea
A novel about a cartographer who maps cities that don't exist yet.

## 2026-10-11 — Gift idea for Sam
...
```

Keep each note short. A one-line title after the date, then the body.

## When to use this skill

- The user says "note this", "remember this", "jot down", or shares an
  idea/snippet they may want later.
- The user asks "what were my notes about X?" or "show my notes".

## Saving a note

1. Read `notes.md` first (if it exists) so you don't clobber anything.
2. Prepend the new note under the `# Notes` heading with today's date.
3. Confirm briefly what you saved.

If the note is really a durable fact about the user (a preference, a
birthday, a project), prefer the `/remember` memory command instead of a
note. Notes are for ephemeral stuff: ideas, quotes, links, todos.

## Finding notes

- To list everything: read the whole file and summarize.
- To search: read the file and scan for the keyword yourself, then quote
  the matching notes back. (There is no separate search tool; the file is
  small enough to read fully.)
- If the file is getting long (over ~200 notes), suggest archiving the
  oldest ones into `notes-archive.md`.
