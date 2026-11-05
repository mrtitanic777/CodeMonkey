"""Proofs: how a supported answer was reached, step by step, for an independent checker to verify.

A proof is a list of steps in order; each step makes one claim and names the earlier steps it uses.
Only supported answers (KNOWN, DERIVED) get a proof: they are what the invariant protects. The
checker (proofcheck.py, outside this package) re-verifies every step from the domain file and the
raw input alone, sharing no code with this engine.

Step kinds ("by"):
  given       a fact written in the input (a property of a named item, or a given relation)
  shift       a value computed by the input's shift lines from an exact given value
  kind        a named item is of a kind (from its let line, or the term it names)
  tree        the kind sits under a class in the reference tree
  store       a knowledge-store entry (every source agrees, or each source's value listed)
  disputed    a given and an observed value that disagree, both listed
  inherit     a store entry of a class, inherited by a kind under it
  band        a band law turning a category value into a range of a quantity, or back
  fixed       a law that gives every item of a class the same category value
  every-source  a band law giving the same category value under each disputed value
  rule        a rule applied: its premises, its conditions on earlier claims, its conclusion
  symmetric   a symmetric relation read the other way round
  operation   an operation run on earlier claims, its boundaries checked
  compare     two earlier values ordered
  answer      the answer, as the claim of an earlier step
"""
import ast

from .logic import Interval
from .states import SUPPORTED


def _iv(payload):
    """An interval as plain data: (lo, hi, lo_open, hi_open)."""
    if isinstance(payload, Interval):
        return (payload.lo, payload.hi, payload.lo_open, payload.hi_open)
    return (payload, payload, False, False)


class _Builder:
    def __init__(self, world):
        self.world = world
        self.steps = []
        self.memo = {}

    def add(self, step):
        key = repr(sorted(step.items()))
        if key not in self.memo:
            step = dict(step, id=len(self.steps))
            self.steps.append(step)
            self.memo[key] = step["id"]
        return self.memo[key]

    # ---- items, kinds, classes
    def kind(self, var):
        return self.add({"by": "kind", "claim": ("kind", var, self.world.entities[var].kind)})

    def isa(self, var, cls):
        return self.add({"by": "tree", "claim": ("isa", var, cls), "uses": (self.kind(var),)})

    # ---- quantities and categories of an item
    def quantity(self, var, q, alts=False):
        e = self.world.entities[var]
        v = e.quantities[q]
        if v.payload is None and v.alternatives and alts:
            # given and observed disagree: each value is listed, and whatever uses it must hold under each
            values = tuple((_iv(p), src) for p, src in v.alternatives)
            return self.add({"by": "disputed", "claim": ("quantity-alts", q, var, values)})
        just = e.just.get(q, ())
        claim = ("quantity", q, var, _iv(v.payload))
        if just == ("given",):
            return self.add({"by": "given", "claim": claim})
        if just == ("shift",):
            return self.add({"by": "shift", "claim": claim})
        if just and just[0] == "band-from":
            _, law, c = just
            return self.add({"by": "band", "law": law, "claim": claim,
                             "uses": (self.category(var, c), self.kind(var))})
        raise _Unprovable(f"no record of how {q} of {var} was reached")

    def category(self, var, c):
        e = self.world.entities[var]
        v = e.categories[c]
        just = e.just.get(c, ())
        claim = ("category", c, var, v.payload)
        if just == ("given",):
            return self.add({"by": "given", "claim": claim})
        if just and just[0] == "fixed":
            return self.add({"by": "fixed", "law": just[1], "claim": claim, "uses": (self.kind(var),)})
        if just and just[0] == "band-to":
            _, law, q = just
            return self.add({"by": "band", "law": law, "claim": claim,
                             "uses": (self.quantity(var, q), self.kind(var))})
        if just and just[0] == "band-every-source":
            _, law, q = just
            return self.add({"by": "every-source", "law": law, "claim": claim, "uses": (self.kind(var),)})
        raise _Unprovable(f"no record of how {c} of {var} was reached")

    # ---- the knowledge store
    def attribute(self, node, attr, alts=False):
        """A store value for a kind: the entry of the nearest class that has one, inherited if needed.
        With alts, sources that disagree are listed (whatever uses it must hold under each)."""
        lang = self.world.lang
        for cls in lang.ancestors(node):
            facts = [f for f in self.world.domain.get("facts", [])
                     if f["term"] == cls and f["attr"] == attr and not f.get("refuted_by")]
            if not facts:
                continue
            if len({f["value"] for f in facts}) != 1:
                if not alts:
                    raise _Unprovable(f"sources disagree on {attr} of {cls}")
                values = tuple(sorted((f["value"], f["source"]) for f in facts))
                sid = self.add({"by": "store", "claim": ("attr-alts", cls, attr, values),
                                "sources": tuple(sorted(f["source"] for f in facts))})
                if cls == node:
                    return sid
                return self.add({"by": "inherit", "claim": ("attr-alts", node, attr, values), "uses": (sid,)})
            value = facts[0]["value"]
            sid = self.add({"by": "store", "claim": ("attr", cls, attr, value),
                            "sources": tuple(sorted(f["source"] for f in facts))})
            if cls == node:
                return sid
            return self.add({"by": "inherit", "claim": ("attr", node, attr, value), "uses": (sid,)})
        raise _Unprovable(f"{attr} of {node} is not in the store")

    # ---- relations
    def literal(self, key):
        lit = self.world.literals.get(key)
        if lit is None or lit.polarity is None:
            raise _Unprovable(f"{key} is not settled")
        return self._literal(key, lit.polarity, lit.just, lit.attrs, lit.beats)

    def _literal(self, key, polarity, just, attrs=(), beats=()):
        claim = ("rel", key, polarity, tuple(attrs))
        if key[0] == "isa":
            return self.isa(key[1], key[2])
        if just == ("given",):
            return self.add({"by": "given", "claim": claim})
        if just and just[0] == "symmetric":
            _, original, original_just = just
            inner = self._literal(original, polarity, original_just, attrs)
            return self.add({"by": "symmetric", "claim": claim, "uses": (inner,)})
        if just and just[0] == "rule":
            _, rule_id, binding, premises, conditions = just
            uses = tuple(self.literal(p) for p in premises)
            conds = tuple((c, tuple(self.term(t) for t in ast.literal_eval(c)[1:])) for c in conditions)
            return self.add({"by": "rule", "rule": rule_id, "binding": binding, "claim": claim,
                             "uses": uses, "conditions": conds, "beats": tuple(beats)})
        raise _Unprovable(f"no record of how {key} was reached")

    def term(self, spec):
        """A condition's operand as a step id, or a constant as itself."""
        if not isinstance(spec, list):
            return ("const", spec)
        kind = spec[0]
        if kind == "q":
            return ("step", self.quantity(spec[2], spec[1], alts=True))
        if kind == "c":
            return ("step", self.category(spec[2], spec[1]))
        if kind == "a":
            return ("step", self.attribute(self.world.entities[spec[1]].kind, spec[2], alts=True))
        raise _Unprovable(f"unknown operand {spec}")

    # ---- operations
    def operation(self, op_id, binding, value):
        op = next(o for o in self.world.domain.get("operations", []) if o["id"] == op_id)
        b = dict(binding)
        uses = tuple(self.literal((p[0],) + tuple(b.get(t, t) for t in p[1:])) for p in op["match"])
        orient = (repr(_subst(op["orient"], b)), tuple(self.term(t) for t in _subst(op["orient"], b)[1:]))
        inputs = {}
        for name, spec in op["inputs"].items():
            if spec[0] == "a":
                inputs[name] = self.attribute(self.world.entities[b[spec[1]]].kind, spec[2], alts=True)
            elif spec[0] == "q":
                inputs[name] = self.quantity(b[spec[2]], spec[1])
            elif spec[0] == "r":
                inputs[name] = self.literal((spec[1], b[spec[2]], b[spec[3]]))
        return self.add({"by": "operation", "op": op_id, "binding": tuple(sorted(b.items())),
                         "claim": ("value", _iv(value.payload)), "uses": uses, "orient": orient,
                         "inputs": tuple(sorted(inputs.items()))})


class _Unprovable(Exception):
    pass


def _subst(term, b):
    if isinstance(term, str):
        return b.get(term, term)
    if isinstance(term, list):
        return [_subst(t, b) for t in term]
    return term


def build(world, goal, value):
    """The proof of one supported answer, or None for an answer that claims nothing supported.
    Raises nothing: an answer whose path was not recorded gets a proof with a 'missing' step, which
    the checker rejects (an unproven supported answer is reported, never hidden)."""
    if value.state not in SUPPORTED or not value.just:
        return None
    pb = _Builder(world)
    just = value.just
    try:
        if just[0] == "quantity":
            last = pb.quantity(just[2], just[1])
        elif just[0] == "category":
            _, c, var, as_bool = just
            last = pb.category(var, c)
        elif just[0] == "relation":
            last = pb.literal(just[1])
        elif just[0] == "operation":
            last = pb.operation(just[1], just[2], value)
        elif just[0] == "compare":
            _, name, x, y = just
            if name in world.lang.quantities:
                a, b = pb.quantity(x, name, alts=True), pb.quantity(y, name, alts=True)
            else:
                kx = world.entities[x].kind if x in world.entities else x
                ky = world.entities[y].kind if y in world.entities else y
                a, b = pb.attribute(kx, name, alts=True), pb.attribute(ky, name, alts=True)
            last = pb.add({"by": "compare", "claim": ("order", value.payload), "uses": (a, b)})
        elif just[0] == "attribute":
            last = pb.attribute(just[1], just[2])
        elif just[0] == "class":
            last = pb.add({"by": "parent", "claim": ("parent", just[1], value.payload)})
        else:
            raise _Unprovable(f"no proof for an answer reached by {just[0]}")
        pb.add({"by": "answer", "goal": goal, "uses": (last,), "claim": ("answer", value.payload, just)})
    except _Unprovable as gap:
        pb.add({"by": "missing", "goal": goal, "claim": ("missing", str(gap))})
    return pb.steps
