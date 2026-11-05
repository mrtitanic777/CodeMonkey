"""Values, intervals, and three-valued comparison."""
import math
from dataclasses import dataclass

from .states import State

INF = math.inf


def fmt_num(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:g}" if isinstance(v, float) else str(v)


@dataclass(frozen=True)
class Interval:
    """A range of a quantity. Bounds may be infinite; *_open marks an excluded bound."""
    lo: float = -INF
    hi: float = INF
    lo_open: bool = False
    hi_open: bool = False

    @classmethod
    def point(cls, v):
        return cls(v, v)

    @classmethod
    def band(cls, lo, hi):
        """A band from data: lo included, hi excluded, None meaning unbounded."""
        return cls(-INF if lo is None else lo, INF if hi is None else hi, False, hi is not None)

    @property
    def is_point(self):
        return self.lo == self.hi

    def intersect(self, other):
        lo, lo_open = max((self.lo, self.lo_open), (other.lo, other.lo_open))
        hi, hi_closed = min((self.hi, not self.hi_open), (other.hi, not other.hi_open))
        hi_open = not hi_closed
        if lo > hi or (lo == hi and (lo_open or hi_open)):
            return None
        return Interval(lo, hi, lo_open, hi_open)

    def within(self, other):
        return self.intersect(other) == self

    def shifted(self, delta):
        return Interval(self.lo + delta, self.hi + delta, self.lo_open, self.hi_open)

    # ---- arithmetic on ranges: the result holds every value the operation can give, and an end
    # is excluded when every way of reaching it uses an excluded end.
    @staticmethod
    def of(v):
        return v if isinstance(v, Interval) else Interval.point(v)

    @staticmethod
    def _span(ends):
        """The interval spanned by (value, open) candidates: an end is open only if all ways to it are."""
        lo = min(v for v, _ in ends)
        hi = max(v for v, _ in ends)
        lo_open = all(o for v, o in ends if v == lo)
        hi_open = all(o for v, o in ends if v == hi)
        return Interval(lo, hi, lo_open and lo != -INF, hi_open and hi != INF)

    def __neg__(self):
        return Interval(-self.hi, -self.lo, self.hi_open, self.lo_open)

    def __add__(self, other):
        o = Interval.of(other)
        return Interval(self.lo + o.lo, self.hi + o.hi, self.lo_open or o.lo_open, self.hi_open or o.hi_open)

    __radd__ = __add__

    def __sub__(self, other):
        return self + (-Interval.of(other))

    def __rsub__(self, other):
        return Interval.of(other) + (-self)

    def __mul__(self, other):
        o = Interval.of(other)
        ends = []
        for a, ao in ((self.lo, self.lo_open), (self.hi, self.hi_open)):
            for b, bo in ((o.lo, o.lo_open), (o.hi, o.hi_open)):
                p = 0.0 if 0 in (a, b) and INF in (abs(a), abs(b)) else a * b
                ends.append((p, ao or bo))
        return Interval._span(ends)

    __rmul__ = __mul__

    def __truediv__(self, other):
        o = Interval.of(other)
        if o.contains(0):
            raise ZeroDivisionError("division by a range that includes zero")
        recip = Interval._span([(1 / o.lo if o.lo not in (-INF, INF) else 0.0, o.lo_open),
                                (1 / o.hi if o.hi not in (-INF, INF) else 0.0, o.hi_open)])
        return self * recip

    def __rtruediv__(self, other):
        return Interval.of(other) / self

    def contains(self, v):
        if v < self.lo or v > self.hi:
            return False
        return not ((v == self.lo and self.lo_open) or (v == self.hi and self.hi_open))

    def simplest(self):
        """A one-value range as a plain number; otherwise the range itself."""
        return self.lo if self.is_point and not (self.lo_open or self.hi_open) else self

    def describe(self, unit=None):
        u = f" {unit}" if unit else ""
        if self.is_point:
            return f"{fmt_num(self.lo)}{u}"
        if self.lo == -INF:
            return f"{'below' if self.hi_open else 'at most'} {fmt_num(self.hi)}{u}"
        if self.hi == INF:
            return f"{'above' if self.lo_open else 'at least'} {fmt_num(self.lo)}{u}"
        lower = "above " if self.lo_open else ""
        upper = "below " if self.hi_open else ""
        return f"from {lower}{fmt_num(self.lo)} to {upper}{fmt_num(self.hi)}{u}"


def greater(a, b):
    """Three-valued a > b: True, False, or None when the intervals cannot decide it."""
    if a.lo > b.hi or (a.lo == b.hi and (a.lo_open or b.hi_open)):
        return True
    if a.hi <= b.lo:
        return False
    return None


def compare(op, a, b):
    if op == "gt":
        return greater(a, b)
    if op == "lt":
        return greater(b, a)
    if op == "le":
        r = greater(a, b)
        return None if r is None else not r
    if op == "ge":
        r = greater(b, a)
        return None if r is None else not r
    raise ValueError(f"unknown comparison '{op}'")


@dataclass(frozen=True)
class Value:
    payload: object
    state: State
    unit: object = None
    provenance: tuple = ()
    causes: tuple = ()
    alternatives: tuple = ()  # ((payload, source), ...) when sources disagree
    experiment: object = None
    support: frozenset = frozenset()  # what it rests on: given facts and knowledge-store entries
    just: tuple = ()  # how it was concluded, for its proof (mshmoe/proof.py)


def failure(state, *causes):
    return Value(None, state, causes=tuple(causes))
