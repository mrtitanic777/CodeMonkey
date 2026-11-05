"""Skills: lazily-loaded markdown playbooks.

A skill is a directory containing a ``SKILL.md`` file with YAML frontmatter::

    ---
    name: notes
    description: Save and search quick notes.
    keywords: [note, remember, snippet]
    ---
    # instructions ...

:func:`load_skills` scans a directory for skills. :func:`skill_search`
keyword-matches a query against skill names/descriptions so the agent can
inject only the *relevant* skill bodies into its system prompt instead of
loading everything up front.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Tuple

import mini_yaml


@dataclass
class Skill:
    """One skill playbook."""

    name: str
    description: str
    path: str
    keywords: Tuple[str, ...] = ()
    _body: str = field(default=None, repr=False)

    @property
    def body(self) -> str:
        """Lazily read the full SKILL.md body (without frontmatter)."""
        if self._body is None:
            with open(self.path, "r", encoding="utf-8") as fh:
                text = fh.read()
            self._body = _strip_frontmatter(text).strip()
        return self._body


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :])
    return text


def _parse_skill(skill_md: str) -> Skill | None:
    try:
        with open(skill_md, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    front = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                try:
                    front = mini_yaml.parse("\n".join(lines[1:i])) or {}
                except ValueError:
                    front = {}
                break
    name = str(front.get("name", "")).strip()
    desc = str(front.get("description", "")).strip()
    if not name:
        return None
    keywords = front.get("keywords") or []
    if isinstance(keywords, str):
        # The small YAML reader hands an inline list ("[a, b]") back as one string.
        text = keywords.strip()
        if text.startswith("[") and text.endswith("]"):
            keywords = [k.strip().strip("'\"") for k in text[1:-1].split(",") if k.strip()]
        else:
            keywords = [keywords]
    return Skill(
        name=name,
        description=desc,
        path=skill_md,
        keywords=tuple(str(k) for k in keywords),
    )


def load_skills(skills_dir: str) -> List[Skill]:
    """Load every skill found directly under *skills_dir*."""
    skills: List[Skill] = []
    if not os.path.isdir(skills_dir):
        return skills
    for entry in sorted(os.listdir(skills_dir)):
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if os.path.isfile(skill_md):
            skill = _parse_skill(skill_md)
            if skill is not None:
                skills.append(skill)
    return skills


_TOKEN = re.compile(r"[a-z0-9]+")


def skill_search(query: str, skills: List[Skill], top_n: int = 3) -> List[Skill]:
    """Keyword-match *query* against skill names/descriptions.

    Name hits weigh most, then keywords, then description. Returns up to
    *top_n* skills with a positive score, best first.
    """
    tokens = [t for t in _TOKEN.findall(query.lower()) if len(t) > 2]
    if not tokens or not skills:
        return []
    scored = []
    for skill in skills:
        name = skill.name.lower()
        desc = skill.description.lower()
        kws = " ".join(skill.keywords).lower()
        score = 0
        for tok in tokens:
            if tok in name:
                score += 3
            if tok in kws:
                score += 2
            if tok in desc:
                score += 1
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [skill for _, skill in scored[:top_n]]
