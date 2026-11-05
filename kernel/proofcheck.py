"""An independent checker for the kernel's proofs.

It shares no code with the engine (mshmoe/): it reads the domain file and the raw input text itself,
with its own parsing, reference tree, interval arithmetic and law matching, and re-verifies every
step of every proof. A supported answer (KNOWN or DERIVED) passes only if its proof checks; that is
the invariant ("no unsupported value is promoted"), confirmed from outside the engine on every run.

A rule step lists the opposing laws it beat; the checker verifies that each really is less specific,
and runs the laws over the input itself (its own naive matching, _World) to find any opposing law
that applies there and is missing from that list. A law counts as applying only when its premises
hold and its conditions hold for certain, so what it finds is never a guess.

Use:
    problems = check_run(domain, text, answers)   # answers: dicts with goal, state, proof
    problems == {}                                 # every supported answer is proven
"""
import ast
import json
import math

INF = math.inf
SUPPORTED = ("KNOWN", "DERIVED")


class Bad(Exception):
    pass


# ---------------------------------------------------------------- intervals (own implementation)

def iv(t):
    lo, hi, lo_open, hi_open = t
    return (float(lo), float(hi), bool(lo_open), bool(hi_open))


def point(v):
    return (float(v), float(v), False, False)


def iv_eq(a, b, tol=1e-9):
    return all(abs(x - y) <= tol or x == y for x, y in zip(a[:2], b[:2])) and a[2:] == b[2:]


def _span(cands):
    lo = min(v for v, _ in cands)
    hi = max(v for v, _ in cands)
    lo_open = all(o for v, o in cands if v == lo) and lo != -INF
    hi_open = all(o for v, o in cands if v == hi) and hi != INF
    return (lo, hi, lo_open, hi_open)


def iv_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] or b[2], a[3] or b[3])


def iv_neg(a):
    return (-a[1], -a[0], a[3], a[2])


def iv_mul(a, b):
    c = []
    for x, xo in ((a[0], a[2]), (a[1], a[3])):
        for y, yo in ((b[0], b[2]), (b[1], b[3])):
            p = 0.0 if 0 in (x, y) and INF in (abs(x), abs(y)) else x * y
            c.append((p, xo or yo))
    return _span(c)


def iv_div(a, b):
    lo, hi, lo_open, hi_open = b
    has_zero = lo < 0 < hi or (lo == 0 and not lo_open) or (hi == 0 and not hi_open)
    if has_zero:
        raise Bad("division by a range that includes zero")
    inv = _span([(1 / lo if abs(lo) != INF else 0.0, lo_open), (1 / hi if abs(hi) != INF else 0.0, hi_open)])
    return iv_mul(a, inv)


def gt(a, b):
    """Three-valued a > b over intervals."""
    if a[0] > b[1] or (a[0] == b[1] and (a[2] or b[3])):
        return True
    if a[1] <= b[0]:
        return False
    return None


def cmp(op, a, b):
    if op == "gt":
        return gt(a, b)
    if op == "lt":
        return gt(b, a)
    if op == "le":
        r = gt(a, b)
        return None if r is None else not r
    if op == "ge":
        r = gt(b, a)
        return None if r is None else not r
    raise Bad(f"unknown comparison {op}")


def within(a, band):
    """a lies inside band (a band has its low end in, its high end out)."""
    lo_ok = a[0] > band[0] or (a[0] == band[0] and (not band[2] or a[2]))
    hi_ok = a[1] < band[1] or (a[1] == band[1] and (not band[3] or a[3]))
    return lo_ok and hi_ok


def band_iv(b):
    lo = -INF if b["lo"] is None else float(b["lo"])
    hi = INF if b["hi"] is None else float(b["hi"])
    return (lo, hi, False, b["hi"] is not None and not b.get("hi_in"))


_ARITH = {ast.Add: iv_add, ast.Sub: lambda a, b: iv_add(a, iv_neg(b)), ast.Mult: iv_mul, ast.Div: iv_div}
_CMP = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}


def ev(node, env):
    if isinstance(node, str):
        node = ast.parse(node, mode="eval").body
    if isinstance(node, ast.Constant):
        return point(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        return _ARITH[type(node.op)](ev(node.left, env), ev(node.right, env))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return iv_neg(ev(node.operand, env))
    raise Bad(f"unsupported expression {ast.unparse(node)}")


def holds(expr, env):
    tree = ast.parse(expr, mode="eval").body
    if not (isinstance(tree, ast.Compare) and len(tree.ops) == 1 and type(tree.ops[0]) in _CMP):
        raise Bad(f"unsupported boundary {expr}")
    return cmp(_CMP[type(tree.ops[0])], ev(tree.left, env), ev(tree.comparators[0], env))


# ---------------------------------------------------------------- the input, read independently

class Input:
    def __init__(self, domain, text):
        self.d = domain
        self.parent = dict(domain["tree"])
        self.alias = dict(domain.get("aliases", {}))
        self.kinds, self.q, self.c, self.observed, self.shifts = {}, {}, {}, {}, {}
        self.disputed = {}   # (var, name) -> (given, observed) when an observation disagrees
        self.rels = []
        lines = [raw.split("#", 1)[0].strip() for raw in text.splitlines()]
        lines = [ln for ln in lines if ln]
        first = [ln for ln in lines if ln.split()[0] in ("teach", "confirm")]
        for ln in first + [ln for ln in lines if ln not in first]:
            self._line(ln.split())

    def term(self, t):
        t = self.alias.get(t, t)
        return t if t in self.parent else None

    def ancestors(self, node):
        out = []
        while node is not None and node not in out:
            out.append(node)
            node = self.parent.get(node)
        return out

    def _measure(self, q, number, unit):
        spec = self.d["quantities"][q]
        units = {spec["unit"]: 1, **spec.get("conversions", {})}
        return float(number) * units[unit]

    def _props(self, toks):
        q, c, i = {}, {}, 0
        while i < len(toks):
            name = toks[i]
            if name in self.d["quantities"]:
                q[name] = self._measure(name, toks[i + 1], toks[i + 2])
                i += 3
            else:
                c[name] = toks[i + 1]
                i += 2
        return q, c

    def _introduce(self, name):
        if name not in self.kinds:
            self.kinds[name] = self.term(name)

    def _line(self, t):
        head, rest = t[0], t[1:]
        try:
            if head == "let":
                var = rest[0]
                self.kinds[var] = self.term(rest[2])
                q, c = self._props(rest[3:])
                self.q.setdefault(var, {}).update(q)
                self.c.setdefault(var, {}).update(c)
            elif head == "rel":
                for n in rest[1:3]:
                    self._introduce(n)
                attrs = {rest[i]: float(rest[i + 1]) for i in range(3, len(rest) - 2, 3)}
                self.rels.append((rest[0], rest[1], rest[2], attrs))
            elif head == "shift":
                var, q, number, unit = rest
                self.shifts.setdefault((var, q), []).append(self._measure(q, number, unit))
                self.c[var] = {}   # observations before a change no longer hold
            elif head == "teach":
                self.parent.setdefault(rest[0], self.term(rest[2]))
            elif head == "confirm":
                self.alias[rest[0]] = self.term(rest[2])
            elif head == "observe" and rest and rest[0] in self.kinds:
                var = rest[0]
                q, c = self._props(rest[1:])
                for have, new in ((self.q.setdefault(var, {}), q), (self.c.setdefault(var, {}), c)):
                    for name, value in new.items():
                        if name in have and have[name] != value:
                            self.disputed[(var, name)] = (have[name], value)
                        else:
                            have[name] = value   # an observation adds to what is given about it
        except (IndexError, KeyError, ValueError):
            pass   # a malformed line supports nothing; a proof leaning on it will fail


# ---------------------------------------------------------------- checking one proof

def _rule_subst(pattern, binding):
    return tuple(binding.get(t, t) for t in pattern)


def _contains(specific, general, relations):
    """`specific`'s pattern contains `general`'s: every premise and condition of `general`, with the
    conclusions aligned, appears in `specific`, and `specific` has more of them."""
    hs, hg = specific["conclude"], general["conclude"]
    if hs[0] != hg[0] or len(hs) != len(hg):
        return False
    size = lambda r: len(r["match"]) + len(r.get("conditions", []))  # noqa: E731
    if size(specific) <= size(general):
        return False
    orders = [hs[1:]]
    if len(hs) == 3 and relations.get(hs[0], {}).get("symmetric"):
        orders.append(list(reversed(hs[1:])))

    def unify(p, v, th):
        if p.startswith("?"):
            if th.setdefault(p, v) != v:
                return False
            return True
        return p == v

    def embed(prem, th):
        if not prem:
            return th
        head, rest = prem[0], prem[1:]
        for target in specific["match"]:
            if target[0] != head[0] or len(target) != len(head):
                continue
            t = dict(th)
            if all(unify(g, s, t) for g, s in zip(head[1:], target[1:])):
                found = embed(rest, t)
                if found is not None:
                    return found
        return None

    def subst(x, th):
        if isinstance(x, str):
            return th.get(x, x)
        if isinstance(x, list):
            return [subst(y, th) for y in x]
        return x

    for args in orders:
        th = {}
        if not all(unify(g, s, th) for g, s in zip(hg[1:], args)):
            continue
        th = embed(general["match"], th)
        if th is None:
            continue
        conds = [repr(c) for c in specific.get("conditions", [])]
        if all(repr(subst(c, th)) in conds for c in general.get("conditions", [])):
            return True
    return False


class _World:
    """The checker's own run of the laws over the input, to find opposing laws a proof did not list.

    Relations from the input (both ways when symmetric) and 'is a' from the reference tree; laws are
    applied to a fixpoint, a conclusion standing when every law that opposes it there is less
    specific. A law counts as applying only when all its premises hold and every condition holds for
    certain (values from the input, its shifts, the band laws and the store). It is written
    separately from the engine's matching: naive joins, no index."""

    def __init__(self, checker):
        self.c = checker
        self.d, self.inp = checker.d, checker.inp
        self.rels = self.d["relations"]
        self.vars = sorted(v for v, k in self.inp.kinds.items() if k)
        self.fired = {}   # (key, polarity) -> [rules that apply there]
        facts = set()
        for rel, a, b, _ in self.inp.rels:
            facts.add((rel, a, b))
            if self.rels.get(rel, {}).get("symmetric"):
                facts.add((rel, b, a))
        for _ in range(30):
            fired = self._apply(facts)
            new = set(facts)
            for (key, pol), rules in fired.items():
                if pol is True and self._stands(key, True, fired):
                    new.add(key)
                    if self.rels.get(key[0], {}).get("symmetric"):
                        new.add((key[0], key[2], key[1]))
            self.fired = fired
            if new == facts:
                break
            facts = new
        self.facts = facts

    def _stands(self, key, pol, fired):
        mine = fired.get((key, pol), [])
        if pol is True and mine and self.rels.get(key[0], {}).get("any"):
            return True   # "any": one way it holds is enough, whatever says otherwise elsewhere
        theirs = fired.get((key, not pol), [])
        return any(all(_contains(r, o, self.rels) for o in theirs) for r in mine)

    def opposers(self, key, polarity):
        if polarity is True and self.rels.get(key[0], {}).get("any"):
            return []
        keys = [key]
        if self.rels.get(key[0], {}).get("symmetric") and len(key) == 3:
            keys.append((key[0], key[2], key[1]))
        out = {}
        for k in keys:
            for r in self.fired.get((k, not polarity), []):
                out[r["id"]] = r
        return list(out.values())

    # ---- values, for conditions
    def _isa(self, var, cls):
        kind = self.inp.kinds.get(var)
        return kind is not None and cls in self.inp.ancestors(kind)

    def _quantity(self, var, q):
        if (var, q) in self.inp.disputed:
            return None
        have = self.inp.q.get(var, {}).get(q)
        if have is not None:
            return point(have + sum(self.inp.shifts.get((var, q), [])))
        if self.inp.shifts.get((var, q)):
            return None
        kind = self.inp.kinds.get(var)
        for law in self.d.get("bands", []):
            if law.get("quantity") != q or "bands" not in law or kind is None:
                continue
            if not any(t in self.inp.ancestors(kind) for t in law["applies_to"]):
                continue
            seen = self.inp.c.get(var, {}).get(law["category"])
            band = next((b for b in law["bands"] if b["value"] == seen), None)
            if band is not None:
                return band_iv(band)
        return None

    def _category(self, var, cat):
        if (var, cat) in self.inp.disputed:
            return None
        have = self.inp.c.get(var, {}).get(cat)
        if have is not None:
            return have
        kind = self.inp.kinds.get(var)
        if kind is None:
            return None
        for law in self.d.get("bands", []):
            if law.get("category") != cat or not any(t in self.inp.ancestors(kind) for t in law["applies_to"]):
                continue
            if "fixed" in law:
                return law["fixed"]
            qv = self._quantity(var, law["quantity"])
            if qv is not None:
                hit = [b["value"] for b in law["bands"] if within(qv, band_iv(b))]
                if len(hit) == 1:
                    return hit[0]
        return None

    def _attr(self, var, attr):
        kind = self.inp.kinds.get(var)
        for node in self.inp.ancestors(kind) if kind else []:
            facts = [f for f in self.d.get("facts", []) if f["term"] == node and f["attr"] == attr and not f.get("refuted_by")]
            if facts:
                values = sorted({float(f["value"]) for f in facts})
                return ("iv", point(values[0])) if len(values) == 1 else ("alts", [point(v) for v in values])
        return None

    def _operand(self, t, b):
        if isinstance(t, list):
            if t[0] == "q":
                v = self._quantity(b.get(t[2]), t[1])
                return None if v is None else ("iv", v)
            if t[0] == "c":
                v = self._category(b.get(t[2]), t[1])
                return None if v is None else ("cat", v)
            if t[0] == "a":
                return self._attr(b.get(t[1]), t[2])
            return None
        return ("const", t)

    def _certain(self, rule, b):
        for cond in rule.get("conditions", []):
            ops = [self._operand(t, b) for t in cond[1:]]
            if any(o is None for o in ops) or self.c._condition(cond[0], ops) is not True:
                return False
        return True

    # ---- the laws, applied
    def _bindings(self, match, facts, b):
        if not match:
            yield b
            return
        lit, rest = match[0], match[1:]
        if lit[0] == "isa":
            var, cls = lit[1], lit[2]
            options = [b[var]] if var in b else self.vars
            for v in options:
                if self._isa(v, cls):
                    yield from self._bindings(rest, facts, {**b, var: v})
            return
        for fact in facts:
            if fact[0] != lit[0] or len(fact) != len(lit):
                continue
            nb, ok = dict(b), True
            for p, v in zip(lit[1:], fact[1:]):
                if p.startswith("?"):
                    if nb.setdefault(p, v) != v:
                        ok = False
                        break
                elif p != v:
                    ok = False
                    break
            if ok:
                yield from self._bindings(rest, facts, nb)

    def _apply(self, facts):
        fired = {}
        for rule in self.d.get("rules", []):
            for b in self._bindings(rule["match"], facts, {}):
                if any(b.get(x) == b.get(y) for x, y in rule.get("distinct", [])):
                    continue
                if not self._certain(rule, b):
                    continue
                key = tuple(b.get(t, t) for t in rule["conclude"])
                bucket = fired.setdefault((key, rule.get("polarity", True)), [])
                if rule not in bucket:
                    bucket.append(rule)
        return fired


class Checker:
    def __init__(self, domain, text):
        self.d = domain
        self.inp = Input(domain, text)
        self.rules = {r["id"]: r for r in domain.get("rules", [])}
        self.bands = {b["id"]: b for b in domain.get("bands", [])}
        self.ops = {o["id"]: o for o in domain.get("operations", [])}

    def _world(self):
        if getattr(self, "_w", None) is None:
            self._w = _World(self)
        return self._w

    def check(self, proof):
        """Problems with one proof (empty if every step checks)."""
        claims, problems = {}, []
        for step in proof:
            try:
                for u in step.get("uses", ()):
                    if u not in claims:
                        raise Bad(f"uses step {u}, which is not an earlier verified step")
                getattr(self, "_" + step["by"].replace("-", "_"))(step, claims)
                claims[step["id"]] = step["claim"]
            except Bad as err:
                problems.append(f"step {step['id']} ({step['by']}): {err}")
            except (KeyError, IndexError, TypeError, ValueError, AttributeError) as err:
                problems.append(f"step {step['id']} ({step['by']}): malformed ({type(err).__name__}: {err})")
        return problems

    # ---- step kinds
    def _given(self, s, claims):
        cl = s["claim"]
        if cl[0] == "quantity":
            _, q, var, value = cl
            if self.inp.shifts.get((var, q)):
                raise Bad(f"{q} of {var} was shifted; a given step cannot claim it")
            have = self.inp.q.get(var, {}).get(q)
            if have is None or not iv_eq(point(have), iv(value)):
                raise Bad(f"the input does not give {q} of {var} as {value[0]}")
            if (var, q) in self.inp.disputed:
                raise Bad(f"an observation of {q} of {var} disagrees with the given value")
        elif cl[0] == "category":
            _, c, var, value = cl
            if self.inp.c.get(var, {}).get(c) != value:
                raise Bad(f"the input does not give {c} of {var} as {value}")
            if (var, c) in self.inp.disputed:
                raise Bad(f"an observation of {c} of {var} disagrees with the given value")
        elif cl[0] == "rel":
            _, key, polarity, attrs = cl
            if polarity is not True:
                raise Bad("a given relation can only hold")
            rel, a, b = key
            sym = self.d["relations"].get(rel, {}).get("symmetric")
            found = [r for r in self.inp.rels if r[0] == rel and ((r[1], r[2]) == (a, b) or (sym and (r[1], r[2]) == (b, a)))]
            if not found:
                raise Bad(f"the input has no rel {rel} {a} {b}")
            if dict(attrs) and not any(dict(attrs) == r[3] for r in found):
                raise Bad(f"the input gives other attributes for {rel} {a} {b}")
        else:
            raise Bad(f"cannot be given: {cl[0]}")

    def _shift(self, s, claims):
        _, q, var, value = s["claim"]
        base = self.inp.q.get(var, {}).get(q)
        if base is None:
            raise Bad(f"{q} of {var} has no exact given value to shift")
        if not iv_eq(point(base + sum(self.inp.shifts.get((var, q), []))), iv(value)):
            raise Bad(f"{q} of {var} after its shifts is not {value[0]}")

    def _kind(self, s, claims):
        _, var, kind = s["claim"]
        named = self.inp.kinds.get(var) if var in self.inp.kinds else self.inp.term(var)
        if named != kind:
            raise Bad(f"{var} is not introduced as a {kind}, nor is it that kind's name")

    def _tree(self, s, claims):
        _, var, cls = s["claim"]
        kind = claims[s["uses"][0]][2]
        if cls not in self.inp.ancestors(kind):
            raise Bad(f"{cls} is not above {kind} in the reference tree")

    def _store(self, s, claims):
        tag, cls, attr, value = s["claim"]
        facts = [f for f in self.d.get("facts", [])
                 if f["term"] == cls and f["attr"] == attr and not f.get("refuted_by")]
        if tag == "attr-alts":
            if tuple(sorted((f["value"], f["source"]) for f in facts)) != tuple(tuple(v) for v in value):
                raise Bad(f"the store's sources for {attr} of {cls} are not these")
        elif not facts or any(f["value"] != value for f in facts):
            raise Bad(f"the store does not hold {attr} of {cls} = {value} from every source")
        if tuple(sorted(f["source"] for f in facts)) != tuple(s["sources"]):
            raise Bad("the sources named are not the store's")

    def _inherit(self, s, claims):
        tag, node, attr, value = s["claim"]
        tag2, cls, attr2, value2 = claims[s["uses"][0]]
        chain = self.inp.ancestors(node)
        if tag2 != tag or attr2 != attr or value2 != value or cls not in chain:
            raise Bad(f"{node} does not inherit {attr} from {cls}")
        nearer = chain[:chain.index(cls)]
        if any(f["term"] in nearer and f["attr"] == attr and not f.get("refuted_by") for f in self.d.get("facts", [])):
            raise Bad(f"a class nearer to {node} than {cls} has its own {attr}")

    def _band(self, s, claims):
        law = self.bands[s["law"]]
        cl = s["claim"]
        kind = claims[s["uses"][1]][2]
        if not any(t in self.inp.ancestors(kind) for t in law["applies_to"]):
            raise Bad(f"{law['id']} does not cover {kind}")
        src = claims[s["uses"][0]]
        if cl[0] == "quantity":   # category -> range
            band = next((b for b in law["bands"] if b["value"] == src[3]), None)
            if src[0] != "category" or src[1] != law["category"] or cl[1] != law["quantity"] or band is None:
                raise Bad("the band law does not turn that value into this quantity")
            if not iv_eq(band_iv(band), iv(cl[3])):
                raise Bad(f"{law['id']} gives a different range for {src[3]}")
        else:                     # range -> category
            band = next((b for b in law["bands"] if b["value"] == cl[3]), None)
            if band is None or src[0] != "quantity" or not within(iv(src[3]), band_iv(band)):
                raise Bad(f"{cl[2]}'s {law['quantity']} is not inside the {cl[3]} band of {law['id']}")

    def _fixed(self, s, claims):
        law = self.bands[s["law"]]
        _, c, var, value = s["claim"]
        kind = claims[s["uses"][0]][2]
        if law.get("fixed") != value or c != law["category"]:
            raise Bad(f"{law['id']} does not fix {c} at {value}")
        if not any(t in self.inp.ancestors(kind) for t in law["applies_to"]):
            raise Bad(f"{law['id']} does not cover {kind}")
        if self.inp.c.get(var, {}).get(c) not in (None, value):
            raise Bad(f"the input gives a different {c} for {var}")

    def _every_source(self, s, claims):
        law = self.bands[s["law"]]
        _, c, var, value = s["claim"]
        q = law["quantity"]
        values = list(self.inp.disputed.get((var, q), ()))
        band = next((b for b in law["bands"] if b["value"] == value), None)
        if not values or band is None or not all(within(point(v), band_iv(band)) for v in values):
            raise Bad(f"not every reported {q} of {var} lies in the {value} band")

    def _rule(self, s, claims):
        rule = self.rules[s["rule"]]
        b = dict(s["binding"])
        _, key, polarity, _ = s["claim"]
        if tuple(key) != _rule_subst(rule["conclude"], b):
            raise Bad(f"{rule['id']} does not conclude {key} under this binding")
        if polarity is not rule.get("polarity", True):
            raise Bad(f"{rule['id']} concludes the opposite")
        prem = [claims[u] for u in s["uses"]]
        for pattern in rule["match"]:
            want = _rule_subst(pattern, b)
            ok = any((c[0] == "rel" and tuple(c[1]) == want and c[2] is True)
                     or (c[0] == "isa" and ("isa",) + tuple(c[1:]) == want) for c in prem)
            if not ok:
                raise Bad(f"no premise shows {want}")
        for x, y in rule.get("distinct", []):
            if b.get(x) == b.get(y):
                raise Bad(f"{x} and {y} must differ")
        for cond_text, terms in s.get("conditions", ()):
            cond = ast.literal_eval(cond_text)
            want = [_subst_list(t, b) for t in rule.get("conditions", [])]
            if cond not in want:
                raise Bad(f"{cond_text} is not a condition of {rule['id']}")
            if self._condition(cond[0], [self._operand(t, claims) for t in terms]) is not True:
                raise Bad(f"{cond_text} does not hold on the claims")
        for loser in s.get("beats", ()):
            other = self.rules.get(loser)
            if other is None or other["conclude"][0] != rule["conclude"][0] or \
                    other.get("polarity", True) == rule.get("polarity", True):
                raise Bad(f"{loser} is not an opposing law")
            if not _contains(rule, other, self.d["relations"]):
                raise Bad(f"{rule['id']} is not more specific than {loser}")
        # and no opposing law that applies here was left out of that list
        listed = set(s.get("beats", ()))
        for other in self._world().opposers(tuple(key), polarity):
            if other["id"] not in listed:
                raise Bad(f"{other['id']} also applies to {key} and concludes the opposite; "
                          f"the proof does not show it beaten")

    def _operand(self, t, claims):
        if t[0] == "const":
            return ("const", t[1])
        c = claims[t[1]]
        if c[0] == "quantity":
            return ("iv", iv(c[3]))
        if c[0] == "category":
            return ("cat", c[3])
        if c[0] == "attr":
            return ("iv", point(c[3]))
        if c[0] == "attr-alts":
            return ("alts", [point(v) for v, _ in c[3]])
        if c[0] == "quantity-alts":
            return ("alts", [iv(v) for v, _ in c[3]])
        raise Bad(f"cannot use {c[0]} as an operand")

    @staticmethod
    def _each(operand):
        """The values an operand may take: one, or one per source."""
        return [("iv", v) for v in operand[1]] if operand[0] == "alts" else [operand]

    def _condition(self, op, ops):
        """True only if the condition holds under every source's value."""
        outcomes = {self._condition1(op, (x, y)) for x in self._each(ops[0]) for y in self._each(ops[1])}
        return outcomes.pop() if len(outcomes) == 1 else None

    def _condition1(self, op, ops):
        a, b = ops
        if a[0] == "cat" or b[0] == "cat" or (a[0] == "const" and isinstance(a[1], str)) \
                or (b[0] == "const" and isinstance(b[1], str)):
            x, y = a[1], b[1]
            return (x == y) if op == "eq" else (x != y) if op == "neq" else None
        x = a[1] if a[0] == "iv" else point(a[1])
        y = b[1] if b[0] == "iv" else point(b[1])
        return cmp(op, x, y)

    def _symmetric(self, s, claims):
        _, key, polarity, _ = s["claim"]
        inner = claims[s["uses"][0]]
        if not self.d["relations"].get(key[0], {}).get("symmetric"):
            raise Bad(f"{key[0]} is not symmetric")
        if inner[0] != "rel" or tuple(inner[1]) != (key[0], key[2], key[1]) or inner[2] != polarity:
            raise Bad("not the same relation read the other way")

    def _operation(self, s, claims):
        op = self.ops[s["op"]]
        b = dict(s["binding"])
        prem = [claims[u] for u in s["uses"]]
        for pattern in op["match"]:
            want = _rule_subst(pattern, b)
            if not any(c[0] == "rel" and tuple(c[1]) == want and c[2] is True for c in prem):
                raise Bad(f"no premise shows {want}")
        cond_text, terms = s["orient"]
        if ast.literal_eval(cond_text) != _subst_list(op["orient"], b):
            raise Bad("not the operation's own orientation")
        if self._condition(op["orient"][0], [self._operand(t, claims) for t in terms]) is not True:
            raise Bad("the orientation does not hold")
        env, choices = {}, {}
        for name, sid in s["inputs"]:
            spec, c = op["inputs"][name], claims[sid]
            if spec[0] == "a":
                if c[0] not in ("attr", "attr-alts") or c[1] != self.inp.kinds.get(b[spec[1]]) or c[2] != spec[2]:
                    raise Bad(f"input {name} is not {spec[2]} of {b[spec[1]]}'s kind")
                if c[0] == "attr-alts":
                    choices[name] = [point(v) for v, _ in c[3]]
                    env[name] = choices[name][0]
                else:
                    env[name] = point(c[3])
            elif spec[0] == "q":
                if c[0] != "quantity" or c[1] != spec[1] or c[2] != b[spec[2]]:
                    raise Bad(f"input {name} is not {spec[1]} of {b[spec[2]]}")
                env[name] = iv(c[3])
            elif spec[0] == "r":
                if c[0] != "rel" or tuple(c[1]) != (spec[1], b[spec[2]], b[spec[3]]):
                    raise Bad(f"input {name} is not that relation")
                attrs = dict(c[3])
                if spec[4] not in attrs:
                    raise Bad(f"input {name}: {spec[4]} is not given")
                env[name] = point(attrs[spec[4]])
        if set(env) != set(op["inputs"]):
            raise Bad("not every input is accounted for")
        import itertools
        names = list(choices)
        for combo in itertools.product(*(choices[n] for n in names)) if names else [()]:
            run = dict(env, **dict(zip(names, combo)))
            for expr in op.get("boundary", []):
                if holds(expr, run) is not True:
                    raise Bad(f"{expr} does not hold for every value")
            result = ev(op["formula"], run)
            if not iv_eq(result, iv(s["claim"][1]), tol=1e-6):
                raise Bad(f"recomputed {op['formula']} gives {result[:2]}, not {s['claim'][1][:2]}")

    def _compare(self, s, claims):
        _, text = s["claim"]
        oa, ob = (self._operand(("step", u), claims) for u in s["uses"])
        parts = str(text).split()
        if len(parts) != 3 or parts[1] not in (">", "<", "="):
            raise Bad(f"cannot read the order '{text}'")
        rel = parts[1]
        for (_, a), (_, b) in ((x, y) for x in self._each(oa) for y in self._each(ob)):
            ok = (rel == ">" and gt(a, b) is True) or (rel == "<" and gt(b, a) is True) or \
                 (rel == "=" and a[0] == a[1] == b[0] == b[1])
            if not ok:
                raise Bad(f"the values do not give {text} under every source")

    def _disputed(self, s, claims):
        _, q, var, values = s["claim"]
        given = self.inp.disputed.get((var, q))
        if given is None or sorted(iv(v)[0] for v, _ in values) != sorted(float(x) for x in given):
            raise Bad(f"the input does not give these disagreeing values of {q} of {var}")

    def _parent(self, s, claims):
        _, node, parent = s["claim"]
        if self.inp.parent.get(node) != parent:
            raise Bad(f"{parent} is not the parent of {node}")

    def _missing(self, s, claims):
        raise Bad(f"no proof could be made: {s['claim'][1]}")

    def _answer(self, s, claims):
        _, payload, just = s["claim"]
        c = claims[s["uses"][0]]
        kind = just[0]
        if kind == "relation":
            _, key, polarity = just
            if c[0] != "rel" or tuple(c[1]) != tuple(key) or c[2] != polarity:
                raise Bad("the answer is not the proven relation")
        elif kind == "quantity":
            if c[0] != "quantity" or not iv_eq(iv(c[3]), iv(payload) if isinstance(payload, tuple) else iv(_unpack(payload))):
                raise Bad("the answer is not the proven value")
        elif kind == "category":
            if c[0] != "category" or (payload not in ("yes", "no") and payload != c[3]):
                raise Bad("the answer is not the proven category")
            if just[3]:
                absent = self.d["categories"][just[1]].get("absent")
                if payload != ("no" if c[3] == absent else "yes"):
                    raise Bad("yes/no does not follow from the proven category")
        elif kind == "operation":
            if c[0] != "value" or not iv_eq(iv(c[1]), iv(_unpack(payload)), tol=1e-6):
                raise Bad("the answer is not the computed value")
        elif kind == "compare":
            if c[0] != "order" or c[1] != payload:
                raise Bad("the answer is not the proven order")
        elif kind == "attribute":
            if c[0] != "attr" or c[3] != payload:
                raise Bad("the answer is not the proven store value")
        elif kind == "class":
            if c[0] != "parent" or c[2] != payload:
                raise Bad("the answer is not the proven parent")
        else:
            raise Bad(f"unknown answer kind {kind}")


def _subst_list(term, b):
    if isinstance(term, str):
        return b.get(term, term)
    if isinstance(term, list):
        return [_subst_list(t, b) for t in term]
    return term


def _unpack(payload):
    """A payload that is a number or an object with interval fields, as interval data."""
    if hasattr(payload, "lo"):
        return (payload.lo, payload.hi, payload.lo_open, payload.hi_open)
    if isinstance(payload, (int, float)):
        return point(payload)
    raise Bad(f"not a number or a range: {payload!r}")


def check_run(domain, text, answers):
    """{goal: [problems]} for every supported answer whose proof does not check (empty if all do).
    `answers`: objects or dicts with goal, state (string or enum) and proof."""
    checker = Checker(domain, text)
    out = {}
    for a in answers:
        goal = a["goal"] if isinstance(a, dict) else a.goal
        state = a["state"] if isinstance(a, dict) else a.value.state
        state = getattr(state, "value", state)
        proof = a.get("proof") if isinstance(a, dict) else a.proof
        if state not in SUPPORTED:
            continue
        if not proof:
            out[goal] = ["a supported answer with no proof"]
            continue
        problems = checker.check(proof)
        if problems:
            out[goal] = problems
    return out


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(json.load(fh), indent=1)[:200])
