"""The language knowledge base (codekb/): headers read into packs, code checked against them.

Built from a small header folder written here, so these run on any computer. The last class uses
the real Xbox 360 SDK and is skipped where it is not installed.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codekb import check as kbcheck  # noqa: E402
from codekb.headers import condition_masks, scan  # noqa: E402
from codekb.store import Pack, build  # noqa: E402
from codekb.target import detect, language_of  # noqa: E402

DEMO_H = """#pragma once
#ifdef __cplusplus
extern "C" {
#endif
int DemoOpen(const char* name);
void DemoClose(int handle);
#define DEMO_MAX 0xbff7ff3fU
#ifdef __cplusplus
}
#endif
typedef struct _DEMO_INFO { int size; } DEMO_INFO;
"""

DEMO_STD = """#pragma once
namespace std {
#if __cplusplus >= 201703L
  template<class T>
    T demo_clamp(T v, T lo, T hi);
#endif
  template<class T> class demo_vec {
  public:
    demo_vec();
  };
  template<class T>
    inline
    demo_vec<T>::~demo_vec() { }
}
"""


class FakeKB:
    """What check() and verify() need from a KnowledgeBase: one pack, its folder."""

    def __init__(self, home, root):
        self.root = root
        self.db = os.path.join(home, "demo.db")
        self.sources = {"demo": {"roots": [root]}}
        self.closed = 0
        self._pack = None
        self.rebuild()

    def rebuild(self):
        def close():
            if self._pack is not None:
                self._pack.close()
                self._pack = None
                self.closed += 1
        result = build(self.db, "demo", [self.root], before_swap=close)
        if self._pack is None:
            self._pack = Pack(self.db)
        return result

    def packs_for(self, platform):
        return [self._pack]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inc = os.path.join(self.tmp, "include")
        os.makedirs(self.inc)
        self.write("demo.h", DEMO_H)
        self.write("demostd.h", DEMO_STD)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.kb = FakeKB(self.tmp, self.inc)

    def tearDown(self):
        if self.kb._pack is not None:
            self.kb._pack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.inc, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def findings(self, code, cpp="c++17", path="main.cpp"):
        target = {"platform": None, "language": language_of(path, code), "cpp": cpp, "c": "c99", "evidence": []}
        return kbcheck.check(self.kb, self.proj, path, code, target)

    def one(self, code, name, **kw):
        hits = [f for f in self.findings(code, **kw) if f["name"] == name]
        self.assertTrue(hits, f"no finding for {name}")
        return hits[0]


class Reading(_Base):
    def test_declarations_found_at_the_line_their_name_is_on(self):
        rows = {r["name"]: r for r in self.kb._pack.lookup("DemoOpen") + self.kb._pack.lookup("~demo_vec")
                + self.kb._pack.lookup("demo_clamp")}
        self.assertEqual(rows["DemoOpen"]["line"], 5)
        self.assertEqual(rows["~demo_vec"]["line"], 13)      # a destructor is a name too
        self.assertEqual(rows["demo_clamp"]["line"], 5)      # the name's line, not the template<> line

    def test_a_number_is_not_a_name(self):
        self.assertEqual(self.kb._pack.lookup("xbff7ff3fU"), [])
        self.assertEqual(self.kb._pack.lookup("bff7ff3fU"), [])

    def test_versions_come_from_the_conditions_around_a_declaration(self):
        row = self.kb._pack.lookup("demo_clamp")[0]
        self.assertEqual(self.kb._pack.first_version(row, "c++"), "c++17")
        masks = condition_masks("__cplusplus >= 201103L")
        self.assertIsNotNone(masks)

    def test_scan_reads_extern_c_blocks_and_typedefs(self):
        names = {s[0] for s in scan(DEMO_H)}
        self.assertTrue({"DemoOpen", "DemoClose", "DEMO_INFO", "DEMO_MAX"} <= names, names)

    def test_rebuild_only_when_a_header_changed_and_while_the_pack_is_open(self):
        self.assertFalse(self.kb.rebuild()["rebuilt"])
        time.sleep(0.05)
        self.write("demo.h", DEMO_H + "int DemoFlush(int handle);\n")
        self.assertTrue(self.kb.rebuild()["rebuilt"])
        self.assertEqual(self.kb.closed, 1)              # the open pack was closed before the swap
        self.assertTrue(self.kb._pack.lookup("DemoFlush"))


class Checking(_Base):
    CODE = '#include "demo.h"\n#include <demostd.h>\nint main() {\n  int h = DemoOpen("x");\n  DemoClose(h);\n  return 0;\n}\n'

    def test_known_calls_carry_a_proof_that_checks_against_the_header(self):
        f = self.one(self.CODE, "DemoOpen")
        self.assertEqual(f["state"], "KNOWN")
        self.assertIsNone(kbcheck.verify(self.kb, f))
        f["proof"][0]["line"] += 1
        self.assertIn("is not on line", kbcheck.verify(self.kb, f))

    def test_a_misspelt_call_is_low_confidence_with_the_close_name(self):
        f = self.one(self.CODE.replace("DemoClose(h)", "DemoClsoe(h)"), "DemoClsoe")
        self.assertEqual(f["state"], "LOW_CONFIDENCE")
        self.assertIn("DemoClose", f["near"])

    def test_a_name_from_a_later_version_is_conflicted(self):
        code = "#include <demostd.h>\nint f() { return std::demo_clamp(1, 0, 2); }\n"
        self.assertEqual(self.one(code, "demo_clamp", cpp="c++11")["state"], "CONFLICTED")
        self.assertEqual(self.one(code, "demo_clamp", cpp="c++17")["state"], "KNOWN")

    def test_a_feature_newer_than_the_target_is_found_on_its_line(self):
        code = "int f(int* v, int n) {\n  int s = 0;\n  for (int x : v) s += x;\n  return s;\n}\n"
        hits = [f for f in self.findings(code, cpp="c++03") if f["kind"] == "feature"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["line"], 3)
        self.assertEqual(hits[0]["state"], "CONFLICTED")

    def test_the_projects_own_names_are_left_alone(self):
        with open(os.path.join(self.proj, "util.h"), "w") as fh:
            fh.write("int ProjectHelper(int v);\n")
        code = '#include "util.h"\nint main() { return ProjectHelper(2); }\n'
        self.assertFalse([f for f in self.findings(code) if f["name"] == "ProjectHelper"])

    def test_an_unknown_header_is_unverified_not_refused(self):
        f = self.one("#include <nowhere.h>\n", "nowhere.h")
        self.assertEqual(f["state"], "UNKNOWN_REFERENCE")


class Targets(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.proj, ignore_errors=True)

    def test_a_vcxproj_names_the_platform(self):
        with open(os.path.join(self.proj, "game.vcxproj"), "w") as fh:
            fh.write('<ProjectConfiguration Include="Release|Xbox 360"><Platform>Xbox 360</Platform></ProjectConfiguration>')
        t = detect(self.proj, "", "game.cpp")
        self.assertEqual(t["platform"], "xbox360")
        self.assertEqual(t["language"], "c++")
        self.assertTrue(t["evidence"])

    def test_cmake_and_request_words(self):
        with open(os.path.join(self.proj, "CMakeLists.txt"), "w") as fh:
            fh.write("set(CMAKE_CXX_STANDARD 14)\n")
        self.assertEqual(detect(self.proj, "", "a.cpp")["cpp"], "c++14")
        self.assertEqual(detect(self.proj, "", "a.c", "write it in C89")["c"], "c89")

    def test_nothing_to_go_on_is_unknown_not_a_question(self):
        t = detect(self.proj, "int main(){}", "a.cpp")
        self.assertIsNone(t["platform"])
        self.assertEqual(t["language"], "c++")

    def test_a_header_with_classes_is_cpp(self):
        self.assertEqual(language_of("x.h", "class A {};"), "c++")
        self.assertEqual(language_of("x.h", "int f(void);"), "c")


class GateNotes(_Base):
    """webui/codeknow.py turns findings into notes on an edit: new problems only, never KNOWN."""

    def notes(self, new, old=None):
        from webui.codeknow import CodeKnowledge

        class App:
            workspace = self.tmp
            settings = {}
        ck = CodeKnowledge(App())
        ck.kb = self.kb
        return ck.notes(self.proj, "main.cpp", new, old, "in C++17")

    def test_only_what_the_change_brought_in(self):
        bad = Checking.CODE.replace("DemoClose(h)", "DemoClsoe(h)")
        self.assertEqual(self.notes(Checking.CODE), [])
        notes = self.notes(bad)
        self.assertIn("LOW_CONFIDENCE", notes[1])
        self.assertIn("not refusals", notes[0])
        self.assertEqual(self.notes(bad + "\n", old=bad), [])   # already there before this edit

    def test_lookup_tool(self):
        from webui.codeknow import CodeKnowledge

        class App:
            workspace = self.tmp
            settings = {}
        ck = CodeKnowledge(App())
        ck.kb = self.kb
        self.assertIn("demo.h:5", ck.lookup("DemoOpen"))
        self.assertIn("NOT visible in c++11", ck.lookup("std::demo_clamp", version="c++11"))
        self.assertIn("DemoClose", ck.lookup("DemoClsoe"))
        self.assertIn("DemoOpen", ck.lookup("<demo.h>"))


DEMO_DEV = """#pragma once
DECLARE_INTERFACE_(IDemoDevice, IUnknown)
{
    STDMETHOD(QueryInterface)(THIS_ REFIID riid, void** ppv) PURE;
    STDMETHOD_(ULONG, AddRef)(THIS) PURE;
    STDMETHOD(SetMode)(THIS_ DWORD mode) PURE;
};
class DemoBox {
public:
    DemoBox();
    ~DemoBox();
    int Count() const { return n; }
#if __cplusplus >= 201103L
    void Shrink();
#endif
    typedef int size_type;
private:
    int n, flags : 3;
};
"""


class Members(_Base):
    def setUp(self):
        super().setUp()
        self.write("demodev.h", DEMO_DEV)
        self.kb.rebuild()

    def test_members_are_indexed_with_their_class_and_line(self):
        p = self.kb._pack
        got = {(r["name"], r["kind"], r["owner"], r["line"]) for n in ("SetMode", "AddRef", "Count", "DemoBox", "~DemoBox", "n", "flags", "size_type")
               for r in p.lookup(n, members=True)}
        self.assertTrue({("SetMode", "method", "IDemoDevice", 6), ("AddRef", "method", "IDemoDevice", 5),
                         ("Count", "method", "DemoBox", 12), ("DemoBox", "method", "DemoBox", 10),
                         ("~DemoBox", "method", "DemoBox", 11), ("n", "field", "DemoBox", 18),
                         ("flags", "field", "DemoBox", 18), ("size_type", "member_type", "DemoBox", 16)} <= got, got)
        self.assertEqual(p.lookup("SetMode"), [])            # not a namespace-level name
        self.assertEqual(p.first_version(p.lookup("Shrink", members=True)[0], "c++"), "c++11")
        self.assertIn("Count", p.members_of("DemoBox"))

    def test_member_calls_are_checked(self):
        code = '#include "demodev.h"\nvoid f(IDemoDevice* d, DemoBox& b) {\n  d->SetMdoe(1);\n  b.Shrink();\n  b.Count();\n}\n'
        f = self.one(code, "SetMdoe")
        self.assertEqual(f["state"], "LOW_CONFIDENCE")
        self.assertIn("SetMode", f["near"])
        self.assertEqual(self.one(code, "Shrink", cpp="c++03")["state"], "CONFLICTED")
        self.assertEqual(self.one(code, "Shrink", cpp="c++11")["state"], "KNOWN")
        known = self.one(code, "Count")
        self.assertIsNone(kbcheck.verify(self.kb, known))

    def test_a_member_through_its_class(self):
        f = self.one('#include "demodev.h"\nint g() { return DemoBox::Cuont(); }\n', "DemoBox::Cuont")
        self.assertEqual(f["state"], "LOW_CONFIDENCE")
        self.assertIn("Count", f["near"])

    def test_a_platform_library_can_offer_more_than_its_language(self):
        code = '#include "demodev.h"\nvoid f(DemoBox& b) { b.Shrink(); }\n'
        target = {"platform": None, "language": "c++", "cpp": "c++03", "library_cpp": "c++11", "evidence": []}
        hits = [f for f in kbcheck.check(self.kb, self.proj, "a.cpp", code, target) if f["name"] == "Shrink"]
        self.assertEqual(hits[0]["state"], "KNOWN")

    def test_a_variable_made_with_arguments_is_not_a_call(self):
        code = "void f() { DemoBox box(3); int total(0); }\n"
        names = {f["name"] for f in self.findings(code)}
        self.assertNotIn("box", names)
        self.assertNotIn("total", names)


class AssemblyReading(unittest.TestCase):
    def test_dialect(self):
        from codekb.asm import dialect
        self.assertEqual(dialect("bits 64\nsection .text\n", {}), "nasm")
        self.assertEqual(dialect(".globl f\nf: movl %eax, %ebx\n", {}), "gas")
        self.assertEqual(dialect("_TEXT SEGMENT\nf PROC\n mov eax, 1\nf ENDP\n", {}), "masm")
        self.assertEqual(dialect("_TEXT SEGMENT\nf PROC\n mflr r11\n blr\nf ENDP\n", {}), "xenon")
        self.assertEqual(dialect("anything", {"platform": "xbox360"}), "xenon")

    def test_only_instruction_lines_are_read(self):
        from codekb.asm import mnemonics
        code = ("bits 64\nsection .text\nglobal f\n%macro SAVE 0\n push rbx\n%endmacro\nf:  mov rax, 1 ; c\n"
                "    SAVE\nloop1: dec rcx\nvalue dq 0\nbuf resb 8\nCOUNT equ 4\n")
        self.assertEqual(mnemonics(code, "nasm"), [(7, "mov"), (9, "dec")])
        code = "_TEXT SEGMENT PARA 'CODE'\nf PROC\n  mflr r11\n  stwcx. r3, 0, r4\n  blr\nf ENDP\n_TEXT ENDS\n  END\n"
        self.assertEqual(mnemonics(code, "xenon"), [(3, "mflr"), (4, "stwcx."), (5, "blr")])


def _assembler(kind):
    from codekb.asm import assemblers
    return next((a for a in assemblers() if a["assembler"] == kind), None)


class _AsmPack(unittest.TestCase):
    KIND = ""
    NAME = ""

    @classmethod
    def setUpClass(cls):
        from codekb.kb import KnowledgeBase
        # the app's own packs when they are there and current (building is what takes the time)
        shared = os.path.join(ROOT, "agent-workspace", "codekb")
        cls.home = shared if os.path.isdir(shared) else tempfile.mkdtemp()
        cls.kb = KnowledgeBase(cls.home)
        cls.kb.build(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        cls.kb._close(cls.NAME)
        if not cls.home.endswith(os.path.join("agent-workspace", "codekb")):
            shutil.rmtree(cls.home, ignore_errors=True)

    def findings(self, code, target):
        return {f["name"]: f for f in kbcheck.check(self.kb, tempfile.gettempdir(), "a.asm", code,
                                                     {**target, "language": "asm"})}


@unittest.skipUnless(_assembler("nasm"), "NASM is not installed")
class NasmPack(_AsmPack):
    NAME = "asm-x86"

    def test_instructions_misspellings_and_64_bit_mode(self):
        # `addd` looks like a typo of `add` and is not: it is NASM's x87 add, so the pack is right
        # to know it. `addx` is the misspelling, and NASM itself is what settled both.
        code = ("bits 64\nsection .text\nf:\n    mov rax, rcx\n    addd rax, rdx\n"
                "    addx rax, rdx\n    pusha\n    ret\n")
        f = self.findings(code, {"platform": "win64"})
        self.assertEqual(f["mov"]["state"], "KNOWN")
        self.assertIsNone(kbcheck.verify(self.kb, f["mov"]))       # NASM asked again
        self.assertEqual(f["addd"]["state"], "KNOWN")
        self.assertEqual(f["addx"]["state"], "LOW_CONFIDENCE")
        self.assertIn("add", f["addx"]["near"])
        self.assertEqual(f["pusha"]["state"], "CONFLICTED")

    def test_the_assembler_has_the_last_word(self):
        code = "bits 64\nsection .text\nf:\n    mov rax, rcx\n    addd rax, rdx\n    ret\n"
        r = kbcheck.compile_check({"language": "asm", "platform": "win64"}, tempfile.gettempdir(), "a.asm", code)
        self.assertTrue(r["ran"])
        self.assertEqual([ln for ln, _ in r["errors"]], [5])


@unittest.skipUnless(_assembler("xenon"), "no Xbox 360 SDK assembler")
class PowerPCPack(_AsmPack):
    NAME = "asm-ppc"
    CODE = ("_TEXT   SEGMENT PARA 'CODE'\nTwice   PROC\n        mflr    r11\n        addii   r3, r3, 1\n"
            "        stwcx.  r3, 0, r4\n        blr\nTwice   ENDP\n_TEXT   ENDS\n        END\n")

    def test_powerpc_instructions(self):
        f = self.findings(self.CODE, {"platform": "xbox360"})
        self.assertEqual({f[w]["state"] for w in ("mflr", "stwcx.", "blr")}, {"KNOWN"})
        self.assertEqual(f["addii"]["state"], "LOW_CONFIDENCE")
        self.assertIn("addi", f["addii"]["near"])

    def test_its_errors_land_on_their_lines(self):
        r = kbcheck.compile_check({"language": "asm", "platform": "xbox360"}, tempfile.gettempdir(), "t.asm", self.CODE)
        self.assertEqual([ln for ln, _ in r["errors"]], [4])
        ok = kbcheck.compile_check({"language": "asm", "platform": "xbox360"}, tempfile.gettempdir(), "t.asm",
                                   self.CODE.replace("addii", "addi"))
        self.assertEqual(ok["errors"], [])


XEDK = os.environ.get("XEDK")


@unittest.skipUnless(XEDK and os.path.exists(os.path.join(XEDK or "", "bin", "win32", "cl.exe")), "no Xbox 360 SDK")
class Xbox360Compiler(unittest.TestCase):
    def test_the_real_compiler_agrees(self):
        proj = tempfile.mkdtemp()
        try:
            code = "#include <xtl.h>\nvoid f() { XGetVideoModee(0); }\n"
            target = {"platform": "xbox360", "language": "c++", "cpp": "c++03", "c": "c89", "evidence": []}
            result = kbcheck.compile_check(target, proj, "a.cpp", code)
            self.assertTrue(result["ran"])
            self.assertTrue(any("C3861" in m for _, m in result["errors"]), result)
        finally:
            shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
