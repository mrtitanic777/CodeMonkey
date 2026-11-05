"""Analogy across domains (mshmoe/analogy.py): the map is found from structure, a missing law is
carried over only when asked to estimate, and what it gives is never more than CONFABULATED."""
import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from mshmoe import Library, load_domain  # noqa: E402
from mshmoe.analogy import structural_map, transfer  # noqa: E402
from mshmoe.states import State  # noqa: E402
from mshmoe.transcribe import value_text  # noqa: E402
from nour_map import NAMES  # noqa: E402  (how Nour was made; the kernel never sees it)

THREN = load_domain(os.path.join(ROOT, "domain", "thren.json"))
NOUR = load_domain(os.path.join(ROOT, "domain", "nour.json"))
CLASSES = ("kind", "rel", "qty", "cat", "val", "attr", "rattr")


def without(domain, prefix):
    d = copy.deepcopy(domain)
    d["rules"] = [r for r in d["rules"] if not r["id"].startswith(prefix)]
    return d


def one(result):
    a = result.answers[-1]
    return a.value.state, value_text(a.value) if a.value.payload is not None else None, a


class TheMap(unittest.TestCase):
    def test_every_pairing_found_from_structure_is_the_one_nour_was_made_with(self):
        m = structural_map(THREN, NOUR, ("thren", "nour"))
        self.assertGreater(len(m.symbols), 30)
        for (cls, name), got in m.symbols.items():
            self.assertEqual(got, NAMES[name], f"{cls} {name}")
        self.assertEqual(m.unpaired, [])

    def test_what_nothing_tells_apart_is_ambiguous_not_guessed(self):
        m = structural_map(THREN, NOUR)
        # ulm and seph are two seeps with no law and no fact of their own: either pairing fits
        self.assertEqual(m.ambiguous[("kind", "ulm")], ["brume", "wettle"])
        self.assertNotIn(("kind", "ulm"), m.symbols)

    def test_it_runs_the_other_way(self):
        m = structural_map(NOUR, THREN)
        back = {v: k for k, v in NAMES.items()}
        for (cls, name), got in m.symbols.items():
            self.assertEqual(got, back[name])

    def test_a_domain_of_another_shape_pairs_almost_nothing_and_carries_nothing_wrong(self):
        jars = {"tree": {"root": None, "vessel": "root", "jar": "vessel"}, "quantities": {"fill": {"unit": "cup"}},
                "categories": {}, "attributes": {"capacity": "cup"}, "relations": {"poured_into": {}, "overflows": {}},
                "facts": [], "bands": [],
                "rules": [{"id": "R1", "match": [["poured_into", "?x", "?y"]],
                           "conditions": [["gt", ["q", "fill", "?x"], ["a", "?y", "capacity"]]],
                           "conclude": ["overflows", "?y"]}], "operations": []}
        m = structural_map(THREN, jars)
        self.assertEqual(m.pairs, [])
        laws, blocked = transfer(m, THREN)
        self.assertEqual(laws, [])
        self.assertTrue(blocked)


class CarryingALawOver(unittest.TestCase):
    TEXT = "let a = orrin tarn {} vx\nlet m = mote\nrel abuts a m\n{} cracked m"

    def setUp(self):
        self.lib = Library({"thren": THREN, "nour": without(NOUR, "N5")})   # Nour without its cracking law

    def run_(self, tarn, verb):
        return one(self.lib.run(self.TEXT.format(tarn, verb), "nour", learn=False))

    def test_only_an_estimate_carries_it(self):
        state, _, a = self.run_(2400, "ask")
        self.assertEqual(state, State.NO_VALID_RULE)
        self.assertIn("`estimate` would carry it over", a.value.causes[-1])

    def test_it_gives_a_guess_on_each_side_of_the_threshold(self):
        # thren's L5 / L5-safe, renamed: above the mote's crack threshold (225 vx) it cracks
        state, value, a = self.run_(2400, "estimate")
        self.assertEqual((state, value), (State.CONFABULATED, "possibly yes"))
        self.assertIn("thren's law L5 has the same place", a.value.causes[0])
        self.assertIn("harmed → cracked", a.value.causes[0])
        self.assertIsNone(a.proof)
        state, value, a = self.run_(200, "estimate")
        self.assertEqual((state, value), (State.CONFABULATED, "possibly no"))
        self.assertEqual(a.value.provenance, ("thren's L5-safe by analogy",))

    def test_and_from_nour_to_thren(self):
        lib = Library({"thren": without(THREN, "L5"), "nour": NOUR})
        state, value, a = one(lib.run("let a = qelt pell 300 dg\nlet k = kiv\nrel touches a k\nestimate harmed k",
                                      "thren", learn=False))
        self.assertEqual((state, value), (State.CONFABULATED, "possibly yes"))
        self.assertIn("nour's law N5", a.value.causes[0])

    def test_a_law_resting_on_an_ambiguous_pairing_is_not_carried(self):
        thren = copy.deepcopy(THREN)
        thren["rules"].append({"id": "L9", "match": [["touches", "?u", "?k"], ["isa", "?u", "ulm"], ["isa", "?k", "kiv"]],
                               "conclude": ["harmed", "?k"]})
        laws, blocked = transfer(structural_map(thren, NOUR, ("thren", "nour")), thren)
        self.assertNotIn("thren:L9", [law["id"] for law in laws])
        self.assertIn("ambiguous", blocked["L9"])

    def test_a_domain_that_has_the_law_is_untouched(self):
        lib = Library({"thren": THREN, "nour": NOUR})
        state, value, _ = one(lib.run(self.TEXT.format(2400, "estimate"), "nour", learn=False))
        self.assertEqual((state, value), (State.DERIVED, "yes"))   # its own law, not a guess
        self.assertEqual(lib.kernels["nour"].carried()["thren"][0], [])

    def test_nothing_carried_over_is_ever_known_or_derived(self):
        texts = [self.TEXT.format(t, v) for t in (100, 225, 226, 4000) for v in ("ask", "estimate")]
        for text in texts:
            for a in self.lib.run(text, "nour", learn=False).answers:
                if a.goal == "cracked(m)":
                    self.assertNotIn(a.value.state, (State.KNOWN, State.DERIVED), text)


if __name__ == "__main__":
    unittest.main()
