"""Thren -> Nour: the map that made the second domain, and that the mirror test reads back.

Nour (domain/nour.json, kernel/DOMAIN2.md) has thren's structure on a different surface: every
name, unit and number is different. Names are swapped one for one; quantities are rescaled
(tarn = 5 x pell, reach = 2 x span, pass = 0.5 x conductance), so a rate is 1.25 x thren's. A
misspelling in a thren case becomes a Nour misspelling at the same distance from its word (the
close-spelling threshold is 0.3 of the longer word), so near and not-near stay what they were.

translate_input() turns a thren case into a Nour case; translate_value() turns a thren answer into
the Nour answer it must equal. Only the tests and make_nour.py use this: the kernel never sees it.
"""
import re

NAMES = {
    # the tree
    "thing": "ens", "body": "form", "substance": "mass", "metalline": "resonant", "stonic": "dullrock",
    "fluent": "seep", "fibrous": "woven", "vital": "living",
    "qelt": "orrin", "dornite": "veskite", "morvane": "telkane", "brask": "shalk",
    "ulm": "wettle", "seph": "brume", "fenn": "loam", "kiv": "mote",
    "q-metal": "o-tone", "morv": "telk",
    # misspellings, each as near (or as far) as the thren one
    "dorniet": "veskiet", "dornit": "veskit", "fen": "loa", "kivv": "motee", "kiw": "moxi",
    "morvain": "telkain", "murvain": "tulkain", "morvite": "telkite", "qelth": "orrinh", "thrane": "sapher",
    "gren": "tenr",
    # words in no vocabulary
    "tessik": "brimmel", "zuppan": "pockle", "vorn": "jusk", "zint": "frab", "grell": "wimble",
    "yellow": "shrill",
    # what moves, how much is held, what shows it
    "thren": "saph", "pell": "tarn", "glim": "keen",
    "none": "silent", "blue": "treble", "green": "tenor", "red": "bass",
    "conductance": "linkance", "harm_threshold": "crack_threshold", "resting_pell": "resting_tarn",
    # relations
    "touches": "abuts", "wraps": "sheathes", "soaks": "steeps", "sight": "hears",
    "contact_path": "link_path", "contact": "link", "contact_flow": "link_flow", "radiant_flow": "chime_flow",
    "flows": "passes", "harmed": "cracked", "span": "reach", "radiant": "chiming", "Radiant": "Chiming",
    # units and words
    "dg": "vx", "Gdg": "Kvx", "t": "rd", "tick": "beat", "derg": "vex", "tal": "rod", "grand": "kilo",
    "Q": "S",
}

# what a number in a unit becomes
SCALE = {"dg": 5.0, "Gdg": 0.5, "t": 2.0, "Q·t/(dg·tick)": 0.5, "Q/tick": 1.25}
UNITS = {"Q·t/(dg·tick)": "S·rd/(vx·beat)", "Q/tick": "S/beat"}

_WORD = re.compile(r"(?<![\w-])([A-Za-z_][\w-]*)")
_NUM = r"-?\d+(?:\.\d+)?"


def _num(x: float) -> str:
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _names(text: str) -> str:
    return _WORD.sub(lambda m: NAMES.get(m.group(1), m.group(1)), text)


def _rule_ids(text: str) -> str:
    return re.sub(r"\bL(\d)\b", r"N\1", text)


def translate_input(text: str) -> str:
    """A thren case in the controlled language, as the same case in Nour."""
    out = []
    for line in text.split("\n"):
        line = re.sub(rf"({_NUM})\s+(Gdg|dg)\b", lambda m: f"{_num(float(m.group(1)) * SCALE[m.group(2)])} {m.group(2)}", line)
        line = re.sub(rf"^(measure\s+\S+\s+conductance\s+)({_NUM})",
                      lambda m: m.group(1) + _num(float(m.group(2)) * SCALE["Q·t/(dg·tick)"]), line)
        line = re.sub(rf"\bspan\s+({_NUM})\s+t\b", lambda m: f"span {_num(float(m.group(1)) * SCALE['t'])} t", line)
        out.append(_names(line))
    return "\n".join(out)


def translate_value(text: str) -> str:
    """A thren answer's text, as the Nour answer it must equal. Every number is scaled by the one
    unit the answer is in."""
    text = str(text)
    unit = next((u for u in ("Q·t/(dg·tick)", "Q/tick", "Gdg", "dg") if u in text), None)
    if unit is None and re.search(r"\d\s*t\b", text):
        unit = "t"
    if unit:
        text = re.sub(_NUM, lambda m: _num(float(m.group(0)) * SCALE[unit]), text)
    for old, new in UNITS.items():
        text = text.replace(old, new)
    return _rule_ids(_names(text))


def translate_goal(goal: str) -> str:
    return _names(goal)
