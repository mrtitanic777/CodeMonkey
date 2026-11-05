"""The kernel's Phase 0 success criteria. Run: python -m unittest discover -s tests"""
import glob
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mshmoe import Kernel, load_domain  # noqa: E402
from mshmoe.kernel import domain_units  # noqa: E402
from mshmoe.states import SUPPORTED, State  # noqa: E402
from mshmoe.transcribe import NoisyTranscriber, check, value_text  # noqa: E402

DOMAIN = load_domain(os.path.join(ROOT, "domain", "thren.json"))
with open(os.path.join(ROOT, "tests", "thren_cases_controlled.json"), encoding="utf-8") as f:
    CASES = {c["id"]: c for c in json.load(f)}


def run(text, **kw):
    return Kernel(DOMAIN, **kw).run(text)


def answer(result, goal):
    return next(a for a in result.answers if a.goal == goal)


class ExpectedAnswers(unittest.TestCase):
    def test_all_cases(self):
        for case in CASES.values():
            with self.subTest(case=case["id"]):
                result = run(case["input"])
                for exp in case["expect"]:
                    a = answer(result, exp["goal"])
                    self.assertEqual(a.value.state.value, exp["state"], a.value.causes)
                    if "value" in exp:
                        self.assertEqual(value_text(a.value), exp["value"])
                    if "alternatives" in exp:
                        alts = [f"{v:g} {a.value.unit} (from {s})" for v, s in a.value.alternatives]
                        self.assertEqual(alts, exp["alternatives"])


class Criteria(unittest.TestCase):
    # 1. The invariant holds in every test, including fault injection.
    def test_1_invariant(self):
        for case in CASES.values():
            for a in run(case["input"]).answers:
                with self.subTest(case=case["id"], goal=a.goal):
                    if a.value.state in SUPPORTED:
                        self.assertEqual(a.value.causes, (), "a supported value may not carry failure causes")
                        self.assertIsNotNone(a.value.payload)
                    elif a.value.state is State.CONFABULATED:
                        self.assertTrue(value_text(a.value).startswith("possibly "))
                        self.assertTrue(a.value.experiment)
                    elif a.value.state is State.CONFLICTED:  # low confidence: marked, with an experiment
                        self.assertTrue(a.value.causes)
                        self.assertTrue(a.value.experiment)
                        text = value_text(a.value)
                        self.assertTrue(text == "unknown" or text.startswith("either "), text)
                    else:
                        self.assertEqual(value_text(a.value), "unknown")

    # 2. Failures come from the step that raised them; no later step does work.
    def test_2_early_failure_does_no_work(self):
        for cid in ("U1", "U2", "LC1", "LC2"):
            r = run(CASES[cid]["input"])
            self.assertEqual(r.steps, ["deduce", "transcribe"], cid)
            self.assertEqual(r.op_calls, 0)
            self.assertTrue(all(a.step == "deduce" for a in r.answers))
        for cid in ("M1", "M4", "N2", "B1", "B2", "B3"):
            r = run(CASES[cid]["input"])
            self.assertEqual(r.op_calls, 0, cid)
        self.assertEqual(answer(run(CASES["B1"]["input"]), "rate(a, b)").step, "execute")
        self.assertEqual(answer(run(CASES["M1"]["input"]), "rate(d, k)").step, "plan")

    # 3. Questions a pattern answers alone run zero operations.
    def test_3_qualitative_runs_no_operations(self):
        for cid in ("D1", "D2", "D3", "D4", "D5", "D6", "N3", "A1", "A2", "R1", "R2", "R3"):
            self.assertEqual(run(CASES[cid]["input"]).op_calls, 0, cid)
        self.assertEqual(run(CASES["O1"]["input"]).op_calls, 1)
        self.assertEqual(run(CASES["C2"]["input"]).op_calls, 2)  # once per disagreeing source

    # 4. Adding a domain is data only.
    def test_4_kernel_mentions_no_domain_names(self):
        names = set(DOMAIN["tree"]) | set(DOMAIN["aliases"]) | set(DOMAIN["quantities"]) \
            | set(DOMAIN["categories"]) | set(DOMAIN["attributes"]) | set(DOMAIN["relations"])
        for c in DOMAIN["categories"].values():
            names |= set(c["values"])
        source = ""
        for path in glob.glob(os.path.join(ROOT, "mshmoe", "*.py")):
            with open(path, encoding="utf-8") as f:
                source += f.read()
        # (?<!\.) skips Python attribute access such as ast's `.body`
        found = sorted(n for n in names if re.search(rf"(?<![.\w]){re.escape(n)}\b", source))
        self.assertEqual(found, [])

    def test_4_second_domain_runs_unchanged(self):
        mini = {
            "tree": {"root": None, "vessel": "root", "jar": "vessel", "urn": "vessel"},
            "quantities": {"fill": {"unit": "cup"}},
            "categories": {},
            "attributes": {"capacity": "cup"},
            "relations": {"poured_into": {}, "overflows": {}},
            "facts": [{"term": "jar", "attr": "capacity", "value": 4, "source": "M"}],
            "bands": [],
            "rules": [{"id": "R1", "match": [["poured_into", "?x", "?y"]],
                       "conditions": [["gt", ["q", "fill", "?x"], ["a", "?y", "capacity"]]],
                       "conclude": ["overflows", "?y"]}],
            "operations": [{"id": "R2", "goal": "spill", "match": [["poured_into", "?a", "?b"]],
                            "orient": ["gt", ["q", "fill", "?a"], 0],
                            "inputs": {"f": ["q", "fill", "?a"], "c": ["a", "?b", "capacity"]},
                            "formula": "f - c", "unit": "cup", "boundary": ["f >= c"]}],
        }
        r = Kernel(mini).run("let a = urn fill 6 cup\nlet b = jar\nrel poured_into a b\nask overflows b\nask spill a b")
        self.assertEqual(value_text(answer(r, "overflows(b)").value), "yes")
        self.assertEqual(value_text(answer(r, "spill(a, b)").value), "2 cup")

    # 5. The round-trip check catches all injected drift; the template always passes.
    def test_5_round_trip_catches_all_drift(self):
        injected = 0
        for case in CASES.values():
            for seed in range(25):
                k = Kernel(DOMAIN, transcriber=NoisyTranscriber(domain_units(DOMAIN), seed=seed, rate=0.8))
                r = k.run(case["input"])
                for attempt in r.transcript["attempts"]:
                    if attempt["drift"]:
                        injected += 1
                        self.assertFalse(attempt["accepted"], (case["id"], attempt))
                self.assertTrue(check(r.text, r.answers)[0])
        self.assertGreater(injected, 1000)

    # 6. A term taught in one run is used in a later run without code changes.
    def test_6_taught_term_persists(self):
        k = Kernel(DOMAIN)
        self.assertEqual(answer(k.run("ask class thrane"), "class(thrane)").value.state, State.UNKNOWN_REFERENCE)
        k.run("teach thrane isa metalline")
        a = answer(k.run("ask class thrane"), "class(thrane)")
        self.assertEqual((a.value.state, a.value.payload), (State.KNOWN, "metalline"))

    # 7. The matcher learns in use: a confirmation takes effect at once and stays.
    def test_7_matcher_learns_in_situ(self):
        k = Kernel(DOMAIN)
        q = "ask value qelth conductance"
        self.assertEqual(answer(k.run(q), "value(qelth, conductance)").value.state, State.LOW_CONFIDENCE)
        a = answer(k.run(q + "\nconfirm qelth means qelt"), "value(qelth, conductance)")  # same input, any order
        self.assertEqual((a.value.state, value_text(a.value)), (State.KNOWN, "3 Q·t/(dg·tick)"))
        a = answer(k.run(q), "value(qelth, conductance)")  # and in later runs
        self.assertEqual((a.value.state, value_text(a.value)), (State.KNOWN, "3 Q·t/(dg·tick)"))

    # 8 + 9. Validated commitment, only in consolidation, no regressions.
    def test_8_9_consolidation_commits_only_validated(self):
        k = Kernel(DOMAIN)
        q = "let d = dornite pell 600 dg\nask glim d"
        k.run("let a = qelt pell 300 dg\nask glim a")  # a supported past answer that must survive replay
        self.assertEqual(answer(k.run(q), "glim(d)").value.state, State.CONFABULATED)
        k.run("observe dornite pell 600 dg glim blue")
        k.run("observe dornite pell 800 dg glim green")
        self.assertEqual(answer(k.run(q), "glim(d)").value.state, State.CONFABULATED, "no commitment during runs")
        report = k.consolidate()
        self.assertEqual(len(report["committed"]), 1, report)
        a = answer(k.run(q), "glim(d)")
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "blue"))

    def test_8_refuted_by_observation(self):
        k = Kernel(DOMAIN)
        k.run("observe dornite pell 600 dg glim red")
        k.run("observe dornite pell 800 dg glim green")
        report = k.consolidate()
        self.assertEqual(report["committed"], [])
        self.assertEqual(len(report["refuted"]), 1)
        self.assertEqual(answer(k.run("let d = dornite pell 600 dg\nask glim d"), "glim(d)").value.state,
                         State.CONFABULATED)

    def test_8_rejected_on_replay_regression(self):
        k = Kernel(DOMAIN)
        k.run("let d = dornite pell 300 dg glim blue\nask pell d")  # KNOWN 300 dg; the extension would contradict it
        k.run("observe dornite pell 600 dg glim blue")
        k.run("observe dornite pell 800 dg glim green")
        report = k.consolidate()
        self.assertEqual(report["committed"], [])
        self.assertIn("replay regression", report["rejected"][0])

    # 10. Headspace is a reported cost, not a wall (spec revision 1).
    def test_10_headspace_is_a_cost(self):
        text = "\n".join(f"let e{i} = qelt pell {600 + i} dg" for i in range(5)) + "\nask glim e0"
        r = run(text)
        self.assertEqual(value_text(r.answers[0].value), "blue")
        self.assertEqual(r.headspace, {"chunks": 5, "limit": 4, "wide": True})
        r = run(CASES["A1"]["input"])
        self.assertEqual(answer(r, "contact(a, k)").value.state, State.AMBIGUOUS_PATTERN)
        self.assertFalse(r.headspace["wide"])

    # 11. Unreinforced items decay; committed constructs do not.
    def test_11_decay(self):
        k = Kernel(DOMAIN)
        k.run("observe dornite pell 600 dg glim blue")
        pruned = [p for _ in range(3) for p in k.consolidate()["pruned"]]
        self.assertEqual(len(pruned), 1, pruned)
        self.assertEqual(k.evidence, {})
        k.run("observe dornite pell 600 dg glim blue")
        k.run("observe dornite pell 700 dg glim green")
        k.consolidate()
        for _ in range(5):
            k.consolidate()
        self.assertIn("dornite", k._law("L2")["applies_to"])

    # 12. Confabulations are useful, marked, and never promoted without consolidation.
    def test_12_confabulation(self):
        for cid in ("F1", "F2", "F3"):
            a = run(CASES[cid]["input"]).answers[0]
            self.assertEqual(a.value.state, State.CONFABULATED)
            self.assertTrue(a.value.experiment)
        k = Kernel(DOMAIN)
        for _ in range(3):
            self.assertEqual(answer(k.run(CASES["F1"]["input"]), "glim(d)").value.state, State.CONFABULATED)
        # Numbers are not borrowed unless an estimate is asked for.
        self.assertEqual(answer(run(CASES["M1"]["input"]), "rate(d, k)").value.state, State.MISSING_KNOWLEDGE)


class HeldOutFixes(unittest.TestCase):
    """Regressions for the kernel bugs found by held-out batch 1."""

    def test_h31_channels(self):  # spec revision 1: contact m -> q; radiant would be uphill, so no passage
        r = run("let q = qelt pell 600 dg\nlet m = morvane pell 650 dg\n"
                "rel touches q m span 2 t\nrel sight q m\nask flows q m\nask radiant_flow q m\nask rate q m")
        self.assertEqual((answer(r, "flows(q, m)").value.state, value_text(answer(r, "flows(q, m)").value)),
                         (State.DERIVED, "m → q"))
        self.assertEqual(answer(r, "radiant_flow(q, m)").value.state, State.NO_VALID_RULE)
        self.assertEqual(value_text(answer(r, "rate(q, m)").value), "either 30 Q/tick or 50 Q/tick")

    def test_h09_harm_from_any_contact(self):
        r = run("let k = kiv\nlet m = morvane pell 45 dg\nlet q = qelt pell 0.46 Gdg\n"
                "rel touches k m\nrel touches k q\nask harmed k")
        a = r.answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "yes"))

    def test_h20_misspelled_color_is_low_confidence(self):
        r = run("let q = qelt pell 750 dg glim gren\nask glim q")
        self.assertEqual(r.answers[0].value.state, State.LOW_CONFIDENCE)
        self.assertIn("green", r.answers[0].value.causes[0])


class Revision1(unittest.TestCase):
    """Spec revision 1: answers that changed on purpose."""

    def test_failure_stays_with_its_questions(self):
        r = run("let s = qelt pell 750 dg glim yellow\nask glim s\nask value qelt conductance")
        self.assertEqual(answer(r, "glim(s)").value.state, State.UNKNOWN_REFERENCE)
        self.assertEqual(value_text(answer(r, "value(qelt, conductance)").value), "3 Q·t/(dg·tick)")

    def test_connected_entities_share_a_failure(self):
        r = run("let k = kiw\nlet q = qelt pell 300 dg\nrel touches k q\nask has glim q")
        self.assertEqual(r.answers[0].value.state, State.UNKNOWN_REFERENCE)

    def test_shifted_value_is_derived(self):
        a = run("let z = qelt pell 650 dg\nshift z pell 3 Gdg\nask pell z").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "950 dg"))

    def test_radiant_flow_needs_a_known_downhill(self):
        a = run("let h = qelt glim green\nlet k = kiv\nrel sight h k\nask flows h k").answers[0]
        self.assertEqual(a.value.state, State.MISSING_KNOWLEDGE)
        a = run("let h = qelt glim green\nlet k = kiv pell 20 dg\nrel sight h k\nask flows h k").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "h → k"))

    def test_specificity_is_pattern_containment(self):
        vessels = {
            "tree": {"root": None, "vessel": "root", "jar": "vessel", "urn": "vessel"},
            "quantities": {}, "categories": {}, "attributes": {}, "facts": [], "bands": [], "operations": [],
            "relations": {"poured_into": {}, "sealed": {}, "overflows": {}},
            "rules": [
                {"id": "G", "match": [["poured_into", "?x", "?y"]], "conclude": ["overflows", "?y"]},
                {"id": "S", "match": [["poured_into", "?x", "?y"], ["isa", "?y", "jar"]],
                 "conclude": ["overflows", "?y"], "polarity": False},
                {"id": "T", "match": [["sealed", "?y", "?z"]], "conclude": ["overflows", "?y"], "polarity": False},
            ],
        }
        k = Kernel(vessels)
        a = k.run("let a = urn\nlet b = jar\nrel poured_into a b\nask overflows b").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "no"))  # S contains G
        a = k.run("let a = urn\nlet b = urn\nlet c = urn\nrel poured_into a b\nrel sealed b c\n"
                  "ask overflows b").answers[0]
        self.assertEqual(a.value.state, State.AMBIGUOUS_PATTERN)  # neither contains the other


class LanguageExtensions(unittest.TestCase):
    """Forms the translator produces naturally."""

    def test_unintroduced_name_is_read_as_a_kind(self):
        a = run("ask has glim brask").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "no"))
        self.assertEqual(run("ask has glim morvite").answers[0].value.state, State.LOW_CONFIDENCE)
        self.assertEqual(run("ask harmed k2").answers[0].value.state, State.UNKNOWN_REFERENCE)

    def test_class_and_value_of_an_entity(self):
        r = run("let d = dornite pell 200 dg\nask class d\nask value d conductance")
        self.assertEqual(value_text(answer(r, "class(d)").value), "metalline")
        self.assertEqual(answer(r, "value(d, conductance)").value.state, State.MISSING_KNOWLEDGE)

    def test_observation_of_an_entity(self):
        a = run("let q = qelt pell 7 Gdg\nobserve q glim blue\nask glim q").answers[0]
        self.assertEqual(a.value.state, State.CONFLICTED)
        a = run("let q = qelt pell 600 dg\nobserve q pell 650 dg\nask pell q").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.CONFLICTED, "either 600 dg or 650 dg"))

    def test_disputed_quantity_that_does_not_change_the_band(self):  # batch 2, B2-25
        a = run("let h = qelt pell 300 dg\nobserve h pell 340 dg\nask has glim h").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.DERIVED, "no"))
        a = run("let h = qelt pell 450 dg\nobserve h pell 550 dg\nask glim h").answers[0]
        self.assertEqual((a.value.state, value_text(a.value)), (State.CONFLICTED, "either none or blue"))


if __name__ == "__main__":
    unittest.main()


class Ranges(unittest.TestCase):
    """Spec revision 2: operations run on ranges."""

    def test_rate_from_a_band_is_a_range(self):  # held-out B2-10
        r = run("let r = qelt glim green\nlet s = morvane pell 650 dg\nrel touches r s span 2 t\nask rate r s")
        a = answer(r, "rate(r, s)")
        self.assertEqual(a.value.state, State.DERIVED)
        self.assertEqual(value_text(a.value), "from 75 to below 375 Q/tick")

    def test_boundary_for_every_value_none_or_part(self):
        past = answer(run("let r = qelt glim green\nlet s = morvane pell 100 dg\nrel touches r s span 2 t\nask rate r s"), "rate(r, s)")
        self.assertEqual(past.value.state, State.OUT_OF_BOUNDARY)
        part = answer(run("let r = qelt glim red\nlet s = morvane pell 650 dg\nrel touches r s span 2 t\nask rate r s"), "rate(r, s)")
        self.assertEqual(part.value.state, State.MISSING_KNOWLEDGE)
        self.assertIn("only partly", " ".join(part.value.causes))

    def test_disagreeing_sources_give_one_range_each(self):
        a = answer(run("let m = morvane pell 800 dg\nlet r = qelt glim blue\nrel touches m r span 2 t\nask rate m r"), "rate(m, r)")
        self.assertEqual(a.value.state, State.CONFLICTED)
        self.assertEqual(sorted(p.describe() for p, _ in a.value.alternatives), ["from above 100 to 300", "from above 60 to 180"])

    def test_range_arithmetic_keeps_open_ends(self):
        from mshmoe.values import Interval
        self.assertEqual((Interval.band(700, 900) - 650).describe(), "from 50 to below 250")
        self.assertEqual((Interval(1, 2) * Interval(-3, 4)).describe(), "from -6 to 8")
        with self.assertRaises(ZeroDivisionError):
            Interval(1, 2) / Interval(-1, 1)


class WhatAnswersRestOn(unittest.TestCase):
    """Each answer says what it rests on and which clashes it touches; a fact's impact can be asked."""

    TEXT = ("let m = morvane pell 800 dg\nlet r = qelt glim blue\nlet s = qelt pell 300 dg\n"
            "rel touches m r span 2 t\nask pell r\nask flows m r\nask rate m r\nask compare pell r s")

    def test_rests_on_and_clashes(self):
        r = run(self.TEXT)
        rate = answer(r, "rate(m, r)")
        self.assertIn("store: conductance of morvane (S1)", rate.rests_on)
        self.assertIn("given: pell of m", rate.rests_on)
        self.assertEqual(len(rate.clashes), 1)
        self.assertIn("S1 says 1.2", rate.clashes[0])
        self.assertEqual(answer(r, "pell(r)").rests_on, ("given: glim of r",))
        self.assertEqual(answer(r, "pell(r)").clashes, ())

    def test_what_a_wrong_fact_would_change(self):
        r = run(self.TEXT)
        self.assertEqual(r.affected_by("pell of m"), ["flows(m, r)", "rate(m, r)"])
        self.assertEqual(r.affected_by("pell of s"), ["compare(pell, r, s)"])

    def test_given_and_observed_disagreeing_is_a_clash(self):
        r = run("let a = qelt pell 300 dg\nobserve a pell 500 dg\nask pell a")
        a = answer(r, "pell(a)")
        self.assertEqual(a.value.state, State.CONFLICTED)
        self.assertTrue(any("given and the observed pell of a disagree" in c for c in a.clashes), a.clashes)
