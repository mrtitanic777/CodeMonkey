"""Proofs and the independent checker (kernel item 3).

Every supported answer in the development set and both held-out batches must carry a proof that
proofcheck.py accepts; tampered proofs must be rejected; the checker must not import the engine.
"""
import copy
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mshmoe import Kernel, load_domain  # noqa: E402
import proofcheck  # noqa: E402

DOMAIN_PATH = os.path.join(ROOT, "domain", "thren.json")
with open(DOMAIN_PATH, encoding="utf-8") as fh:
    DOMAIN = json.load(fh)
SETS = ["tests/thren_cases_controlled.json", "tests/heldout/batch1.json", "tests/heldout/batch2.json"]


def run(text):
    return Kernel(load_domain(DOMAIN_PATH)).run(text, learn=False)


def answer(result, goal):
    return next(a for a in result.answers if a.goal == goal)


class EverySupportedAnswerIsProven(unittest.TestCase):
    def test_all_case_sets(self):
        count = 0
        for path in SETS:
            with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                for case in json.load(fh):
                    r = run(case["input"])
                    count += sum(a.value.state.value in proofcheck.SUPPORTED for a in r.answers)
                    bad = proofcheck.check_run(DOMAIN, case["input"], r.answers)
                    self.assertEqual(bad, {}, f"{path} {case['id']}")
        self.assertGreater(count, 100)

    def test_unsupported_answers_carry_no_proof(self):
        r = run("let m = morvane pell 800 dg\nlet r = qelt glim blue\nrel touches m r span 2 t\nask rate m r")
        a = answer(r, "rate(m, r)")
        self.assertEqual(a.value.state.value, "CONFLICTED")
        self.assertIsNone(a.proof)


class TamperedProofsAreRejected(unittest.TestCase):
    TEXT = "let m = morvane pell 800 dg\nlet r = qelt glim blue\nrel touches r m span 2 t\nask flows m r\nask pell r"
    RATE = "let a = qelt pell 900 dg\nlet b = morvane pell 650 dg\nrel touches a b span 2 t\nask rate a b"

    def _proof(self, text, goal):
        return copy.deepcopy(answer(run(text), goal).proof)

    def _rejects(self, text, proof):
        return proofcheck.Checker(DOMAIN, text).check(proof)

    def test_untouched_proofs_check(self):
        self.assertEqual(self._rejects(self.TEXT, self._proof(self.TEXT, "flows(m, r)")), [])
        self.assertEqual(self._rejects(self.RATE, self._proof(self.RATE, "rate(a, b)")), [])

    def test_a_changed_range_is_caught(self):
        proof = self._proof(self.TEXT, "pell(r)")
        band = next(s for s in proof if s["by"] == "band")
        band["claim"] = band["claim"][:3] + ((500, 800, False, True),)
        self.assertTrue(self._rejects(self.TEXT, proof))

    def test_a_made_up_given_fact_is_caught(self):
        proof = self._proof(self.TEXT, "flows(m, r)")
        given = next(s for s in proof if s["by"] == "given" and s["claim"][0] == "quantity")
        given["claim"] = ("quantity", "pell", "m", (950.0, 950.0, False, False))
        self.assertTrue(self._rejects(self.TEXT, proof))

    def test_a_wrong_computed_value_is_caught(self):
        proof = self._proof(self.RATE, "rate(a, b)")
        op = next(s for s in proof if s["by"] == "operation")
        op["claim"] = ("value", (400.0, 400.0, False, False))
        self.assertTrue(self._rejects(self.RATE, proof))

    def test_a_missing_premise_is_caught(self):
        proof = self._proof(self.TEXT, "flows(m, r)")
        rule = next(s for s in proof if s["by"] == "rule" and s["rule"] == "flow-contact")
        rule["uses"] = ()
        self.assertTrue(self._rejects(self.TEXT, proof))

    def test_a_flipped_conclusion_is_caught(self):
        proof = self._proof(self.TEXT, "flows(m, r)")
        rule = next(s for s in proof if s["by"] == "rule" and s["rule"] == "flow-contact")
        rule["claim"] = ("rel", ("flows", "r", "m"), True, ())
        self.assertTrue(self._rejects(self.TEXT, proof))

    def test_a_relation_the_input_never_gave_is_caught(self):
        proof = self._proof(self.TEXT, "flows(m, r)")
        given = next(s for s in proof if s["by"] == "given" and s["claim"][0] == "rel")
        given["claim"] = ("rel", ("sight", "r", "m"), True, ())
        self.assertTrue(self._rejects(self.TEXT, proof))


class OpposingLawsAreFoundNotTrusted(unittest.TestCase):
    """The checker runs the laws itself, so a rule step cannot leave out a law that opposes it.

    Thren has no pair of laws where one is strictly more specific, so a copy gets one: L6b, a wrap
    that blocks even a direct touch (it contains direct-contact's pattern, and more)."""

    BLOCKED = "let a = qelt\nlet b = qelt\nlet w = fenn\nrel wraps w a\nrel touches b w\nrel touches a b\nask contact a b"
    OPEN = "let a = qelt\nlet b = qelt\nlet w = fenn\nrel touches a b\nask contact a b"

    @classmethod
    def setUpClass(cls):
        cls.domain = copy.deepcopy(DOMAIN)
        cls.domain["rules"].append({"id": "L6b", "match": [["isa", "?w", "fenn"], ["wraps", "?w", "?x"],
                                                           ["touches", "?y", "?w"], ["touches", "?x", "?y"]],
                                    "distinct": [["?x", "?y"]], "conclude": ["contact", "?x", "?y"], "polarity": False})
        cls.domain["rules"] = [r for r in cls.domain["rules"] if r["id"] != "L6"]

    def run_(self, text):
        return answer(Kernel(self.domain).run(text, learn=False), "contact(a, b)")

    def rule_step(self, proof):
        return next(s for s in proof if s["by"] == "rule")

    def test_the_real_proof_lists_what_it_beat(self):
        a = self.run_(self.BLOCKED)
        self.assertEqual((a.value.state.value, a.value.payload), ("DERIVED", "no"))
        self.assertIn("direct-contact", self.rule_step(a.proof)["beats"])
        self.assertEqual(proofcheck.Checker(self.domain, self.BLOCKED).check(a.proof), [])

    def test_leaving_a_beaten_law_off_the_list_is_caught(self):
        proof = copy.deepcopy(self.run_(self.BLOCKED).proof)
        self.rule_step(proof)["beats"] = []
        problems = proofcheck.Checker(self.domain, self.BLOCKED).check(proof)
        self.assertTrue(any("direct-contact also applies" in p for p in problems), problems)

    def test_a_proof_that_ignores_a_blocking_law_is_caught(self):
        # "they touch, so they are in contact" is sound without the wrap; with it, L6b opposes
        proof = copy.deepcopy(self.run_(self.OPEN).proof)
        self.assertEqual(proofcheck.Checker(self.domain, self.OPEN).check(proof), [])
        problems = proofcheck.Checker(self.domain, self.BLOCKED).check(proof)
        self.assertTrue(any("L6b also applies" in p for p in problems), problems)


class TheCheckerIsIndependent(unittest.TestCase):
    def test_it_imports_nothing_from_the_engine(self):
        with open(os.path.join(ROOT, "proofcheck.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(re.search(r"^\s*(from|import)\s+mshmoe", source, re.M))
