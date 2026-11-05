"""Choosing the experiment that settles the most (kernel item 7).

For the open answers of one input (CONFLICTED, CONFABULATED, MISSING_KNOWLEDGE, ...), list the
experiments that could settle them, imagine every outcome each could have, and re-run the input as
if that outcome had been seen. An experiment is ranked by how many open questions it settles under
its worst outcome (it settles at least that many whatever happens), then by the average over its
outcomes. Nothing here changes what the kernel knows: outcomes are imagined on copies.

Experiments considered:
  observe <item> <category>        each category value it could show (a band law then gives a range)
  observe <item> <quantity> <n>    one value inside each stretch the domain's thresholds cut out
  measure <kind> <attribute>       each value a disagreeing source gives
"""
import ast
import copy

from .states import SUPPORTED, State
from .values import INF, fmt_num

OPEN = {State.CONFLICTED, State.CONFABULATED, State.MISSING_KNOWLEDGE, State.NO_VALID_RULE}


def _settled(result):
    return {a.goal for a in result.answers if a.value.state in SUPPORTED}


def _thresholds(domain, quantity, unit, others):
    """Every number the domain compares this quantity against: band edges, store values in the same
    unit, the other items' values, and those values moved by an operation's boundary constants."""
    cuts = set(others)
    for law in domain.get("bands", []):
        if law.get("quantity") == quantity:
            for b in law.get("bands", []):
                cuts.update(x for x in (b["lo"], b["hi"]) if x is not None)
    units = domain.get("attributes", {})
    for f in domain.get("facts", []):
        if units.get(f.get("attr")) == unit and isinstance(f.get("value"), (int, float)):
            cuts.add(f["value"])
    for op in domain.get("operations", []):
        for expr in op.get("boundary", []):
            for n in ast.walk(ast.parse(expr, mode="eval")):
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and n.value:
                    cuts.update(o + sign * n.value for o in others for sign in (1, -1))
    return sorted(cuts)


def _samples(lo, hi, cuts, given):
    """One value inside each stretch between consecutive cut points (and the given values), within
    what is already known about the quantity."""
    points = sorted({c for c in cuts + given if lo < c < hi} | ({lo} if lo != -INF else set())
                    | ({hi} if hi != INF else set()))
    edges = [lo if lo != -INF else (points[0] - 100 if points else 0)] + points + \
            [hi if hi != INF else (points[-1] + 100 if points else 100)]
    out = []
    for a, b in zip(edges, edges[1:]):
        if b > a:
            out.append((a + b) / 2)
    for c in points:   # the cut points themselves can matter (an included or excluded end)
        if lo <= c <= hi:
            out.append(c)
    return sorted(set(out))


def plan(kernel, text, limit=5):
    """The experiments worth doing for this input, best first.

    Each: {"experiment": what to do, "statement": the line to add once it is done, "outcomes": [(outcome, settled goals)], "worst": n,
    "mean": x, "open": [goals still open now]}.
    """
    base = kernel.run(text, learn=False)
    open_goals = [a.goal for a in base.answers if a.value.state in OPEN]
    if not open_goals:
        return []
    before = _settled(base)
    from .language import deduce
    given = deduce(copy.deepcopy(kernel.lang), text)
    lang, domain = kernel.lang, kernel.domain
    candidates = []

    for var, spec in given.entities.items():
        if spec is None:
            continue
        kind = spec["kind"]
        # a category it could show
        for c, cspec in lang.categories.items():
            if c not in spec["categories"]:
                outcomes = [(f"{c} {v}", f"observe {var} {c} {v}") for v in cspec["values"]]
                candidates.append((f"observe the {c} of {var}", outcomes, f"observe {var} {c} <value>"))
        # a quantity it could have
        for q, qspec in lang.quantities.items():
            have = spec["quantities"].get(q)
            if have is not None and have[0].is_point:
                continue
            lo, hi = (have[0].lo, have[0].hi) if have is not None else (-INF, INF)
            others = [s["quantities"][q][0].lo for s in given.entities.values()
                      if s and q in s["quantities"] and s["quantities"][q][0].is_point]
            unit = qspec["unit"]
            values = _samples(lo, hi, _thresholds(domain, q, unit, others), others)
            if values:
                outcomes = [(f"{q} {fmt_num(v)} {unit}", f"observe {var} {q} {fmt_num(v)} {unit}") for v in values]
                candidates.append((f"measure the {q} of {var}", outcomes, f"observe {var} {q} <number> {unit}"))
        # a store value its sources disagree on
        for attr in lang.attributes:
            for cls in lang.ancestors(kind):
                facts = [f for f in domain.get("facts", [])
                         if f["term"] == cls and f["attr"] == attr and not f.get("refuted_by")]
                if facts:
                    if len({f["value"] for f in facts}) > 1:
                        outcomes = [(f"{attr} {fmt_num(f['value'])} ({f['source']} right)", ("refute", cls, attr, f["source"]))
                                    for f in facts]
                        candidates.append((f"measure {attr} of {cls}", outcomes, f"measure {cls} {attr} <number>"))
                    break

    ranked, seen = [], set()
    for name, outcomes, statement in candidates:
        if name in seen:
            continue
        seen.add(name)
        results = []
        for label, how in outcomes:
            if isinstance(how, tuple):    # imagine the measurement settled it for this source
                _, cls, attr, keep = how
                trial = kernel.__class__(copy.deepcopy(domain), kernel.headspace_limit, kernel.attempts)
                trial.lang = copy.deepcopy(kernel.lang)
                for f in trial.domain["facts"]:
                    if f["term"] == cls and f["attr"] == attr and f["source"] != keep:
                        f["refuted_by"] = "imagined"
                r = trial.run(text, learn=False)
            else:
                r = kernel.__class__(copy.deepcopy(domain), kernel.headspace_limit, kernel.attempts)
                r.lang = copy.deepcopy(kernel.lang)
                r = r.run(text + "\n" + how, learn=False)
            newly = sorted((_settled(r) - before) & set(open_goals))
            results.append((label, newly))
        counts = [len(n) for _, n in results]
        if not any(counts):
            continue
        ranked.append({"experiment": name, "statement": statement, "outcomes": results, "worst": min(counts),
                       "mean": round(sum(counts) / len(counts), 2), "open": open_goals})
    ranked.sort(key=lambda e: (-e["worst"], -e["mean"], e["experiment"]))
    return ranked[:limit]


def grouped(outcomes):
    """Neighbouring outcomes that settle the same questions, as one stretch: [(label, goals)]."""
    runs = []
    for label, goals in outcomes:
        if runs and runs[-1][2] == goals:
            runs[-1][1] = label
        else:
            runs.append([label, label, goals])
    def numeric(label):
        if "(" in label:   # one source's value: alternatives, not a stretch
            return False
        try:
            float(label.split()[1])
            return True
        except (IndexError, ValueError):
            return False

    out = []
    for first, last, goals in runs:
        if first == last:
            out.append((first, goals))
        elif numeric(first):   # a stretch of values: from the first to the last
            out.append((f"{first} to {last.split(' ', 1)[1]}", goals))
        else:                  # values with no order between them
            members = [lab for lab, g in outcomes if g == goals]
            out.append((" or ".join(members), goals))
    return out


def describe(plan_list):
    """The plan in words, one experiment per paragraph."""
    lines = []
    for e in plan_list:
        lines.append(f"{e['experiment']}: settles {e['worst']} open question(s) whatever the outcome "
                     f"({e['mean']} on average); then add: {e['statement']}")
        for label, goals in grouped(e["outcomes"]):
            lines.append(f"    if {label}: {', '.join(goals) if goals else 'nothing settled'}")
    return "\n".join(lines)


__all__ = ["plan", "describe", "grouped"]
