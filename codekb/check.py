"""Checking code against the knowledge base, with the kernel's states and checkable proofs.

For C and C++: every include, every function called and every std:: name is looked up in the packs
for the code's target (names the file or the project defines itself are left alone):

    KNOWN              found, visible in the target's language version; the proof names the header line
    CONFLICTED         it exists, but not in the target's version ("from C++17; this project is C++11")
    LOW_CONFIDENCE     not found, but a name a few letters away is ("did you mean XMemAlloc?")
    UNKNOWN_REFERENCE  not found in any pack: unverified (a library list is never complete)

Language features a version added (a range-for in C++03, a declaration in a for loop in C89) are
CONFLICTED on the line they are on. These are findings, not refusals: the code is written, checked,
and fixed, and the target's compiler (compile()) has the final word.

verify() re-checks a KNOWN finding's proof without the database: it opens the cited header and
confirms the name is on the cited line.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from . import languages
from .headers import CPP_VERSIONS, C_VERSIONS, header_files, scan, scan_file
from .platforms import PLATFORMS

_CALL = re.compile(r"(?<![\w.>:$])([A-Za-z_]\w*)\s*\(")
_STD = re.compile(r"\bstd\s*::\s*([A-Za-z_]\w*)")
_MEMBER = re.compile(r"(?:\.|->)\s*([A-Za-z_]\w*)\s*(?:<[^<>;()]*>\s*)?\(")
_SCOPED = re.compile(r"(?<![\w:])([A-Za-z_]\w*)\s*::\s*([A-Za-z_]\w*)\s*\(")
_INCLUDE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.M)
_NOT_CALLS = languages.all_keywords("c++") | languages.all_keywords("c") | {
    "defined", "sizeof", "alignof", "decltype", "typeof", "__declspec", "__attribute__", "__pragma", "_Pragma",
    "main", "static_cast", "dynamic_cast", "const_cast", "reinterpret_cast", "__asm", "__asm__", "asm", "assert",
    "__builtin_expect", "offsetof", "va_start", "va_end", "va_arg", "__uuidof", "__noop"}

_project_cache: Dict[str, tuple] = {}


def project_names(project_dir: str) -> set:
    """Names the project's own sources declare or define (cached until a file changes)."""
    stamp = []
    files = []
    for root, dirs, fs in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "build", "out", "bin", "obj")]
        for f in fs:
            if f.lower().endswith((".h", ".hpp", ".hh", ".hxx", ".c", ".cpp", ".cc", ".cxx", ".inl")):
                p = os.path.join(root, f)
                files.append(p)
                try:
                    stamp.append((p, os.path.getmtime(p)))
                except OSError:
                    pass
        if len(files) > 4000:
            break
    key = tuple(sorted(stamp))
    cached = _project_cache.get(project_dir)
    if cached and cached[0] == key:
        return cached[1]
    names = set()
    for p in files:
        names.update(s[0] for s in scan_file(p))
    _project_cache[project_dir] = (key, names)
    return names


def _line_of(code: str, pos: int) -> int:
    return code.count("\n", 0, pos) + 1


def _version_index(lang: str, version: Optional[str]) -> Optional[int]:
    versions = CPP_VERSIONS if lang == "c++" else C_VERSIONS
    return versions.index(version) if version in versions else None


def check(kb, project_dir: str, path: str, code: str, target: Dict) -> List[Dict]:
    lang = target.get("language")
    if lang == "asm":
        from . import asm
        return asm.check(kb, project_dir, path, code, target)
    if lang not in ("c", "c++"):
        return []
    version = target.get(lang if lang == "c" else "cpp")
    # names come from the platform's own library, which may offer more than its language version
    lib_version = target.get("library_cpp") if lang == "c++" and target.get("library_cpp") else version
    platform = target.get("platform")
    packs = kb.packs_for(platform) if platform else kb.packs_for(None)
    if not packs:
        return [{"state": "UNKNOWN_REFERENCE", "kind": "setup", "name": "", "line": 0,
                 "message": "the knowledge base has no packs built yet; nothing could be looked up"}]
    blanked = languages.blank(code)
    own = {s[0] for s in scan(code)} | project_names(project_dir)
    findings: List[Dict] = []
    seen = set()

    # includes
    for m in _INCLUDE.finditer(code):
        quote, header = m.group(1), m.group(2).strip()
        line = _line_of(code, m.start())
        here = os.path.dirname(os.path.join(project_dir, path))
        if quote == '"' and (os.path.exists(os.path.join(here, header)) or os.path.exists(os.path.join(project_dir, header))):
            continue
        hit = next(((p, f) for p in packs for f in [p.has_header(header)] if f), None)
        if hit:
            findings.append({"state": "KNOWN", "kind": "include", "name": header, "line": line,
                             "message": f"<{header}> is in {hit[0].name}",
                             "proof": [{"by": "file", "pack": hit[0].name, "file": hit[1]}]})
        else:
            findings.append({"state": "UNKNOWN_REFERENCE", "kind": "include", "name": header, "line": line,
                             "message": f"<{header}> is not in {', '.join(p.name for p in packs)} or the project; unverified"})

    # functions called, and std:: names
    uses = [(m.group(1), m.start(), "call") for m in _CALL.finditer(blanked)] + \
           [(m.group(1), m.start(), "std") for m in _STD.finditer(blanked)]
    for name, pos, how in uses:
        if name in _NOT_CALLS or (how == "call" and name in own) or (name, how) in seen:
            continue
        if how == "call" and re.match(r"[A-Z_][A-Z0-9_]*$", name) and name in own:
            continue
        if how == "call" and _declares(blanked, pos):
            continue   # `Type name(args)`: a variable made with arguments, not a call
        seen.add((name, how))
        line = _line_of(blanked, pos)
        rows = [r for p in packs for r in p.lookup(name)]
        if how == "call" and blanked[max(0, pos - 2):pos].endswith("::"):
            continue
        if not rows and platform and len(name) > 2:
            # not in this platform's libraries: another platform's may have it, and say from when
            elsewhere = [(p, r) for p in kb.packs_for(None) if p not in packs for r in p.lookup(name)]
            if elsewhere:
                p, r = elsewhere[0]
                since = p.first_version(r, lang)
                findings.append({"state": "CONFLICTED", "kind": how, "name": name, "line": line,
                                 "message": f"'{name}' is not in {', '.join(q.name for q in packs)}; it is in "
                                            f"{p.name} ({r['header']}, from {since}), another platform's library",
                                 "proof": [_proof(r)]})
                continue
        if not rows:
            near = []
            for p in packs:
                near += [n for n in p.near(name, members=False) if n not in near]
            near += [n for n in own if _close(name, n) and n not in near]
            if near:
                findings.append({"state": "LOW_CONFIDENCE", "kind": how, "name": name, "line": line, "near": near[:3],
                                 "message": f"'{name}' is not in {', '.join(p.name for p in packs)}; did you mean "
                                            + " or ".join(near[:3]) + "?"})
            else:
                findings.append({"state": "UNKNOWN_REFERENCE", "kind": how, "name": name, "line": line,
                                 "message": f"'{name}' is not in {', '.join(p.name for p in packs)} or the project; "
                                            "it does not exist for this target"})
            continue
        vi = _version_index(lang, lib_version)
        ok_rows = rows if vi is None else [r for r in rows if (r["cpp_mask"] if lang == "c++" else r["c_mask"]) & (1 << vi)]
        if not ok_rows:
            pack = next(p for p in packs if p.name == rows[0]["pack"])
            first = pack.first_version(rows[0], lang)
            findings.append({"state": "CONFLICTED", "kind": how, "name": name, "line": line,
                             "message": f"'{name}' exists ({rows[0]['header']}, {rows[0]['pack']}) but only from {first}; "
                                        f"this code is {version} ({_why(target, lang)})",
                             "proof": [_proof(rows[0])]})
            continue
        r = ok_rows[0]
        findings.append({"state": "KNOWN", "kind": how, "name": name, "line": line,
                         "message": f"'{name}': {r['header']}:{r['line']} ({r['pack']})", "proof": [_proof(r)]})

    findings += _member_findings(blanked, lang, lib_version, packs, own, target)

    # language features newer than the target's version
    findings += _features(code, blanked, lang, version, platform)
    return findings


def _member_findings(blanked: str, lang: str, version: Optional[str], packs, own: set, target: Dict) -> List[Dict]:
    """Calls through an object (`obj.f(`, `p->f(`) and through a class (`Class::f(`).

    Through an object its class is not known here, so the name must be a member of some class in the
    target's libraries; through a class, a member of that class. A misspelling gets the near name; a
    member visible only in later versions than the target's is CONFLICTED. A name found nowhere is
    left alone: it is most likely a member of the project's own classes or of a library not packed."""
    out, seen = [], set()
    vi = _version_index(lang, version) if lang == "c++" else None
    for m in _MEMBER.finditer(blanked):
        name = m.group(1)
        if name in own or name in _NOT_CALLS or ("member", name) in seen:
            continue
        seen.add(("member", name))
        line = _line_of(blanked, m.start(1))
        rows = [r for p in packs for r in p.lookup(name, members=True)]
        if not rows:
            near = []
            for p in packs:
                near += [n for n in p.near(name, members=True) if n not in near]
            if near:
                out.append({"state": "LOW_CONFIDENCE", "kind": "member", "name": name, "line": line, "near": near[:3],
                            "message": f"no class in {', '.join(p.name for p in packs)} has a member '{name}'; "
                                       f"did you mean {' or '.join(near[:3])}?"})
            continue
        ok = rows if vi is None else [r for r in rows if r["cpp_mask"] & (1 << vi)]
        if not ok:
            r = rows[0]
            first = next(p for p in packs if p.name == r["pack"]).first_version(r, "c++")
            out.append({"state": "CONFLICTED", "kind": "member", "name": name, "line": line,
                        "message": f"'{name}' is a member ({r['owner']}, {r['header']}) only from {first}; "
                                   f"this code is {target.get('cpp') or version} ({_why(target, lang)})",
                        "proof": [_proof(r)]})
            continue
        r = ok[0]
        out.append({"state": "KNOWN", "kind": "member", "name": name, "line": line,
                    "message": f"'{name}': member of {r['owner']}, {r['header']}:{r['line']} ({r['pack']})",
                    "proof": [_proof(r)]})
    for m in _SCOPED.finditer(blanked):
        owner, name = m.group(1), m.group(2)
        if owner == "std" or owner in own or ("scoped", owner, name) in seen:
            continue
        seen.add(("scoped", owner, name))
        if not any(r["kind"] == "type" for p in packs for r in p.lookup(owner)):
            continue   # a namespace, or a class this knowledge base does not have
        line = _line_of(blanked, m.start(2))
        rows = [r for p in packs for r in p.member(owner, name)]
        if rows:
            r = rows[0]
            out.append({"state": "KNOWN", "kind": "member", "name": f"{owner}::{name}", "line": line,
                        "message": f"'{owner}::{name}': {r['header']}:{r['line']} ({r['pack']})", "proof": [_proof(r)]})
            continue
        members = [n for p in packs for n in p.members_of(owner)]
        from .store import _edit_distance
        allow = max(2, len(name) // 4)   # the rule the packs use for a near name
        near = sorted((d, n) for n in members for d in [_edit_distance(name, n, allow)] if 0 < d <= allow)
        near = [n for _, n in near][:3]
        out.append({"state": "LOW_CONFIDENCE" if near else "UNKNOWN_REFERENCE", "kind": "member",
                    "name": f"{owner}::{name}", "line": line, "near": near,
                    "message": f"{owner} has no member '{name}'" + (f"; did you mean {' or '.join(near)}?" if near
                                                                    else f" in {', '.join(p.name for p in packs)}")})
    return out


_CALL_AFTER = {"return", "else", "case", "new", "delete", "throw", "do", "co_await", "co_return", "co_yield"}


def _declares(code: str, pos: int) -> bool:
    """Is the name at pos being declared (`int x(3)`, `std::vector<int> v(n)`, `Foo* f(...)`) rather than
    called? It is when a type comes right before it: a name (not a keyword that takes an expression),
    a closing '>' of template arguments, or a '*' / '&' after one of those."""
    before = code[max(0, pos - 200):pos].rstrip()
    if before.endswith(("&&", "||")):
        return False
    before = before.rstrip("*&").rstrip()
    if before.endswith(">"):
        return not before.endswith("->") and not re.search(r"[=<>!]=?\s*>$|>>$", before)
    m = re.search(r"([A-Za-z_]\w*)$", before)
    return bool(m) and m.group(1) not in _CALL_AFTER


def _why(target: Dict, lang: str) -> str:
    """The evidence for a target that bears on this language: its platform and that language's version."""
    why = target.get("why") or {}
    parts = [why[k] for k in ("platform", "cpp" if lang == "c++" else "c") if why.get(k)]
    return "; ".join(parts) or "; ".join(target.get("evidence", [])[:2])


def _proof(row: Dict) -> Dict:
    return {"by": "header", "pack": row["pack"], "file": row["header"], "line": row["line"], "name": row["name"]}


def _close(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 2 or len(a) < 4:
        return False
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.85


def _features(code: str, blanked: str, lang: str, version: Optional[str], platform: Optional[str]) -> List[Dict]:
    if version is None:
        return []
    vi = _version_index(lang, version)
    spec = PLATFORMS.get(platform, {}) if platform else {}
    out = []
    for name, flang, since, pattern, cpp11_name in languages.FEATURES:
        if flang != lang:
            continue
        if _version_index(lang, since) <= vi:
            continue
        if cpp11_name and cpp11_name in spec.get("cpp11_has", []):
            continue   # this compiler had it before the standard did
        if name == "line comments (//)":
            if "line comments" in spec.get("c_extras", []):
                continue
            m = re.search(r"(?<![:\"'])//", re.sub(r'"(?:\\.|[^"\\\n])*"', '""', code))
            if m:
                out.append({"state": "CONFLICTED", "kind": "feature", "name": name, "line": _line_of(code, m.start()),
                            "message": f"{name} are {since} and later; this code is {version}"})
            continue
        if pattern is None:
            continue
        m = pattern.search(blanked)
        if m:
            where = spec.get("name") or version
            out.append({"state": "CONFLICTED", "kind": "feature", "name": name, "line": _line_of(blanked, m.start()),
                        "message": f"{name} needs {since}; {where} does not have it ({version})"})
    return out


def verify(kb, finding: Dict) -> Optional[str]:
    """Re-check a finding's proof from the files themselves (not the database). None if it holds,
    otherwise what is wrong."""
    for step in finding.get("proof", []):
        source = kb.sources.get(step["pack"])
        if source is None:
            return f"unknown pack {step['pack']}"
        if step["by"] == "assembler":   # asked of the assembler again
            from .asm import probe
            if not probe(source, step["name"]):
                return f"{source['assembler']} does not take {step['name']} as an instruction"
            continue
        path = next((os.path.join(r, step["file"]) for r in source["roots"]
                     if os.path.exists(os.path.join(r, step["file"]))), None)
        if path is None:
            return f"{step['file']} is not in {step['pack']}'s folders"
        if step["by"] == "header":
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().split("\n")
            except OSError as exc:
                return str(exc)
            text = lines[step["line"] - 1] if 0 < step["line"] <= len(lines) else ""
            if not re.search(r"(?<!\w)" + re.escape(step["name"]) + r"(?!\w)", text):
                return f"{step['name']} is not on line {step['line']} of {step['file']}"
    return None


# ---------------------------------------------------------------- the compiler, as the final judge

def compiler_for(target: Dict) -> Optional[Dict]:
    """The compiler that judges this target's code, if this computer has one."""
    lang = target.get("language")
    if lang not in ("c", "c++"):
        return None
    if target.get("platform") == "xbox360":
        xedk = os.environ.get("XEDK")
        cl = os.path.join(xedk or "", "bin", "win32", "cl.exe")
        if xedk and os.path.exists(cl):
            return {"name": "Xbox 360 cl", "kind": "msvc", "exe": cl,
                    "args": ["/nologo", "/c", "/Zs", "/TP" if lang == "c++" else "/TC",
                             "/I" + os.path.join(xedk, "include", "xbox"), "/D_XBOX", "/DXBOX"]}
        return None
    for exe in (["clang++", "g++"] if lang == "c++" else ["clang", "gcc"]):
        found = shutil.which(exe)
        if found:
            std = target.get("cpp" if lang == "c++" else "c")
            args = ["-fsyntax-only", "-ferror-limit=30" if "clang" in exe else "-fmax-errors=30"]
            if std:
                args.append("-std=" + std.replace("c++03", "c++03").replace("c89", "c89"))
            return {"name": os.path.basename(found), "kind": "gnu", "exe": found, "args": args}
    return None


_MSVC_ERR = re.compile(r"\((\d+)\)\s*:\s*(fatal error|error)\s+(C\d+)\s*:\s*(.*)")
_GNU_ERR = re.compile(r":(\d+):(?:\d+:)?\s*(fatal error|error):\s*(.*)")


def compile_check(target: Dict, project_dir: str, path: str, code: str, timeout: int = 60) -> Dict:
    """Run the target's compiler on the code (syntax and declarations only, nothing is built).
    {"compiler", "errors": [(line, message)], "ran": bool}."""
    if target.get("language") == "asm":
        from .asm import assemble_check
        return assemble_check(target, project_dir, path, code, timeout=timeout)
    comp = compiler_for(target)
    if comp is None:
        return {"compiler": None, "errors": [], "ran": False}
    suffix = os.path.splitext(path)[1] or (".cpp" if target.get("language") == "c++" else ".c")
    here = os.path.dirname(os.path.join(project_dir, path)) or project_dir
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, dir=here if os.path.isdir(here) else None,
                                     encoding="utf-8") as fh:
        fh.write(code)
        tmp = fh.name
    try:
        inc = ["/I" + project_dir, "/I" + here] if comp["kind"] == "msvc" else ["-I" + project_dir, "-I" + here]
        proc = subprocess.run([comp["exe"], *comp["args"], *inc, tmp], capture_output=True, text=True,
                              timeout=timeout, cwd=here if os.path.isdir(here) else None,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = proc.stdout + proc.stderr
        errors = []
        rx = _MSVC_ERR if comp["kind"] == "msvc" else _GNU_ERR
        for line in out.splitlines():
            if os.path.basename(tmp) not in line:
                continue
            m = rx.search(line)
            if m:
                if comp["kind"] == "msvc":
                    errors.append((int(m.group(1)), f"{m.group(3)}: {m.group(4).strip()}"))
                else:
                    errors.append((int(m.group(1)), m.group(3).strip()))
        return {"compiler": comp["name"], "errors": errors[:30], "ran": True}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"compiler": comp["name"], "errors": [], "ran": False, "problem": str(exc)}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ------------------------------------------------- names that do not exist, as the compiler sees them

# Microsoft's: undeclared identifier; not a member; identifier not found; an unknown name where a type
# goes (C2061, C3646, C4430); an include that is not there; not a class or namespace; an undefined type
_MSVC_MISSING = {"C2065", "C2039", "C3861", "C2061", "C3646", "C4430", "C1083", "C2653", "C2027", "C2079"}
_GNU_MISSING = re.compile(r"undeclared identifier|unknown type name|no member named|was not declared in this scope|"
                          r"has no member named|does not name a type|file not found|No such file or directory|"
                          r"no type named|is not a class, namespace, or enumeration|not a namespace-name", re.I)
_QUOTED = re.compile(r"'([^']+)'")


def missing_names(target: Dict, project_dir: str, path: str, code: str, timeout: int = 60) -> Optional[List[tuple]]:
    """The names the code uses that neither the target's SDK nor the project has, as the target's own
    compiler reports them: [(line, name, message)]. None when no compiler for the target is here.

    The compiler reads the SDK's headers and the project's (the file's folder and the project folder
    are on its include path), so what it cannot find is, for this target, not there."""
    result = compile_check(target, project_dir, path, code, timeout=timeout)
    if not result.get("ran"):
        return None
    out = []
    for line, message in result["errors"]:
        code_m = re.match(r"(C\d+):\s*(.*)", message)
        if code_m:
            if code_m.group(1) not in _MSVC_MISSING:
                continue
            text = code_m.group(2)
        else:
            if not _GNU_MISSING.search(message):
                continue
            text = message
        names = _QUOTED.findall(text)
        # MSVC also says "syntax error : identifier 'X'" (C2061): the name is the quoted one
        name = names[0] if names else text.split(":")[-1].strip()
        out.append((line, name, message))
    return out


__all__ = ["check", "verify", "compile_check", "compiler_for", "project_names", "header_files", "missing_names"]
