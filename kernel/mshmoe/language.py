"""Base model: vocabulary, reference tree, matcher, and the controlled input language (Deduce)."""
import copy
from dataclasses import dataclass, field

from .states import State, combine
from .values import Interval


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class Language:
    """Vocabulary and reference tree. Grows in use: taught terms and confirmed aliases."""

    def __init__(self, domain):
        self.parent = dict(domain["tree"])
        self.aliases = dict(domain.get("aliases", {}))
        self.quantities = domain.get("quantities", {})
        self.categories = domain.get("categories", {})
        self.attributes = domain.get("attributes", {})
        self.relations = domain.get("relations", {})
        self.max_distance = domain.get("matcher", {}).get("max_distance", 0.3)
        self.taught = {}  # term -> provenance

    def view(self):
        """A run-scoped copy: what a run teaches or confirms is visible to it at once, and kept after it."""
        v = copy.copy(self)
        v.parent = dict(self.parent)
        v.aliases = dict(self.aliases)
        return v

    # --- reference tree ---
    def ancestors(self, node):
        chain = []
        while node is not None:
            chain.append(node)
            node = self.parent.get(node)
        return chain

    def is_a(self, node, cls):
        return cls in self.ancestors(node)

    def is_sibling(self, a, b):
        return a != b and self.parent.get(a) is not None and self.parent.get(a) == self.parent.get(b)

    # --- matcher ---
    def near(self, term, words):
        """Words within the close-spelling threshold of term."""
        return sorted(w for w in words if edit_distance(term, w) / max(len(term), len(w)) <= self.max_distance)

    def resolve(self, term):
        """Bind a term to a node. Returns (node, state, causes)."""
        if term in self.parent:
            return term, State.KNOWN, ()
        if term in self.aliases:
            return self.aliases[term], State.KNOWN, ()
        near = {self.aliases.get(w, w) for w in self.near(term, list(self.parent) + list(self.aliases))}
        if not near:
            return None, State.UNKNOWN_REFERENCE, (f"'{term}' is not in the vocabulary",)
        return None, State.LOW_CONFIDENCE, (
            f"'{term}' is not in the vocabulary; did you mean {' or '.join(sorted(near))}?",)

    # --- plasticity ---
    def teach(self, term, parent, source):
        self.parent.setdefault(term, parent)
        self.taught.setdefault(term, source)

    def confirm(self, term, node):
        """A confirmed spelling takes effect at once and stays."""
        self.aliases[term] = node


# ---------------------------------------------------------------- Deduce

@dataclass
class Goal:
    name: str
    args: list
    label: str
    estimate: bool = False
    raw: tuple = ()  # the arguments as written, for display
    failure: object = None  # (state, causes) if the question itself could not be bound


@dataclass
class Given:
    lang: Language
    entities: dict = field(default_factory=dict)       # var -> {"kind", "quantities", "categories"} or None
    relations: list = field(default_factory=list)      # (rel, a, b, attrs)
    goals: list = field(default_factory=list)
    taught: list = field(default_factory=list)         # (term, parent)
    confirmations: list = field(default_factory=list)  # (term, node)
    observations: list = field(default_factory=list)   # (node, {quantity: Interval}, {category: value})
    measurements: list = field(default_factory=list)   # (node, attribute, value): a store value measured
    failures: list = field(default_factory=list)       # (state, causes, names the failed statement was about)
    links: list = field(default_factory=list)          # (a, b) for every relation statement, even failed ones

    def failure_for(self, goal):
        """The failure a question inherits, if any: its own, or that of a statement about something it
        depends on (the entities it names, and whatever relations connect to them)."""
        if goal.failure is not None:
            return goal.failure
        names, grown = set(goal.raw), True
        while grown:
            grown = False
            for a, b in self.links:
                if (a in names) != (b in names):
                    names |= {a, b}
                    grown = True
        hits = [(state, causes) for state, causes, about in self.failures if not about or about & names]
        return combine(hits) if hits else None


class _Fail(Exception):
    def __init__(self, state, *causes):
        super().__init__(causes)
        self.state, self.causes = state, causes


def deduce(lang, text):
    """Step 1: parse the controlled language, bind terms, normalize units.

    `teach` and `confirm` lines are read first, so they take effect for the whole input.
    """
    given = Given(lang.view())
    lines = [raw.split("#", 1)[0].strip() for raw in text.splitlines()]
    lines = [line for line in lines if line]
    first = [line for line in lines if line.split()[0] in ("teach", "confirm")]
    for line in first + [line for line in lines if line not in first]:
        tokens = line.split()
        if tokens[0] == "rel" and len(tokens) >= 4:
            given.links.append((tokens[2], tokens[3]))
        goals_before = len(given.goals)
        try:
            _statement(given, tokens)
        except _Fail as f:
            if len(given.goals) > goals_before:  # the question itself failed
                given.goals[-1].failure = (f.state, f.causes)
            else:
                given.failures.append((f.state, f.causes, _about(tokens)))
    return given


def _about(tokens):
    """What a failed statement is about: questions that depend on these names fail with it.
    An empty set (a statement that names nothing) fails every question."""
    if tokens[0] == "rel":
        return set(tokens[2:4])
    if tokens[0] in ("let", "shift", "teach", "confirm", "observe", "measure") and len(tokens) > 1:
        return {tokens[1]}
    return set()


def _bind(view, term):
    node, state, causes = view.resolve(term)
    if node is None:
        raise _Fail(state, *causes)
    return node


def _choose(view, token, words, what):
    """Bind a token from a closed word list (colors, units, relation names)."""
    if token in words:
        return token
    near = view.near(token, words)
    if near:
        raise _Fail(State.LOW_CONFIDENCE, f"'{token}' is not a known {what}; did you mean {' or '.join(near)}?")
    raise _Fail(State.UNKNOWN_REFERENCE, f"'{token}' is not a known {what}")


def _number(token):
    try:
        return float(token)
    except ValueError:
        raise _Fail(State.UNKNOWN_STRUCTURE, f"'{token}' is not a number")


def _measure(view, name, number, unit):
    spec = view.quantities[name]
    value = _number(number)
    units = {spec["unit"]: 1, **spec.get("conversions", {})}
    return value * units[_choose(view, unit, list(units), f"unit of {name}")]


def _properties(view, tokens):
    quantities, categories = {}, {}
    i = 0
    while i < len(tokens):
        name = tokens[i]
        if name in view.quantities:
            if i + 3 > len(tokens):
                raise _Fail(State.UNKNOWN_STRUCTURE, f"'{name}' needs a number and a unit")
            quantities[name] = Interval.point(_measure(view, name, tokens[i + 1], tokens[i + 2]))
            i += 3
        elif name in view.categories:
            if i + 2 > len(tokens):
                raise _Fail(State.UNKNOWN_STRUCTURE, f"'{name}' needs a value")
            categories[name] = _choose(view, tokens[i + 1], view.categories[name]["values"], name)
            i += 2
        else:
            raise _Fail(State.UNKNOWN_REFERENCE, f"'{name}' is not a known quantity or category")
    return quantities, categories


def _entities(given, names):
    """Names should be introduced; a name that isn't is read as a term and introduced implicitly."""
    for name in names:
        if name not in given.entities:
            given.entities[name] = None  # counts as introduced even if binding fails
            given.entities[name] = {"kind": _bind(given.lang, name), "quantities": {}, "categories": {}}


def _term_or_entity(given, name):
    """An introduced name stands for its entity; anything else is read as a term."""
    return name if name in given.entities else _bind(given.lang, name)


def _statement(given, tokens):
    view = given.lang
    head, rest = tokens[0], tokens[1:]
    if head in ("ask", "estimate"):
        _goal(given, rest, head == "estimate")
    elif head == "let":
        if len(rest) < 3 or rest[1] != "=":
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: let <name> = <term> ...")
        var = rest[0]
        given.entities[var] = None  # introduced, even if binding fails below
        kind = _bind(view, rest[2])
        quantities, categories = _properties(view, rest[3:])
        given.entities[var] = {
            "kind": kind,
            "quantities": {q: (iv, view.quantities[q]["unit"]) for q, iv in quantities.items()},
            "categories": categories,
        }
    elif head == "shift":
        if len(rest) != 4:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: shift <name> <quantity> <number> <unit>")
        var, name, number, unit = rest
        _entities(given, [var])
        entity = given.entities[var]
        current = entity["quantities"].get(name) if entity else None
        if current is None or not current[0].is_point:
            raise _Fail(State.MISSING_KNOWLEDGE, f"{name}({var}) must be known exactly to shift it")
        entity["quantities"][name] = (current[0].shifted(_measure(view, name, number, unit)), current[1])
        entity.setdefault("shifted", set()).add(name)  # a computed value: DERIVED, not KNOWN
        entity["categories"] = {}  # earlier observations describe the state before the change
    elif head == "rel":
        if len(rest) < 3:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: rel <relation> <a> <b> ...")
        name, a, b = rest[:3]
        name = _choose(view, name, list(view.relations), "relation")
        spec = view.relations[name]
        _entities(given, [a, b])
        attrs, extra = {}, rest[3:]
        for i in range(0, len(extra), 3):
            attr = extra[i]
            unit = spec.get("attrs", {}).get(attr)
            if unit is None or i + 3 > len(extra):
                raise _Fail(State.UNKNOWN_STRUCTURE, f"'{attr}' is not an attribute of {name}")
            _choose(view, extra[i + 2], [unit], f"unit of {attr}")
            attrs[attr] = _number(extra[i + 1])
        given.relations.append((name, a, b, attrs))
    elif head == "teach":
        if len(rest) != 3 or rest[1] != "isa":
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: teach <term> isa <class>")
        parent = _bind(view, rest[2])
        view.parent.setdefault(rest[0], parent)
        given.taught.append((rest[0], parent))
    elif head == "confirm":
        if len(rest) != 3 or rest[1] != "means":
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: confirm <term> means <term>")
        node = _bind(view, rest[2])
        view.aliases[rest[0]] = node
        given.confirmations.append((rest[0], node))
    elif head == "observe":
        if not rest:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: observe <term or name> ...")
        quantities, categories = _properties(view, rest[1:])
        if rest[0] in given.entities:  # an observation of a described entity adds to what is given about it
            entity = given.entities[rest[0]]
            if entity is None:
                return
            for key, value, have in [(q, (iv, view.quantities[q]["unit"]), entity["quantities"]) for q, iv in
                                     quantities.items()] + [(c, v, entity["categories"]) for c, v in categories.items()]:
                old = have.get(key)
                if old is not None and old != value:
                    entity.setdefault("disputed", {})[key] = (old, value)
                else:
                    have[key] = value
            known = {q: iv for q, (iv, _) in entity["quantities"].items() if iv.is_point}
            given.observations.append((entity["kind"], known, dict(entity["categories"])))
        else:
            given.observations.append((_bind(view, rest[0]), quantities, categories))
    elif head == "measure":
        if len(rest) not in (3, 4):
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: measure <term or name> <attribute> <number> [unit]")
        node = given.entities[rest[0]]["kind"] if given.entities.get(rest[0]) else _bind(view, rest[0])
        attr = _choose(view, rest[1], list(view.attributes), "attribute")
        value = _number(rest[2])
        if len(rest) == 4:
            _choose(view, rest[3], [view.attributes[attr]], f"unit of {attr}")
        given.measurements.append((node, attr, value))
    else:
        raise _Fail(State.UNKNOWN_STRUCTURE, f"'{head}' is not a statement of the input language")


def _goal(given, tokens, estimate):
    if not tokens:
        raise _Fail(State.UNKNOWN_STRUCTURE, "empty question")
    name, raw = tokens[0], tokens[1:]
    goal = Goal(name, list(raw), f"{name}({', '.join(raw)})", estimate, tuple(raw))
    given.goals.append(goal)
    if name == "value":
        if len(raw) != 2:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: value <term> <attribute>")
        goal.args = [_term_or_entity(given, raw[0]), raw[1]]
    elif name == "class":
        if len(raw) != 1:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: class <term or name>")
        goal.args = [_term_or_entity(given, raw[0])]
    elif name == "compare":
        if len(raw) != 3:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: compare <name> <a> <b>")
        goal.args = [raw[0]] + [_term_or_entity(given, a) for a in raw[1:]]
    elif name == "has":
        if len(raw) != 2:
            raise _Fail(State.UNKNOWN_STRUCTURE, "expected: has <category> <name>")
        _entities(given, raw[1:])
    elif name == "next":   # only a moment has a next: `at <t> ask next <quantity> <name>` (timeline.py)
        raise _Fail(State.UNKNOWN_STRUCTURE, "'next' asks what follows a moment: write it as "
                    "at <t> ask next <quantity> <name>; to compare two states, use compare")
    else:
        _entities(given, raw)
