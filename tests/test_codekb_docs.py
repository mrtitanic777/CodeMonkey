"""The SDK documentation index (codekb/docs.py): reading help pages, and searching the real index when
this computer has it built."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codekb.docs import Docs, read_page  # noqa: E402

PAGE = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=Windows-1252" />
<title>FooGetBar</title><xml><MSHelp:Keyword Index="K" Term="FooGetBar" /></xml><script>var x = 1;</script></head>
<body><p>See Also</p><p>Collapse All  Expand All</p><p> Gets the bar of a foo.</p>
<h2>Syntax</h2><pre>DWORD FooGetBar(
    DWORD dwFoo
)</pre><h2>Remarks</h2><p>Call it once a frame &amp; check the result.</p></body></html>"""

DOCS_HOME = os.path.join(ROOT, "agent-workspace", "codekb")


class ReadPage(unittest.TestCase):
    def test_text_sections_and_code(self):
        path = os.path.join(tempfile.mkdtemp(), "p.htm")
        with open(path, "w", encoding="cp1252") as fh:
            fh.write(PAGE)
        p = read_page(path)
        self.assertEqual(p["title"], "FooGetBar")
        self.assertEqual(p["keywords"], ["FooGetBar"])
        heads = [h for h, _ in p["sections"]]
        self.assertEqual(heads, ["", "Syntax", "Remarks"])
        text = dict(p["sections"])
        self.assertIn("Gets the bar of a foo.", text[""])
        self.assertNotIn("Collapse", text[""])                 # the viewer's own words are dropped
        self.assertNotIn("var x", text[""])                     # scripts are not text
        self.assertIn("DWORD FooGetBar(\n    DWORD dwFoo\n)", text["Syntax"])   # code keeps its lines
        self.assertEqual(text["Remarks"], "Call it once a frame & check the result.")


@unittest.skipUnless(Docs(DOCS_HOME).ready("xdk"), "the Xbox 360 SDK documentation is not indexed here")
class RealIndex(unittest.TestCase):
    def test_a_name_finds_its_own_page_first(self):
        hits = Docs(DOCS_HOME).search("XInputGetState", "xbox360")
        self.assertEqual(hits[0]["title"], "XInputGetState")

    def test_a_question_finds_the_how_to(self):
        hits = Docs(DOCS_HOME).search("how do I play a sound with XAudio2", "xbox360")
        self.assertIn("Play a Sound with XAudio2", hits[0]["title"])

    def test_read_a_page_and_platforms(self):
        d = Docs(DOCS_HOME)
        page = d.page("XInputGetState", "xbox360")
        self.assertIn("ERROR_DEVICE_NOT_CONNECTED", page["text"])
        self.assertIsNone(d.page("XInputGetState", "posix"))     # no docs for that platform
        self.assertEqual(d.search("what is the zzqqxx frobnicator"), [])


if __name__ == "__main__":
    unittest.main()
