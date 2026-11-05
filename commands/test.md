---
name: test
description: Run the project's test suite and summarize the results.
---

Run the project's tests and report what happened.

1. Figure out how tests run here: use `glob` to look for clues (`pytest.ini`, `tox.ini`, `pyproject.toml`, `package.json` scripts, `Makefile`, a `tests/` directory).
2. Run the suite with `bash` (for example `python -m pytest -q` or `npm test`). Prefer the project's own runner over guessing.
3. Summarize: total passed / failed / skipped, and for each failure the file, the test name, and the key error lines.
4. Do not fix failures unless asked.

Extra instructions from the user: $ARGUMENTS
