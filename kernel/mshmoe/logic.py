"""Logic engine: Relate, Compare, Plan and Execute over the headspace H."""
import ast
import itertools
import operator
from dataclasses import dataclass, replace

from .states import PRECEDENCE, SUPPORTED, State, combine, is_decided, is_failure
from .values import Interval, Value, compare as compare_intervals, failure, fmt_num

SYMBOL = {"gt": ">", "lt": "<", "ge": "≥", "le": "≤", "eq": "=", "neq": "≠"}


@dataclass(frozen=True)
class Literal:
    key: tuple
    polarity: object          # True, False, or None when undecided
    state: State
    causes: tuple = ()
    support: frozenset = frozenset()  # the given facts it rests on
    rules: tuple = ()
    attrs: tuple = ()         # relation attributes as (name, value) pairs
    just: tuple = ()          # how it was concluded: ("given",), ("rule", id, binding, premises, conditions)
    beats: tuple = ()         # opposing laws it won against by being more specific


@dataclass(frozen=True)
class Term:
    payload: object
    state: State
    causes: tuple
    label: str
    unit: object = None
    provenance: tuple = ()
    alternatives: tuple = ()  # ((number, source), ...) when sources disagree
    experiment: object = None
    support: frozenset = frozenset()  # the given facts this value rests on

    def options(self):
        """The values this term may take: one, or one per disagreeing source."""
        if self.alternatives:
            return [(Interval.point(v) if isinstance(v, (int, float)) else v, src) for v, src in self.alternatives]
        return [] if self.payload is None else [(self.payload, None)]

    def text(self):
        if self.alternatives:
            values = " or ".join(fmt_num(v) for v, _ in self.alternatives)
            return f"{self.label} (either {values}{' ' + self.unit if self.unit else ''})"
        if isinstance(self.payload, Interval):
            return f"{self.label} ({self.payload.describe(self.unit)})"
        return self.label


class Entity:
    def __init__(self, var, kind):
        self.var, self.kind = var, kind
        self.quantities = {}  # name -> Value(Interval)
        self.categories = {}  # name -> Value(str)
        self.undecided = {}   # category -> cause: a law applies but the facts do not settle it
        self.guesses = {}     # category -> CONFABULATED Value from a near-miss law
        self.origin = {}      # quantity/category name -> the given facts its value rests on
        self.just = {}        # quantity/category name -> how its value was concluded, for proofs


def show(key):
    return f"{key[0]}({', '.join(key[1:])})"


def band_interval(band):
    """A band's range: its low end in, its high end out, unless the band says its high end is in."""
    iv = Interval.band(band["lo"], band["hi"])
    return Interval(iv.lo, iv.hi, False, False) if band.get("hi_in") else iv


def band_value(law, interval):
    for band in law["bands"]:
        if interval.within(band_interval(band)):
            return band["value"]
    return None


def stratify(rules):
    """Group rules by the relation they conclude, ordered so premises are settled first."""
    by_rel = {}
    for rule in rules:
        by_rel.setdefault(rule["conclude"][0], []).append(rule)
    deps = {rel: {p[0] for r in group for p in r["match"] if p[0] in by_rel and p[0] != rel}
            for rel, group in by_rel.items()}
    order, done = [], set()
    while len(done) < len(by_rel):
        ready = sorted(rel for rel in by_rel if rel not in done and deps[rel] <= done)
        if not ready:  # a cycle: run what is left as one group
            order.append([r for rel in by_rel if rel not in done for r in by_rel[rel]])
            break
        for rel in ready:
            order.append(by_rel[rel])
            done.add(rel)
    return order


def _better(new, old):
    """A better derivation of the same literal: a better state; on a tie, one fixed order (direct before
    read the other way round, fewer supporting facts, then the laws' names), so what an answer says
    it rests on never depends on the order things were matched in."""
    if _rank(new.state) != _rank(old.state):
        return _rank(new.state) < _rank(old.state)

    def tie(lit):
        return (lit.just[:1] == ("symmetric",), len(lit.support), lit.rules, repr(sorted(lit.support, key=repr)))
    return tie(new) < tie(old)


def _rank(state):
    return 0 if state in SUPPORTED else 2 if is_failure(state) else 1


def _unify(pattern, value, binding):
    if pattern.startswith("?"):
        return binding.setdefault(pattern, value) == value
    return pattern == value


def _test(op, a, b):
    if isinstance(a, Interval):
        return compare_intervals(op, a, b)
    return (a == b) if op == "eq" else (a != b)


def _order(x, y, a, b):
    if a.is_point and b.is_point and a.lo == b.lo:
        return f"{x} = {y}"
    if compare_intervals("gt", a, b):
        return f"{x} > {y}"
    if compare_intervals("lt", a, b):
        return f"{x} < {y}"
    return None


def _subst(term, theta):
    if isinstance(term, str):
        return theta.get(term, term)
    if isinstance(term, list):
        return [_subst(t, theta) for t in term]
    return term


def _embed(premises, targets, theta):
    """Map every premise into some target premise, extending theta; None if impossible."""
    if not premises:
        return theta
    head, rest = premises[0], premises[1:]
    for target in targets:
        if target[0] != head[0] or len(target) != len(head):
            continue
        t = dict(theta)
        if all(_unify(g, s, t) for g, s in zip(head[1:], target[1:])):
            found = _embed(rest, targets, t)
            if found is not None:
                return found
    return None


def _subsumes(general, specific, relations):
    """True when `specific`'s pattern contains `general`'s: with the conclusions aligned, every premise
    and condition of `general` appears in `specific`, and `specific` has more."""
    hg, hs = general["conclude"], specific["conclude"]
    if hg[0] != hs[0] or len(hg) != len(hs):
        return False
    orders = [hs[1:]]
    if len(hs) == 3 and relations.get(hs[0], {}).get("symmetric"):
        orders.append(list(reversed(hs[1:])))
    size = lambda r: len(r["match"]) + len(r.get("conditions", []))  # noqa: E731
    for args in orders:
        theta = {}
        if not all(_unify(g, s, theta) for g, s in zip(hg[1:], args)):
            continue
        theta = _embed(general["match"], specific["match"], theta)
        if theta is None:
            continue
        conditions = [repr(c) for c in specific.get("conditions", [])]
        if all(repr(_subst(c, theta)) in conditions for c in general.get("conditions", [])) \
                and size(specific) > size(general):
            return True
    return False


def _resolve_any(key, pos, neg, undecided, more_specific=None):
    """For a relation that holds if any derivation holds: one decided 'yes' is enough."""
    if pos is not None:
        return pos
    if undecided is not None:
        return undecided
    return neg


def _resolve(key, pos, neg, undecided, more_specific):
    """Settle one literal. Opposite conclusions: the law whose pattern contains the other's wins;
    a given fact contradicted by a law is CONFLICTED; otherwise AMBIGUOUS_PATTERN.

    pos/neg are decided derivations; `undecided` is one whose premises or conditions were not settled.
    """
    if pos is not None and neg is not None:
        rules = pos.rules + neg.rules
        if "input" in rules:
            law = " + ".join(r for r in rules if r != "input")
            return Literal(key, None, State.CONFLICTED, (f"the given {show(key)} contradicts {law}",),
                           pos.support | neg.support, rules, pos.attrs)
        if more_specific(pos, neg):
            return replace(pos, beats=pos.beats + tuple(neg.rules[-1:]))
        if more_specific(neg, pos):
            return replace(neg, beats=neg.beats + tuple(pos.rules[-1:]))
        cause = (f"{' + '.join(pos.rules)} concludes {show(key)}; {' + '.join(neg.rules)} concludes the "
                 f"opposite; neither law's pattern contains the other's")
        return Literal(key, None, State.AMBIGUOUS_PATTERN, (cause,), pos.support | neg.support, rules, pos.attrs)
    decided = pos if pos is not None else neg
    if undecided is None:
        return decided
    lits = [lit for lit in (decided, undecided) if lit is not None]
    state, causes = combine([(lit.state, lit.causes) for lit in lits])
    return Literal(key, None, state, causes, frozenset().union(*(lit.support for lit in lits)),
                   sum((lit.rules for lit in lits), ()), lits[0].attrs)


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
        ast.GtE: operator.ge, ast.Eq: operator.eq}


def _eval(node, env):
    if isinstance(node, str):
        node = ast.parse(node, mode="eval").body
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_eval(node.operand, env))
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return _OPS[type(node.ops[0])](_eval(node.left, env), _eval(node.comparators[0], env))
    raise ValueError(f"unsupported expression: {ast.unparse(node)}")


def _holds(expr, env):
    """Does a boundary hold? True or False for exact values; for ranges, True only if it holds for
    every value in them, False if for no value, None if for some."""
    tree = ast.parse(expr, mode="eval").body
    if not (isinstance(tree, ast.Compare) and len(tree.ops) == 1):
        return bool(_eval(tree, env))
    left, right = _eval(tree.left, env), _eval(tree.comparators[0], env)
    if not isinstance(left, Interval) and not isinstance(right, Interval):
        return bool(_OPS[type(tree.ops[0])](left, right))
    op = {ast.Gt: "gt", ast.Lt: "lt", ast.GtE: "ge", ast.LtE: "le"}.get(type(tree.ops[0]))
    if op is None:
        return None
    return compare_intervals(op, Interval.of(left), Interval.of(right))


def _show_number(v):
    return v.describe() if isinstance(v, Interval) else fmt_num(v)


def _names(expr):
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}


class _Index:
    """The literals rules can match (settled ones not ruled out, and new conclusions that hold),
    looked up by relation and by any bound argument."""

    def __init__(self):
        self.lit = {}       # key -> the literal matched for it now
        self.by_rel = {}    # relation -> {key: None}, in the order they appeared
        self.by_arg = {}    # (relation, position, value) -> {key: None}

    def expose(self, key, lit):
        if key not in self.lit:
            self.by_rel.setdefault(key[0], {})[key] = None
            for i, v in enumerate(key[1:], start=1):
                self.by_arg.setdefault((key[0], i, v), {})[key] = None
        self.lit[key] = lit

    def _candidates(self, head, binding):
        rel = head[0]
        for i, term in enumerate(head[1:], start=1):
            value = binding.get(term) if term.startswith("?") else term
            if value is not None:
                return list(self.by_arg.get((rel, i, value), ()))
        return list(self.by_rel.get(rel, ()))

    def _match(self, premises, seeds, binding, used):
        if not premises:
            yield dict(binding), used
            return
        head, rest = premises[0], premises[1:]
        seed = seeds[0]
        keys = [seed] if seed is not None else self._candidates(head, binding)
        for key in keys:
            if len(key) != len(head) or key[0] != head[0]:
                continue
            b = dict(binding)
            if all(_unify(p, v, b) for p, v in zip(head[1:], key[1:])):
                yield from self._match(rest, seeds[1:], b, used + (self.lit[key],))

    def matches(self, premises, delta):
        """Every binding of the premises; with a delta, only those using at least one changed literal."""
        if delta is None:
            yield from self._match(premises, [None] * len(premises), {}, ())
            return
        seen = set()
        for j, head in enumerate(premises):
            for key in delta:
                if key[0] != head[0]:
                    continue
                seeds = [None] * len(premises)
                seeds[j] = key
                for binding, used in self._match(premises, seeds, {}, ()):
                    mark = tuple(lit.key for lit in used)
                    if mark not in seen:
                        seen.add(mark)
                        yield binding, used


class World:
    """The headspace H for one run."""

    def __init__(self, given, domain, headspace_limit):
        self.given, self.domain, self.lang = given, domain, given.lang
        self.limit = headspace_limit
        self.headspace = None
        self.rules = {r["id"]: r for r in domain.get("rules", [])}
        self.entities = {}
        self.literals = {}
        self.op_calls = 0
        self.nogoods = {}   # description -> frozenset of assumptions that cannot all hold

    def _nogood(self, description, *atoms):
        self.nogoods.setdefault(description, frozenset(atoms))

    def _more_specific(self, a, b):
        """The law that concluded `a` has a pattern containing the law that concluded `b`."""
        ra, rb = self.rules.get(a.rules[-1]), self.rules.get(b.rules[-1])
        return ra is not None and rb is not None and _subsumes(rb, ra, self.lang.relations)

    # ================================================================ Relate
    def relate(self):
        """Step 2: build H from the given facts that did not fail in Deduce."""
        for var, spec in self.given.entities.items():
            if spec is None:  # its introduction failed; questions about it already carry that failure
                continue
            e = Entity(var, spec["kind"])
            for q, (iv, unit) in spec["quantities"].items():
                shifted = q in spec.get("shifted", ())
                e.quantities[q] = Value(iv, State.DERIVED if shifted else State.KNOWN, unit,
                                        ("input", "shift") if shifted else ("input",))
                e.origin[q] = frozenset([("given", q, var)])
                e.just[q] = ("shift",) if shifted else ("given",)
            for c, v in spec["categories"].items():
                e.categories[c] = Value(v, State.KNOWN, None, ("input",))
                e.origin[c] = frozenset([("given", c, var)])
                e.just[c] = ("given",)
            for name, (given_value, observed) in spec.get("disputed", {}).items():
                if name in self.lang.quantities:  # ((Interval, unit), (Interval, unit))
                    unit = given_value[1]
                    alternatives = ((given_value[0].lo, "given"), (observed[0].lo, "observed"))
                    e.quantities[name] = Value(None, State.CONFLICTED, unit, ("input",),
                                               (f"given and observed {name} of {var} disagree",), alternatives,
                                               f"re-measure {name} of {var}")
                else:
                    e.categories[name] = Value(None, State.CONFLICTED, None, ("input",),
                                               (f"given and observed {name} of {var} disagree",),
                                               ((given_value, "given"), (observed, "observed")),
                                               f"re-observe {name} of {var}")
                e.origin[name] = frozenset([("given", name, var), ("observed", name, var)])
                self._nogood(f"the given and the observed {name} of {var} disagree",
                             ("given", name, var), ("observed", name, var))
            self.entities[var] = e
            for cls in self.lang.ancestors(e.kind):
                self._given(("isa", var, cls))
        for rel, a, b, attrs in self.given.relations:
            if a not in self.entities or b not in self.entities:
                continue
            self._given((rel, a, b), attrs)
            if self.lang.relations[rel].get("symmetric"):
                self._given((rel, b, a), attrs)
        chunks = self._chunks()
        self.headspace = {"chunks": chunks, "limit": self.limit, "wide": chunks > self.limit}  # a cost, not a wall

    def _given(self, key, attrs=None):
        self.literals[key] = Literal(key, True, State.KNOWN, (), frozenset([key]), ("input",),
                                     tuple(sorted((attrs or {}).items())), ("given",))

    def _chunks(self):
        """Entities bound together by a binding relation count as one chunk."""
        parent = {v: v for v in self.entities}

        def find(v):
            while parent[v] != v:
                parent[v] = parent[parent[v]]
                v = parent[v]
            return v

        for rel, a, b, _ in self.given.relations:
            if self.lang.relations[rel].get("binds") and a in parent and b in parent:
                parent[find(a)] = find(b)
        return len({find(v) for v in self.entities})

    def attribute(self, node, attr):
        """Knowledge-store lookup, inherited down the tree. Disagreeing sources give CONFLICTED."""
        unit = self.lang.attributes.get(attr)
        for cls in self.lang.ancestors(node):
            facts = [f for f in self.domain.get("facts", [])
                     if f["term"] == cls and f["attr"] == attr and not f.get("refuted_by")]
            if not facts:
                continue
            provenance = tuple(f"{attr} of {cls}: {f['source']}" for f in facts)
            support = frozenset(("store", cls, attr, f["source"]) for f in facts)
            if len({f["value"] for f in facts}) == 1:
                return Value(facts[0]["value"], State.KNOWN, unit, provenance, support=support)
            sources = " vs ".join(f["source"] for f in facts)
            said = ", ".join(f"{f['source']} says {fmt_num(f['value'])}" for f in facts)
            self._nogood(f"{attr} of {cls}: {said}; they cannot all be right", *sorted(support))
            record = self.domain.get("source_record", {})
            track = [f"{s} was right {record[s]['right']} of {record[s]['right'] + record[s]['wrong']} times when measured"
                     for s in sorted({f["source"] for f in facts}) if s in record]
            causes = (f"sources disagree on {attr} of {cls}",) + ((
                "; ".join(track) + " (a record, not evidence: the value stays unsettled)",) if track else ())
            return Value(None, State.CONFLICTED, unit, provenance, causes,
                         tuple((f["value"], f["source"]) for f in facts),
                         f"measure {attr} of {cls} to settle {sources}", support)
        return Value(None, State.MISSING_KNOWLEDGE, unit, (), (f"{attr} of {node} is not in the knowledge store",))

    # ================================================================ Compare
    def compare(self):
        """Step 3: match every law against H."""
        self._apply_bands()
        for group in stratify(self.domain.get("rules", [])):
            self._run(group)

    def _apply_bands(self):
        bands = self.domain.get("bands", [])
        for e in self.entities.values():
            for c in self.lang.categories:
                laws = [b for b in bands if b["category"] == c
                        and any(self.lang.is_a(e.kind, t) for t in b["applies_to"])]
                for law in laws:
                    if "fixed" in law:
                        self._fixed(e, c, law)
                    else:
                        self._band(e, c, law)
                if not laws and c not in e.categories:
                    self._near_miss(e, c, bands)

    def _fixed(self, e, c, law):
        observed = e.categories.get(c)
        if observed is not None and observed.state is State.KNOWN and observed.payload != law["fixed"]:
            cause = (f"observed {c} '{observed.payload}' of {e.var} contradicts {law['id']}: "
                     f"{c} of every {' or '.join(law['applies_to'])} is '{law['fixed']}'")
            e.categories[c] = Value(None, State.CONFLICTED, None, ("input", law["id"]), (cause,),
                                    experiment=f"re-observe {c} of {e.var}; if it holds, "
                                               f"{law['id']} does not cover {e.kind}")
            self._nogood(cause, ("given", c, e.var), ("law", law["id"]))
        elif observed is None:
            e.categories[c] = Value(law["fixed"], State.DERIVED, None, (law["id"],))
            e.origin[c] = frozenset([("isa", e.var, e.kind)])
            e.just[c] = ("fixed", law["id"])

    def _band(self, e, c, law):
        q = law["quantity"]
        unit = self.lang.quantities[q]["unit"]
        observed = e.categories.get(c)
        current = e.quantities.get(q)
        if observed is not None and observed.state is State.KNOWN:
            band = next((b for b in law["bands"] if b["value"] == observed.payload), None)
            if band is None:
                return
            implied = band_interval(band)
            if current is None:
                e.quantities[q] = Value(implied, State.DERIVED, unit, (law["id"],))
                e.origin[q] = e.origin.get(c, frozenset())
                e.just[q] = ("band-from", law["id"], c)
            elif current.payload is not None and current.payload.intersect(implied) is None:
                cause = (f"{q}({e.var}) = {current.payload.describe(unit)} contradicts {c} "
                         f"'{observed.payload}' under {law['id']} ({implied.describe(unit)})")
                experiment = f"re-measure {q} and re-observe {c} of {e.var}"
                self._nogood(cause, ("given", q, e.var), ("given", c, e.var), ("law", law["id"]))
                e.quantities[q] = Value(None, State.CONFLICTED, unit, ("input", law["id"]), (cause,),
                                        experiment=experiment)
                e.categories[c] = Value(None, State.CONFLICTED, None, ("input", law["id"]), (cause,),
                                        experiment=experiment)
            return
        if current is not None and current.payload is None and current.alternatives:
            # disputed quantity: settle the band once per source; the same band under every source settles it
            bands = tuple((band_value(law, Interval.point(v)), src) for v, src in current.alternatives)
            if len({b for b, _ in bands}) == 1 and bands[0][0] is not None:
                e.categories[c] = Value(bands[0][0], State.DERIVED, None, (law["id"],))
                e.just[c] = ("band-every-source", law["id"], q)
            else:
                e.categories[c] = Value(None, State.CONFLICTED, None, (law["id"],), current.causes, bands,
                                        current.experiment)
            e.origin[c] = e.origin.get(q, frozenset())
            return
        if current is not None and current.payload is not None:
            value = band_value(law, current.payload)
            if value is not None:
                e.categories[c] = Value(value, State.DERIVED, None, (law["id"],))
                e.origin[c] = e.origin.get(q, frozenset())
                e.just[c] = ("band-to", law["id"], q)
                return
            e.undecided[c] = (f"{q}({e.var}) is only known to be {current.payload.describe(unit)}, "
                              f"which spans more than one {c} band of {law['id']}")
        else:
            e.undecided[c] = f"{q}({e.var}) is not known, and {law['id']} needs it to settle {c}"

    def _near_miss(self, e, c, bands):
        """A law that covers a sibling kind: kept as a confabulation, used only if Plan asks."""
        for law in bands:
            if law["category"] != c or "bands" not in law:
                continue
            for leaf in law["applies_to"]:
                if not self.lang.is_sibling(e.kind, leaf):
                    continue
                q = law["quantity"]
                unit = self.lang.quantities[q]["unit"]
                current = e.quantities.get(q)
                if current is None or current.payload is None:
                    continue
                value = band_value(law, current.payload)
                if value is None:
                    continue
                e.guesses[c] = Value(
                    value, State.CONFABULATED, None, (f"{law['id']} by analogy with {leaf}",),
                    (f"no law gives {c} for {e.kind}; {law['id']} covers {leaf}, a sibling of {e.kind}",),
                    experiment=f"observe {e.kind} at {current.payload.describe(unit)} and record its {c}")
                return

    def _run(self, rules):
        """Forward chaining to a fixpoint, matched through an index and re-matched only where something
        changed (semi-naive): after the first round, a rule is tried only on bindings that use a premise
        that appeared or improved in the round before."""
        derived = {}
        index = _Index()
        settled = {k for k, lit in self.literals.items() if lit.polarity is not False}
        for key in settled:
            index.expose(key, self.literals[key])
        delta = None   # the first round matches everything
        while True:
            fresh = set()
            for rule in rules:
                for binding, premises in index.matches(rule["match"], delta):
                    if any(binding.get(a) == binding.get(b) for a, b in rule.get("distinct", [])):
                        continue
                    lit = self._derive(rule, binding, premises)
                    if lit is None:
                        continue
                    keys = [lit.key]
                    if len(lit.key) == 3 and self.lang.relations.get(lit.key[0], {}).get("symmetric"):
                        keys.append((lit.key[0], lit.key[2], lit.key[1]))
                    for key in keys:
                        slot = (key, lit.polarity)
                        old = derived.get(slot)
                        new = replace(lit, key=key) if key == lit.key else \
                            replace(lit, key=key, just=("symmetric", lit.key, lit.just))
                        if old is None or _better(new, old):
                            derived[slot] = new
                            if lit.polarity is True and key not in settled:   # a premise later rules can use
                                index.expose(key, derived[slot])
                                fresh.add(key)
            if not fresh:
                break
            delta = fresh
        keys = {k for k, _ in derived}
        for key in keys:
            pos, neg, undecided = (derived.get((key, p)) for p in (True, False, None))
            given = self.literals.get(key)
            if given is not None and (pos is None or _rank(given.state) <= _rank(pos.state)):
                pos = given
            resolve = _resolve_any if self.lang.relations.get(key[0], {}).get("any") else _resolve
            self.literals[key] = resolve(key, pos, neg, undecided, self._more_specific)
        self._exclusive(keys)

    def _exclusive(self, keys):
        """A relation marked exclusive holds in at most one direction between two entities."""
        for key in sorted(keys):
            if len(key) != 3 or not self.lang.relations.get(key[0], {}).get("exclusive"):
                continue
            rev = (key[0], key[2], key[1])
            a, b = self.literals.get(key), self.literals.get(rev)
            if key > rev or a is None or b is None or a.polarity is not True or b.polarity is not True:
                continue
            if self._more_specific(a, b):
                self.literals[rev] = replace(b, polarity=False, rules=a.rules)
            elif self._more_specific(b, a):
                self.literals[key] = replace(a, polarity=False, rules=b.rules)
            else:
                cause = (f"{' + '.join(a.rules)} concludes {show(key)}; {' + '.join(b.rules)} concludes "
                         f"{show(rev)}; neither law's pattern contains the other's")
                for k in (key, rev):
                    self.literals[k] = Literal(k, None, State.AMBIGUOUS_PATTERN, (cause,),
                                               a.support | b.support, a.rules + b.rules)

    def _candidates(self, rel, derived):
        seen = set()
        for key, lit in self.literals.items():
            if key[0] == rel and lit.polarity is not False:
                seen.add(key)
                yield lit
        for (key, polarity), lit in derived.items():
            if key[0] == rel and polarity is True and key not in seen:
                yield lit

    def _matches(self, premises, derived, binding=None, used=()):
        binding = binding or {}
        if not premises:
            yield dict(binding), used
            return
        head, rest = premises[0], premises[1:]
        for lit in self._candidates(head[0], derived):
            if len(lit.key) != len(head):
                continue
            b = dict(binding)
            if all(_unify(p, v, b) for p, v in zip(head[1:], lit.key[1:])):
                yield from self._matches(rest, derived, b, used + (lit,))

    def _derive(self, rule, binding, premises):
        parts = [(p.state, p.causes) for p in premises]
        settled = all(p.polarity is not None for p in premises)
        support = frozenset().union(*(p.support for p in premises))
        for cond in rule.get("conditions", []):
            outcome, state, causes, cond_support = self._condition(cond, binding)
            if outcome is False:
                return None
            settled = settled and outcome is not None
            parts.append((state, causes))
            support |= cond_support
        state, causes = combine(parts)
        if state is State.KNOWN:
            state = State.DERIVED
        chain = []
        for p in premises:
            chain += [r for r in p.rules if r != "input" and r not in chain]
        chain.append(rule["id"])
        head = rule["conclude"]
        key = (head[0],) + tuple(binding.get(t, t) for t in head[1:])
        polarity = rule.get("polarity", True) if settled else None
        just = ("rule", rule["id"], tuple(sorted(binding.items())), tuple(p.key for p in premises),
                tuple(repr(_subst(c, binding)) for c in rule.get("conditions", [])))
        return Literal(key, polarity, state, causes, support, tuple(chain), (), just)

    def _condition(self, cond, binding):
        """Three-valued: (True/False/None, state, causes, support). Disagreeing sources are tried one by one."""
        op, left, right = cond
        a, b = self._term(left, binding), self._term(right, binding)
        support = a.support | b.support
        if not a.options() or not b.options():
            state, causes = combine([(a.state, a.causes), (b.state, b.causes)])
            return None, state, causes, support
        outcomes = {_test(op, pa, pb) for (pa, _), (pb, _) in itertools.product(a.options(), b.options())}
        if None in outcomes:
            return (None, State.MISSING_KNOWLEDGE,
                    (f"cannot decide whether {a.text()} {SYMBOL[op]} {b.text()}",), support)
        if len(outcomes) > 1:
            return None, State.CONFLICTED, a.causes + b.causes + (
                f"whether {a.text()} {SYMBOL[op]} {b.text()} depends on which source is right",), support
        # Decided the same way under every source: the disagreement does not matter here.
        state, causes = combine([(t.state, t.causes) for t in (a, b) if not t.alternatives])
        return outcomes.pop(), state, causes, support

    def _term(self, spec, binding):
        if isinstance(spec, (int, float)):
            return Term(Interval.point(spec), State.KNOWN, (), fmt_num(spec))
        if isinstance(spec, str):
            return Term(spec, State.KNOWN, (), spec)
        kind = spec[0]
        if kind == "q":
            _, q, var = spec
            e = self.entities[binding[var]]
            label, unit = f"{q}({e.var})", self.lang.quantities[q]["unit"]
            v = e.quantities.get(q)
            if v is None:
                return Term(None, State.MISSING_KNOWLEDGE, (f"{label} is not known",), label, unit)
            return Term(v.payload, v.state, v.causes, label, unit, v.provenance, v.alternatives, v.experiment,
                        e.origin.get(q, frozenset()))
        if kind == "c":
            _, c, var = spec
            e = self.entities[binding[var]]
            label = f"{c}({e.var})"
            v = e.categories.get(c)
            if v is None:
                return Term(None, State.MISSING_KNOWLEDGE, (e.undecided.get(c, f"{label} is not known"),), label)
            return Term(v.payload, v.state, v.causes, label, None, v.provenance, v.alternatives, v.experiment,
                        e.origin.get(c, frozenset()))
        if kind == "a":
            _, var, attr = spec
            e = self.entities[binding[var]]
            return self._attr_term(e.kind, attr)
        raise ValueError(f"unknown term {spec}")

    def _attr_term(self, node, attr):
        v = self.attribute(node, attr)
        payload = None if v.payload is None else Interval.point(v.payload)
        return Term(payload, v.state, v.causes, f"{attr} of {node}", v.unit, v.provenance,
                    v.alternatives, v.experiment, v.support)

    # ================================================================ Plan
    def answer(self, goal):
        """Step 4 (and 5 when an operation is needed). Returns (Value, step that produced it)."""
        try:
            value, step = self._strict(goal)
        except (IndexError, KeyError, ValueError) as err:
            return failure(State.UNKNOWN_STRUCTURE, f"malformed question: {err}"), "compare"
        gate = value.state is State.NO_VALID_RULE or (goal.estimate and value.state is State.MISSING_KNOWLEDGE)
        if gate:
            guess = self._guess(goal, value)
            if guess is not None:
                return guess, "plan"
        return value, step

    def _strict(self, goal):
        name, args = goal.name, goal.args
        if name == "value":
            node, attr = self._kind(args[0]), args[1]
            if attr not in self.lang.attributes:
                return failure(State.UNKNOWN_STRUCTURE, f"'{attr}' is not a known attribute"), "compare"
            return replace(self.attribute(node, attr), just=("attribute", node, attr)), "relate"
        if name == "class":
            node = self._kind(args[0])
            return Value(self.lang.parent.get(node), State.KNOWN, None, ("reference tree",),
                         just=("class", node)), "relate"
        if name == "compare":
            return self._compare_goal(*args, shown=goal.raw[1:])
        if name == "has":
            return self._category_goal(args[0], args[1], as_bool=True)
        if name in self.lang.quantities:
            return self._quantity_goal(name, args[0])
        if name in self.lang.categories:
            return self._category_goal(name, args[0])
        ops = [op for op in self.domain.get("operations", []) if op["goal"] == name]
        if ops:
            return self._operation_goal(ops, *args)
        if name in self.lang.relations:
            return self._relation_goal(name, args)
        return failure(State.UNKNOWN_STRUCTURE, f"no known structure answers '{name}'"), "compare"

    def _kind(self, name):
        """An introduced name stands for its entity's kind; anything else is already a kind."""
        return self.entities[name].kind if name in self.entities else name

    def _guess(self, goal, strict):
        """Confabulation, gated: a near-miss law, or (only when asked to estimate) a sibling's fact."""
        name = goal.name
        if name == "has" or name in self.lang.categories:
            c, var = (goal.args[0], goal.args[1]) if name == "has" else (name, goal.args[0])
            guess = self.entities[var].guesses.get(c)
            if guess is not None and name == "has":
                guess = replace(guess, payload="no" if guess.payload == self._absent(c) else "yes")
            return guess
        if name == "value" and goal.estimate:
            node, attr = self._kind(goal.args[0]), goal.args[1]
            for sibling in sorted(n for n in self.lang.parent if self.lang.is_sibling(node, n)):
                v = self.attribute(sibling, attr)
                if v.state in SUPPORTED:
                    return Value(v.payload, State.CONFABULATED, v.unit,
                                 (f"by analogy with {sibling}",) + v.provenance, strict.causes,
                                 experiment=f"measure {attr} of {node}")
        return None

    def _absent(self, c):
        return self.lang.categories[c].get("absent")

    def _compare_goal(self, name, x, y, shown=()):
        sx, sy = shown if len(shown) == 2 else (x, y)
        if name in self.lang.quantities:
            if x not in self.entities or y not in self.entities:
                return failure(State.UNKNOWN_STRUCTURE, f"comparing {name} needs two introduced entities"), "compare"
            a = self._term(["q", name, "?x"], {"?x": x})
            b = self._term(["q", name, "?y"], {"?y": y})
        elif name in self.lang.attributes:
            a = self._attr_term(self.entities[x].kind if x in self.entities else x, name)
            b = self._attr_term(self.entities[y].kind if y in self.entities else y, name)
        else:
            return failure(State.UNKNOWN_STRUCTURE, f"'{name}' is not a known quantity or attribute"), "compare"
        if not a.options() or not b.options():
            state, causes = combine([(a.state, a.causes), (b.state, b.causes)])
            return Value(None, state, None, (), causes), "compare"
        results = [(_order(sx, sy, pa, pb), "/".join(s for s in (sa, sb) if s))
                   for (pa, sa), (pb, sb) in itertools.product(a.options(), b.options())]
        texts = {text for text, _ in results}
        if None in texts:
            return failure(State.MISSING_KNOWLEDGE, f"cannot decide how {a.text()} compares with {b.text()}"), "compare"
        provenance = tuple(dict.fromkeys(a.provenance + b.provenance))
        support = a.support | b.support
        if len(texts) == 1:  # the same under every source
            state, _ = combine([(t.state, t.causes) for t in (a, b) if not t.alternatives])
            return Value(texts.pop(), State.DERIVED if state in SUPPORTED else state, None, provenance,
                         support=support, just=("compare", name, x, y)), "compare"
        return Value(None, State.CONFLICTED, None, provenance, a.causes + b.causes, tuple(results),
                     a.experiment or b.experiment, support), "compare"

    def _quantity_goal(self, q, var):
        e = self.entities[var]
        v = e.quantities.get(q)
        if v is not None:
            return replace(v, support=v.support | e.origin.get(q, frozenset()), just=("quantity", q, var)), "compare"
        conflicts = [(c, cv) for c, cv in e.categories.items() if cv.state is State.CONFLICTED]
        if conflicts:
            c, cv = conflicts[0]
            return replace(cv, support=cv.support | e.origin.get(c, frozenset())), "compare"
        return failure(State.NO_VALID_RULE, f"no law gives {q} of {e.kind} from the facts given"), "compare"

    def _category_goal(self, c, var, as_bool=False):
        if c not in self.lang.categories:
            return failure(State.UNKNOWN_STRUCTURE, f"'{c}' is not a known category"), "compare"
        e = self.entities[var]
        v = e.categories.get(c)
        if v is None:
            if c in e.undecided:
                return failure(State.MISSING_KNOWLEDGE, e.undecided[c]), "compare"
            return failure(State.NO_VALID_RULE, f"no law gives {c} for {e.kind}"), "compare"
        if as_bool and v.payload is not None:
            v = replace(v, payload="no" if v.payload == self._absent(c) else "yes")
        return replace(v, support=v.support | e.origin.get(c, frozenset()), just=("category", c, var, as_bool)), "compare"

    def _relation_goal(self, name, args):
        spec = self.lang.relations[name]
        directed = spec.get("directed") and len(args) == 2
        keys = [(name,) + tuple(args)]
        if directed:
            keys.append((name, args[1], args[0]))
        order = {True: 0, False: 1, None: 2}  # a holding direction first
        lits = sorted((self.literals[k] for k in keys if k in self.literals), key=lambda lit: order[lit.polarity])
        for lit in lits:
            if lit.polarity is not None:
                if directed:
                    payload = f"{lit.key[1]} → {lit.key[2]}" if lit.polarity else f"not {show(lit.key)}"
                else:
                    payload = "yes" if lit.polarity else "no"
                return Value(payload, lit.state, None, lit.rules, lit.causes, support=lit.support,
                             just=("relation", lit.key, lit.polarity)), "compare"
        if lits:
            return Value(None, lits[0].state, None, lits[0].rules, lits[0].causes, support=lits[0].support), "compare"
        where = " in either direction" if directed else ""
        return failure(State.NO_VALID_RULE, f"no law concludes {show(keys[0])}{where} from the facts given"), "compare"

    def _operation_goal(self, ops, x, y):
        outcomes = [self._plan_operation(op, x, y) for op in ops]
        for outcome in outcomes:
            if is_decided(outcome[0].state):
                return outcome
        return min(outcomes, key=lambda o: PRECEDENCE.index(o[0].state))

    def _plan_operation(self, op, x, y):
        causes = []
        for a, b in ((x, y), (y, x)):
            binding = {"?a": a, "?b": b}
            keys = [(p[0],) + tuple(binding.get(t, t) for t in p[1:]) for p in op["match"]]
            premises = [self.literals.get(k) for k in keys]
            absent = [k for k, lit in zip(keys, premises) if lit is None or lit.polarity is False]
            if absent:
                for k, lit in zip(keys, premises):
                    if lit is not None and lit.polarity is False:
                        causes.append(f"{show(k)} is ruled out by {' + '.join(lit.rules)}")
                causes.append(f"{op['id']} needs {', '.join(show(k) for k in absent)}")
                continue
            orient, state, orient_causes, _ = self._condition(op["orient"], binding)
            if orient is False:
                continue
            parts = [(p.state, p.causes) for p in premises]
            support = frozenset().union(*(p.support for p in premises))
            if orient is None:
                parts.append((state, orient_causes))
                state, all_causes = combine(parts)
                return Value(None, state, op.get("unit"), (), all_causes, support=support), "plan"
            value, step = self._prepare(op, binding, parts, support)
            return replace(value, just=("operation", op["id"], tuple(sorted(binding.items())))), step
        return failure(State.NO_VALID_RULE, *dict.fromkeys(causes or [f"{op['id']} does not apply"])), "plan"

    def _prepare(self, op, binding, parts, support=frozenset()):
        inputs, choices, provenance = {}, {}, [op["id"]]
        for name, spec in op["inputs"].items():
            v = self._input(spec, binding)
            support |= v.support
            if v.state is State.CONFLICTED and v.alternatives:
                choices[name] = v  # sources disagree: run once per source, promote no result
            elif v.payload is None:
                parts.append((v.state, v.causes))
            else:
                inputs[name] = v.payload
                provenance.extend(v.provenance)
        # Execute's guard: boundaries are checked on the runtime values before the operation runs.
        # With a range among the inputs the check has three outcomes: holds for every value in it,
        # for no value in it (out of boundary), or for only part of it (an exact value is needed).
        for expr in op.get("boundary", []):
            if not _names(expr) <= set(inputs):
                continue
            holds = _holds(expr, inputs)
            if holds is True:
                continue
            tree = ast.parse(expr, mode="eval").body
            side = tree.left if isinstance(tree, ast.Compare) else tree
            here = _show_number(_eval(side, inputs))
            if holds is False:
                parts.append((State.OUT_OF_BOUNDARY, (f"{op['id']} is valid only when {expr}; here {ast.unparse(side)} = {here}",)))
            else:
                parts.append((State.MISSING_KNOWLEDGE, (
                    f"{op['id']} is valid only when {expr}; here {ast.unparse(side)} is {here}, which is only partly "
                    "inside that, so an exact value is needed",)))
        state, causes = combine(parts)
        if not is_decided(state):
            step = "execute" if state is State.OUT_OF_BOUNDARY else "plan"
            return Value(None, state, op.get("unit"), (), causes, support=support), step
        value, step = self._execute(op, inputs, choices, tuple(dict.fromkeys(provenance)), state)
        return replace(value, support=value.support | support), step

    def _input(self, spec, binding):
        kind = spec[0]
        if kind == "a":
            return self.attribute(self.entities[binding[spec[1]]].kind, spec[2])
        if kind == "q":
            _, q, var = spec
            e = self.entities[binding[var]]
            label = f"{q}({e.var})"
            v = e.quantities.get(q)
            if v is None:
                return failure(State.MISSING_KNOWLEDGE, f"{label} is not known")
            if v.payload is None:
                return v
            # a range goes into the operation as a range (spec revision 2): the answer is then a range
            payload = v.payload.lo if v.payload.is_point else v.payload
            return Value(payload, v.state, v.unit, tuple(f"{label}: {p}" for p in v.provenance),
                         support=e.origin.get(q, frozenset()))
        if kind == "r":
            _, rel, a, b, attr = spec
            key = (rel, binding[a], binding[b])
            lit = self.literals.get(key)
            attrs = dict(lit.attrs) if lit is not None else {}
            if attr not in attrs:
                return failure(State.MISSING_KNOWLEDGE, f"{attr} of {show(key)} is not given")
            return Value(attrs[attr], State.KNOWN, None, (f"{attr} of {show(key)}: input",),
                         support=frozenset([key]) | (lit.support if lit is not None else frozenset()))
        raise ValueError(f"unknown input {spec}")

    # ================================================================ Execute
    def _execute(self, op, inputs, choices, provenance, state):
        """Step 5: run the operation, once per combination of disagreeing sources."""
        names = list(choices)
        results = []
        for combo in itertools.product(*(choices[n].alternatives for n in names)):
            env = dict(inputs)
            env.update({n: val for n, (val, _) in zip(names, combo)})
            self.op_calls += 1
            try:
                out = _eval(op["formula"], env)
                if isinstance(out, Interval):
                    out = out.simplest()   # a range in, a range out; one value stays a number
                results.append((out, "/".join(src for _, src in combo)))
            except ZeroDivisionError:
                return failure(State.OUT_OF_BOUNDARY, f"{op['id']} is undefined for these values"), "execute"
        unit = op.get("unit")
        if names and len({r for r, _ in results}) > 1:
            causes = tuple(c for n in names for c in choices[n].causes)
            experiment = next((choices[n].experiment for n in names if choices[n].experiment), None)
            return Value(None, State.CONFLICTED, unit, provenance, causes, tuple(results), experiment), "execute"
        return Value(results[0][0], State.DERIVED if state in SUPPORTED else state, unit, provenance), "execute"
