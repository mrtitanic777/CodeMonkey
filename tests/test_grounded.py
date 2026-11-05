"""Only names that exist: a C/C++/assembly change that uses a name neither the SDK nor the project has
is refused before it is written (webui/codeknow.py refusals, called by the code gate)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from codekb import check as kbcheck  # noqa: E402
from webui.codeknow import CodeKnowledge  # noqa: E402

PACKS = os.path.join(ROOT, "agent-workspace", "codekb")
XEDK = os.environ.get("XEDK", "")
HAVE_360 = os.path.exists(os.path.join(XEDK, "bin", "win32", "cl.exe"))

GOOD = """#include <xtl.h>
void ClearIt(D3DDevice* dev)
{
    dev->Clear(0, NULL, D3DCLEAR_TARGET, D3DCOLOR_XRGB(0, 0, 255), 1.0f, 0);
}
"""


class _App:
    def __init__(self, workspace, **settings):
        self.workspace = workspace
        self.settings = settings


class _Base(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        with open(os.path.join(self.proj, "Game.vcxproj"), "w") as fh:
            fh.write("<Platform>Xbox 360</Platform>")
        self.ck = CodeKnowledge(_App(os.path.dirname(PACKS)))

    def tearDown(self):
        shutil.rmtree(self.proj, ignore_errors=True)

    def refused(self, new, old=None, rel="main.cpp"):
        return self.ck.refusals(self.proj, rel, new, old)


@unittest.skipUnless(HAVE_360, "no Xbox 360 compiler here")
class WithTheTargetsCompiler(_Base):
    def test_real_code_is_written(self):
        self.assertEqual(self.refused(GOOD), [])

    def test_an_invented_method_type_or_header_is_refused(self):
        for bad, name in (("dev->SetBackBufferPointer(0);", "SetBackBufferPointer"),
                          ("D3D_FULL_PRESENT_PARAMETERS fpp;", "D3D_FULL_PRESENT_PARAMETERS"),
                          ("XMadeUpCall(1);", "XMadeUpCall")):
            out = self.refused(GOOD.replace("    dev->Clear", "    " + bad + "\n    dev->Clear"))
            self.assertTrue(any(f"'{name}' does not exist" in p for p in out), (name, out))
        out = self.refused("#include <nothere.h>\n" + GOOD)
        self.assertTrue(any("'nothere.h' does not exist" in p for p in out), out)

    def test_a_broken_file_can_be_mended_a_change_at_a_time_but_never_made_worse(self):
        broken = GOOD.replace("    dev->Clear", "    dev->FakeOne();\n    dev->FakeTwo();\n    dev->Clear")
        mended = broken.replace("    dev->FakeOne();\n", "")
        self.assertEqual(self.refused(mended, old=broken), [])          # one fewer: allowed
        worse = mended.replace("    dev->FakeTwo();", "    dev->FakeTwo();\n    dev->FakeThree();")
        out = self.refused(worse, old=mended)
        self.assertTrue(any("'FakeThree'" in p for p in out))
        self.assertFalse(any("'FakeTwo'" in p for p in out))            # already there: not counted again

    def test_the_projects_own_functions_count_once_declared(self):
        with open(os.path.join(self.proj, "helpers.h"), "w") as fh:
            fh.write("void MyHelper(int v);\n")
        uses = '#include "helpers.h"\n' + GOOD.replace("    dev->Clear", "    MyHelper(2);\n    dev->Clear")
        self.assertEqual(self.refused(uses), [])
        self.assertTrue(self.refused(GOOD.replace("    dev->Clear", "    NotDeclared(2);\n    dev->Clear")))

    def test_switched_off_nothing_is_refused(self):
        ck = CodeKnowledge(_App(os.path.dirname(PACKS), code_grounded=False))
        self.assertEqual(ck.refusals(self.proj, "main.cpp", "void f() { Nope(); }", None), [])


@unittest.skipUnless(os.path.exists(os.path.join(PACKS, "xdk.db")), "the app has not built the Xbox 360 pack")
class WithoutACompiler(_Base):
    """Where no compiler for the target is installed, the packs and the project decide, as strictly."""

    def test_the_packs_decide(self):
        with mock.patch.object(kbcheck, "missing_names", return_value=None):
            self.assertEqual(self.refused(GOOD), [])
            out = self.refused(GOOD.replace("    dev->Clear", "    dev->SetRenderStatee(0, 0);\n    dev->Clear"))
            self.assertTrue(any("'SetRenderStatee' does not exist" in p for p in out), out)


if __name__ == "__main__":
    unittest.main()
