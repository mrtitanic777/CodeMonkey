"""Learning new laws, and the sources' record (kernel items 5 and 6)."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mshmoe import Kernel, load_domain  # noqa: E402
from mshmoe.states import State  # noqa: E402
from mshmoe.transcribe import value_text  # noqa: E402
import proofcheck  # noqa: E402

DOMAIN = load_domain(os.path.join(ROOT, "domain", "thren.json"))


def answer(result, goal):
    return next(a for a in result.answers if a.goal == goal)


def observe(k, *pairs):
    for pell, glim in pairs:
        k.run(f"observe ulm pell {pell} dg glim {glim}")


class NewLaws(unittest.TestCase):
    def test_a_law_is_learned_from_observations_and_proven(self):
        k = Kernel(DOMAIN)
        observe(k, (100, "blue"), (200, "blue"), (600, "red"), (700, "red"))
        report = k.consolidate()
        self.assertEqual(len(report["learned"]), 1, report)
        text = "let u = ulm pell 150 dg\nask glim u"
        r = k.run(text, learn=False)
        a = answer(r, "glim(u)")
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "blue"))
        self.assertEqual(proofcheck.check_run(json.loads(json.dumps(k.domain)), text, r.answers), {})
        # nothing is claimed beyond what was seen: between the bands, and past them, it stays unsettled
        for pell in (400, 900):
            a = answer(k.run(f"let u = ulm pell {pell} dg\nask glim u", learn=False), "glim(u)")
            self.assertNotIn(a.value.state, (State.KNOWN, State.DERIVED), pell)

    def test_too_little_evidence_proposes_nothing(self):
        k = Kernel(DOMAIN)
        observe(k, (100, "blue"), (600, "red"))
        self.assertEqual(k.consolidate()["learned"], [])

    def test_overlapping_values_refute_any_law(self):
        k = Kernel(DOMAIN)
        observe(k, (100, "blue"), (150, "red"), (200, "blue"))
        report = k.consolidate()
        self.assertEqual(report["learned"], [])
        self.assertTrue(any("overlapping" in r for r in report["refuted"]), report)

    def test_a_learned_law_is_withdrawn_on_one_contradiction_and_stays_refuted(self):
        k = Kernel(DOMAIN)
        observe(k, (100, "blue"), (200, "blue"), (600, "red"), (700, "red"))
        k.consolidate()
        observe(k, (150, "red"))
        report = k.consolidate()
        self.assertEqual(len(report["withdrawn"]), 1, report)
        observe(k, (110, "blue"), (210, "blue"), (610, "red"), (710, "red"))
        self.assertEqual(k.consolidate()["learned"], [], "a refuted law is not learned again")
        a = answer(k.run("let u = ulm pell 150 dg\nask glim u", learn=False), "glim(u)")
        self.assertNotEqual(a.value.state, State.DERIVED)

    def test_replay_regression_blocks_a_new_law(self):
        k = Kernel(DOMAIN)
        k.run("let u = ulm pell 150 dg glim red\nask glim u")   # a supported past answer the law would contradict
        observe(k, (100, "blue"), (200, "blue"), (600, "red"), (700, "red"))
        report = k.consolidate()
        self.assertEqual(report["learned"], [])
        self.assertTrue(any("replay regression" in r for r in report["rejected"]), report)


class Measurements(unittest.TestCase):
    RATE = "let m = morvane pell 800 dg\nlet r = qelt pell 600 dg\nrel touches m r span 2 t\nask rate m r"

    def test_measurements_settle_a_disagreement_and_the_answer_is_proven(self):
        k = Kernel(DOMAIN)
        self.assertEqual(answer(k.run(self.RATE, learn=False), "rate(m, r)").value.state, State.CONFLICTED)
        k.run("measure morvane conductance 2.0")
        k.run("measure morvane conductance 2.05")
        report = k.consolidate()
        self.assertEqual(len(report["settled"]), 1, report)
        self.assertIn("S1 refuted", report["settled"][0])
        r = k.run(self.RATE, learn=False)
        a = answer(r, "rate(m, r)")
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "200 Q/tick"))
        self.assertEqual(proofcheck.check_run(json.loads(json.dumps(k.domain)), self.RATE, r.answers), {})
        self.assertEqual(k.domain["source_record"]["S2"], {"right": 2, "wrong": 0})
        self.assertEqual(k.domain["source_record"]["S1"], {"right": 0, "wrong": 2})

    def test_one_measurement_leaves_it_unsettled_but_shows_the_record(self):
        k = Kernel(DOMAIN)
        k.run("measure morvane conductance 2.0")
        self.assertEqual(k.consolidate()["settled"], [])
        a = answer(k.run(self.RATE, learn=False), "rate(m, r)")
        self.assertEqual(a.value.state, State.CONFLICTED, "a record is not evidence: the value stays unsettled")
        self.assertTrue(any("S2 was right 1 of 1" in c for c in a.value.causes), a.value.causes)

    def test_measurements_that_disagree_settle_nothing(self):
        k = Kernel(DOMAIN)
        k.run("measure morvane conductance 1.2")
        k.run("measure morvane conductance 2.0")
        self.assertEqual(k.consolidate()["settled"], [])

    def test_a_value_no_source_gave_is_kept_as_measured(self):
        k = Kernel(DOMAIN)
        k.run("measure morvane conductance 3.0")
        k.run("measure morvane conductance 3.05")
        report = k.consolidate()
        self.assertIn("measured value is kept", report["settled"][0])
        a = answer(k.run("ask value morvane conductance", learn=False), "value(morvane, conductance)")
        self.assertEqual(a.value.state, State.KNOWN)


class BestExperiment(unittest.TestCase):
    """Kernel item 7: the experiment that settles the most open questions whatever its outcome."""

    def test_a_disagreeing_source_is_settled_by_measuring_it(self):
        from mshmoe import experiments
        k = Kernel(DOMAIN)
        text = "let m = morvane pell 800 dg\nlet r = qelt pell 600 dg\nrel touches m r span 2 t\nask rate m r"
        best = experiments.plan(k, text)[0]
        self.assertEqual(best["experiment"], "measure conductance of morvane")
        self.assertEqual(best["worst"], 1)

    def test_outcomes_are_imagined_not_learned(self):
        from mshmoe import experiments
        k = Kernel(DOMAIN)
        text = "let m = morvane pell 800 dg\nlet r = qelt\nrel touches m r span 2 t\nask pell r\nask flows m r"
        plans = experiments.plan(k, text)
        self.assertEqual(plans[0]["experiment"], "measure the pell of r")
        self.assertIn("flows(m, r)", plans[0]["outcomes"][0][1])
        self.assertEqual(k.episodes, [], "planning runs nothing that is kept")
        self.assertEqual(answer(k.run(text, learn=False), "flows(m, r)").value.state, State.MISSING_KNOWLEDGE)

    def test_nothing_open_means_nothing_to_plan(self):
        from mshmoe import experiments
        self.assertEqual(experiments.plan(Kernel(DOMAIN), "let a = qelt pell 600 dg\nask glim a"), [])


class Time(unittest.TestCase):
    """Kernel item 8: facts at points in time."""

    TEXT = ("let a = qelt pell 800 dg glim green\nlet b = qelt pell 600 dg\nrel touches a b span 2 t\n"
            "at 0 ask flows a b\nat 3 ask flows a b\nat 5 shift a pell -300 dg\nat 5 ask flows a b\n"
            "at 5 ask glim a\nat 8 unrel touches a b\nat 8 ask flows a b")

    def test_each_moment_sees_the_facts_that_hold_then(self):
        r = Kernel(DOMAIN).run(self.TEXT, learn=False)
        got = [(a.goal, a.value.state, value_text(a.value) if a.value.payload is not None else None) for a in r.answers]
        self.assertEqual(got, [
            ("flows(a, b) at 0", State.DERIVED, "a → b"),
            ("flows(a, b) at 3", State.DERIVED, "a → b"),
            ("flows(a, b) at 5", State.DERIVED, "b → a"),
            ("glim(a) at 5", State.DERIVED, "blue"),      # the earlier observation ended with the shift
            ("flows(a, b) at 8", State.NO_VALID_RULE, None),
        ])

    def test_answers_about_a_moment_are_proven_against_that_moment(self):
        r = Kernel(DOMAIN).run(self.TEXT, learn=False)
        for a in r.answers:
            if a.value.state in (State.KNOWN, State.DERIVED):
                self.assertTrue(a.input)
                self.assertEqual(proofcheck.Checker(json.loads(json.dumps(DOMAIN)), a.input).check(a.proof), [], a.goal)

    def test_what_happens_next_says_what_is_missing(self):
        r = Kernel(DOMAIN).run("let a = qelt pell 800 dg\nat 2 ask next pell a", learn=False)
        self.assertEqual(r.answers[0].value.state, State.MISSING_KNOWLEDGE)
        self.assertIn("no law for how fast", r.answers[0].value.causes[0])

    def test_untimed_input_is_unchanged(self):
        text = "let a = qelt pell 800 dg\nask glim a"
        self.assertEqual(Kernel(DOMAIN).run(text, learn=False).answers[0].goal, "glim(a)")


class Matching(unittest.TestCase):
    """Kernel item 9: indexed, semi-naive matching; what an answer says never depends on input order."""

    def test_answers_and_explanations_do_not_depend_on_statement_order(self):
        import random
        rng = random.Random(7)
        for path in ("tests/thren_cases_controlled.json", "tests/heldout/batch1.json", "tests/heldout/batch2.json"):
            with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                cases = json.load(fh)
            for case in cases:
                lines = case["input"].splitlines()
                facts = [ln for ln in lines if ln.split()[:1] not in (["ask"], ["estimate"], ["teach"], ["confirm"],
                                                                      ["shift"], ["observe"])]
                if any(ln.split()[:1] in (["shift"], ["observe"]) for ln in lines):
                    continue   # order is meaningful for these
                rest = [ln for ln in lines if ln not in facts]
                # a name must be introduced before a relation uses it: shuffle within each kind of line
                lets = [ln for ln in facts if ln.startswith("let ")]
                rels = [ln for ln in facts if not ln.startswith("let ")]
                rng.shuffle(lets)
                rng.shuffle(rels)
                shuffled = lets + rels
                facts = [ln for ln in facts if ln.startswith("let ")] + [ln for ln in facts if not ln.startswith("let ")]
                a = Kernel(DOMAIN).run("\n".join(facts + rest), learn=False).answers
                b = Kernel(DOMAIN).run("\n".join(shuffled + rest), learn=False).answers
                self.assertEqual([(x.goal, x.value.state, value_text(x.value), x.rests_on) for x in a],
                                 [(x.goal, x.value.state, value_text(x.value), x.rests_on) for x in b], case["id"])

    def test_a_long_chain_stays_fast(self):
        import time
        n = 120
        lines = [f"let e{i} = qelt pell {900 - i} dg" for i in range(n)]
        lines += [f"rel touches e{i} e{i + 1} span 2 t" for i in range(n - 1)]
        lines += ["ask flows e0 e1", f"ask flows e{n - 2} e{n - 1}"]
        t0 = time.perf_counter()
        r = Kernel(DOMAIN).run("\n".join(lines), learn=False)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(a.value.state == State.DERIVED for a in r.answers))
