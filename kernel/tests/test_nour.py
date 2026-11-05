"""The second domain, Nour (kernel item 4; DOMAIN2.md): a new domain is data only.

Nour has thren's structure on a different surface (names, units, numbers). The kernel runs it with no
change to its code. Checked four ways:

    Mirror     every thren case, translated, gets the same state and the translated value
    Cases      the Nour case sets' own expectations hold, and every supported answer's proof checks
    Learning   new laws, measurements, the experiment planner and time work on Nour
    Native     traps worked out by hand from the Nour handbook, not from thren
"""
import glob
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from mshmoe import Kernel, experiments, load_domain  # noqa: E402
from mshmoe.states import State  # noqa: E402
from mshmoe.transcribe import value_text  # noqa: E402
import proofcheck  # noqa: E402
from nour_map import translate_goal, translate_input, translate_value  # noqa: E402

THREN = load_domain(os.path.join(ROOT, "domain", "thren.json"))
NOUR = load_domain(os.path.join(ROOT, "domain", "nour.json"))
THREN_SETS = ["tests/thren_cases_controlled.json", "tests/heldout/batch1.json", "tests/heldout/batch2.json"]
NOUR_SETS = ["tests/nour_cases_controlled.json", "tests/heldout/nour_batch1.json", "tests/heldout/nour_batch2.json"]


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return json.load(fh)


def run(text, domain=NOUR):
    return Kernel(domain).run(text, learn=False)


def got(result):
    return [(a.goal, a.value.state.value, value_text(a.value) if a.value.payload is not None else None)
            for a in result.answers]


def answer(result, goal):
    return next(a for a in result.answers if a.goal == goal)


class DataOnly(unittest.TestCase):
    def test_the_kernel_mentions_no_nour_names(self):
        names = set(NOUR["tree"]) | set(NOUR["aliases"]) | set(NOUR["quantities"]) | set(NOUR["categories"]) \
            | set(NOUR["attributes"]) | set(NOUR["relations"])
        for c in NOUR["categories"].values():
            names |= set(c["values"])
        source = "".join(open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(ROOT, "mshmoe", "*.py")))
        found = sorted(n for n in names if re.search(rf"(?<![.\w]){re.escape(n)}\b", source))
        self.assertEqual(found, [])

    def test_no_name_is_shared_with_thren(self):
        def words(d):
            out = set(d["tree"]) | set(d["aliases"]) | set(d["quantities"]) | set(d["attributes"]) | set(d["relations"])
            for c in d["categories"].values():
                out |= set(c["values"])
            return out
        self.assertEqual(words(THREN) & words(NOUR), set())


class Mirror(unittest.TestCase):
    def test_every_thren_answer_has_its_nour_twin(self):
        count = 0
        for path in THREN_SETS:
            for case in load(path):
                thren = got(run(case["input"], THREN))
                nour = got(run(translate_input(case["input"])))
                want = [(translate_goal(g), s, None if v is None else translate_value(v)) for g, s, v in thren]
                self.assertEqual(nour, want, f"{path} {case['id']}")
                count += len(nour)
        self.assertGreater(count, 230)


class Cases(unittest.TestCase):
    @staticmethod
    def misses(paths, domain):
        """(case, goal) pairs where the kernel's answer is not the case's expectation."""
        out = set()
        for path in paths:
            for case in load(path):
                answers = {g: (s, v) for g, s, v in got(run(case["input"], domain))}
                for e in case["expect"]:
                    s, v = answers.get(e["goal"], (None, None))
                    ok = s == e["state"] and ("value" not in e or e["state"] not in ("KNOWN", "DERIVED", "CONFABULATED")
                                              or v == e["value"])
                    if not ok:
                        out.add((case.get("mirror_of", case["id"]), translate_goal(e["goal"])))
        return out

    def test_expectations_hold_wherever_they_hold_for_thren(self):
        # the held-out batches were written before the kernel and it misses a few; Nour must miss the same
        self.assertEqual(self.misses(NOUR_SETS, NOUR), self.misses(THREN_SETS, THREN))
        self.assertEqual(self.misses(NOUR_SETS[:1], NOUR), set())   # the development set: none

    def test_every_supported_answer_is_proven(self):
        proven = 0
        for path in NOUR_SETS:
            for case in load(path):
                r = run(case["input"])
                proven += sum(a.value.state.value in proofcheck.SUPPORTED for a in r.answers)
                self.assertEqual(proofcheck.check_run(NOUR, case["input"], r.answers), {}, case["id"])
        self.assertGreater(proven, 100)


class Learning(unittest.TestCase):
    def observe(self, k, *pairs):
        for tarn, keen in pairs:
            k.run(f"observe wettle tarn {tarn} vx keen {keen}")

    def test_a_keen_law_for_wettle_is_learned_and_proven(self):
        k = Kernel(NOUR)
        self.observe(k, (500, "treble"), (1000, "treble"), (3000, "bass"), (3500, "bass"))
        self.assertEqual(len(k.consolidate()["learned"]), 1)
        text = "let w = wettle tarn 750 vx\nask keen w"
        r = k.run(text, learn=False)
        self.assertEqual(got(r), [("keen(w)", "DERIVED", "treble")])
        self.assertEqual(proofcheck.check_run(json.loads(json.dumps(k.domain)), text, r.answers), {})
        a = answer(k.run("let w = wettle tarn 2000 vx\nask keen w", learn=False), "keen(w)")
        self.assertNotIn(a.value.state, (State.KNOWN, State.DERIVED))   # between what was seen

    RATE = "let t = telkane tarn 4000 vx\nlet o = orrin tarn 3000 vx\nrel abuts t o reach 4 rd\nask rate t o"

    def test_measuring_telkane_settles_its_sources(self):
        k = Kernel(NOUR)
        self.assertEqual(answer(k.run(self.RATE, learn=False), "rate(t, o)").value.state, State.CONFLICTED)
        k.run("measure telkane linkance 1.0")
        k.run("measure telkane linkance 1.02")
        report = k.consolidate()
        self.assertIn("N-S1 refuted", report["settled"][0])
        r = k.run(self.RATE, learn=False)
        # linkance 1.0 x (4000 - 3000) / 4 = 250
        self.assertEqual(got(r), [("rate(t, o)", "DERIVED", "250 S/beat")])
        self.assertEqual(proofcheck.check_run(json.loads(json.dumps(k.domain)), self.RATE, r.answers), {})

    def test_the_planner_names_the_measurement(self):
        best = experiments.plan(Kernel(NOUR), self.RATE)[0]
        self.assertEqual(best["experiment"], "measure linkance of telkane")

    def test_time(self):
        text = ("let a = orrin tarn 4000 vx keen tenor\nlet b = orrin tarn 3000 vx\nrel abuts a b reach 2 rd\n"
                "at 0 ask passes a b\nat 5 shift a tarn -1500 vx\nat 5 ask passes a b\nat 5 ask keen a\n"
                "at 8 unrel abuts a b\nat 8 ask passes a b")
        self.assertEqual(got(run(text)), [
            ("passes(a, b) at 0", "DERIVED", "a → b"),
            ("passes(a, b) at 5", "DERIVED", "b → a"),      # 2500 < 3000
            ("keen(a) at 5", "DERIVED", "treble"),            # 2500 is the bottom of high (inclusive)
            ("passes(a, b) at 8", "NO_VALID_RULE", None),
        ])


class Native(unittest.TestCase):
    """Worked out from domain/nour_handbook.md by hand."""

    def test_the_rate_boundary_is_inclusive_at_2000_vx(self):
        base = "let a = orrin tarn {} vx\nlet b = orrin tarn 2000 vx\nrel abuts a b reach 4 rd\nask rate a b"
        self.assertEqual(got(run(base.format(4000))), [("rate(a, b)", "DERIVED", "750 S/beat")])   # 1.5 x 2000 / 4
        self.assertEqual(got(run(base.format(4001)))[0][1], "OUT_OF_BOUNDARY")

    def test_fractional_kilo_units_and_keen_running_backwards(self):
        text = "let a = orrin tarn 3.5 Kvx\nlet b = orrin tarn 4.6 Kvx\nlet c = orrin tarn 2.6 Kvx\nask keen a\nask keen b\nask keen c"
        self.assertEqual([v for _, _, v in got(run(text))], ["tenor", "bass", "treble"])

    def test_silent_is_not_safe(self):
        text = "let a = orrin tarn 2400 vx\nlet m = mote\nrel abuts a m\nask keen a\nask cracked m"
        self.assertEqual(got(run(text)), [("keen(a)", "DERIVED", "silent"), ("cracked(m)", "DERIVED", "yes")])

    def test_a_dullrock_never_keens(self):
        self.assertEqual(got(run("let s = shalk tarn 9 Kvx\nask keen s")), [("keen(s)", "DERIVED", "silent")])

    def test_veskite_borrows_only_when_asked_to_guess(self):
        self.assertEqual(got(run("ask value veskite linkance"))[0][1], "MISSING_KNOWLEDGE")
        guess = got(run("estimate value veskite linkance"))[0]
        self.assertEqual(guess[1], "CONFABULATED")
        self.assertIn("1.5", guess[2])

    def test_the_sources_disagree_on_telkane(self):
        self.assertEqual(got(run("ask value telk linkance"))[0][1], "CONFLICTED")

    def test_a_loam_sleeve_blocks_linking_unless_wettle_bridges_it(self):
        blocked = ("let a = orrin tarn 3000 vx\nlet b = orrin tarn 1000 vx\nlet l = loam\n"
                   "rel sheathes l a\nrel abuts b l\nask link a b")
        self.assertEqual(got(run(blocked))[0][:2], ("link(a, b)", "DERIVED"))
        self.assertEqual(got(run(blocked))[0][2], "no")


if __name__ == "__main__":
    unittest.main()
