"""Memory: curated long-term facts plus episodic daily logs.

- ``MEMORY.md`` is the curated notebook: durable facts about the user.
- ``memory/YYYY-MM-DD.md`` files are daily logs: things that happened.
- :meth:`MemoryManager.remember` / :meth:`MemoryManager.recall` are the API.
- :meth:`MemoryManager.summary` produces the short text injected into the
  agent's system prompt so it starts each conversation with context.
"""

from __future__ import annotations

import datetime
import os
import re
from typing import List

_MEMORY_HEADER = """# Memory

Curated facts about the user. One fact per line, newest at the bottom.
Keep entries short and durable (preferences, projects, people, decisions).
Ephemeral chatter belongs in the daily logs, not here.

---
"""


class MemoryManager:
    """Curated + episodic memory stored as markdown under a data dir."""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.mem_dir = os.path.join(self.data_dir, "memory")
        os.makedirs(self.mem_dir, exist_ok=True)
        self.memory_file = os.path.join(self.mem_dir, "MEMORY.md")
        if not os.path.exists(self.memory_file):
            with open(self.memory_file, "w", encoding="utf-8") as fh:
                fh.write(_MEMORY_HEADER)

    # ------------------------------------------------------------ write ---

    def remember(self, fact: str) -> str:
        """Append a durable fact to MEMORY.md. Returns the stored line."""
        fact = " ".join(fact.split())
        if not fact:
            raise ValueError("Cannot remember an empty fact")
        today = datetime.date.today().isoformat()
        line = f"- [{today}] {fact}\n"
        with open(self.memory_file, "a", encoding="utf-8") as fh:
            fh.write(line)
        return line.strip()

    def log_daily(self, text: str) -> str:
        """Append an entry to today's daily log. Returns the log path."""
        text = text.strip()
        if not text:
            raise ValueError("Cannot log empty text")
        today = datetime.date.today().isoformat()
        path = os.path.join(self.mem_dir, f"{today}.md")
        stamp = datetime.datetime.now().strftime("%H:%M")
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write(f"# {today}\n\n")
            fh.write(f"## {stamp}\n{text}\n\n")
        return path

    def forget(self, query: str) -> int:
        """Remove MEMORY.md lines matching *query*. Returns lines removed."""
        tokens = [t.lower() for t in re.findall(r"[a-z0-9]+", query)]
        if not tokens:
            return 0
        with open(self.memory_file, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [
            ln
            for ln in lines
            if not (ln.startswith("- [") and all(t in ln.lower() for t in tokens))
        ]
        removed = len(lines) - len(kept)
        if removed:
            with open(self.memory_file, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
        return removed

    # ------------------------------------------------------------- read ---

    def _search_files(self, tokens: List[str], limit: int) -> List[str]:
        hits: List[str] = []
        files = [self.memory_file] + sorted(
            os.path.join(self.mem_dir, f)
            for f in os.listdir(self.mem_dir)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f)
        )
        for path in files:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            for line in content.splitlines():
                s = line.strip()
                if (
                    len(s) > 2
                    and all(t in s.lower() for t in tokens)
                    and s not in hits
                ):
                    hits.append(s)
                    if len(hits) >= limit:
                        return hits
        return hits

    def recall(self, query: str, limit: int = 10) -> List[str]:
        """Keyword-search MEMORY.md and daily logs. Returns matching lines."""
        tokens = [t.lower() for t in re.findall(r"[a-z0-9]+", query)]
        if not tokens:
            return []
        return self._search_files(tokens, limit)

    def summary(self, max_chars: int = 1500) -> str:
        """Short memory digest for injection into the system prompt."""
        with open(self.memory_file, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # Drop the header boilerplate; keep the actual facts.
        if "---" in text:
            text = text.split("---", 1)[1]
        text = text.strip() or "(no memories stored yet)"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"
        return text
