"""Assembly: which instructions an assembler on this computer accepts, asked of the assembler itself.

A pack per assembler: NASM (x86 and x86-64) and the Xbox 360 SDK's PowerPC assembler (ml.exe, MASM
syntax, with the 360's VMX128 instructions). Nothing is typed in from a manual. Candidate words are
read out of the assembler's own program file, and each is put to the assembler on a line of its own:

    NASM      an instruction is accepted bare (nop), or refused for its operands ("invalid combination
              of opcode and operands", "not supported in 64-bit mode"); any other word is read as a label
    PowerPC   an unknown word is named in the error ("syntax error : frobnicate"); an instruction is
              accepted bare (blr) or refused for its operands ("syntax error : in instruction")

A KNOWN finding's proof is the assembler again: verify() puts the mnemonic to it once more.

Checking a file: its dialect (NASM, MASM, GAS, or the 360's PowerPC) from the target and the text,
then each line's mnemonic looked up (labels, directives, macros the file defines and data lines are
left alone). The assembler then assembles the file, syntax only, and has the final word.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from .headers import ALL_C, ALL_CPP
from .store import FORMAT, SCHEMA

PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# MASM directives and data words: accepted bare by the PowerPC assembler, but not instructions
MASM_DIRECTIVES = {
    "align", "even", "org", "end", "endp", "ends", "proc", "segment", "assume", "public", "extrn", "extern",
    "include", "includelib", "db", "dw", "dd", "dq", "df", "dt", "byte", "word", "dword", "qword", "sbyte",
    "sword", "sdword", "real4", "real8", "real10", "struct", "struc", "union", "record", "typedef", "equ",
    "textequ", "macro", "endm", "exitm", "local", "purge", "if", "ife", "ifdef", "ifndef", "ifb", "ifnb",
    "else", "elseif", "endif", "for", "forc", "irp", "irpc", "repeat", "rept", "while", "comment", "option",
    "title", "subtitle", "page", "list", "nolist", "xlist", "label", "name", "invoke", "proto", "offset",
    "sizeof", "lengthof", "type", "this", "ptr", "short", "near", "far", "dup", "mod", "shl", "shr",
    "eq", "ne", "lt", "le", "gt", "ge", "high", "low", "highword", "lowword", "catstr",
    "instr", "sizestr", "substr", "group", "alias", "echo", "err", "radix", "model", "code", "data", "const",
    "stack", "startup", "exit", "endprolog", "endepilog", "pdata", "xdata", "allocstack", "savereg",
}
NASM_DIRECTIVES = {
    "bits", "use16", "use32", "use64", "default", "section", "segment", "absolute", "extern", "global", "common",
    "static", "cpu", "float", "org", "align", "alignb", "struc", "endstruc", "istruc", "at", "iend", "times",
    "db", "dw", "dd", "dq", "dt", "do", "dy", "dz", "resb", "resw", "resd", "resq", "rest", "reso", "resy", "resz",
    "incbin", "equ", "import", "export", "group", "uppercase", "library", "module", "warning", "required", "seg",
    "wrt", "strict", "byte", "word", "dword", "qword", "tword", "oword", "yword", "zword", "ptr",
}


def assemblers() -> List[Dict]:
    """The assemblers on this computer that can build a pack."""
    out = []
    nasm = shutil.which("nasm") or next((p for p in (os.path.join(PF, "NASM", "nasm.exe"),
                                                       os.path.join(PF86, "NASM", "nasm.exe")) if os.path.exists(p)), None)
    if nasm:
        out.append({"name": "asm-x86", "kind": "asm", "assembler": "nasm", "exe": nasm,
                    "platforms": ["win32", "win64", "posix", "xbox"],
                    "note": f"x86 and x86-64 instructions NASM accepts ({os.path.basename(os.path.dirname(nasm))})"})
    xedk = os.environ.get("XEDK") or os.path.join(PF86, "Microsoft Xbox 360 SDK")
    ml = os.path.join(xedk, "bin", "win32", "ml.exe")
    if os.path.exists(ml):
        out.append({"name": "asm-ppc", "kind": "asm", "assembler": "xenon", "exe": ml, "platforms": ["xbox360"],
                    "note": "PowerPC and VMX128 instructions the Xbox 360 SDK's assembler accepts"})
    return out


def _words(exe: str, dotted: bool) -> List[str]:
    """Lower-case words in a program file: the candidates (an assembler keeps its mnemonics as text)."""
    with open(exe, "rb") as fh:
        data = fh.read()
    rx = rb"(?<![A-Za-z0-9_.])([a-z][a-z0-9]{1,15}" + (rb"\.?" if dotted else b"") + rb")(?=\x00)"
    return sorted({m.decode() for m in re.findall(rx, data)})


def _run(cmd: List[str], cwd: str, timeout: int = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, creationflags=_NOWINDOW)
    return proc.stdout + proc.stderr


# ------------------------------------------------------------------ asking each assembler

_NASM_YES = ("invalid combination of opcode and operands", "not supported in 64-bit mode",
             "no instruction for this cpu level", "instruction not supported",
             "is not lockable", "invalid with this instruction")   # the last two: prefixes (lock, xacquire)


def _nasm_said(exe: str, words: List[str], folder: str, tag: str) -> Dict[int, List[str]]:
    path = os.path.join(folder, f"probe{tag}.asm")
    with open(path, "w", encoding="ascii") as fh:
        fh.write("bits 64\n" + "\n".join(words) + "\n")
    out = _run([exe, "-f", "win64", "-o", os.path.join(folder, f"probe{tag}.obj"), path], folder, 300)
    said: Dict[int, List[str]] = {}
    for line in out.splitlines():
        m = re.match(r".*?:(\d+): (?:error|warning): (.*)", line)
        if m:
            said.setdefault(int(m.group(1)), []).append(m.group(2))
    return said


def _nasm_classify(exe: str, words: List[str], folder: str) -> Dict[str, str]:
    """word -> note for the words NASM takes as instructions ('' or 'not in 64-bit mode').

    All at once, the words NASM reads as labels are found (it says so in its first pass); an error
    there stops its later passes, so every other word is then put to it on its own."""
    from concurrent.futures import ThreadPoolExecutor

    said = _nasm_said(exe, words, folder, "")
    left = [w for i, w in enumerate(words, start=2)
            if w not in NASM_DIRECTIVES and not any("label alone" in m for m in said.get(i, []))]

    def one(item):
        i, w = item
        msgs = _nasm_said(exe, [w], folder, str(i)).get(2, [])
        if not msgs or any(any(y in m for y in _NASM_YES) for m in msgs):
            return w, ("not in 64-bit mode" if any("64-bit mode" in m for m in msgs) else "")
        return w, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {w: note for w, note in pool.map(one, enumerate(left)) if note is not None}


def _xenon_probe(exe: str, body: str, folder: str) -> str:
    path = os.path.join(folder, "probe.asm")
    with open(path, "w", encoding="ascii") as fh:
        fh.write("_TEXT SEGMENT PARA 'CODE'\nprobe PROC\n" + body + "\nprobe ENDP\n_TEXT ENDS\n        END\n")
    out = _run([exe, "/nologo", "/Zs", "/c", path], folder)
    # writing no object fails with a COFF error after the real ones: noise
    return "\n".join(ln for ln in out.splitlines() if "coff" not in ln.lower() and "Assembling" not in ln)


def _xenon_classify(exe: str, words: List[str], folder: str) -> Dict[str, str]:
    left = [w for w in words if w.rstrip(".") not in MASM_DIRECTIVES]
    named = set()
    for i in range(0, len(left), 60):   # it stops after so many errors: a few dozen words at a time
        out = _xenon_probe(exe, "\n".join("        " + w for w in left[i:i + 60]), folder)
        named.update(m.lower() for m in re.findall(r"syntax error : (\S+)", out))
    found = {}
    for w in left:
        if w in named:
            continue
        out = _xenon_probe(exe, "        " + w, folder)   # one at a time: is it refused only for operands?
        if not out.strip() or "syntax error : in instruction" in out or "operand" in out.lower():
            found[w] = ""
    return found


def probe(source: Dict, word: str) -> bool:
    """Does the assembler take `word` as an instruction? (A KNOWN finding's proof, re-checked.)"""
    with tempfile.TemporaryDirectory() as folder:
        if source["assembler"] == "nasm":
            return word in _nasm_classify(source["exe"], [word], folder)
        return word in _xenon_classify(source["exe"], [word], folder)


def build_pack(path: str, source: Dict, before_swap=None) -> Dict:
    """A pack of the instructions an assembler accepts. Rebuilt only when the assembler changes."""
    exe = source["exe"]
    stamp = (os.path.getsize(exe), os.path.getmtime(exe))
    label = f"{source['assembler']}: {exe}"
    if os.path.exists(path):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            have = db.execute("SELECT size, mtime FROM files WHERE path = ?", (label,)).fetchone()
            fmt = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            db.close()
            if have and tuple(have) == stamp and fmt and fmt[0] == FORMAT:
                return {"pack": source["name"], "rebuilt": False, "files": 1}
        except sqlite3.Error:
            pass
    t0 = time.time()
    with tempfile.TemporaryDirectory() as folder:
        if source["assembler"] == "nasm":
            found = _nasm_classify(exe, _words(exe, dotted=False), folder)
        else:
            found = _xenon_classify(exe, _words(exe, dotted=True), folder)
    tmp = path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO files VALUES (1, ?, ?, ?)", (label, *stamp))
    names = sorted(found)
    db.executemany("INSERT INTO name_list VALUES (?, ?)", list(enumerate(names, start=1)))
    db.executemany("INSERT INTO symbols VALUES (?, 'instruction', 1, 0, ?, ?, ?, NULL)",
                   [(i, found[n] or None, ALL_C, ALL_CPP) for i, n in enumerate(names, start=1)])
    db.execute("INSERT INTO names(names) VALUES ('rebuild')")
    meta = {"name": source["name"], "language": "asm", "assembler": source["assembler"], "exe": exe,
            "note": source["note"], "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "declarations": str(len(names)), "names": str(len(names)), "format": FORMAT}
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    if before_swap:
        before_swap()
    os.replace(tmp, path)
    return {"pack": source["name"], "rebuilt": True, "files": 1, "declarations": len(names),
            "seconds": round(time.time() - t0, 1), "bytes": os.path.getsize(path)}


# ------------------------------------------------------------------ checking a file

def dialect(code: str, target: Dict) -> str:
    """nasm, masm, gas or xenon (the 360's PowerPC MASM)."""
    if target.get("platform") == "xbox360":
        return "xenon"
    low = code.lower()
    if re.search(r"^\s*(?:bits\s+(?:16|32|64)|section\s+\.|default\s+rel|%define|%macro|global\s+\w)", low, re.M):
        return "nasm"
    if re.search(r"^\s*\.(?:globl|text|data|intel_syntax|att_syntax|section)\b", low, re.M) or re.search(r"%[re]?[abcd]x\b", low):
        return "gas"
    if re.search(r"\b(?:proc|endp|segment|ends)\b|^\s*\.(?:code|data|model)\b", low, re.M):
        if re.search(r"\b(?:mflr|mtlr|blr|stwu|lwz|addi)\b", low):
            return "xenon"
        return "masm"
    return "nasm"


def _strip_comment(line: str, d: str) -> str:
    line = re.sub(r"'[^']*'|\"[^\"]*\"", "''", line)
    marks = ("#", "//") if d == "gas" else (";", "//")
    cut = min([i for i in (line.find(m) for m in marks) if i >= 0] or [len(line)])
    return line[:cut]


def mnemonics(code: str, d: str) -> List[Tuple[int, str]]:
    """(line, mnemonic) for each instruction line: labels, directives, data, macros and the file's own
    definitions are left out."""
    directives = NASM_DIRECTIVES if d in ("nasm", "gas") else MASM_DIRECTIVES
    own = set()
    for m in re.finditer(r"^\s*%(?:i?macro|i?define|assign)\s+([A-Za-z_.$]\w*)", code, re.M | re.I):
        own.add(m.group(1).lower())
    for m in re.finditer(r"^\s*([A-Za-z_$?@][\w$?@]*)\s+(?:macro|equ|textequ|=|struct|struc|record|typedef|proc|label)\b",
                         code, re.M | re.I):
        own.add(m.group(1).lower())
    out = []
    in_macro = False
    for i, raw in enumerate(code.splitlines(), start=1):
        line = _strip_comment(raw, d).strip()
        if not line:
            continue
        low = line.lower()
        if re.match(r"%(?:i?macro)\b|\w+\s+macro\b", low):
            in_macro = True
        if in_macro:
            if re.match(r"%endmacro\b|endm\b", low):
                in_macro = False
            continue
        line = re.sub(r"^[A-Za-z_.$?@][\w.$?@]*:\s*", "", line)   # a label
        if not line or line[0] in "%.#[=":
            continue
        words = line.split()
        first = words[0].lower()
        second = words[1].lower() if len(words) > 1 else ""
        if first in directives or first in own or second in directives or second in ("=", "equ", ":"):
            continue
        if d != "gas" and second and (second in MASM_DIRECTIVES or second in NASM_DIRECTIVES):
            continue   # `name dd 0`, `name PROC`, `buffer resb 64`
        out.append((i, first))
    return out


def _bare(word: str, d: str) -> List[str]:
    """Ways a mnemonic may be written: PowerPC's record dot and branch hints, GAS's size suffix."""
    forms = [word]
    if d == "xenon":
        forms += [word.rstrip("+-"), word.rstrip("+-").rstrip(".")]
    if d == "gas" and len(word) > 2 and word[-1] in "bwlqs":
        forms.append(word[:-1])
    return list(dict.fromkeys(forms))


def check(kb, project_dir: str, path: str, code: str, target: Dict) -> List[Dict]:
    d = dialect(code, target)
    pack_name = "asm-ppc" if d == "xenon" else "asm-x86"
    pack = kb.pack(pack_name)
    if pack is None:
        return [{"state": "UNKNOWN_REFERENCE", "kind": "setup", "name": "", "line": 0,
                 "message": f"no {pack_name} pack is built (no assembler for it was found); nothing could be looked up"}]
    sixty_four = d == "nasm" and (re.search(r"^\s*bits\s+64", code, re.M | re.I) is not None
                                  or (target.get("platform") == "win64" and not re.search(r"^\s*bits\s+(16|32)", code, re.M | re.I)))
    findings, seen = [], set()
    for line, word in mnemonics(code, d):
        if word in seen:
            continue
        seen.add(word)
        row = next((r for form in _bare(word, d) for r in pack.lookup(form)), None)
        if row is None:
            near = pack.near(word)
            if near:
                findings.append({"state": "LOW_CONFIDENCE", "kind": "instruction", "name": word, "line": line, "near": near,
                                 "message": f"'{word}' is not an instruction {pack.name} accepts; did you mean {' or '.join(near)}?"})
            else:
                findings.append({"state": "UNKNOWN_REFERENCE", "kind": "instruction", "name": word, "line": line,
                                 "message": f"'{word}' is not an instruction {pack.name} accepts (a macro from an include?)"})
            continue
        proof = [{"by": "assembler", "pack": pack.name, "name": row["name"]}]
        if sixty_four and row.get("signature") == "not in 64-bit mode":
            findings.append({"state": "CONFLICTED", "kind": "instruction", "name": word, "line": line, "proof": proof,
                             "message": f"'{word}' is an instruction, but not in 64-bit mode; this code is 64-bit"})
            continue
        findings.append({"state": "KNOWN", "kind": "instruction", "name": word, "line": line, "proof": proof,
                         "message": f"'{word}': an instruction {pack.name} accepts"})
    return findings


def assembler_for(target: Dict, code: str, kb) -> Optional[Dict]:
    d = dialect(code, target)
    for s in assemblers():
        if (d == "xenon") == (s["assembler"] == "xenon") and d in ("nasm", "xenon"):
            return {**s, "dialect": d}
    return None


def assemble_check(target: Dict, project_dir: str, path: str, code: str, kb=None, timeout: int = 60) -> Dict:
    """The assembler on the file, nothing kept: {"compiler", "errors": [(line, message)], "ran"}."""
    a = assembler_for(target, code, kb)
    if a is None:
        return {"compiler": None, "errors": [], "ran": False}
    here = os.path.dirname(os.path.join(project_dir, path)) or project_dir
    with tempfile.TemporaryDirectory() as folder:
        src = os.path.join(folder, os.path.basename(path) or "check.asm")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(code)
        if a["assembler"] == "nasm":
            fmt = "win32" if re.search(r"^\s*bits\s+32", code, re.M | re.I) or target.get("platform") == "win32" else "win64"
            out = _run([a["exe"], "-f", fmt, "-I", here + os.sep, "-o", os.path.join(folder, "out.obj"), src], folder, timeout)
            errors = [(int(m.group(1)), m.group(2).strip()) for m in
                      (re.match(r".*?:(\d+): error: (.*)", ln) for ln in out.splitlines()) if m]
            name = "NASM"
        else:
            # its messages carry no line; its listing puts each one right after the line it is about
            listing = os.path.join(folder, "out.lst")
            _run([a["exe"], "/nologo", "/c", "/I", here, "/Fo" + os.path.join(folder, "out.obj"), "/Fl" + listing, src],
                 folder, timeout)
            errors = _listing_errors(listing, code)
            name = "Xbox 360 PowerPC assembler"
        return {"compiler": name, "errors": errors[:30], "ran": True}


def _listing_errors(listing: str, code: str) -> List[Tuple[int, str]]:
    """(line, message) from a MASM listing: each source line is shown in order, after its address and
    bytes, and an error follows the line it is about."""
    try:
        with open(listing, encoding="latin-1") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return []
    lines = [ln.rstrip() for ln in code.splitlines()]
    at, out = 0, []
    for row in rows:
        m = re.search(r"error (A\d+: .*)", row)
        if m:
            if "COFF" not in m.group(1):
                out.append((at, m.group(1).strip()))
            continue
        text = row.split("\t")[-1].rstrip() if "\t" in row else None
        if text is None:
            continue
        hit = next((i for i in range(at, len(lines)) if lines[i] == text), None)
        if hit is not None:
            at = hit + 1   # 1-based: the line just shown
    return out


__all__ = ["assemblers", "build_pack", "check", "dialect", "mnemonics", "probe", "assemble_check"]
