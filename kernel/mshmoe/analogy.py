"""Analogy across domains: a law one domain has and another lacks, carried over by structure.

structural_map(source, target) finds which of the source's symbols play the same part in the target
(kinds, relations, quantities, categories and their values, attributes), from structure alone: no
name is compared with a name. Evidence, strongest first:

    laws       two laws of the same shape (the same literals, conditions, conclusion and polarity,
               up to renaming) pair their symbols; the largest set of laws that pair consistently wins
    bands      band laws pair their kinds, quantity, category and the category's values in order
    operations the same formula over the same kinds of inputs pairs attributes and relation attributes
    facts      kinds with stored facts pair by what is stored (which attributes, from how many sources);
               attributes of one unit on one kind pair by the order of their values
    the tree   a parent of paired kinds is paired; the one unpaired child left on each side is paired
    flags      a relation whose flags (symmetric, directed, ...) no other unpaired relation shares

A symbol two or more ways fit equally is ambiguous and left unpaired, with its candidates.

transfer(map) gives the source laws the target has no counterpart for, renamed into the target's
words; a law that needs an unpaired symbol is not transferred, and the reason is kept.

apply(kernel, ...) runs during a kernel run: a question asked with `estimate` that its own domain
cannot answer (NO_VALID_RULE, MISSING_KNOWLEDGE) is tried again with the carried-over laws. What they
give is CONFABULATED, never KNOWN or DERIVED, with the law and the pairing it rests on. Numbers do not
carry over (the domains' units differ); laws whose conditions compare against a constant number are
not transferred.
"""
import copy
import itertools
from dataclasses import dataclass, field

from .states import SUPPORTED, State
from .values import Value

FAILED = (State.NO_VALID_RULE, State.MISSING_KNOWLEDGE)


@dataclass
class Map:
    source: str
    target: str
    symbols: dict = field(default_factory=dict)    # (class, source name) -> target name
    evidence: dict = field(default_factory=dict)   # (class, source name) -> why
    ambiguous: dict = field(default_factory=dict)  # (class, source name) -> [target names]
    pairs: list = field(default_factory=list)      # (source item id, target item id): laws, bands, operations
    unpaired: list = field(default_factory=list)   # source item ids with no counterpart in the target

    def get(self, cls, name):
        return self.symbols.get((cls, name))

    def describe(self, keys=None):
        keys = keys or sorted(self.symbols)
        return ", ".join(f"{name} → {self.symbols[(cls, name)]}" for cls, name in keys if (cls, name) in self.symbols)


# ------------------------------------------------------------------ the shapes of laws

class _Fit:
    """Pairing in progress: variables one to one, symbols one to one within their class."""

    def __init__(self):
        self.vars = {}
        self.back = {}
        self.sym = {}
        self.rsym = {}

    def copy(self):
        f = _Fit()
        f.vars, f.back, f.sym, f.rsym = dict(self.vars), dict(self.back), dict(self.sym), dict(self.rsym)
        return f

    def var(self, a, b):
        if self.vars.get(a, b) != b or self.back.get(b, a) != a:
            return False
        self.vars[a], self.back[b] = b, a
        return True

    def pair(self, cls, a, b):
        if self.sym.get((cls, a), b) != b or self.rsym.get((cls, b), a) != a:
            return False
        self.sym[(cls, a)], self.rsym[(cls, b)] = b, a
        return True


def _term(f, a, b):
    if isinstance(a, str) and a.startswith("?"):
        return isinstance(b, str) and b.startswith("?") and f.var(a, b)
    if isinstance(a, list) and isinstance(b, list) and a and b and a[0] == b[0] and len(a) == len(b):
        head = a[0]
        if head == "q":
            return f.pair("qty", a[1], b[1]) and _term(f, a[2], b[2])
        if head == "c":
            return f.pair("cat", a[1], b[1]) and _term(f, a[2], b[2])
        if head == "a":
            return _term(f, a[1], b[1]) and f.pair("attr", a[2], b[2])
        if head == "r":
            return (f.pair("rel", a[1], b[1]) and _term(f, a[2], b[2]) and _term(f, a[3], b[3])
                    and f.pair("rattr", a[4], b[4]))
        return False
    return False


def _literal(f, a, b):
    if len(a) != len(b):
        return False
    if a[0] == "isa" or b[0] == "isa":
        return a[0] == b[0] == "isa" and _term(f, a[1], b[1]) and f.pair("kind", a[2], b[2])
    return f.pair("rel", a[0], b[0]) and all(_term(f, x, y) for x, y in zip(a[1:], b[1:]))


def _condition(f, a, b, cats):
    """[op, t1, t2]: a category value beside a category term pairs as a value; a bare number cannot
    carry over (units differ), so a law that compares with one is not paired."""
    if a[0] != b[0] or len(a) != len(b):
        return False
    for x, y in zip(a[1:], b[1:]):
        if isinstance(x, list):
            if not _term(f, x, y):
                return False
        elif isinstance(x, str) and not x.startswith("?"):
            cat = next((t[1] for t in a[1:] if isinstance(t, list) and t[0] == "c"), None)
            if cat is None or not isinstance(y, str) or not f.pair("val", x, y):
                return False
        else:
            return False
    return True


def _law_fits(src, dst, cats):
    """Every way two laws pair, as _Fit objects (the match literals may come in any order)."""
    if len(src["match"]) != len(dst["match"]) or len(src.get("conditions", [])) != len(dst.get("conditions", [])):
        return []
    if src.get("polarity", True) != dst.get("polarity", True) or len(src.get("distinct", [])) != len(dst.get("distinct", [])):
        return []
    out = []
    for order in itertools.permutations(dst["match"]):
        f = _Fit()
        if not all(_literal(f, a, b) for a, b in zip(src["match"], order)):
            continue
        if not all(_condition(f, a, b, cats) for a, b in zip(src.get("conditions", []), dst.get("conditions", []))):
            continue
        if not _literal(f, src["conclude"], dst["conclude"]):
            continue
        if {frozenset(f.vars.get(v, "") for v in pair) for pair in src.get("distinct", [])} != \
                {frozenset(pair) for pair in dst.get("distinct", [])}:
            continue
        out.append(f)
    return out


def _band_fits(src, dst):
    f = _Fit()
    if ("bands" in src) != ("bands" in dst) or len(src["applies_to"]) != len(dst["applies_to"]):
        return []
    if not all(f.pair("kind", a, b) for a, b in zip(src["applies_to"], dst["applies_to"])):
        return []
    if not f.pair("cat", src["category"], dst["category"]):
        return []
    if "bands" in src:
        if len(src["bands"]) != len(dst["bands"]) or not f.pair("qty", src["quantity"], dst["quantity"]):
            return []
        if not all(f.pair("val", a["value"], b["value"]) for a, b in zip(src["bands"], dst["bands"])):
            return []
    elif not f.pair("val", src["fixed"], dst["fixed"]):
        return []
    return [f]


def _skeleton(text):
    import re
    return re.sub(r"-?\d+(?:\.\d+)?", "#", text)


def _operation_fits(src, dst):
    if src["formula"] != dst["formula"] or set(src["inputs"]) != set(dst["inputs"]):
        return []
    if [_skeleton(b) for b in src.get("boundary", [])] != [_skeleton(b) for b in dst.get("boundary", [])]:
        return []
    out = []
    for order in itertools.permutations(dst["match"]):
        f = _Fit()
        if not all(_literal(f, a, b) for a, b in zip(src["match"], order)):
            continue
        if not _condition(f, src["orient"], dst["orient"], {}):
            continue
        if all(_term(f, src["inputs"][k], dst["inputs"][k]) for k in src["inputs"]):
            out.append(f)
    return out


# ------------------------------------------------------------------ the map

def _items(domain):
    items = {}
    for r in domain.get("rules", []):
        items["law:" + r["id"]] = ("law", r)
    for b in domain.get("bands", []):
        items["band:" + b["id"]] = ("band", b)
    for o in domain.get("operations", []):
        items["op:" + o["id"]] = ("op", o)
    return items


def _merge(into, f):
    """into ∪ f, or None where they disagree (one to one within each class)."""
    out = dict(into)
    back = {(cls, b): a for (cls, a), b in out.items()}
    for (cls, a), b in f.sym.items():
        if out.get((cls, a), b) != b or back.get((cls, b), a) != a:
            return None
        out[(cls, a)], back[(cls, b)] = b, a
    return out


def structural_map(source, target, names=("source", "target")):
    """Pair what plays the same part in two domains; see the module notes."""
    m = Map(*names)
    s_items, t_items = _items(source), _items(target)
    cats = set(source.get("categories", {}))
    fits = {}
    for sid, (kind, s) in s_items.items():
        options = []
        for tid, (tkind, t) in t_items.items():
            if tkind != kind:
                continue
            found = (_law_fits(s, t, cats) if kind == "law" else _band_fits(s, t) if kind == "band"
                     else _operation_fits(s, t))
            options += [(tid, f) for f in found]
        fits[sid] = options

    # the largest consistent set of pairings (every best one is kept, to find what stays undecided)
    order = sorted(fits, key=lambda k: len(fits[k]))
    best = {"count": -1, "solutions": []}

    def search(i, symbols, used, chosen):
        if i == len(order):
            n = len(chosen)
            if n > best["count"]:
                best["count"], best["solutions"] = n, []
            if n == best["count"] and len(best["solutions"]) < 64:
                best["solutions"].append((symbols, dict(chosen)))
            return
        if len(chosen) + (len(order) - i) < best["count"]:
            return
        sid = order[i]
        for tid, f in fits[sid]:
            if tid in used:
                continue
            merged = _merge(symbols, f)
            if merged is None:
                continue
            chosen[sid] = tid
            search(i + 1, merged, used | {tid}, chosen)
            del chosen[sid]
        search(i + 1, symbols, used, chosen)

    search(0, {}, frozenset(), {})
    solutions = best["solutions"] or [({}, {})]
    first_symbols, first_pairs = solutions[0]
    for key, value in first_symbols.items():
        options = {sol[0].get(key) for sol in solutions}
        if len(options) == 1:
            m.symbols[key] = value
            m.evidence[key] = "laws of the same shape"
        else:
            m.ambiguous[key] = sorted(o for o in options if o)
    stable = {sid: tid for sid, tid in first_pairs.items() if all(sol[1].get(sid) == tid for sol in solutions)}
    m.pairs = sorted(stable.items())
    m.unpaired = sorted(sid for sid in s_items if sid not in stable)

    _by_facts(m, source, target)
    _by_tree(m, source, target)
    _by_flags(m, source, target)
    _by_facts(m, source, target)   # attributes again, now that more kinds are paired
    return m


def _taken(m, cls):
    return {v for (c, _), v in m.symbols.items() if c == cls}


def _set(m, cls, a, b, why):
    if (cls, a) in m.symbols or (cls, a) in m.ambiguous or b in _taken(m, cls):
        return False
    m.symbols[(cls, a)] = b
    m.evidence[(cls, a)] = why
    return True


def _store(domain):
    """kind -> attribute -> [(value, source)]"""
    out = {}
    for f in domain.get("facts", []):
        out.setdefault(f["term"], {}).setdefault(f["attr"], []).append((f["value"], f.get("source")))
    return out


def _by_facts(m, source, target):
    s_store, t_store = _store(source), _store(target)
    s_qunits = {q["unit"] for q in source.get("quantities", {}).values()}
    t_qunits = {q["unit"] for q in target.get("quantities", {}).values()}

    def attr_sig(domain, qunits, attr):
        return "level" if domain["attributes"].get(attr) in qunits else "other"

    # attributes on paired kinds: same role, and the same place in value order among that kind's attributes
    for (cls, s_kind), t_kind in list(m.symbols.items()):
        if cls != "kind" or s_kind not in s_store or t_kind not in t_store:
            continue
        for role in ("level", "other"):
            s_attrs = sorted((max(v for v, _ in vals), a) for a, vals in s_store[s_kind].items()
                             if attr_sig(source, s_qunits, a) == role)
            t_attrs = sorted((max(v for v, _ in vals), a) for a, vals in t_store[t_kind].items()
                             if attr_sig(target, t_qunits, a) == role)
            if len(s_attrs) == len(t_attrs):
                for (_, a), (_, b) in zip(s_attrs, t_attrs):
                    if m.get("attr", a) in (None, b):
                        _set(m, "attr", a, b, f"stored for {s_kind} / {t_kind}, in the same order of value")

    # kinds by what is stored about them, in paired attributes, from how many sources
    def kind_sig(store, kind, translate):
        return tuple(sorted((translate(a), len(vals)) for a, vals in store.get(kind, {}).items()))

    s_sigs = {k: kind_sig(s_store, k, lambda a: m.get("attr", a)) for k in s_store}
    t_sigs = {k: kind_sig(t_store, k, lambda a: a) for k in t_store}
    for k, sig in s_sigs.items():
        if None in (a for a, _ in sig):
            continue
        same_s = [x for x, s in s_sigs.items() if s == sig]
        same_t = [x for x, s in t_sigs.items() if s == sig]
        if len(same_s) == 1 and len(same_t) == 1:
            _set(m, "kind", k, same_t[0], "the same facts are stored about it")


def _children(domain):
    out = {}
    for k, p in domain["tree"].items():
        out.setdefault(p, []).append(k)
    return out


def _by_tree(m, source, target):
    s_par, t_par = source["tree"], target["tree"]
    s_kids, t_kids = _children(source), _children(target)
    changed = True
    while changed:
        changed = False
        for (cls, a), b in list(m.symbols.items()):
            if cls != "kind":
                continue
            pa, pb = s_par.get(a), t_par.get(b)
            if pa is not None and pb is not None and _set(m, "kind", pa, pb, f"the parent of {a} / {b}"):
                changed = True
        if (s_roots := s_kids.get(None)) and (t_roots := t_kids.get(None)) and len(s_roots) == len(t_roots) == 1:
            changed |= _set(m, "kind", s_roots[0], t_roots[0], "the root of the tree")
        for (cls, a), b in list(m.symbols.items()):
            if cls != "kind":
                continue
            left_s = [k for k in s_kids.get(a, []) if ("kind", k) not in m.symbols]
            left_t = [k for k in t_kids.get(b, []) if k not in _taken(m, "kind")]
            if len(left_s) == 1 and len(left_t) == 1:
                changed |= _set(m, "kind", left_s[0], left_t[0], f"the one child of {a} / {b} left")
            elif len(left_s) == len(left_t) > 1:
                for k in left_s:
                    shape = len(s_kids.get(k, []))
                    fit = [t for t in left_t if len(t_kids.get(t, [])) == shape]
                    if len(fit) > 1 and ("kind", k) not in m.ambiguous:
                        m.ambiguous[("kind", k)] = sorted(fit)
    for key in [k for k in m.ambiguous if k in m.symbols]:
        del m.ambiguous[key]


def _by_flags(m, source, target):
    def flags(r):
        return tuple(sorted((k, v) for k, v in r.items() if k != "attrs")) + (len(r.get("attrs", {})),)

    s_left = {k: flags(r) for k, r in source["relations"].items() if ("rel", k) not in m.symbols}
    t_left = {k: flags(r) for k, r in target["relations"].items() if k not in _taken(m, "rel")}
    for k, sig in s_left.items():
        same_s = [x for x, s in s_left.items() if s == sig]
        same_t = [x for x, s in t_left.items() if s == sig]
        if len(same_s) == 1 and len(same_t) == 1:
            _set(m, "rel", k, same_t[0], "the only relation left with its flags")
        elif len(same_t) > 1:
            m.ambiguous.setdefault(("rel", k), sorted(same_t))
    # a relation attribute of paired relations
    for (cls, a), b in list(m.symbols.items()):
        if cls == "rel":
            sa, ta = list(source["relations"][a].get("attrs", {})), list(target["relations"][b].get("attrs", {}))
            if len(sa) == len(ta) == 1:
                _set(m, "rattr", sa[0], ta[0], f"the attribute of {a} / {b}")


# ------------------------------------------------------------------ carrying laws over

def _rename(x, m, need, pos=None):
    """A law's structure in the target's words; `need` collects what is unpaired."""
    if isinstance(x, list):
        if not x:
            return x
        head = x[0]
        if head == "q":
            return ["q", _sym(m, "qty", x[1], need), _rename(x[2], m, need)]
        if head == "c":
            return ["c", _sym(m, "cat", x[1], need), _rename(x[2], m, need)]
        if head == "a":
            return ["a", _rename(x[1], m, need), _sym(m, "attr", x[2], need)]
        if head == "r":
            return ["r", _sym(m, "rel", x[1], need), x[2], x[3], _sym(m, "rattr", x[4], need)]
        if head == "isa":
            return ["isa", x[1], _sym(m, "kind", x[2], need)]
        if head in ("gt", "ge", "lt", "le", "eq", "neq"):
            return [head] + [_rename(t, m, need, "cond") for t in x[1:]]
        return [_sym(m, "rel", head, need)] + list(x[1:])
    if isinstance(x, str) and not x.startswith("?") and pos == "cond":
        return _sym(m, "val", x, need)
    if isinstance(x, (int, float)) and pos == "cond":
        need.append(f"the number {x} (units do not carry over)")
    return x


def _sym(m, cls, name, need):
    got = m.get(cls, name)
    if got is None:
        alts = m.ambiguous.get((cls, name))
        need.append(f"{name} ({' or '.join(alts)}: ambiguous)" if alts else f"{name} (no counterpart)")
        return name
    return got


def transfer(m, source):
    """(laws carried over, {source law id: why it could not be})."""
    laws, blocked = [], {}
    rules = {r["id"]: r for r in source.get("rules", [])}
    for sid in m.unpaired:
        if not sid.startswith("law:"):
            if sid.startswith(("band:", "op:")):
                blocked[sid[sid.index(":") + 1:]] = "it is stated in numbers, and units do not carry over"
            continue
        r = rules[sid[4:]]
        need = []
        new = {"id": f"{m.source}:{r['id']}",
               "match": [_rename(lit, m, need) for lit in r["match"]],
               "conclude": _rename(r["conclude"], m, need)}
        if r.get("conditions"):
            new["conditions"] = [_rename(c, m, need) for c in r["conditions"]]
        for k in ("distinct", "polarity"):
            if k in r:
                new[k] = copy.deepcopy(r[k])
        if need:
            blocked[r["id"]] = "needs " + ", ".join(dict.fromkeys(need))
            continue
        used = sorted({(c, n) for (c, n) in m.symbols if _mentions(r, n)})
        new["analogy"] = {"source": m.source, "law": r["id"], "pairing": m.describe(used)}
        laws.append(new)
    return laws, blocked


def _mentions(x, name):
    if isinstance(x, dict):
        return any(_mentions(v, name) for k, v in x.items() if k not in ("id", "note"))
    if isinstance(x, list):
        return any(_mentions(v, name) for v in x)
    return x == name


# ------------------------------------------------------------------ during a run

def apply(kernel, given, answers):
    """Questions asked with `estimate` that their own domain cannot answer, tried with laws carried over
    from the kernel's other domains. Returns the answers, some replaced by CONFABULATED ones."""
    from .kernel import Answer   # (kernel imports this module)
    from .logic import World

    out = list(answers)
    worlds = {}

    def world_for(name, laws):
        if name not in worlds:
            domain = copy.deepcopy(kernel.domain)
            domain["rules"] = domain.get("rules", []) + laws
            w = World(given, domain, kernel.headspace_limit)
            w.relate()
            w.compare()
            worlds[name] = w
        return worlds[name]

    def noted(a, note):
        v = a.value
        return Answer(a.goal, Value(v.payload, v.state, v.unit, v.provenance, tuple(v.causes) + (note,),
                                    v.alternatives, v.experiment, v.support, v.just),
                      a.step, a.rests_on, a.clashes, a.proof, a.input)

    for i, (g, a) in enumerate(zip(given.goals, answers)):   # answers come in the goals' order
        if a.value.state not in FAILED or a.step == "deduce":
            continue
        carried = kernel.carried()
        found = None
        for name, (laws, blocked, m) in carried.items():
            if not laws:
                continue
            value, _ = world_for(name, laws).answer(g)
            if value.state in SUPPORTED:
                found = (name, laws, value)
                break
        if found is None:
            reasons = [f"{name}'s {law}: {why}" for name, (_, blocked, _) in carried.items()
                       for law, why in blocked.items()]
            if g.estimate and reasons:
                out[i] = noted(a, "no law carried over from another domain answers it ("
                               + "; ".join(reasons[:3]) + ")")
            continue
        name, laws, value = found
        if not g.estimate:   # a guess only when asked for one
            out[i] = noted(a, f"{name} has a law in the same place; `estimate` would carry it over")
            continue
        used = [law for law in laws if law["id"] in value.provenance] or laws
        law = used[0]["analogy"]
        out[i] = Answer(a.goal, Value(
            value.payload, State.CONFABULATED, value.unit,
            tuple(f"{name}'s {u['analogy']['law']} by analogy" for u in used),
            (f"no law of this domain answers {a.goal}; {name}'s law {law['law']} has the same place "
             f"there, carried over as {law['pairing']}",),
            experiment=f"observe whether {a.goal} holds, to test the law carried over from {name}"),
            "plan")
    return out
