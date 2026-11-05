"""The MSHMOE kernel as a tool you can open from CodeMonkey.

Runs the kernel in ``kernel/`` on its controlled input language (see kernel/LANGUAGE.md) and
returns every answer with its epistemic state. No model is involved: the kernel is deterministic.
The English door (kernel/bridge) needs two models loaded and is not used here.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List

from .epistemic import _KERNEL_DIR  # also puts kernel/ on sys.path

from mshmoe import Kernel, load_domain  # noqa: E402
import proofcheck  # noqa: E402  (kernel/proofcheck.py: the independent checker)
from mshmoe.states import State, is_failure  # noqa: E402
from mshmoe.transcribe import value_text  # noqa: E402

# Each domain is data only: its file, its development cases, its handbook (kernel/DOMAIN2.md)
DOMAINS = {
    "thren": ("thren.json", "thren_cases_controlled.json", "thren_handbook.md"),
    "nour": ("nour.json", "nour_cases_controlled.json", "nour_handbook.md"),
}


def _paths(domain: str):
    if domain not in DOMAINS:
        raise ValueError(f"no domain {domain!r}; there are {', '.join(DOMAINS)}")
    data, cases, book = DOMAINS[domain]
    return (os.path.join(_KERNEL_DIR, "domain", data), os.path.join(_KERNEL_DIR, "tests", cases),
            os.path.join(_KERNEL_DIR, "domain", book))
_lock = threading.Lock()

# What each state means, in plain words, for the interface.
STATE_HELP = {
    "KNOWN": "Stated directly by the domain or by your input.",
    "DERIVED": "Worked out from known facts by a rule.",
    "CONFABULATED": "A best guess you asked for. Marked, never treated as known.",
    "CONFLICTED": "Two sources disagree. Both are kept.",
    "UNKNOWN_REFERENCE": "A word it cannot place in the domain.",
    "LOW_CONFIDENCE": "A near-match to a known word, not confirmed.",
    "MISSING_KNOWLEDGE": "The domain has no fact that answers this.",
    "UNKNOWN_STRUCTURE": "A question form it does not have.",
    "AMBIGUOUS_PATTERN": "More than one reading fits.",
    "NO_VALID_RULE": "No rule applies here.",
    "OUT_OF_BOUNDARY": "Outside the range the rules cover.",
}


def info(domain: str = "thren") -> Dict[str, Any]:
    domain_path, cases_path, book = _paths(domain)
    examples: List[Dict[str, str]] = []
    try:
        with open(cases_path, "r", encoding="utf-8") as fh:
            for case in json.load(fh):
                examples.append(
                    {"id": case.get("id", ""), "category": case.get("category", ""), "input": case.get("input", "")}
                )
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "domain": domain,
        "domains": list(DOMAINS),
        "states": STATE_HELP,
        "examples": examples,
        "language_doc": os.path.join(_KERNEL_DIR, "LANGUAGE.md"),
        "handbook": book,
    }


def run(text: str, domain: str = "thren") -> Dict[str, Any]:
    """Run one controlled input through a fresh kernel (nothing is learned between calls)."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("nothing to run")
    if len(text) > 20_000:
        raise ValueError("input is too long")
    domain_path = _paths(domain)[0]
    with _lock:
        # the other domains stand by: `estimate` can carry a law over from them by analogy
        others = {n: load_domain(_paths(n)[0]) for n in DOMAINS if n != domain}
        kernel = Kernel(load_domain(domain_path), name=domain, analogues=others)
        result = kernel.run(text, learn=False)
        with open(domain_path, encoding="utf-8") as fh:
            data = json.load(fh)
        unproven = {}
        for a in result.answers:   # an answer about a moment is proven against the input that held then
            unproven.update(proofcheck.check_run(data, a.input or text, [a]))   # checked from outside
    answers = []
    for a in result.answers:
        state: State = a.value.state
        answers.append(
            {
                "goal": a.goal,
                "state": state.value,
                "value": value_text(a.value),
                "step": a.step,
                "causes": [str(c) for c in (a.value.causes or ())],
                "supported": not is_failure(state) and state.value in ("KNOWN", "DERIVED"),
                "rests_on": list(a.rests_on),   # the given facts and store entries it rests on
                "clashes": list(a.clashes),     # assumptions it uses that cannot all hold
                # a supported answer's proof, re-checked by kernel/proofcheck.py (no engine code)
                "proven": a.proof is not None and a.goal not in unproven,
                "proof_problems": unproven.get(a.goal, []),
            }
        )
    return {
        "answers": answers,
        "text": result.text,
        "steps": list(result.steps),
        "op_calls": result.op_calls,
        "clashes": list(result.nogoods),
    }
