---
name: deps
description: Audit project dependencies for outdated or vulnerable packages.
---

Audit the project's dependencies. Scope ($ARGUMENTS) may name a
manifest or ecosystem (e.g. `python`, `node`); empty means check
everything found.

Scope: $ARGUMENTS

1. **Find the manifests.** Look for `requirements*.txt`,
   `pyproject.toml`, `Pipfile`, `package.json` (+ lockfiles),
   `Cargo.toml`, `go.mod`. Audit what's actually installed against
   what's pinned.
2. **Outdated.** Python: `pip list --outdated`. Node: `npm outdated`.
   Report package, installed version, latest version.
3. **Vulnerable.** Python: `pip-audit` if installed — if it isn't,
   say so and suggest `pip install pip-audit` rather than guessing.
   Node: `npm audit`. Report severity, affected versions, and whether
   a patched release exists.
4. **Summarize.** A compact table: package | installed | latest |
   status (ok / outdated / vulnerable:severity). Flag major-version
   jumps separately — those need a changelog read before upgrading.

Recommend upgrades conservatively: patch and minor bumps are usually
safe; majors get a "check the changelog" note. Never upgrade anything
yourself — this command reports; the user decides.

If no manifests are found, say so in one line and stop.
