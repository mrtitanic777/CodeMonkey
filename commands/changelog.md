---
name: changelog
description: Draft a concise changelog from git history.
---

Write a changelog for the given range. Arguments ($ARGUMENTS) may be a
git range like `v1.2.0..HEAD`, a tag, or empty.

Range: $ARGUMENTS

1. **Determine the range.** If arguments name a range or ref, use it.
   Otherwise use commits since the most recent tag
   (`git describe --tags --abbrev=0`); if there are no tags, use the
   last 20 commits.
2. **Read the history.** `git log --oneline <range>`. Skip merge
   commits unless a merge carried real work with no other record.
3. **Group, don't list.** Organize under `### Features`, `### Fixes`,
   `### Other`. One line per change, present tense, no fluff
   ("Add retry to the websocket client", not "Implemented various
   improvements to connection handling").
4. **Collapse noise.** Fold typo fixes, lint-only commits, and
   reverts-of-reverts into the line they belong with. If a revert
   stands alone, say what was reverted and why in one line.
5. **Keep references.** Preserve PR/issue numbers from the commit
   messages (`(#123)`).

Output markdown, ready to paste into a release:

## <version or range>

### Features
- ...

### Fixes
- ...

### Other
- ...

If the range contains no meaningful changes, say so in one line
instead of inventing entries.
