"""Epistemic states and how they combine."""
from enum import Enum


class State(str, Enum):
    KNOWN = "KNOWN"
    DERIVED = "DERIVED"
    CONFABULATED = "CONFABULATED"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MISSING_KNOWLEDGE = "MISSING_KNOWLEDGE"
    CONFLICTED = "CONFLICTED"
    UNKNOWN_STRUCTURE = "UNKNOWN_STRUCTURE"
    AMBIGUOUS_PATTERN = "AMBIGUOUS_PATTERN"
    NO_VALID_RULE = "NO_VALID_RULE"
    OUT_OF_BOUNDARY = "OUT_OF_BOUNDARY"


SUPPORTED = frozenset({State.KNOWN, State.DERIVED})

# Low confidence, needs experimentation: usable, always marked, never treated as supported.
NEEDS_EXPERIMENT = frozenset({State.CONFLICTED, State.CONFABULATED})

# When several failures combine, the headline state is the first in this order.
# Every cause is kept regardless.
PRECEDENCE = (
    State.UNKNOWN_REFERENCE,
    State.LOW_CONFIDENCE,
    State.UNKNOWN_STRUCTURE,
    State.AMBIGUOUS_PATTERN,
    State.OUT_OF_BOUNDARY,
    State.MISSING_KNOWLEDGE,
    State.NO_VALID_RULE,
)


def is_failure(state):
    return state not in SUPPORTED and state not in NEEDS_EXPERIMENT


def is_decided(state):
    return not is_failure(state)


def combine(parts):
    """Combine (state, causes) pairs into one (state, causes).

    Any failure wins; then CONFLICTED, then CONFABULATED spread; otherwise the result is supported.
    """
    states, causes = [], []
    for state, part_causes in parts:
        states.append(state)
        for cause in part_causes:
            if cause not in causes:
                causes.append(cause)
    failures = [s for s in states if is_failure(s)]
    if failures:
        return min(failures, key=PRECEDENCE.index), tuple(causes)
    for state in (State.CONFLICTED, State.CONFABULATED):
        if state in states:
            return state, tuple(causes)
    if State.DERIVED in states:
        return State.DERIVED, ()
    return State.KNOWN, ()
