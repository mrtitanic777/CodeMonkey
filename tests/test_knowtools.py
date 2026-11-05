"""The knowledge tools (webui/knowtools.py) and their MCP server (webui/knowmcp.py)."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from webui.codeknow import CodeKnowledge  # noqa: E402
from webui.knowtools import agent_tools  # noqa: E402

PACKS = os.path.join(ROOT, "agent-workspace", "codekb")


class _App:
    def __init__(self, workspace):
        self.workspace = workspace
        self.settings = {}


class Tools(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.tools = {t.name: t for t in agent_tools(CodeKnowledge(_App(self.home)), lambda: self.proj)}

    def tearDown(self):
        shutil.rmtree(self.proj, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_the_set(self):
        self.assertEqual(sorted(self.tools), ["code_build", "code_check", "code_compile", "code_doc", "code_docs", "code_example", "code_lookup", "code_members", "code_read",
                                              "code_search", "code_target", "kernel_ask", "kernel_guide", "kernel_map"])

    def test_kernel_answers_carry_their_state_and_proof(self):
        out = self.tools["kernel_ask"].func(input="let a = qelt pell 720 dg\nask glim a\nask value dornite conductance")
        self.assertIn("glim(a): green [DERIVED] proven", out)
        self.assertIn("[MISSING_KNOWLEDGE]", out)
        nour = self.tools["kernel_ask"].func(input="let a = orrin tarn 3.5 Kvx\nask keen a", domain="nour")
        self.assertIn("keen(a): tenor [DERIVED] proven", nour)

    def test_with_the_question_given_an_unfaithful_input_is_not_run(self):
        q = "A block of qelt holds a pell of 7 grand derg. What color does it glim?"
        self.assertIn("green [DERIVED]", self.tools["kernel_ask"].func(input="let q = qelt pell 7 Gdg\nask glim q", question=q))
        out = self.tools["kernel_ask"].func(input="let q = qelt pell 9 Gdg\nask glim q", question=q)
        self.assertTrue(out.startswith("Not run"), out)
        self.assertIn("the number 9 is not in the question", out)

    def test_the_map_and_the_guide(self):
        self.assertIn("harmed → cracked", self.tools["kernel_map"].func())
        guide = self.tools["kernel_guide"].func(domain="nour")
        self.assertIn("Handbook of Nour Chimes", guide)
        self.assertIn("input language", guide)

    def test_nothing_outside_the_project_is_read(self):
        self.assertIn("outside the project", self.tools["code_check"].func(path="../secret.cpp"))

    def test_the_target_is_read_not_asked(self):
        with open(os.path.join(self.proj, "game.vcxproj"), "w") as fh:
            fh.write("<Platform>Xbox 360</Platform>")
        out = self.tools["code_target"].func()
        self.assertIn("Xbox 360", out)
        self.assertIn("big-endian", out)

    def test_without_packs_it_says_so(self):
        self.assertIn("no packs", self.tools["code_check"].func(code="int main() { return f(); }"))


@unittest.skipUnless(os.path.exists(os.path.join(PACKS, "xdk.db")), "the app has not built the Xbox 360 pack")
class ToolsOnTheRealPacks(unittest.TestCase):
    def test_check_and_members(self):
        proj = tempfile.mkdtemp()
        try:
            with open(os.path.join(proj, "game.vcxproj"), "w") as fh:
                fh.write("<Platform>Xbox 360</Platform>")
            tools = {t.name: t for t in agent_tools(CodeKnowledge(_App(os.path.dirname(PACKS))), lambda: proj)}
            out = tools["code_check"].func(code="#include <xtl.h>\nvoid F(D3DDevice* d) { d->SetRenderStatee(0, 0); }\n")
            self.assertIn("did you mean SetRenderState", out)
            self.assertIn("SetRenderState", tools["code_members"].func(name="D3DDevice", platform="xbox360"))
            self.assertIn("XALLOC_MEMPROTECT_WRITECOMBINE", tools["code_search"].func(text="XALLOC_MEMPROTECT", platform="xbox360"))
            self.assertIn("XMemAlloc(", tools["code_read"].func(name="XMemAlloc", platform="xbox360"))
        finally:
            shutil.rmtree(proj, ignore_errors=True)


class McpServer(unittest.TestCase):
    def talk(self, *messages):
        from webui.knowmcp import serve
        out = io.StringIO()
        home = tempfile.mkdtemp()
        try:
            serve(tempfile.gettempdir(), os.path.join(home, "codekb"),
                  io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n"), out)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_the_protocol(self):
        replies = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kernel_ask", "arguments": {"input": "ask class morv"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "nope"})
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])   # the notification gets no reply
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "codemonkey-knowledge")
        listed = {t["name"]: t for t in replies[1]["result"]["tools"]}
        self.assertEqual(len(listed), 14)   # the 14 knowledge tools
        writers = sorted(n for n, t in listed.items() if not t["annotations"]["readOnlyHint"])
        self.assertEqual(writers, ["code_build"])
        self.assertIn("class(morv): stonic [KNOWN] proven", replies[2]["result"]["content"][0]["text"])
        self.assertIn("error", replies[3])
        self.assertEqual(replies[4]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
