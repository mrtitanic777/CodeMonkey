"""Runtime translation guards. No model needed."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bridge.guards import Guard  # noqa: E402
from bridge.translate import PLACEHOLDERS  # noqa: E402
from mshmoe import load_domain  # noqa: E402

GUARD = Guard(load_domain(os.path.join(ROOT, "domain", "thren.json")), placeholders=PLACEHOLDERS)


def check(english, controlled):
    return GUARD.check(english, controlled)


class Guards(unittest.TestCase):
    def test_faithful_translation_passes(self):
        english = "A qelt block at 3 Gdg touches a brask block at 20 dg over a 1 tal path. What's the passage rate?"
        controlled = "let a = qelt pell 3 Gdg\nlet b = brask pell 20 dg\nrel touches a b span 1 t\nask rate a b"
        self.assertEqual(check(english, controlled), [])

    def test_sign_change_is_not_an_invented_number(self):
        english = "A qelt rod at 720 dg is glimming green. Its pell drops by 50 dg. What color is it now?"
        controlled = "let a = qelt pell 720 dg glim green\nshift a pell -50 dg\nask glim a"
        self.assertEqual(check(english, controlled), [])

    def test_silent_spelling_repair_is_rejected(self):
        english = "A qelt block at 750 dg is observed to glim gren. What color does it glim?"
        controlled = "let q = qelt pell 750 dg glim green\nask glim q"
        problems = check(english, controlled)
        self.assertTrue(any("'green'" in p for p in problems))
        self.assertTrue(any("'gren'" in p for p in problems))

    def test_invented_span_is_rejected(self):
        english = "A qelt block at 200 dg touches a brask block at 50 dg. What is the passage rate?"
        controlled = "let a = qelt pell 200 dg\nlet b = brask pell 50 dg\nrel touches a b span 1 t\nask rate a b"
        self.assertTrue(any("number 1" in p for p in check(english, controlled)))

    def test_dropped_unfamiliar_word_is_rejected(self):
        english = "A kivv touches a qelt block at 40 dg. Is the kivv harmed?"
        controlled = "let q = qelt pell 40 dg\nask glim q"
        self.assertTrue(any("'kivv'" in p for p in check(english, controlled)))

    def test_common_words_are_not_required(self):  # batch 2, B2-38: "nothing" is not a misspelling of "thing"
        english = ("A bit of kiv k is in line of sight of a qelt lump h that's glimming green, though k touches "
                   "nothing. Is k harmed, and which way does thren move between h and k?")
        controlled = "let k = kiv\nlet h = qelt glim green\nrel sight h k\nask harmed k\nask flows h k"
        self.assertEqual(check(english, controlled), [])

    def test_placeholder_is_rejected(self):
        english = "A qelt block at 300 dg: what color does it glim?"
        controlled = "let g = gorp pell 300 dg\nask glim g"
        self.assertTrue(any("placeholder" in p for p in check(english, controlled)))

    def test_swapped_attribute_is_rejected(self):
        english = "A qelt block sits at 650 dg. What is its temperature?"
        controlled = "let q = qelt pell 650 dg\nask value q resting_pell"
        self.assertTrue(any("resting_pell" in p for p in check(english, controlled)))

    def test_named_attribute_passes(self):
        english = "Roughly how conductive is dornite? Above what pell does contact harm kiv?"
        controlled = "estimate value dornite conductance\nask value kiv harm_threshold"
        self.assertEqual(check(english, controlled), [])

    def test_not_glimming_and_taught_words_pass(self):
        english = "Suppose a zuppan is a new kind of metalline substance. A zuppan that is not glimming: does it glim?"
        controlled = "teach zuppan isa metalline\nlet z = zuppan glim none\nask has glim z"
        self.assertEqual(check(english, controlled), [])


if __name__ == "__main__":
    unittest.main()
