"""Write domain/nour.json and the Nour case sets from thren's, through nour_map.

Run once (python tests/make_nour.py); the written files are what the kernel and the tests read.
The case sets' expectations are thren's, translated: if the kernel is domain-free, it gives the
same states and the translated values on Nour without any change to its code.
"""
import copy
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from nour_map import NAMES, SCALE, UNITS, _names, _num, _rule_ids, translate_goal, translate_input, translate_value  # noqa: E402

ATTR_SCALE = {"conductance": SCALE["Q·t/(dg·tick)"], "harm_threshold": SCALE["dg"], "resting_pell": SCALE["dg"]}


def _unit(u):
    return UNITS.get(u) or NAMES.get(u, u)


def _deep(x):
    """Every name in a rule or operation, renamed."""
    if isinstance(x, str):
        return x if x.startswith("?") else NAMES.get(x, x)
    if isinstance(x, list):
        return [_deep(v) for v in x]
    if isinstance(x, dict):
        return {k: _deep(v) for k, v in x.items()}
    return x


def domain(thren):
    d = {}
    d["tree"] = {NAMES[k]: (NAMES[v] if v else None) for k, v in thren["tree"].items()}
    d["aliases"] = {NAMES[k]: NAMES[v] for k, v in thren["aliases"].items()}
    d["matcher"] = dict(thren["matcher"])
    d["words"] = [NAMES[w] for w in thren["words"]]
    d["quantities"] = {"tarn": {"unit": "vx", "conversions": {"Kvx": 1000}}}
    d["categories"] = {NAMES[k]: {"values": [NAMES[v] for v in c["values"]], "absent": NAMES[c["absent"]]}
                       for k, c in thren["categories"].items()}
    d["attributes"] = {NAMES[k]: _unit(u) for k, u in thren["attributes"].items()}
    d["relations"] = {}
    for k, r in thren["relations"].items():
        r = copy.deepcopy(r)
        if "attrs" in r:
            r["attrs"] = {NAMES[a]: _unit(u) for a, u in r["attrs"].items()}
        d["relations"][NAMES[k]] = r
    d["facts"] = [{"term": NAMES[f["term"]], "attr": NAMES[f["attr"]],
                   "value": float(_num(f["value"] * ATTR_SCALE[f["attr"]])), "source": "N-" + f["source"]}
                  for f in thren["facts"]]
    d["bands"] = []
    for b in thren["bands"]:
        nb = {"id": _rule_ids(b["id"]), "applies_to": [NAMES[t] for t in b["applies_to"]]}
        if "quantity" in b:
            nb["quantity"] = NAMES[b["quantity"]]
        nb["category"] = NAMES[b["category"]]
        if "fixed" in b:
            nb["fixed"] = NAMES[b["fixed"]]
        if "bands" in b:
            nb["bands"] = [{"value": NAMES[x["value"]],
                            "lo": None if x["lo"] is None else x["lo"] * SCALE["dg"],
                            "hi": None if x["hi"] is None else x["hi"] * SCALE["dg"]} for x in b["bands"]]
        d["bands"].append(nb)
    d["rules"] = []
    for r in thren["rules"]:
        r = _deep(r)
        r["id"] = _rule_ids(r["id"])
        if "note" in r:
            r["note"] = _rule_ids(_names(r["note"]))
        d["rules"].append(r)
    d["operations"] = []
    for o in thren["operations"]:
        o = _deep(o)
        o["id"] = _rule_ids(o["id"])
        o["unit"] = _unit(thren["operations"][0]["unit"])
        o["boundary"] = [re.sub(r"<= (\d+)", lambda m: "<= " + _num(int(m.group(1)) * SCALE["dg"]), b) for b in o["boundary"]]
        d["operations"].append(o)
    return d


def prose(text):
    """Handbook and case English: names swapped, numbers in units rescaled."""
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(Q·t/\(dg·tick\)|Q/tick|Gdg|dg)(?![\w])",
                  lambda m: f"{_num(float(m.group(1)) * SCALE[m.group(2)])} {m.group(2)}", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?) t\b", lambda m: f"{_num(float(m.group(1)) * SCALE['t'])} t", text)
    for old, new in UNITS.items():
        text = text.replace(old, new)

    def word(m):
        w = m.group(1)
        low = w.lower()
        if low in NAMES and low not in ("t", "q", "none", "body", "thing"):
            new = NAMES[low]
            return new.capitalize() if w[0].isupper() else new
        return w
    return _rule_ids(re.sub(r"(?<![\w-])([A-Za-z_][\w-]*)", word, text))


def cases(path_in, path_out):
    with open(path_in, encoding="utf-8") as fh:
        source = json.load(fh)
    out = []
    for c in source:
        n = {"id": "N-" + c["id"], "category": c.get("category", ""), "mirror_of": c["id"]}
        if c.get("english"):
            n["english"] = prose(c["english"])
        n["input"] = translate_input(c["input"])
        n["expect"] = [{**e, "goal": translate_goal(e["goal"]),
                        **({"value": translate_value(e["value"])} if "value" in e else {})} for e in c["expect"]]
        if c.get("laws"):
            n["laws"] = [_rule_ids(x) for x in c["laws"]]
        out.append(n)
    with open(path_out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return len(out)


SETS = [("tests/thren_cases_controlled.json", "tests/nour_cases_controlled.json"),
        ("tests/heldout/batch1.json", "tests/heldout/nour_batch1.json"),
        ("tests/heldout/batch2.json", "tests/heldout/nour_batch2.json")]

if __name__ == "__main__":
    with open(os.path.join(ROOT, "domain", "thren.json"), encoding="utf-8") as fh:
        thren = json.load(fh)
    with open(os.path.join(ROOT, "domain", "nour.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(domain(thren), fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    # domain/nour_handbook.md is written by hand: prose does not translate word for word
    for a, b in SETS:
        print(b, cases(os.path.join(ROOT, a), os.path.join(ROOT, b)))
