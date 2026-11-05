---
name: todo-scan
description: Find and triage TODO/FIXME comments in the codebase.
---

Scan the codebase for outstanding work markers and triage them.
Scope ($ARGUMENTS) may be a path to limit the scan; empty means the
whole project.

Scope: $ARGUMENTS

1. **Find them.** `grep -rn` for `TODO`, `FIXME`, `HACK`, `XXX`,
   `BUG` in code comments. Skip `.git/`, `node_modules/`,
   vendored/third-party dirs, and generated files.
2. **Read each in context.** A marker without context is noise —
   look at the surrounding code to understand what was deferred and why.
3. **Triage into three piles:**
   - **Real work:** still relevant, actionable. State what the task
     actually is in one line each.
   - **Stale:** the code moved on and the marker no longer applies,
     or it describes something already done. List these for deletion.
   - **Wontfix-ish:** understood and deliberately deferred (e.g.
     "HACK: works around old API, remove when v2 ships"). Leave
     these, but say why.
4. **Report.** Three sections matching the piles, each entry as
   `path:line` + one line of substance. End with counts.

Don't convert anything into tracked todos or edit files yourself —
triage is the job. The user decides what becomes real work.
