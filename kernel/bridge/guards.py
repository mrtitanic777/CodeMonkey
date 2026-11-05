"""Runtime guards on the incoming translation: the invariant applied to the sentence.

The model is not asked to promise anything. A translation is rejected when it
- contains a number the English never gave (a sign change such as "drops by 50" -> -50 is allowed),
- uses a term (kind, color, taught or confirmed word) the English never wrote,
- asks for an attribute the English never named,
- drops a close misspelling of the domain's vocabulary (an uncommon word such as "gren" or "morvain"
  must reach the controlled input unchanged, so the kernel's matcher can judge it), or
- still contains a placeholder word from a translator's worked examples.
A rejected translation never reaches the kernel; the user is asked instead.

With `strict=True`, *every* English word that is neither common English nor domain vocabulary must
survive. Measured with the 10,000-word list this refused about half of ordinary questions (slab, lump,
puddle, soaked...), so it is off until a larger, better-suited list is chosen.
"""
import os
import re

from mshmoe.language import edit_distance

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
ALLOWED_TERMS = {"none"}  # "not glimming" is written `glim none`
KEYWORDS = {"let", "shift", "rel", "teach", "isa", "confirm", "means", "observe", "ask", "estimate",
            "value", "class", "compare", "has"}
WORD_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "google-10000-english.txt")


def load_common(path=WORD_LIST):
    with open(path, encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def domain_words(domain):
    """The domain's own vocabulary: familiar even though it is not common English."""
    words = set(domain["tree"]) | set(domain.get("aliases", {})) | set(domain.get("words", []))
    for q, spec in domain.get("quantities", {}).items():
        words |= {q, spec["unit"], *spec.get("conversions", {})}
    for c, spec in domain.get("categories", {}).items():
        words |= {c, *spec["values"]}
    for name in list(domain.get("attributes", {})) + list(domain.get("relations", {})):
        words |= set(name.split("_"))
    for spec in domain.get("relations", {}).values():
        words |= set(spec.get("attrs", {})) | set(spec.get("attrs", {}).values())
    words |= {op["goal"] for op in domain.get("operations", [])}
    return {w.lower() for w in words}


def _plain(n):
    n = n.lstrip("-")
    return n.rstrip("0").rstrip(".") if "." in n else n


def english_words(text):
    """Words of the English, whole ("q-metal", "red-glimming"); contractions kept as their words."""
    text = re.sub(r"n['’]t\b", " not", text.lower())
    text = re.sub(r"['’](s|re|ll|ve|d|m)\b", "", text)
    return set(re.findall(r"[a-z0-9][a-z0-9-]*", text))


def _parts(words):
    return words | {part for w in words for part in w.split("-") if part}


def english_numbers(text):
    found = {_plain(n) for n in NUMBER.findall(text)}
    return found | {NUMBER_WORDS[w] for w in _parts(english_words(text)) if w in NUMBER_WORDS}


def _declared(controlled):
    return {t[1] for t in (line.split() for line in controlled.splitlines()) if len(t) >= 4 and t[0] == "let"}


def term_tokens(controlled):
    """Tokens the kernel will bind as terms: kinds, colors, taught and confirmed words."""
    names, out = _declared(controlled), []
    for line in controlled.splitlines():
        t = line.split()
        if not t:
            continue
        colors = [t[i + 1] for i in range(len(t) - 1) if t[i] == "glim"]
        if t[0] == "let" and len(t) >= 4:
            out += [t[3]] + colors
        elif t[0] in ("teach", "confirm") and len(t) == 4:
            out += [t[1], t[3]]
        elif t[0] == "observe" and len(t) >= 2:
            out += ([] if t[1] in names else [t[1]]) + colors
        elif t[0] in ("ask", "estimate") and len(t) >= 3:
            args = {"value": t[2:3], "compare": t[3:], "has": t[3:]}.get(t[1], t[2:])
            out += [a for a in args if a not in names]
    return out


def attributes_asked(controlled):
    """Attributes asked for in `value <x> <attr>`. Answering a different attribute than the English
    named is promotion by substitution."""
    return [t[3] for t in (line.split() for line in controlled.splitlines())
            if len(t) >= 4 and t[0] in ("ask", "estimate") and t[1] == "value"]


def _stems(word):
    out = {word}
    for suffix in ("s", "es", "ed", "ing"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stem = word[:-len(suffix)]
            out |= {stem, stem[:-1] if stem[-1] == stem[-2] else stem}
    return out


class Guard:
    def __init__(self, domain, common=None, placeholders=(), strict=False):
        self.domain_words = domain_words(domain)
        self.familiar = (load_common() if common is None else set(common)) | self.domain_words | KEYWORDS
        self.placeholders = {p.lower() for p in placeholders}
        self.strict = strict
        self.max_distance = domain.get("matcher", {}).get("max_distance", 0.3)

    def _near_domain(self, word):
        return any(len(w) >= 3 and edit_distance(word, w) / max(len(word), len(w)) <= self.max_distance
                   for w in self.domain_words)

    def _is_familiar(self, word):
        if len(word) == 1 or any(ch.isdigit() for ch in word) or _stems(word) & self.familiar:
            return True
        # inflections of the domain's own words ("conductive" / conductance)
        return any(len(w) >= 6 and word[:6] == w[:6] for w in self.domain_words)

    def check(self, english, controlled):
        """Violations of faithful translation (empty if none)."""
        problems = []
        numbers = english_numbers(english)
        for tok in controlled.split():
            if NUMBER.fullmatch(tok) and _plain(tok) not in numbers:
                problems.append(f"the number {tok} is not in the question")
        words = _parts(english_words(english))
        for term in term_tokens(controlled):
            if term.lower() not in words and term not in ALLOWED_TERMS:
                problems.append(f"'{term}' does not appear in the question")
        for attr in attributes_asked(controlled):
            parts = [p for p in attr.split("_") if p]
            if not any(w.startswith(p[:5]) for p in parts for w in words):
                problems.append(f"the question never names '{attr}'")
        tokens = {tok.lower() for tok in controlled.split()}
        for p in sorted(self.placeholders & tokens):
            problems.append(f"'{p}' is a placeholder from the translator's examples")
        for word in sorted(english_words(english)):
            if word in tokens:
                continue
            pieces = word.split("-") if "-" in word else [word]
            missing = [p for p in pieces if p and p not in tokens and not self._is_familiar(p)
                       and (self.strict or self._near_domain(p))]
            if missing:
                reason = "is not common English" if self.strict else "looks like a misspelling of a known term"
                problems.append(f"'{word}' {reason} and did not reach the controlled input unchanged")
        return problems
