"""The language knowledge base (codekb/) inside CodeMonkey.

One per app. The packs are built from the SDK headers and compilers already on this computer, in the
background when the app starts (a quick check when nothing changed), into the workspace's codekb
folder. Nothing asks which language or platform: the target is read from the project, the code and
the request (codekb/target.py), and what cannot be placed is a finding, not a refusal.

Three uses:
    notes()        before a C/C++ edit is written: what the knowledge base could not confirm
    compiled()     after it is written: the target's own compiler on the file (syntax only)
    lookup tool    the agent asks before writing: where a name is declared, from which version
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from codekb import check as kbcheck  # noqa: E402
from codekb.kb import KnowledgeBase  # noqa: E402
from codekb.platforms import PLATFORMS  # noqa: E402
from codekb.target import detect, language_of  # noqa: E402

C_FILES = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".inl", ".asm", ".s", ".nasm")
MAX_NOTES = 8
COMPILE_SECONDS = 30


class CodeKnowledge:
    def __init__(self, app: Any):
        self.app = app
        self.kb = KnowledgeBase(os.path.join(app.workspace, "codekb"))
        self.state = "idle"        # idle, building, ready, failed
        self.problem = ""
        self.built_at = 0.0
        self._thread: Optional[threading.Thread] = None

    def enabled(self) -> bool:
        return bool(self.app.settings.get("code_knowledge", True))

    # ----------------------------------------------------------- building ---

    def start(self) -> None:
        """Build or refresh the packs in the background (seconds when nothing changed)."""
        if not self.enabled() or (self._thread and self._thread.is_alive()):
            return

        def work() -> None:
            self.state = "building"
            try:
                self.kb.build_all()
                self.state, self.built_at = "ready", time.time()
                try:   # the SDKs' help files, unpacked and indexed once (minutes the first time)
                    from codekb.docs import Docs
                    Docs(self.kb.home).build_all()
                except Exception as exc:  # noqa: BLE001 - the headers work without the docs
                    self.problem = f"documentation: {str(exc)[:200]}"
            except Exception as exc:  # noqa: BLE001 - a missing SDK folder must not stop the app
                self.state, self.problem = "failed", str(exc)[:300]

        self._thread = threading.Thread(target=work, name="codekb-build", daemon=True)
        self._thread.start()

    def status(self) -> Dict[str, Any]:
        return {"state": self.state, "problem": self.problem, "home": self.kb.home, "packs": self.kb.status()}

    # ---------------------------------------------------------- the gate ---

    @staticmethod
    def wants(rel: str) -> bool:
        return rel.lower().endswith(C_FILES)

    def findings(self, project_dir: str, rel: str, code: str, words: str = "") -> List[Dict]:
        target = detect(project_dir, code, rel, words)
        return kbcheck.check(self.kb, project_dir, rel, code, target)

    def notes(self, project_dir: str, rel: str, new: str, old: Optional[str], words: str = "") -> List[str]:
        """Warnings for an edit's result: what changed code uses that the packs could not confirm.
        Lines the old text already had are left alone (they were there before this change)."""
        if not self.enabled() or not self.wants(rel):
            return []
        target = detect(project_dir, new, rel, words)
        found = kbcheck.check(self.kb, project_dir, rel, new, target)
        if found and found[0].get("kind") == "setup":
            return []   # the packs are still being built: say nothing rather than everything
        before = set()
        if old:
            before = {(f["kind"], f["name"], f["state"]) for f in kbcheck.check(self.kb, project_dir, rel, old, target)}
        out = []
        for f in found:
            if f["state"] == "KNOWN" or (f["kind"], f["name"], f["state"]) in before:
                continue
            if f["state"] == "UNKNOWN_REFERENCE" and f["kind"] == "call":
                continue   # a name no pack has is usually the project's own or a macro: too noisy to report
            out.append(f"line {f['line']}: [{f['state']}] {f['message']}")
        if not out:
            return []
        where = describe_target({**target, "code": new})
        head = f"[Code knowledge] Checked against {where}. These are findings, not refusals; the file was written:"
        return [head] + out[:MAX_NOTES] + ([f"(and {len(out) - MAX_NOTES} more)"] if len(out) > MAX_NOTES else [])

    def grounded(self) -> bool:
        return self.enabled() and bool(self.app.settings.get("code_grounded", True))

    def _missing(self, project_dir: str, rel: str, code: str, target: Dict) -> Dict[str, tuple]:
        """name -> (line, why) for every name the code uses that the SDKs and the project do not have."""
        if target.get("language") == "asm":
            found = kbcheck.check(self.kb, project_dir, rel, code, target)
            return {f["name"]: (f["line"], f["message"]) for f in found
                    if f["kind"] == "instruction" and f["state"] != "KNOWN"}
        missing = kbcheck.missing_names(target, project_dir, rel, code)
        if missing is not None:   # the target's own compiler, on the SDK's and the project's headers
            out: Dict[str, tuple] = {}
            for line, name, message in missing:
                out.setdefault(name, (line, message))
            return out
        # no compiler for this target here: the packs and the project's own declarations decide
        found = kbcheck.check(self.kb, project_dir, rel, code, target)
        if found and found[0].get("kind") == "setup":
            return {}
        return {f["name"]: (f["line"], f["message"]) for f in found
                if f["kind"] in ("include", "call", "std", "member") and f["state"] != "KNOWN"}

    def refusals(self, project_dir: str, rel: str, new: str, old: Optional[str], words: str = "") -> List[str]:
        """Why a change may not be written: it uses a name (function, method, type, constant, header,
        instruction) that neither the target's SDK nor the project has. Names the file already used
        before this change are not counted again, so a file can be mended one change at a time; a
        change may never add one."""
        if not self.grounded() or not self.wants(rel):
            return []
        target = detect(project_dir, new, rel, words)
        if target.get("language") not in ("c", "c++", "asm"):
            return []
        after = self._missing(project_dir, rel, new, target)
        if not after:
            return []
        before = self._missing(project_dir, rel, old, target) if old else {}
        added = {n: v for n, v in after.items() if n not in before}
        if not added:
            return []
        where = describe_target({**target, "code": new})
        out = [f"line {ln}: '{name}' does not exist for {where}: it is in neither the SDK nor this project "
               f"({why[:160]})" for name, (ln, why) in sorted(added.items(), key=lambda kv: kv[1][0])][:20]
        out.append("Only names the SDK or this project declares may be written. Find the real one with code_search, "
                   "code_lookup or code_example; a function of your own must be declared (in this file or a "
                   "header it includes) before it is used.")
        return out

    def compiled(self, project_dir: str, rel: str, words: str = "") -> str:
        """After a C/C++ file is written: the target's compiler, syntax only. '' when there is none."""
        if not self.enabled() or not self.wants(rel) or not self.app.settings.get("code_compile_check", True):
            return ""
        full = os.path.join(project_dir, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                code = fh.read()
        except OSError:
            return ""
        target = detect(project_dir, code, rel, words)
        result = kbcheck.compile_check(target, project_dir, rel, code, timeout=COMPILE_SECONDS)
        if not result.get("ran"):
            return ""
        if not result["errors"]:
            return f"\n[Compiler] {result['compiler']} found no errors in {rel} (syntax and declarations; nothing was built)."
        lines = "\n".join(f"  line {ln}: {msg}" for ln, msg in result["errors"][:12])
        return f"\n[Compiler] {result['compiler']} on {rel}:\n{lines}\nFix these; the compiler is the final word."

    # ------------------------------------------------------------- lookup ---

    def lookup(self, name: str, platform: str = "", language: str = "", version: str = "") -> str:
        name = (name or "").strip()
        if not name:
            return "ERROR: give a name (a function, type, macro or header such as <vector>)."
        platform = platform.strip().lower() or None
        if platform and platform not in PLATFORMS:
            return f"ERROR: unknown platform {platform!r}; known: {', '.join(PLATFORMS)}"
        packs = self.kb.packs_for(platform)
        if not packs:
            return ("The knowledge base has no packs ready yet "
                    f"({self.state}{': ' + self.problem if self.problem else ''}). Nothing could be looked up.")
        lang = (language or "c++").lower().replace("cpp", "c++")
        if name.startswith("<") or name.endswith((".h", ".hpp")) or "/" in name:
            header = name.strip("<>\"")
            out = []
            for p in packs:
                f = p.has_header(header)
                if f:
                    names = p.declares(f, 60)
                    out.append(f"{p.name}: {f} declares {len(names)}{'+' if len(names) >= 60 else ''} names: "
                               + ", ".join(names[:60]))
            return "\n".join(out) or f"<{header}> is in none of: {', '.join(p.name for p in packs)}."
        parts = name.split("::")
        bare, owner = parts[-1], (parts[-2] if len(parts) > 1 and parts[-2] not in ("", "std") else None)
        if owner:   # Class::member
            rows = [r for p in packs for r in p.member(owner, bare)]
        else:       # a namespace-level name first, then members of any class
            rows = [r for p in packs for r in p.lookup(bare)] + [r for p in packs for r in p.lookup(bare, members=True)]
        if not rows:
            near = []
            for p in packs:
                pool = [n for n in p.members_of(owner) if n != bare] if owner else p.near(bare)
                if owner:
                    from codekb.store import _edit_distance
                    pool = [n for n in pool if _edit_distance(bare, n, 2) <= 2]
                near += [n for n in pool if n not in near]
            hint = f" Close names: {', '.join(near[:5])}." if near else ""
            return (f"'{name}' is not declared in {', '.join(p.name for p in packs)}: it does not exist for this target, "
                    f"so it may not be written (unless this project declares it).{hint}")
        lines = []
        for r in rows[:12]:
            pack = next(p for p in packs if p.name == r["pack"])
            since = pack.first_version(r, lang)
            ok = ""
            if version:
                from codekb.headers import CPP_VERSIONS, C_VERSIONS
                vs = CPP_VERSIONS if lang == "c++" else C_VERSIONS
                if version in vs:
                    bit = 1 << vs.index(version)
                    ok = "  [visible in " + version + "]" if (r["cpp_mask"] if lang == "c++" else r["c_mask"]) & bit \
                        else "  [NOT visible in " + version + "]"
            what = f"{r['kind']} of {r['owner']}" if r.get("owner") else r["kind"]
            lines.append(f"{r['pack']} {r['header']}:{r['line']}  {what}  {(r['signature'] or r['name'])[:160]}"
                         f"  (from {since or '?'}){ok}")
        more = f"\n(and {len(rows) - 12} more)" if len(rows) > 12 else ""
        return "\n".join(lines) + more


def describe_target(target: Dict) -> str:
    bits = []
    if target.get("platform"):
        bits.append(PLATFORMS[target["platform"]]["name"])
    lang = target.get("language")
    if lang == "asm":
        from codekb.asm import dialect
        d = dialect(target.get("code", ""), target)
        bits.append({"xenon": "PowerPC assembly (the SDK's assembler)", "nasm": "x86 assembly (NASM)",
                     "masm": "x86 assembly (MASM)", "gas": "x86 assembly (GNU as)"}[d])
        return " ".join(bits)
    ver = target.get("cpp") if lang == "c++" else target.get("c")
    if lang:
        bits.append(ver or lang)
    return " ".join(bits) or "every pack (no target found)"


def tool(knowledge: CodeKnowledge):
    from coder.tools import Tool, _str_prop

    return Tool(
        name="code_lookup",
        description=("Look up C/C++ names in the local knowledge base built from this computer's SDK headers and "
                     "compilers (Xbox 360 XDK, Windows SDK, MSVC, DirectX, GCC). Give a function, type or macro "
                     "name to see where it is declared and from which language version, or a header such as "
                     "<xtl.h> to see what it declares. Use it before writing a call you are not sure of."),
        parameters={"type": "object", "properties": {
            "name": _str_prop("Name or header, e.g. XMemAlloc, std::make_unique, <vector>."),
            "platform": _str_prop("Optional: " + ", ".join(PLATFORMS) + "."),
            "language": _str_prop("Optional: c or c++ (default c++)."),
            "version": _str_prop("Optional: a version to check against, e.g. c++03, c89, c++17."),
        }, "required": ["name"]},
        func=lambda name, platform="", language="", version="": knowledge.lookup(name, platform, language, version),
    )


__all__ = ["CodeKnowledge", "tool", "describe_target", "language_of"]
