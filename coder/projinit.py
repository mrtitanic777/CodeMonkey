"""/init: write (or improve) the project's AGENTS.md, the rules file every session loads.

The facts are gathered here first, without the model: which languages, how the project is built,
tested and linted (package.json scripts, pyproject.toml, Makefile targets, Cargo, Go, .NET...),
how it is laid out, which CI and style tools it uses, how its commit messages read, and what the
rule files it may already have (AGENTS.md, CLAUDE.md, .cursorrules, Copilot instructions) say.
The model then checks what it needs to (it can read and run) and writes the file, so what ends up
there is what this project really does, not a template.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from typing import Dict, List

_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".next", ".idea",
         ".vscode", ".pytest_cache", "coverage", ".mypy_cache", "out", "bin", "obj"}
_LANG = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
         ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".cpp": "C++", ".cc": "C++",
         ".c": "C", ".h": "C/C++ headers", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".m": "Objective-C",
         ".scala": "Scala", ".lua": "Lua", ".sh": "Shell", ".ps1": "PowerShell", ".html": "HTML", ".css": "CSS",
         ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart", ".ex": "Elixir", ".zig": "Zig"}
_RULE_FILES = ["AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md", "CONTRIBUTING.md"]
_STYLE = ["ruff.toml", ".ruff.toml", ".flake8", "setup.cfg", "tox.ini", ".pylintrc", "mypy.ini", ".eslintrc",
          ".eslintrc.js", ".eslintrc.json", "eslint.config.js", ".prettierrc", "prettier.config.js", "biome.json",
          ".editorconfig", "rustfmt.toml", ".golangci.yml", ".pre-commit-config.yaml", "tsconfig.json"]


def _read(path: str, limit: int = 6000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def scan(project_dir: str) -> Dict[str, object]:
    from coder.tools import _load_gitignore, _walk_files

    root = os.path.abspath(project_dir)
    langs: Counter = Counter()
    top: Dict[str, int] = {}
    files = 0
    for _abs, rel in _walk_files(root, _load_gitignore(root) + [d + "/" for d in _SKIP]):   # what git tracks
        files += 1
        first = rel.split("/")[0] if "/" in rel else "(top level)"
        top[first] = top.get(first, 0) + 1
        ext = os.path.splitext(rel)[1].lower()
        if ext in _LANG:
            langs[_LANG[ext]] += 1
        if files > 20000:
            break
    facts: Dict[str, object] = {
        "files": files,
        "languages": [f"{lang} ({n} files)" for lang, n in langs.most_common(6)],
        "layout": [f"{d}/ ({n} files)" if d != "(top level)" else f"top level ({n} files)"
                   for d, n in sorted(top.items(), key=lambda kv: -kv[1])[:15]],
        "commands": _commands(root),
        "ci": [p for p in ("Makefile", ".github/workflows", ".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile",
                           ".circleci") if os.path.exists(os.path.join(root, p))],
        "style_tools": [p for p in _STYLE if os.path.exists(os.path.join(root, p))],
        "rule_files": {p: _read(os.path.join(root, p), 4000) for p in _RULE_FILES if os.path.isfile(os.path.join(root, p))},
        "readme": _read(os.path.join(root, "README.md"), 2500) or _read(os.path.join(root, "README.rst"), 2500),
        "commits": _commits(root),
    }
    return facts


def _commands(root: str) -> List[str]:
    out: List[str] = []
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            data = json.loads(_read(pkg, 100_000) or "{}")
        except ValueError:
            data = {}
        manager = "pnpm" if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")) else \
            "yarn" if os.path.isfile(os.path.join(root, "yarn.lock")) else \
            "bun" if os.path.isfile(os.path.join(root, "bun.lockb")) else "npm"
        for name, script in list((data.get("scripts") or {}).items())[:15]:
            out.append(f"{manager} run {name}  ->  {script}")
    py = _read(os.path.join(root, "pyproject.toml"), 20000)
    if py:
        out.append("pyproject.toml: " + ", ".join(sorted(set(re.findall(r"^\[tool\.([\w-]+)", py, re.M))))[:200])
        if "pytest" in py:
            out.append("python -m pytest")
    if os.path.isfile(os.path.join(root, "requirements.txt")):
        out.append("pip install -r requirements.txt")
    if os.path.isdir(os.path.join(root, "tests")) and not any("pytest" in c for c in out) and \
            any(n.startswith("test_") and n.endswith(".py") for n in os.listdir(os.path.join(root, "tests"))):
        out.append("tests/test_*.py present (pytest or unittest)")
    make = _read(os.path.join(root, "Makefile"), 20000)
    if make:
        targets = [t for t in re.findall(r"^([A-Za-z][\w.-]*):(?!=)", make, re.M) if t != "PHONY"][:15]
        out.extend(f"make {t}" for t in targets)
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        out += ["cargo build", "cargo test", "cargo clippy"]
    if os.path.isfile(os.path.join(root, "go.mod")):
        out += ["go build ./...", "go test ./...", "go vet ./..."]
    if any(n.endswith((".csproj", ".sln")) for n in os.listdir(root)):
        out += ["dotnet build", "dotnet test"]
    if os.path.isfile(os.path.join(root, "Gemfile")):
        out += ["bundle install", "bundle exec rake"]
    return out


def _commits(root: str) -> List[str]:
    try:
        proc = subprocess.run(["git", "-C", root, "log", "-15", "--format=%s"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()][:15] if proc.returncode == 0 else []


def prompt(project_dir: str, focus: str = "") -> str:
    """The message /init sends: the facts, and what to write."""
    facts = scan(project_dir)
    existing = facts["rule_files"].get("AGENTS.md")  # type: ignore[union-attr]
    parts = [
        "Write the file AGENTS.md at the top of this project: the instructions every coding session here "
        "reads first. " + ("It exists already (below): improve it, keep what is still right, fix what is "
                           "wrong, and say what you changed." if existing else "It does not exist yet."),
        "",
        "What I found without a model (check anything you rely on; run a command if you need to know it works):",
        f"- {facts['files']} files. Languages: {', '.join(facts['languages']) or 'none recognised'}.",  # type: ignore[arg-type]
        "- Layout: " + "; ".join(facts["layout"]),  # type: ignore[arg-type]
        "- Build, test and lint commands seen: " + ("; ".join(facts["commands"]) or "none found"),  # type: ignore[arg-type]
        "- CI: " + (", ".join(facts["ci"]) or "none"),  # type: ignore[arg-type]
        "- Style and lint config: " + (", ".join(facts["style_tools"]) or "none"),  # type: ignore[arg-type]
    ]
    if facts["commits"]:
        parts.append("- Recent commit subjects (for the message style): " + " | ".join(facts["commits"][:10]))  # type: ignore[index]
    for name, text in facts["rule_files"].items():  # type: ignore[union-attr]
        parts += ["", f"--- {name} (as it is now) ---", text.strip()]
    if facts["readme"]:
        parts += ["", "--- README (start) ---", str(facts["readme"]).strip()]
    parts += [
        "",
        "AGENTS.md should hold, in this order, only what a newcomer could not guess:",
        "1. What the project is, in two or three sentences.",
        "2. Commands: install, build, run, test (all, and one file or one test), lint and format. Exactly as "
        "typed, and only ones you have seen in the project files or checked.",
        "3. Layout: the main folders and what lives where; where the entry points are.",
        "4. Conventions: code style the tools do not enforce, naming, how errors and logging are done, how "
        "tests are written, commit message style.",
        "5. Things that trip people up: generated files not to edit, slow or flaky tests, required services, "
        "platform quirks.",
        "Keep it under about 120 lines of plain Markdown. No marketing, no generic advice ('write clean code'), "
        "nothing you did not see. Write it with write_file (or edit if it exists), then reply with a short "
        "summary of what it says and anything you could not confirm.",
    ]
    if focus:
        parts += ["", f"Pay special attention to: {focus}"]
    return "\n".join(parts)
