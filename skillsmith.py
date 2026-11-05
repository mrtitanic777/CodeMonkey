"""Skillsmith: mine the case store for repeated workflows, propose skills.

The harness extends its own capabilities: tool-use patterns that recur
across *successful* cases are distilled into SKILL.md proposals. The loop
is human-in-the-loop by design — proposals land in ``skills/proposals/``
and are NEVER auto-installed. A person reviews them and promotes the good
ones into ``skills/``.

Pipeline::

    cases.jsonl --mine--> patterns --propose--> skills/proposals/<name>/SKILL.md

Mining is deliberately simple and deterministic (contiguous tool n-grams
with a support threshold, maximality filtering). No model calls, no
embeddings — the proposals are grounded in what actually worked.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

# ---------------------------------------------------------------- constants

MIN_NGRAM = 2
MAX_NGRAM = 5

_STOPWORDS = frozenset(
    """
    a an and are as at be by can do does for from had has have how i in is
    it its me my of on or our so that the their them there these they this
    to up was we were will with you your into out over just than then
    also please could should would when what which who whom will add fix
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Gerund-style step phrases per tool; unknown tools fall back to "Use `x`".
_TOOL_STEPS = {
    "read": "Read the relevant files to understand the current code",
    "glob": "Locate the relevant files",
    "grep": "Search the codebase for the relevant symbols or patterns",
    "edit": "Make the targeted edit",
    "write_file": "Write the new file",
    "bash": "Run the verification command (tests or build)",
    "todo_write": "Record the plan as tracked todos",
    "todo_list": "Check progress against the todo list",
    "delegate": "Delegate a well-scoped subtask to a subagent",
}

# ---------------------------------------------------------------- helpers


def _tokenize(text: str) -> List[str]:
    return [
        tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS
    ]


def _successful(cases: List[Dict]) -> List[Dict]:
    """Cases whose outcome records success."""
    return [c for c in cases if (c.get("outcome") or {}).get("success")]


def _tool_sequence(case: Dict) -> List[str]:
    return [e.get("tool", "?") for e in case.get("trace", []) if e.get("tool")]


def _shape_args(args_summary: str) -> str:
    """Generalize an args summary: values become placeholders.

    ``path='coder.py', n=3`` -> ``path='<str>', n=<n>``.
    """
    s = re.sub(r"'[^']*'", "'<str>'", args_summary or "")
    s = re.sub(r"\b\d+\b", "<n>", s)
    return s


def _sanitize_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    return name or "skill"


# ---------------------------------------------------------------- mining


def mine_patterns(cases: List[Dict], min_support: int = 3) -> List[Dict]:
    """Find repeated tool n-grams (length 2-5) across successful cases.

    Support = number of *cases* containing the n-gram (one vote per case,
    however many times it occurs inside). Shorter n-grams fully explained
    by a longer one with equal support are dropped (maximality), so one
    skill is proposed per workflow, not per prefix.

    Returns pattern dicts sorted by support desc, then length desc::

        {"tools": ("read", "edit", "bash"), "support": 4,
         "example_tasks": [...], "arg_shapes": [...],
         "strategies": [...], "pitfalls": [...]}
    """
    cases = _successful(cases)
    if not cases:
        return []

    # count: ngram -> list of case indexes containing it
    counts: Dict[Tuple[str, ...], List[int]] = {}
    for idx, case in enumerate(cases):
        seq = _tool_sequence(case)
        seen = set()
        for n in range(MIN_NGRAM, MAX_NGRAM + 1):
            for i in range(len(seq) - n + 1):
                ng = tuple(seq[i : i + n])
                if ng not in seen:
                    seen.add(ng)
                    counts.setdefault(ng, []).append(idx)

    candidates = [
        (ng, idxs) for ng, idxs in counts.items() if len(idxs) >= min_support
    ]
    # longest first, then best supported: maximality compares against kept
    candidates.sort(key=lambda item: (-len(item[0]), -len(item[1])))

    kept: List[Tuple[Tuple[str, ...], List[int]]] = []
    for ng, idxs in candidates:
        redundant = False
        for lng, lidxs in kept:
            if len(lng) <= len(ng):
                continue
            # is ng a contiguous subsequence of lng with equal support?
            for i in range(len(lng) - len(ng) + 1):
                if tuple(lng[i : i + len(ng)]) == ng and len(lidxs) == len(
                    idxs
                ):
                    redundant = True
                    break
            if redundant:
                break
        if not redundant:
            kept.append((ng, idxs))

    # most supported first for presentation
    kept.sort(key=lambda item: (-len(item[1]), -len(item[0])))

    patterns = []
    for ng, idxs in kept:
        supporting = [cases[i] for i in idxs]
        tasks = [c.get("task", "") for c in supporting if c.get("task")]
        # arg shapes: most common generalized shape per n-gram position
        arg_shapes = []
        for pos in range(len(ng)):
            shapes: Dict[str, int] = {}
            for c in supporting:
                seq = _tool_sequence(c)
                # first occurrence of the n-gram in this case
                for i in range(len(seq) - len(ng) + 1):
                    if tuple(seq[i : i + len(ng)]) == ng:
                        trace = c.get("trace", [])
                        shapes[_shape_args(
                            trace[i + pos].get("args_summary", "")
                        )] = shapes.get(
                            _shape_args(trace[i + pos].get("args_summary", "")),
                            0,
                        ) + 1
                        break
            best = max(shapes, key=shapes.get) if shapes else ""
            arg_shapes.append(best)
        strategies: List[str] = []
        pitfalls: List[str] = []
        for c in supporting:
            for s in c.get("strategies") or []:
                if s not in strategies:
                    strategies.append(s)
            for p in c.get("pitfalls") or []:
                if p not in pitfalls:
                    pitfalls.append(p)
        patterns.append(
            {
                "tools": ng,
                "support": len(idxs),
                "example_tasks": tasks[:3],
                "arg_shapes": arg_shapes,
                "strategies": strategies[:3],
                "pitfalls": pitfalls[:3],
            }
        )
    return patterns


# ---------------------------------------------------------------- proposing


def _situation(tools: Tuple[str, ...]) -> str:
    """Describe the SITUATION the workflow serves (house rule: no keyword
    stuffing — describe when it applies, don't repeat trigger words)."""
    tset = set(tools)
    if {"edit", "bash"} <= tset or {"write_file", "bash"} <= tset:
        return "making a focused code change and verifying it with tests or a build"
    if tset and tset <= {"glob", "grep", "read"}:
        return "exploring an unfamiliar codebase to understand how something works"
    if "todo_write" in tset:
        return "working through a multi-step task that benefits from tracked progress"
    if "delegate" in tset:
        return "splitting work into subtasks handled in parallel"
    return "carrying out this repeated multi-step workflow"


def _skill_name(tools: Tuple[str, ...], taken: set) -> str:
    base = _sanitize_name("-".join(tools))
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    taken.add(name)
    return name


def propose_skill(pattern: Dict, taken: set) -> Tuple[str, str]:
    """Build (name, SKILL.md text) for a mined pattern.

    *taken* is a set of existing skill names (mutated to reserve the new
    one) so proposals never collide with installed skills.
    """
    tools = pattern["tools"]
    name = _skill_name(tools, taken)
    title = " ".join(w.capitalize() for w in name.split("-"))
    support = pattern["support"]
    examples = [t[:80] for t in pattern["example_tasks"] if t]
    situation = _situation(tools)

    description = f"Use when {situation}."
    if examples:
        description += " Typical tasks: " + "; ".join(examples[:2]) + "."

    keywords = list(dict.fromkeys(
        [t for t in tools] + _top_tokens(pattern["example_tasks"], 3)
    ))

    steps = []
    for i, tool in enumerate(tools):
        phrase = _TOOL_STEPS.get(tool, f"Use the `{tool}` tool")
        shape = pattern["arg_shapes"][i] if i < len(pattern["arg_shapes"]) else ""
        detail = f" (e.g. `{shape}`)" if shape else ""
        steps.append(f"{i + 1}. **{phrase}** (`{tool}`){detail}.")

    trace_line = " → ".join(f"`{t}`" for t in tools)

    tips = []
    for s in pattern["strategies"]:
        tips.append(f"- {s}")
    for p in pattern["pitfalls"]:
        tips.append(f"- ⚠ {p}")
    tips_block = "\n".join(tips) if tips else "- (no distilled tips yet)"

    body = f"""# {title}

A workflow mined from {support} successful tasks: the agent repeatedly
followed this sequence, so it is captured here as a reusable playbook.

## Steps

{chr(10).join(steps)}

## Example

Typical task: "{examples[0] if examples else '—'}"
Trace shape: {trace_line}

## Tips

{tips_block}
"""

    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"keywords: [{', '.join(keywords)}]\n"
        "---\n"
    )
    return name, frontmatter + "\n" + body


def _top_tokens(texts: List[str], n: int) -> List[str]:
    counts: Dict[str, int] = {}
    for text in texts:
        for tok in _tokenize(text or ""):
            counts[tok] = counts.get(tok, 0) + 1
    return [tok for tok, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:n]]


# ---------------------------------------------------------------- output


def write_proposals(
    proposals: List[Tuple[str, str]], proposals_dir: str
) -> List[str]:
    """Write proposals to ``proposals_dir/<name>/SKILL.md``.

    Human-in-the-loop: this writes proposals for review only. Nothing is
    installed into ``skills/`` automatically. Returns the written paths.
    """
    os.makedirs(proposals_dir, exist_ok=True)
    written = []
    for name, skill_md in proposals:
        skill_dir = os.path.join(proposals_dir, name)
        os.makedirs(skill_dir, exist_ok=True)
        path = os.path.join(skill_dir, "SKILL.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(skill_md)
        written.append(path)
        print(f"  proposal: {name} -> {path}")
    if not proposals:
        print("  (no repeated patterns found — nothing proposed)")
    return written


def run(
    case_store_path: str,
    proposals_dir: str,
    min_support: int = 3,
    skills_dir: str | None = None,
) -> List[Tuple[str, str]]:
    """End-to-end: mine cases, propose skills, write proposals for review."""
    from cases.store import CaseStore

    if skills_dir is None:
        skills_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills"
        )
    store = CaseStore(case_store_path)
    cases = store.all_cases()
    if not cases:
        print("Skillsmith: no cases recorded yet — nothing to mine.")
        return []
    patterns = mine_patterns(cases, min_support=min_support)
    print(
        f"Skillsmith: {len(cases)} cases, "
        f"{len([c for c in cases if (c.get('outcome') or {}).get('success')])} successful, "
        f"{len(patterns)} repeated pattern(s) (support >= {min_support})."
    )

    taken: set = set()
    if os.path.isdir(skills_dir):
        from skills import load_skills

        taken = {s.name for s in load_skills(skills_dir)}

    proposals = [propose_skill(p, taken) for p in patterns]
    write_proposals(proposals, proposals_dir)
    print(
        "Review proposals in "
        f"{os.path.abspath(proposals_dir)} and move the good ones into skills/."
    )
    return proposals


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Mine the case store for repeated workflows; propose skills."
    )
    parser.add_argument(
        "--cases",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "workspace",
            "cases",
            "cases.jsonl",
        ),
        help="Path to cases.jsonl",
    )
    parser.add_argument(
        "--proposals-dir",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills", "proposals"
        ),
        help="Where to write proposals for review",
    )
    parser.add_argument(
        "--min-support", type=int, default=3, help="Min cases per pattern"
    )
    args = parser.parse_args()
    run(args.cases, args.proposals_dir, min_support=args.min_support)
