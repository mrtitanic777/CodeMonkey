"""The six steps, run once, in order. Learning happens between runs; commitment only in consolidation."""
import copy
import json
from dataclasses import dataclass

from .language import Language, deduce
from .proof import build as build_proof
from .logic import World, band_value
from .states import SUPPORTED
from .transcribe import transcribe, value_text
from .values import Value


def load_domain(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def domain_units(domain):
    units = set()
    for q in domain.get("quantities", {}).values():
        units.add(q["unit"])
        units.update(q.get("conversions", {}))
    units.update(domain.get("attributes", {}).values())
    for r in domain.get("relations", {}).values():
        units.update(r.get("attrs", {}).values())
    units.update(op.get("unit") for op in domain.get("operations", []))
    return {u for u in units if u}


@dataclass
class Answer:
    goal: str
    value: Value
    step: str  # the step that produced it
    rests_on: tuple = ()   # the given facts and store entries it rests on, in words
    clashes: tuple = ()    # assumptions that cannot all hold, among those this answer uses
    proof: object = None   # for a supported answer: the steps that reach it (mshmoe/proof.py)
    input: str = ""      # for an answer about a moment: the input that held then (its proof is against it)


def describe_support(atom, kinds):
    """One item an answer rests on, in words. `kinds` maps an entity to its kind (for 'is a')."""
    tag = atom[0]
    if tag in ("given", "observed"):
        return f"{tag}: {atom[1]} of {atom[2]}"
    if tag == "store":
        return f"store: {atom[2]} of {atom[1]} ({atom[3]})"
    if tag == "law":
        return f"law: {atom[1]}"
    if tag == "isa":
        return f"given: {atom[1]} is a {atom[2]}" if kinds.get(atom[1]) == atom[2] else None
    return f"given: {tag}({', '.join(atom[1:])})"


def explain(world, value):
    """(rests_on, clashes) for one answer: what it rests on, and the clashes among those things."""
    kinds = {v: e.kind for v, e in world.entities.items()}
    rests = sorted({t for t in (describe_support(a, kinds) for a in value.support) if t})
    clashes = tuple(d for d, atoms in world.nogoods.items() if atoms & value.support)
    return tuple(rests), clashes


@dataclass
class Result:
    answers: list
    text: str
    op_calls: int
    transcript: dict
    steps: list
    headspace: object = None  # {"chunks", "limit", "wide"}: reported, not enforced
    nogoods: tuple = ()       # every clash found in this run, in words

    def affected_by(self, fact):
        """The questions whose answers rest on a fact (matched on its words, as an answer's rests_on shows it).
        If that fact were wrong, these are the answers that would change; the rest stand."""
        return [a.goal for a in self.answers if any(fact in r for r in a.rests_on)]


class Kernel:
    def __init__(self, domain, headspace_limit=4, attempts=3, transcriber=None, evidence_min=2, decay_after=3,
                 name="this domain", analogues=None):
        self.domain = copy.deepcopy(domain)
        self.name = name
        self.analogues = dict(analogues or {})   # other domains, name -> domain: laws carried over by analogy
        self._carried = None
        self.lang = Language(self.domain)
        self.headspace_limit = headspace_limit
        self.attempts = attempts
        self.transcriber = transcriber
        self.evidence_min = evidence_min
        self.decay_after = decay_after
        self.episodes = []   # episodic store: every run, as it happened
        self.evidence = {}   # (law id, kind) -> {"items": [...], "last": consolidation index}
        self.refuted = set()
        self.commits = []
        self.learned_evidence = {}   # ("new", kind, category, quantity) -> observations, for a law of its own
        self.contradicted = {}       # learned law id -> the observation that contradicts it
        self.measurements = {}       # (kind, attribute) -> {"values": [...], "counted": n}
        self.domain.setdefault("source_record", {})   # source -> {"right", "wrong"} when measured
        self.index = 0       # consolidations so far

    # ------------------------------------------------------------ one run
    def run(self, text, learn=True):
        from . import timeline
        if timeline.is_timed(text):   # facts at points in time: each moment asked about, on its own
            return timeline.run(self, text, learn)
        steps = ["deduce"]
        given = deduce(self.lang, text)
        failed = {id(g): given.failure_for(g) for g in given.goals}
        ready = [g for g in given.goals if failed[id(g)] is None]
        world, answered = None, {}
        if ready:  # a failure stays with the questions that depend on it
            world = World(given, self.domain, self.headspace_limit)
            steps.append("relate")
            world.relate()
            steps += ["compare", "plan"]
            world.compare()
            answered = {}
            for g in ready:
                value, step = world.answer(g)
                answered[id(g)] = Answer(g.label, value, step, *explain(world, value),
                                         proof=build_proof(world, g.label, value))
        answers = [answered[id(g)] if id(g) in answered
                   else Answer(g.label, Value(None, failed[id(g)][0], causes=failed[id(g)][1]), "deduce")
                   for g in given.goals]
        if self.analogues and ready:
            from . import analogy
            answers = analogy.apply(self, given, answers)
        if any(a.step == "execute" for a in answers):
            steps.append("execute")
        steps.append("transcribe")
        out, report = transcribe(answers, self.transcriber, self.attempts)
        result = Result(answers, out, world.op_calls if world else 0, report, steps,
                        world.headspace if world else None, tuple(world.nogoods) if world else ())
        if learn:
            self._learn(given, text, result)
        return result

    def carried(self):
        """name -> (laws carried over from that domain, {its law: why not}, the map), worked out once."""
        if self._carried is None:
            from . import analogy
            self._carried = {}
            for name, other in self.analogues.items():
                m = analogy.structural_map(other, self.domain, (name, self.name))
                laws, blocked = analogy.transfer(m, other)
                self._carried[name] = (laws, blocked, m)
        return self._carried

    # ------------------------------------------------------------ in situ learning
    def _learn(self, given, text, result):
        self.episodes.append({"input": text,
                              "answers": {a.goal: (a.value.state, value_text(a.value)) for a in result.answers}})
        for term, parent in given.taught:
            self.lang.teach(term, parent, "taught in use")
        for term, node in given.confirmations:
            self.lang.confirm(term, node)
        for node, quantities, categories in given.observations:
            self._record_observation(node, quantities, categories)
        for node, attr, value in given.measurements:
            self.measurements.setdefault((node, attr), {"values": [], "counted": 0})["values"].append(value)

    def _record_observation(self, node, quantities, categories):
        for law in self.domain.get("bands", []):   # a learned law an observation now contradicts
            if law.get("learned") and law["category"] in categories and law["quantity"] in quantities \
                    and any(self.lang.is_a(node, t) for t in law["applies_to"]):
                got = band_value(law, quantities[law["quantity"]])
                if got is not None and got != categories[law["category"]]:
                    self.contradicted.setdefault(law["id"], f"{node} at {quantities[law['quantity']].describe()} "
                                                            f"showed {categories[law['category']]}, not {got}")
        for category, value in categories.items():   # no law covers this kind: evidence for one of its own
            covered = any(b["category"] == category and any(self.lang.is_a(node, t) for t in b["applies_to"])
                          for b in self.domain.get("bands", []))
            # a sibling's law that may still be extended to it is tried first (the narrow consolidator)
            extensible = any(b["category"] == category and "bands" in b and (b["id"], node) not in self.refuted
                             and any(self.lang.is_sibling(node, t) for t in b["applies_to"])
                             for b in self.domain.get("bands", []))
            covered = covered or extensible
            for q, iv in quantities.items():
                key = ("new", node, category, q)
                if not covered and key not in self.refuted and iv.is_point:
                    ev = self.learned_evidence.setdefault(key, {"items": [], "last": self.index})
                    ev["items"].append((iv.lo, value))
                    ev["last"] = self.index
        for law in self.domain.get("bands", []):
            if "bands" not in law or law["category"] not in categories or law["quantity"] not in quantities:
                continue
            if any(self.lang.is_a(node, t) for t in law["applies_to"]):
                continue
            if not any(self.lang.is_sibling(node, t) for t in law["applies_to"]):
                continue
            key = (law["id"], node)
            if key in self.refuted:
                continue
            ev = self.evidence.setdefault(key, {"items": [], "last": self.index})
            ev["items"].append({"quantity": quantities[law["quantity"]], "category": categories[law["category"]]})
            ev["last"] = self.index

    # ------------------------------------------------------------ consolidation ("sleep")
    def consolidate(self):
        self.index += 1
        report = {"committed": [], "refuted": [], "rejected": [], "pruned": []}
        for key, ev in list(self.evidence.items()):
            law_id, node = key
            law = self._law(law_id)
            if any(band_value(law, item["quantity"]) != item["category"] for item in ev["items"]):
                self.refuted.add(key)
                del self.evidence[key]
                report["refuted"].append(f"{law_id} for {node}: an observation disagrees")
                continue
            if len(ev["items"]) < self.evidence_min:
                continue
            ok, why = self._validate(law_id, node)
            if ok:
                law["applies_to"].append(node)
                self.commits.append({"law": law_id, "extended_to": node, "evidence": len(ev["items"]),
                                     "validation": why, "consolidation": self.index})
                del self.evidence[key]
                report["committed"].append(f"{law_id} extended to {node} ({why})")
            else:
                report["rejected"].append(f"{law_id} for {node}: {why}")
        for key, ev in list(self.evidence.items()):
            if self.index - ev["last"] >= self.decay_after:
                del self.evidence[key]
                report["pruned"].append(f"evidence for {key[0]} on {key[1]}")
        report.update(learned=[], withdrawn=[], settled=[])
        self._withdraw(report)
        self._learn_laws(report)
        self._settle_by_measurement(report)
        return report

    # ------------------------------------------------------------ new laws, learned from observations
    MIN_NEW = 3   # observations before a law of its own is proposed (and at least two values seen)

    def _induce(self, items):
        """A band law from (quantity, category value) pairs: each value's band is exactly the range
        observed for it (nothing claimed beyond what was seen). None if two values' ranges overlap."""
        hull = {}
        for q, v in items:
            lo, hi = hull.get(v, (q, q))
            hull[v] = (min(lo, q), max(hi, q))
        order = sorted(hull.items(), key=lambda kv: kv[1][0])
        for (_, (_, hi)), (_, (lo, _)) in zip(order, order[1:]):
            if lo <= hi:
                return None
        return [{"value": v, "lo": lo, "hi": hi, "hi_in": True} for v, (lo, hi) in order]

    def _learn_laws(self, report):
        for key, ev in list(self.learned_evidence.items()):
            _, node, category, q = key
            items = ev["items"]
            if len(items) < max(self.MIN_NEW, self.evidence_min) or len({v for _, v in items}) < 2:
                if self.index - ev["last"] >= self.decay_after:
                    del self.learned_evidence[key]
                    report["pruned"].append(f"evidence for a {category} law of {node}")
                continue
            bands = self._induce(items)
            if bands is None:
                self.refuted.add(key)
                del self.learned_evidence[key]
                report["refuted"].append(f"no {category} law fits {node}: two values were seen at overlapping {q}")
                continue
            # held out: each observation, left out, must not be contradicted by the law learned from the rest
            for i, (qi, vi) in enumerate(items):
                rest = self._induce(items[:i] + items[i + 1:]) or []
                wrong = [b["value"] for b in rest if b["value"] != vi and b["lo"] <= qi <= b["hi"]]
                if wrong:
                    report["rejected"].append(f"{category} law for {node}: leaving out {qi} {vi} predicts {wrong[0]}")
                    bands = None
                    break
            if bands is None:
                continue
            law = {"id": f"learned-{category}-{node}", "applies_to": [node], "quantity": q, "category": category,
                   "bands": bands, "learned": {"evidence": len(items), "consolidation": self.index}}
            ok, why = self._validate_new(law)
            if not ok:
                report["rejected"].append(f"{law['id']}: {why}")
                continue
            self.domain["bands"].append(law)
            self.commits.append({"law": law["id"], "learned": True, "evidence": len(items), "validation": why,
                                 "consolidation": self.index})
            del self.learned_evidence[key]
            report["learned"].append(f"{law['id']} from {len(items)} observations ({why})")

    def _validate_new(self, law):
        candidate = copy.deepcopy(self.domain)
        candidate["bands"].append(law)
        return self._replay(candidate)

    def _withdraw(self, report):
        for law_id, why in list(self.contradicted.items()):
            law = next((b for b in self.domain["bands"] if b["id"] == law_id), None)
            if law is not None:
                self.domain["bands"].remove(law)
                node = law["applies_to"][0]
                self.refuted.add(("new", node, law["category"], law["quantity"]))
                report["withdrawn"].append(f"{law_id}: {why}")
            del self.contradicted[law_id]

    # ------------------------------------------------------------ measurements and the sources' record
    TOLERANCE = 0.05   # a measurement agrees with a value within 5%

    def _settle_by_measurement(self, report):
        record = self.domain["source_record"]
        for (node, attr), m in self.measurements.items():
            facts = [f for f in self.domain.get("facts", [])
                     if f["term"] == node and f["attr"] == attr and not f.get("refuted_by")]
            new = m["values"][m["counted"]:]
            for value in new:   # every measurement counts once for or against each source it bears on
                for f in facts:
                    r = record.setdefault(f["source"], {"right": 0, "wrong": 0})
                    r["right" if self._agrees(value, f["value"]) else "wrong"] += 1
            m["counted"] = len(m["values"])
            values = m["values"]
            if len(values) < self.evidence_min:
                continue
            mean = sum(values) / len(values)
            if not all(self._agrees(v, mean) for v in values):
                continue   # the measurements disagree among themselves: nothing is settled
            wrong = [f for f in facts if not self._agrees(mean, f["value"])]
            if not wrong:
                continue
            candidate = copy.deepcopy(self.domain)
            why_refuted = f"{len(values)} measurements of {attr} of {node} average {mean:g}"
            for f in candidate["facts"]:
                if f in wrong:
                    f["refuted_by"] = why_refuted
            right = [f for f in facts if f not in wrong]
            if not right:
                candidate["facts"].append({"term": node, "attr": attr, "value": round(mean, 6), "source": "measured"})
            ok, why = self._replay(candidate)
            if not ok:
                report["rejected"].append(f"settling {attr} of {node}: {why}")
                continue
            self.domain["facts"] = candidate["facts"]
            report["settled"].append(f"{attr} of {node}: {', '.join(f['source'] for f in wrong)} refuted by "
                                     f"{why_refuted}" + ("" if right else "; the measured value is kept"))

    def _agrees(self, a, b):
        return abs(a - b) <= self.TOLERANCE * max(abs(a), abs(b), 1e-12)

    def _replay(self, candidate):
        trial = Kernel(candidate, self.headspace_limit, self.attempts)
        trial.lang = copy.deepcopy(self.lang)
        for ep in self.episodes:
            now = {a.goal: (a.value.state, value_text(a.value)) for a in trial.run(ep["input"], learn=False).answers}
            for goal, (state, text) in ep["answers"].items():
                if state in SUPPORTED and now.get(goal) != (state, text):
                    return False, f"replay regression on {goal}: was '{text}', now '{now[goal][1]}'"
        return True, f"replayed {len(self.episodes)} episodes without regression"

    def _law(self, law_id):
        return next(b for b in self.domain["bands"] if b["id"] == law_id)

    def _validate(self, law_id, node):
        """Well-formed, consistent with the library, and no regression when past episodes are replayed."""
        law = self._law(law_id)
        if node not in self.lang.parent:
            return False, f"{node} is not in the reference tree"
        others = [b for b in self.domain["bands"] if b is not law and b["category"] == law["category"]
                  and any(self.lang.is_a(node, t) for t in b["applies_to"])]
        if others:
            return False, f"inconsistent with {others[0]['id']}"
        candidate = copy.deepcopy(self.domain)
        next(b for b in candidate["bands"] if b["id"] == law_id)["applies_to"].append(node)
        trial = Kernel(candidate, self.headspace_limit, self.attempts)
        trial.lang = copy.deepcopy(self.lang)
        for ep in self.episodes:
            now = {a.goal: (a.value.state, value_text(a.value)) for a in trial.run(ep["input"], learn=False).answers}
            for goal, (state, text) in ep["answers"].items():
                if state in SUPPORTED and now.get(goal) != (state, text):
                    return False, f"replay regression on {goal}: was '{text}', now '{now[goal][1]}'"
        return True, f"replayed {len(self.episodes)} episodes without regression"


class Library:
    """Several domains, one kernel each; each kernel can carry laws over from the others by analogy."""

    def __init__(self, domains, **options):
        self.domains = {name: copy.deepcopy(d) for name, d in domains.items()}
        self.kernels = {name: Kernel(d, name=name, analogues={n: o for n, o in self.domains.items() if n != name},
                                     **options)
                        for name, d in self.domains.items()}

    def run(self, text, domain, learn=True):
        return self.kernels[domain].run(text, learn=learn)

    def map(self, source, target):
        from . import analogy
        return analogy.structural_map(self.domains[source], self.domains[target], (source, target))
