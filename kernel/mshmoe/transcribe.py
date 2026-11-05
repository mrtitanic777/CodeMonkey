"""Transcribe: render the result, read it back, accept it only if nothing was added or dropped."""
import random
import re

from .states import State, is_failure
from .values import Interval, fmt_num

POSSIBLY = "possibly "


def _measure(value, unit):
    return f"{fmt_num(value)} {unit}" if unit else fmt_num(value)


def value_text(v):
    if v.state is State.CONFLICTED and v.alternatives:
        return "either " + " or ".join(_measure(val, v.unit) for val, _ in v.alternatives)
    if v.payload is None or is_failure(v.state):
        text = "unknown"
    elif isinstance(v.payload, Interval):
        text = v.payload.describe(v.unit)
    elif isinstance(v.payload, (int, float)):
        text = f"{fmt_num(v.payload)} {v.unit}" if v.unit else fmt_num(v.payload)
    else:
        text = str(v.payload)
    return POSSIBLY + text if v.state is State.CONFABULATED else text


def _alt(value, source, unit):
    return f"{_measure(value, unit)} (from {source})"


def claims(answers):
    """Everything the result asserts, as a set of (kind, goal, text)."""
    out = set()
    for a in answers:
        v, g = a.value, a.goal
        out.add(("value", g, value_text(v)))
        out.add(("state", g, v.state.value))
        out.update(("from", g, p) for p in v.provenance)
        out.update(("because", g, c) for c in v.causes)
        out.update(("alternative", g, _alt(val, src, v.unit)) for val, src in v.alternatives)
        if v.experiment:
            out.add(("experiment", g, v.experiment))
    return out


def render(answers):
    """The template: faithful by construction."""
    lines = []
    for a in answers:
        v = a.value
        lines.append(f"{a.goal} = {value_text(v)} [{v.state.value}]")
        lines += [f"  from: {p}" for p in v.provenance]
        lines += [f"  because: {c}" for c in v.causes]
        lines += [f"  alternative: {_alt(val, src, v.unit)}" for val, src in v.alternatives]
        if v.experiment:
            lines.append(f"  experiment: {v.experiment}")
    return "\n".join(lines)


_HEAD = re.compile(r"^(?P<goal>\S.*?) = (?P<value>.*?)(?: \[(?P<state>[A-Z_]+)\])?$")
_SUB = re.compile(r"^  (?P<kind>from|because|alternative|experiment): (?P<text>.*)$")


def parse(text):
    """The input side read back over the output: which claims does this text make?"""
    out, goal = set(), None
    for line in text.splitlines():
        sub = _SUB.match(line)
        if sub and goal is not None:
            out.add((sub["kind"], goal, sub["text"]))
            continue
        head = _HEAD.match(line)
        if head:
            goal = head["goal"]
            out.add(("value", goal, head["value"]))
            if head["state"]:
                out.add(("state", goal, head["state"]))
        elif line.strip():
            out.add(("unparsed", "", line.strip()))
    return out


def check(text, answers):
    """Round trip: extra = claims the result doesn't make; missing = claims the text dropped."""
    expected, found = claims(answers), parse(text)
    return found == expected, found - expected, expected - found


class NoisyTranscriber:
    """Stands in for a future language model: often renders with a known kind of drift."""

    DRIFTS = ("drop_state", "drop_possibly", "overspecify", "swap_unit", "flip", "extra_claim", "drop_line")

    def __init__(self, units, seed=0, rate=0.7):
        self.units = sorted({u for u in units if u}, key=len, reverse=True)
        self.rng = random.Random(seed)
        self.rate = rate

    def __call__(self, answers):
        clean = render(answers)
        if self.rng.random() >= self.rate:
            return clean, None
        drifts = list(self.DRIFTS)
        self.rng.shuffle(drifts)
        for name in drifts:
            text = getattr(self, "_" + name)(clean)
            if text != clean:
                return text, name
        return clean, None

    @staticmethod
    def _drop_state(t):
        return re.sub(r" \[[A-Z_]+\]", "", t, count=1)

    @staticmethod
    def _drop_possibly(t):
        return t.replace(POSSIBLY, "", 1)

    @staticmethod
    def _overspecify(t):
        for pattern, repl in ((r"= unknown \[", "= 42 ["),
                              (r"= (below|above|at least|at most) ", "= "),
                              (r"= from (\S+) to (below )?", r"= \1 ")):
            new = re.sub(pattern, repl, t, count=1)
            if new != t:
                return new
        return t

    def _swap_unit(self, t):
        for u in self.units:
            pattern = rf" {re.escape(u)}(?=$|[\s\]\(,])"
            if re.search(pattern, t, flags=re.M):
                other = next((x for x in self.units if x != u), None)
                return re.sub(pattern, f" {other}", t, count=1, flags=re.M) if other else t
        return t

    @staticmethod
    def _flip(t):
        for a, b in (("= yes [", "= no ["), ("= no [", "= yes [")):
            if a in t:
                return t.replace(a, b, 1)
        return re.sub(r"= (\S+) → (\S+) \[", r"= \2 → \1 [", t, count=1)

    @staticmethod
    def _extra_claim(t):
        return t + "\nnote = the remaining cases are safe [KNOWN]"

    @staticmethod
    def _drop_line(t):
        lines = t.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("  "):
                return "\n".join(lines[:i] + lines[i + 1:])
        return t


def transcribe(answers, transcriber=None, attempts=3):
    """Step 6. The only loop: up to `attempts` tries, then the template."""
    report = {"attempts": [], "fallback": False}
    if transcriber is not None:
        for _ in range(attempts):
            text, drift = transcriber(answers)
            ok, extra, missing = check(text, answers)
            report["attempts"].append({"drift": drift, "accepted": ok,
                                       "extra": sorted(map(str, extra)), "missing": sorted(map(str, missing))})
            if ok:
                return text, report
        report["fallback"] = True
    text = render(answers)
    if not check(text, answers)[0]:
        raise AssertionError("template failed its own round-trip check")
    return text, report
