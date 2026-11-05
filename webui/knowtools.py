"""Tools over what CodeMonkey knows for certain: the code knowledge base (codekb/) and the kernel.

One definition per tool, used two ways: as the agent's tools in a session (whatever model drives
it), and served over MCP by knowmcp.py to any other MCP client. Every tool only
reads, except code_build, which writes build outputs (and a project file where there is none) and
so asks first.

    code_lookup    where a name, class member, header or instruction is declared, from which version
    code_check     a file or a piece of code checked against the packs, in the kernel's states
    code_compile   the target's own compiler or assembler on it, syntax only
    code_build     the whole project built with the platform's own build system (an .xex for Xbox 360)
    code_target    what a project is built for (platform, CPU, compiler, language versions) and why
    code_members   what a class declares
    code_search    declared names containing a piece of text
    code_read      an SDK header's own lines, around a declaration
    code_example   how the SDK's sample programs use a name
    kernel_guide   a kernel domain's handbook and the kernel's input language
    kernel_ask     questions put to the kernel, each answer with its state and a checked proof
    kernel_map     how two kernel domains line up, and which laws one could borrow from the other
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List

from codekb import check as kbcheck
from codekb.platforms import PLATFORMS
from codekb.target import detect

MAX_FINDINGS = 40
_EXT = {"c": ".c", "c++": ".cpp", "cpp": ".cpp", "asm": ".asm", "assembly": ".asm"}


def _prop(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def _source(project_dir: str, path: str, code: str, language: str):
    """(relative path, code) from a project file or from code given directly."""
    if path:
        full = os.path.abspath(os.path.join(project_dir, path))
        if os.path.commonpath([full, os.path.abspath(project_dir)]) != os.path.abspath(project_dir):
            raise ValueError(f"{path} is outside the project")
        with open(full, encoding="utf-8", errors="replace") as fh:
            return os.path.relpath(full, project_dir).replace("\\", "/"), fh.read()
    if not code:
        raise ValueError("give a path in the project, or the code itself")
    return "snippet" + _EXT.get((language or "c++").lower(), ".cpp"), code


def _target(project_dir: str, rel: str, code: str, platform: str, words: str):
    if platform:
        key = platform.strip().lower()
        if key not in PLATFORMS:
            raise ValueError(f"unknown platform {platform!r}; known: {', '.join(PLATFORMS)}")
        words = f"{words} for {PLATFORMS[key]['name']}"
    return detect(project_dir, code, rel, words)


def _describe(target: Dict) -> str:
    from .codeknow import describe_target
    lang = target.get("language")
    picked = {k: v for k, v in (target.get("why") or {}).items()
              if k == "platform" or (k == "cpp") == (lang == "c++") and lang != "asm"}
    why = "; ".join(picked.values()) or "; ".join(target.get("evidence", [])) \
        or "nothing in the project or the request names one"
    return f"{describe_target(target)} ({why})"


def specs(knowledge, project_dir_of: Callable[[], str]) -> List[Dict[str, Any]]:
    """[{name, description, parameters, func}] over a CodeKnowledge (webui/codeknow.py)."""

    def code_check(path: str = "", code: str = "", language: str = "", platform: str = "", request: str = "") -> str:
        project = project_dir_of()
        rel, text = _source(project, path, code, language)
        target = _target(project, rel, text, platform, request)
        target["code"] = text
        found = kbcheck.check(knowledge.kb, project, rel, text, target)
        if found and found[0].get("kind") == "setup":
            return found[0]["message"]
        known = [f for f in found if f["state"] == "KNOWN"]
        other = [f for f in found if f["state"] != "KNOWN"]
        lines = [f"Checked {rel} against {_describe(target)}.",
                 f"{len(known)} names confirmed in the headers (each with the line it is declared on)."]
        if not other:
            lines.append("Nothing unconfirmed.")
        for f in other[:MAX_FINDINGS]:
            lines.append(f"line {f['line']}: [{f['state']}] {f['message']}")
        if len(other) > MAX_FINDINGS:
            lines.append(f"(and {len(other) - MAX_FINDINGS} more)")
        lines.append("States: KNOWN found; CONFLICTED exists but not for this target; LOW_CONFIDENCE not found, "
                     "a near name is; UNKNOWN_REFERENCE not found anywhere (unverified, not necessarily wrong).")
        return "\n".join(lines)

    def code_compile(path: str = "", code: str = "", language: str = "", platform: str = "", request: str = "") -> str:
        project = project_dir_of()
        rel, text = _source(project, path, code, language)
        target = _target(project, rel, text, platform, request)
        result = kbcheck.compile_check(target, project, rel, text)
        if not result.get("ran"):
            why = result.get("problem") or "no compiler or assembler for this target is installed here"
            return f"Not compiled: {why}. Target: {_describe(target)}."
        if not result["errors"]:
            return f"{result['compiler']} found no errors in {rel} (syntax and declarations only; nothing was built)."
        return f"{result['compiler']} on {rel}:\n" + "\n".join(f"line {ln}: {msg}" for ln, msg in result["errors"])

    def code_target(path: str = "", request: str = "") -> str:
        project = project_dir_of()
        text = ""
        if path:
            path, text = _source(project, path, "", "")
        t = detect(project, text, path, request)
        out = [f"Target: {_describe(t)}"]
        if t.get("platform"):
            p = PLATFORMS[t["platform"]]
            out += [f"CPU: {p['cpu']} ({p['endian']}-endian, {p['pointer_bits']}-bit pointers)",
                    f"Compiler: {p['compiler']}", f"C: {', '.join(p['c'])}; C++: {', '.join(p['cpp'])}"]
            if p.get("library_cpp"):
                out.append(f"Its library also offers {p['library_cpp']} names (beyond the language version)")
            if p.get("cpp11_has"):
                out.append("C++11 features it has: " + ", ".join(p["cpp11_has"]))
            if p.get("cpp11_lacks"):
                out.append("C++11 features it lacks: " + ", ".join(p["cpp11_lacks"]))
            out.append("Packs: " + ", ".join(p["packs"]))
        return "\n".join(out)

    def code_members(name: str, platform: str = "") -> str:
        cls = name.split("::")[-1].strip()
        packs = knowledge.kb.packs_for(platform.strip().lower() or None) if platform else knowledge.kb.packs_for(None)
        rows = [r for p in packs for r in p._rows("WHERE o.name = ?", (cls,), limit=300)]
        if not rows:
            near = [n for p in packs for n in p.near(cls, members=False)][:3]
            return f"No class {cls} in {', '.join(p.name for p in packs)}." + (f" Close names: {', '.join(near)}." if near else "")
        lines = [f"{cls}: {len(rows)} members ({rows[0]['pack']} {rows[0]['header']})"]
        for r in rows:
            pack = next(p for p in packs if p.name == r["pack"])
            since = pack.first_version(r, "c++")
            lines.append(f"  {r['kind']:<11} {(r['signature'] or r['name'])[:140]}  {r['header']}:{r['line']}"
                         + (f"  (from {since})" if since and since not in ("c++98",) else ""))
        return "\n".join(lines)

    def kernel_guide(domain: str = "thren") -> str:
        from . import kerneltool
        info = kerneltool.info(domain)
        with open(info["handbook"], encoding="utf-8") as fh:
            book = fh.read()
        with open(info["language_doc"], encoding="utf-8") as fh:
            language = fh.read()
        examples = "\n".join(f"  {e['id']}: {e['input']!r}" for e in info["examples"][:8])
        return (f"Domains: {', '.join(info['domains'])}.\n\n=== {domain} handbook ===\n{book}\n\n"
                f"=== The kernel's input language ===\n{language[:6000]}\n\nExamples:\n{examples}")

    def kernel_ask(input: str, domain: str = "thren", question: str = "") -> str:   # noqa: A002 - the tool's own word
        from . import kerneltool
        if question:
            # the English bridge's own checks: is this input a faithful translation of the question?
            from bridge.grammar import malformed
            from bridge.guards import Guard
            from bridge.translate import PLACEHOLDERS
            from mshmoe import load_domain
            data = load_domain(kerneltool._paths(domain)[0])
            lines = [ln.strip() for ln in input.splitlines() if ln.strip()]
            problems = [f"not a statement of the language: '{ln}'" for ln in malformed(data, lines)]
            problems += Guard(data, placeholders=PLACEHOLDERS).check(question, "\n".join(lines))
            if problems:
                return ("Not run: the input is not a faithful translation of the question: " + "; ".join(problems)
                        + ". Fix the input (kernel_guide has the language), or ask the user about the terms.")
        r = kerneltool.run(input, domain)
        if not r["answers"]:
            return "No question was asked: add a line that starts with ask (or estimate)."
        out = []
        for a in r["answers"]:
            line = f"{a['goal']}: {a['value']} [{a['state']}]"
            if a["supported"]:
                line += " proven" if a["proven"] else " PROOF FAILED: " + "; ".join(a["proof_problems"])
            if a["causes"]:
                line += " — " + "; ".join(a["causes"])
            if a["rests_on"]:
                line += "\n    rests on: " + "; ".join(a["rests_on"])
            out.append(line)
        out.append("KNOWN and DERIVED are proven; CONFABULATED is a guess you asked for; every other state "
                   "says why there is no answer. Do not present anything but KNOWN or DERIVED as fact.")
        return "\n".join(out)

    def kernel_map(source: str = "thren", target: str = "nour") -> str:
        from . import kerneltool
        from mshmoe import load_domain
        from mshmoe.analogy import structural_map, transfer
        a, b = load_domain(kerneltool._paths(source)[0]), load_domain(kerneltool._paths(target)[0])
        m = structural_map(a, b, (source, target))
        laws, blocked = transfer(m, a)
        out = [f"{source} → {target}: {len(m.symbols)} pairings, found from structure (never from names)."]
        out += [f"  {cls} {name} → {m.symbols[(cls, name)]}   ({m.evidence[(cls, name)]})" for cls, name in sorted(m.symbols)]
        if m.ambiguous:
            out.append("Ambiguous: " + "; ".join(f"{n} could be {' or '.join(v)}" for (_, n), v in sorted(m.ambiguous.items())))
        out.append("Laws it could borrow: " + (", ".join(law["analogy"]["law"] for law in laws) or "none (every law has a counterpart)"))
        if blocked:
            out.append("Not borrowable: " + "; ".join(f"{k}: {v}" for k, v in blocked.items()))
        return "\n".join(out)

    def code_search(text: str, platform: str = "") -> str:
        packs = knowledge.kb.packs_for(platform.strip().lower() or None)
        if not packs:
            return "The knowledge base has no packs ready yet."
        lines = []
        for p in packs:
            for r in p.search(text, 40):
                what = f"{r['kind']} of {r['owner']}" if r.get("owner") else r["kind"]
                lines.append(f"{r['name']:<40} {what:<22} {p.name} {r['header']}:{r['line']}")
        if not lines:
            return f"No declared name contains '{text}' in {', '.join(p.name for p in packs)}."
        return f"Names containing '{text}' ({len(lines)}):\n" + "\n".join(lines[:80])

    def code_read(header: str = "", line: str = "", name: str = "", platform: str = "", lines: str = "40") -> str:
        """An SDK header's own text: around a line, or around where a name is declared."""
        packs = knowledge.kb.packs_for(platform.strip().lower() or None)
        try:
            count = max(5, min(200, int(lines or 40)))
            at = int(line) if str(line).strip() else 0
        except ValueError:
            return "ERROR: line and lines are numbers"
        pack = None
        if name and not header:
            row = next((r for p in packs for r in p.lookup(name) + p.lookup(name, members=True)), None)
            if row is None:
                return f"'{name}' is not declared in {', '.join(p.name for p in packs)}; try code_search."
            header, at, pack = row["header"], row["line"], next(p for p in packs if p.name == row["pack"])
        if not header:
            return "ERROR: give a header (as code_lookup cites it, e.g. xbox.h) or a name"
        for p in ([pack] if pack else packs):
            found = p.has_header(header)
            source = knowledge.kb.sources.get(p.name, {})
            full = next((os.path.join(r, found) for r in source.get("roots", []) if found and os.path.isfile(os.path.join(r, found))), None)
            if full:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read().splitlines()
                first = max(1, (at or 1) - count // 3)
                body = "\n".join(f"{n:>6}  {text[n - 1]}" for n in range(first, min(len(text), first + count - 1) + 1))
                return f"{p.name} {found} (lines {first}-{min(len(text), first + count - 1)} of {len(text)}):\n{body}"
        return f"No header {header} in {', '.join(p.name for p in packs)}."

    def _docs():
        from codekb.docs import Docs
        return Docs(knowledge.kb.home)

    def code_docs(text: str, platform: str = "") -> str:
        """The SDKs' own documentation: the best sections for a question or a name."""
        docs = _docs()
        if not any(d["ready"] for d in docs.status()):
            return "The SDK documentation is not indexed on this computer yet (no help files, or still building)."
        hits = docs.search(text, platform.strip().lower() or None)
        if not hits:
            return f"No documentation page matches '{text}'."
        out = []
        for h in hits:
            body = h["text"] if len(h["text"]) <= 900 else h["text"][:900] + " ..."
            out.append(f"[{h['source']}] {h['title']}" + (f" / {h['heading']}" if h["heading"] else "") + f"\n{body}")
        return "\n\n".join(out) + "\n\n(code_doc reads a page whole, by its title.)"

    def code_doc(title: str, platform: str = "") -> str:
        page = _docs().page(title.strip(), platform.strip().lower() or None)
        if page is None:
            return f"No documentation page is titled '{title}'. code_docs searches for one."
        text = page["text"]
        return f"[{page['source']}] {page['title']}\n\n" + (text if len(text) <= 12000 else text[:12000] + "\n... (cut at 12000 characters)")

    def code_example(name: str, platform: str = "", count: str = "3") -> str:
        """How the SDK's own sample programs use a name: the lines around each use."""
        import re as _re
        try:
            want = max(1, min(6, int(count or 3)))
        except ValueError:
            want = 3
        word = name.split("::")[-1].strip()
        if not _re.fullmatch(r"[A-Za-z_]\w*", word):
            return "ERROR: give one name, e.g. Direct3DCreate9"
        rx = _re.compile(r"(?<!\w)" + _re.escape(word) + r"(?!\w)")
        sources = [s for n, s in knowledge.kb.sources.items() if s.get("examples")
                   and (not platform or platform.strip().lower() in s.get("platforms", []))]
        if not sources:
            return "No sample programs are known for that platform."
        hits = []
        for s in sources:
            for f in _sample_files(tuple(s["examples"])):
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                m = rx.search(text)
                if m and not _re.match(r"\s*(?://|\*|#\s*define)", text[text.rfind("\n", 0, m.start()) + 1:m.start()]):
                    hits.append((len(text), f, text, m.start(), s))
        if not hits:
            return f"No sample program uses {word}."
        hits.sort(key=lambda h: h[0])   # the smallest samples first: they show the plain way to do it
        out = [f"{word} is used in {len(hits)} sample files; the {min(want, len(hits))} smallest:"]
        for size, f, text, at, s in hits[:want]:
            lines = text.splitlines()
            ln = text.count("\n", 0, at)
            lo, hi = max(0, ln - 12), min(len(lines), ln + 18)
            root = next(r for r in s["examples"] if f.startswith(r))
            out.append(f"\n--- {os.path.relpath(f, root)} (lines {lo + 1}-{hi}) ---")
            out += [f"{n + 1:>5}  {lines[n]}" for n in range(lo, hi)]
        return "\n".join(out)

    def code_build(configuration: str = "Release", platform: str = "", request: str = "", folder: str = "",
                   sources: str = "") -> str:
        import re as _re
        from codekb.build import build
        root = os.path.abspath(project_dir_of())
        project = os.path.abspath(os.path.join(root, folder or "."))
        if os.path.commonpath([project, root]) != root:
            raise ValueError(f"{folder} is outside the project")
        extra = [x.strip() for x in _re.split(r"[,;\n]+", sources or "") if x.strip()]
        target = _target(project, "", "", platform, request)
        r = build(project, target, configuration or "Release", extra=extra)
        if not r["ran"]:
            return "Not built: " + r["log"]
        lines = []
        if r["wrote"]:
            lines.append(f"Wrote {r['wrote']} (no complete project file was here): the SDK samples' compiler and "
                         "linker settings, every .cpp and .c in the folder.")
        if r["ok"]:
            lines.append("Built. " + ("Outputs: " + ", ".join(r["outputs"]) if r["outputs"] else ""))
        else:
            lines.append(f"The build failed with {len(r['errors'])} error(s):" if r["errors"] else "The build failed:")
            lines += [f"{f}({ln}): {code}: {msg}" for f, ln, code, msg in r["errors"]]
            if not r["errors"]:
                lines.append(r["log"][-2500:])
            lines.append("Each error is the compiler's or linker's own word: fix the code, then build again.")
        return "\n".join(lines)

    def code_lookup(name: str, platform: str = "", language: str = "", version: str = "") -> str:
        if (language or "").lower() in ("asm", "assembly"):
            return _asm_lookup(knowledge, name, platform)
        return knowledge.lookup(name, platform, language, version)

    target_props = {
        "path": _prop("A file in the project (or give code instead)."),
        "code": _prop("The code itself, when it is not in a file."),
        "language": _prop("c, c++ or asm, for code given directly (default c++)."),
        "platform": _prop("Optional: " + ", ".join(PLATFORMS) + ". Otherwise read from the project."),
        "request": _prop("Optional: the user's words, if they name a platform or version (\"in C89\")."),
    }
    return [
        {"name": "code_lookup", "func": code_lookup,
         "description": ("Where a C/C++ name is declared in the SDK headers on this computer (Xbox 360 SDK, Windows "
                         "SDK, Visual C++, DirectX, GCC) and from which language version: a function, type, macro, "
                         "class member (Class::member), a header such as <xtl.h>, or with language asm an "
                         "instruction (NASM x86, Xbox 360 PowerPC). Use it before writing a call you are not sure of."),
         "parameters": {"type": "object", "properties": {
             "name": _prop("A name, Class::member, <header.h>, or an instruction."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + "."),
             "language": _prop("Optional: c, c++ (default) or asm."),
             "version": _prop("Optional: a version to check against, e.g. c++03, c89, c++17.")},
             "required": ["name"]}},
        {"name": "code_search", "func": code_search,
         "description": ("Find declared names containing a piece of text, in the SDK headers on this computer: use it "
                         "when you know part of a name (the flags a function takes, a family of constants, an API "
                         "prefix) but not the exact one. Gives each name's kind and header line."),
         "parameters": {"type": "object", "properties": {
             "text": _prop("Part of a name, e.g. XALLOC_, MemAlloc, D3DCLEAR."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + ".")}, "required": ["text"]}},
        {"name": "code_read", "func": code_read,
         "description": ("Read an SDK header's own lines, around a line or around where a name is declared: the "
                         "comments, the macros and constants next to a function, how a value is meant to be built. "
                         "Reads only the installed SDK folders."),
         "parameters": {"type": "object", "properties": {
             "name": _prop("A declared name; its header is opened at its declaration."),
             "header": _prop("Or a header as code_lookup cites it, e.g. xbox.h."),
             "line": _prop("With header: the line to show around."),
             "lines": _prop("How many lines (default 40, at most 200)."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + ".")}}},
        {"name": "code_docs", "func": code_docs,
         "description": ("Search the SDKs' own documentation (the Xbox 360 SDK and DirectX SDK help, on this computer) "
                         "for a question, a task or a name: how something is meant to be done, what a function's "
                         "parameters and return values mean, platform rules. Gives the best sections, titles first."),
         "parameters": {"type": "object", "properties": {
             "text": _prop("Words to search for, e.g. 'play a sound with XAudio2' or 'XInputGetState'."),
             "platform": _prop("Optional: xbox360, or win32 for the DirectX/Windows graphics docs.")}, "required": ["text"]}},
        {"name": "code_doc", "func": code_doc,
         "description": "Read one SDK documentation page whole, by its title as code_docs gives it.",
         "parameters": {"type": "object", "properties": {
             "title": _prop("The page title, e.g. XInputGetState."),
             "platform": _prop("Optional: xbox360 or win32.")}, "required": ["title"]}},
        {"name": "code_example", "func": code_example,
         "description": ("How the SDK's own sample programs use a name (Xbox 360 SDK samples, DirectX SDK samples): "
                         "the lines around each use, smallest samples first. The quickest way to learn how a "
                         "program is set up (device creation, the main loop, input) before writing one."),
         "parameters": {"type": "object", "properties": {
             "name": _prop("A function, type or constant, e.g. Direct3DCreate9, XInputGetState."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + "."),
             "count": _prop("How many samples to show (default 3, at most 6).")}, "required": ["name"]}},
        {"name": "code_build", "func": code_build, "approval": True,
         "description": ("Build the whole project for its target with the platform's own build system and get the "
                         "compiler's and linker's errors (file, line, code): for Xbox 360, MSBuild with the SDK's "
                         "compiler, linker and imagexex, producing an .xex. With no complete project file it writes "
                         "one from the SDK samples' settings (every .cpp and .c in the folder). Do not write build "
                         "scripts by hand; build with this, fix what it reports, build again."),
         "parameters": {"type": "object", "properties": {
             "configuration": _prop("Release (default) or Debug."),
             "folder": _prop("Optional: the subfolder of the project to build (default: the project itself)."),
             "sources": _prop("Optional, when the project file is written: more .c/.cpp files to compile, relative "
                              "to the folder and comma-separated; they may be outside it (../core/game.c) and are "
                              "compiled where they are."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + ". Otherwise read from the project."),
             "request": _prop("Optional: the user's words.")}}},
        {"name": "code_check", "func": code_check,
         "description": ("Check C, C++ or assembly against the SDK headers and assemblers on this computer, for the "
                         "platform and language version the project builds with: every include, call, class member, "
                         "std:: name, language feature and instruction. Returns what could not be confirmed, with the "
                         "near name for a misspelling. Reads only."),
         "parameters": {"type": "object", "properties": target_props}},
        {"name": "code_compile", "func": code_compile,
         "description": ("Run the target's own compiler or assembler on a file or code, syntax and declarations "
                         "only (nothing is built): the Xbox 360 compiler for a 360 project, clang/gcc otherwise, NASM "
                         "or the 360's PowerPC assembler for assembly. Its word is final."),
         "parameters": {"type": "object", "properties": target_props}},
        {"name": "code_target", "func": code_target,
         "description": ("What the project is built for, read from its files and code (nothing is asked): platform, "
                         "CPU and byte order, compiler, the C and C++ versions it takes, which C++11 features it has "
                         "and lacks, and the evidence."),
         "parameters": {"type": "object", "properties": {
             "path": _prop("Optional: a file whose includes may name the platform."),
             "request": _prop("Optional: the user's words.")}}},
        {"name": "code_members", "func": code_members,
         "description": "What a class or struct declares (methods, fields, member types), with header lines and versions.",
         "parameters": {"type": "object", "properties": {
             "name": _prop("The class, e.g. D3DDevice, vector, IDirect3DDevice9."),
             "platform": _prop("Optional: " + ", ".join(PLATFORMS) + ".")}, "required": ["name"]}},
        {"name": "kernel_guide", "func": kernel_guide,
         "description": ("The MSHMOE kernel's handbook for a domain and its input language. Read it before using "
                         "kernel_ask."),
         "parameters": {"type": "object", "properties": {"domain": _prop("thren or nour (default thren).")}}},
        {"name": "kernel_ask", "func": kernel_ask,
         "description": ("Put questions to the MSHMOE kernel, a deterministic reasoner that never guesses silently. "
                         "Input is its controlled language, one statement per line (let, rel, observe, shift, ask, "
                         "estimate). Each answer comes with its state (KNOWN, DERIVED, CONFLICTED, MISSING_KNOWLEDGE, "
                         "...) and, when supported, a proof an independent checker verified."),
         "parameters": {"type": "object", "properties": {
             "input": _prop("Statements, one per line, e.g. \"let a = qelt pell 720 dg\\nask glim a\"."),
             "domain": _prop("thren or nour (default thren)."),
             "question": _prop("The user's question in their words. When given, the input is first checked to be a "
                               "faithful translation of it (no invented numbers, terms or units), and not run if not.")},
             "required": ["input"]}},
        {"name": "kernel_map", "func": kernel_map,
         "description": ("How two kernel domains line up, found from the structure of their laws (never their names), "
                         "and which laws one could borrow from the other by analogy."),
         "parameters": {"type": "object", "properties": {
             "source": _prop("Default thren."), "target": _prop("Default nour.")}}},
    ]


_SAMPLES: Dict[tuple, List[str]] = {}


def _sample_files(roots: tuple) -> List[str]:
    """The C and C++ files in sample folders (listed once)."""
    if roots not in _SAMPLES:
        found = []
        for root in roots:
            for dirpath, _, files in os.walk(root):
                found += [os.path.join(dirpath, f) for f in files if f.lower().endswith((".cpp", ".c", ".h", ".inl"))]
        _SAMPLES[roots] = found
    return _SAMPLES[roots]


def _asm_lookup(knowledge, name: str, platform: str) -> str:
    packs = knowledge.kb.packs_for(platform.strip().lower() or None, kind="asm")
    if not packs:
        return "No assembler pack is ready."
    word = name.strip().lower()
    out = []
    for p in packs:
        rows = p.lookup(word)
        if rows:
            note = f" ({rows[0]['signature']})" if rows[0].get("signature") else ""
            out.append(f"{p.name}: '{word}' is an instruction {p.meta.get('assembler')} accepts{note}")
        else:
            near = p.near(word)
            out.append(f"{p.name}: '{word}' is not an instruction" + (f"; close: {', '.join(near)}" if near else ""))
    return "\n".join(out)


def agent_tools(knowledge, project_dir_of: Callable[[], str]):
    """The tools as a session's registry takes them."""
    from tools import Tool

    out = []
    seen: Dict[tuple, int] = {}   # the same question asked again: a sign of going round in circles

    for s in specs(knowledge, project_dir_of):
        def run(_f=s["func"], _name=s["name"], **kw):
            key = (_name, tuple(sorted((k, str(v)) for k, v in kw.items())))
            seen[key] = seen.get(key, 0) + 1
            try:
                text = _f(**kw)
            except (ValueError, OSError, KeyError) as exc:
                return f"ERROR: {exc}"
            if seen[key] >= 3 and _name != "code_build":
                text = (f"(You have asked exactly this {seen[key]} times; the answer has not changed. Use what you "
                        "have: write the code and build it, and let the compiler say what is still wrong.)\n" + text)
            return text
        out.append(Tool(name=s["name"], description=s["description"], parameters=s["parameters"], func=run,
                        needs_approval=bool(s.get("approval"))))
    return out


__all__ = ["specs", "agent_tools"]
