"""Case storage: a local JSONL log plus keyword-overlap retrieval.

Case schema (v1)::

    {
      "id": "case-20261012-0001",
      "timestamp": "2026-10-12T10:15:00",
      "task": "add --version flag to cli.py",
      "plan_summary": "read argparse setup; add flag; smoke-test",
      "trace": [
        {"tool": "read", "args_summary": "path='cli.py'", "ok": True},
        {"tool": "edit", "args_summary": "path='cli.py' …", "ok": True},
        {"tool": "bash", "args_summary": "command='python3 tests/…'", "ok": True},
      ],
      "outcome": {
        "success": True,
        "tests_passed": True,
        "turns": 4,
        "latency_s": 32.5,
      },
      "project_kind": "python-cli",
      "embedding": None,  # reserved: fixed-size vector for the v2 scorer
      "strategies": [     # dual memory (ReasoningBank): transferable
        "Explored the codebase (read/grep/glob) before making edits.",
      ],                  # strategies distilled from successes
      "pitfalls": [       # corrective rules distilled from failures
        "Tool 'edit' failed (args: path='x') — check that the exact "
        "old_text exists in the file first.",
      ],
    }

Retrieval (v1) is stdlib keyword overlap: tokenize, drop stopwords, score
query tokens against the case's task text (2x weight) and plan summary
(1x). Successes always rank above failures; zero-overlap cases are
excluded.

EMBEDDING UPGRADE PATH (v2): replace ``_score`` with cosine similarity
over ``case["embedding"]`` (a list of floats stored in the same schema —
no migration needed). A tiny local embedder (or the provider's embeddings
endpoint) fills the field at ``add_case`` time; cases written by v1 keep
``embedding: None`` and fall back to keyword scoring.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List

_STOPWORDS = frozenset(
    """
    a an and are as at be by can do does for from had has have how i in is
    it its me my of on or our so that the their them there these they this
    to up was we were will with you your me into out over just than then
    also please could should would there here when what which who whom
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return [
        tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS
    ]


def _score(query: str, case: Dict) -> float:
    """Keyword-overlap score, weighted toward the task text."""
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    task_tokens = _tokenize(case.get("task", ""))
    plan_tokens = _tokenize(case.get("plan_summary", "") or "")
    score = 0.0
    for tok in query_tokens:
        # TF-ish: capped multiplicity so repeated words can't dominate.
        score += 2.0 * min(task_tokens.count(tok), 3)
        score += 1.0 * min(plan_tokens.count(tok), 3)
    return score


class CaseStore:
    """Append-only JSONL case log with similarity retrieval."""

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    # ------------------------------------------------------------ write ---

    def add_case(self, case: Dict) -> Dict:
        """Assign id + timestamp, append to the log, return the stored case.

        Backward compatible: cases written before the dual-memory fields
        existed get empty ``strategies``/``pitfalls`` lists.
        """
        stored = dict(case)
        stored.setdefault(
            "id",
            "case-{date}-{seq:04d}".format(
                date=datetime.date.today().strftime("%Y%m%d"),
                seq=len(self.all_cases()) + 1,
            ),
        )
        stored.setdefault(
            "timestamp", datetime.datetime.now().isoformat(timespec="seconds")
        )
        stored.setdefault("embedding", None)
        stored.setdefault("strategies", [])
        stored.setdefault("pitfalls", [])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(stored, ensure_ascii=False) + "\n")
        return stored

    # ------------------------------------------------------------- read ---

    def all_cases(self) -> List[Dict]:
        """Every case in insertion order; [] when the log is missing."""
        if not os.path.isfile(self.path):
            return []
        cases = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        cases.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a corrupt line never breaks the log
        return cases

    def find_similar(self, query: str, k: int = 3, min_overlap: float = 0.0) -> List[Dict]:
        """Top-k similar cases: successes first, then score, no zero-hits.

        *min_overlap*: the share of the query's distinct words a case's task must contain (one
        common word is not similarity). Returns [] for an empty store or when nothing overlaps.
        """
        wanted = set(_tokenize(query))
        scored = []
        for case in self.all_cases():
            score = _score(query, case)
            if score <= 0:
                continue
            if min_overlap and wanted:
                shared = wanted & set(_tokenize(case.get("task", "")))
                if len(shared) < 2 or len(shared) / len(wanted) < min_overlap:
                    continue
            success = bool((case.get("outcome") or {}).get("success"))
            scored.append((success, score, case))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [case for _, _, case in scored[:k]]
