"""The shape of a controlled line, from the domain: what a translation may contain at all.

The model writing the controlled language is given this as a pattern for each line, so a server that
enforces patterns (llama.cpp's JSON-schema grammars, as LM Studio uses) cannot produce a malformed
line; every line is checked against it here as well, for a server that ignores it.

Statement shapes and keywords are fixed (`let <name> = <term> ...`, `rel <relation> <a> <b>`, the
quantity and category words, `ask ...`). Names and terms are any word: a misspelling must reach the
kernel as written, so its matcher (not the translator) decides what the word means.
"""
import re

WORD = r"[A-Za-z][A-Za-z0-9_'-]*"
NUMBER = r"-?[0-9]+([.][0-9]+)?"


def _one_of(words):
    words = sorted({w for w in words if w}, key=len, reverse=True)
    return "(" + "|".join(re.escape(w) for w in words) + ")" if words else "(?!)"


def line_pattern(domain):
    """One regular expression for a well-formed line (plain groups only, so ECMA and Python agree)."""
    q = _one_of(domain.get("quantities", {}))
    c = _one_of(domain.get("categories", {}))
    r = _one_of(domain.get("relations", {}))
    attrs = _one_of({a for spec in domain.get("relations", {}).values() for a in spec.get("attrs", {})})
    prop = f"( {q} {NUMBER} {WORD}| {c} {WORD})"
    statements = [
        f"let {WORD} = {WORD}{prop}*",
        f"rel {r} {WORD} {WORD}( {attrs} {NUMBER} {WORD})*",
        f"unrel {r} {WORD} {WORD}",
        f"shift {WORD} {q} {NUMBER} {WORD}",
        f"teach {WORD} isa {WORD}",
        f"confirm {WORD} means {WORD}",
        f"observe {WORD}{prop}+",
        f"measure {WORD} {WORD} {NUMBER}( [^ ]+)?",
        f"(ask|estimate) {WORD}( {WORD})*",
    ]
    return f"^(at {NUMBER} )?(" + "|".join(f"({s})" for s in statements) + ")$"


def schema(domain):
    """The translator's output schema: a list of lines, each matching the pattern."""
    return {"type": "object",
            "properties": {"lines": {"type": "array", "items": {"type": "string", "pattern": line_pattern(domain)}}},
            "required": ["lines"], "additionalProperties": False}


def malformed(domain, lines):
    """The lines that do not have the shape of a statement of the language."""
    rx = re.compile(line_pattern(domain))
    return [line for line in lines if not rx.match(line.strip())]
