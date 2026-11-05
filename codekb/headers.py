"""Reading C and C++ headers for what they declare, and in which language versions.

A tolerant scanner, not a compiler: it finds functions, macros, types, enum values and the headers
they are in, with their line, and works out from the preprocessor conditions around them which
language versions see them (`#if __cplusplus >= 201703L`, MSVC's `_HAS_CXX17`, `__STDC_VERSION__`,
`#ifdef __cplusplus`). Anything else it cannot judge is taken as visible in every version: the
compiler stays the final judge.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterator, List, Optional, Tuple

C_VERSIONS = ("c89", "c99", "c11", "c17", "c23")
CPP_VERSIONS = ("c++98", "c++03", "c++11", "c++14", "c++17", "c++20", "c++23")
C_VALUE = {"c89": 0, "c99": 199901, "c11": 201112, "c17": 201710, "c23": 202311}
CPP_VALUE = {"c++98": 199711, "c++03": 199711, "c++11": 201103, "c++14": 201402, "c++17": 201703,
             "c++20": 202002, "c++23": 202302}
ALL_C = (1 << len(C_VERSIONS)) - 1
ALL_CPP = (1 << len(CPP_VERSIONS)) - 1
MSVC_HAS = {"_HAS_CXX17": "c++17", "_HAS_CXX20": "c++20", "_HAS_CXX23": "c++23", "_HAS_CPP0X": "c++11"}

KEYWORDS = frozenset("""if while for switch return sizeof alignof decltype typeof __typeof__ __declspec
__attribute__ __pragma _Pragma defined static_assert _Static_assert noexcept throw new delete operator
catch case do else goto""".split())

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"' + r"|'(?:\\.|[^'\\\n])*'")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _mask_where(values: Dict[str, int], versions: Tuple[str, ...], test) -> int:
    m = 0
    for i, v in enumerate(versions):
        if test(values[v]):
            m |= 1 << i
    return m


_CMP = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _split_top(expr: str, op: str) -> List[str]:
    parts, depth, last, i = [], 0, 0, 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[last:i])
            last = i + len(op)
            i += len(op)
            continue
        i += 1
    parts.append(expr[last:])
    return parts


def _unwrap(e: str) -> str:
    e = e.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, c in enumerate(e):
            depth += c == "("
            depth -= c == ")"
            if depth == 0 and i < len(e) - 1:
                return e
        e = e[1:-1].strip()
    return e


def condition_masks(expr: str, features: Optional[Dict] = None) -> Optional[Tuple[int, int]]:
    """(C versions, C++ versions) in which a preprocessor condition is true, or None if it is not
    about the language version (then it is not judged). Understands __cplusplus and __STDC_VERSION__
    comparisons, defined(__cplusplus), MSVC's _HAS_CXXnn, feature-test macros learned from the
    library's own version header, 0 and 1, and !, && and || over them. In an &&, parts that are not
    about the version are left out; an || is judged only if every part is."""
    e = _unwrap(expr)
    ors = _split_top(e, "||")
    if len(ors) > 1:
        masks = [condition_masks(x, features) for x in ors]
        if any(m is None for m in masks):
            return None
        return (ALL_C & sum_or([m[0] for m in masks]), ALL_CPP & sum_or([m[1] for m in masks]))
    ands = _split_top(e, "&&")
    if len(ands) > 1:
        masks = [m for m in (condition_masks(x, features) for x in ands) if m is not None]
        if not masks:
            return None
        c, cpp = ALL_C, ALL_CPP
        for m in masks:
            c, cpp = c & m[0], cpp & m[1]
        return c, cpp
    if e.startswith("!"):
        inner = condition_masks(e[1:], features)
        return None if inner is None else (ALL_C & ~inner[0], ALL_CPP & ~inner[1])
    return _atom(e, features or {})


def sum_or(masks: List[int]) -> int:
    out = 0
    for m in masks:
        out |= m
    return out


def _atom(e: str, features: Dict) -> Optional[Tuple[int, int]]:
    e = _unwrap(e)
    if e in ("0", "0L"):
        return 0, 0
    if e in ("1", "1L"):
        return ALL_C, ALL_CPP
    m = re.fullmatch(r"defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?", e)
    name = m.group(1) if m else (e if re.fullmatch(r"[A-Za-z_]\w*", e) else None)
    if name == "__cplusplus":
        return 0, ALL_CPP
    if name in MSVC_HAS:
        first = CPP_VERSIONS.index(MSVC_HAS[name])
        return 0, ALL_CPP & ~((1 << first) - 1)
    if name in features:
        return sum_or([c for _, c, _ in features[name]]), sum_or([cpp for _, _, cpp in features[name]])
    m = re.fullmatch(r"([A-Za-z_]\w*)\s*(>=|>|<=|<|==|!=)\s*(\d+)L?", e)
    if m:
        macro, op, n = m.group(1), _CMP[m.group(2)], int(m.group(3))
        if macro == "__cplusplus":
            return (ALL_C if op(0, n) else 0), _mask_where(CPP_VALUE, CPP_VERSIONS, lambda v: op(v, n))
        if macro == "__STDC_VERSION__":
            return _mask_where(C_VALUE, C_VERSIONS, lambda v: op(v, n) and v > 0), (ALL_CPP if op(0, n) else 0)
        if macro == "_MSVC_LANG":
            return 0, _mask_where(CPP_VALUE, CPP_VERSIONS, lambda v: op(v, n))
        if macro in features:
            hits = [(c, cpp) for value, c, cpp in features[macro] if value is not None and op(value, n)]
            return sum_or([h[0] for h in hits]), sum_or([h[1] for h in hits])
    return None


def collect_features(paths: List[str]) -> Dict:
    """Feature-test macros a library defines and in which versions, read from its version header(s):
    {macro: [(value, C versions, C++ versions)]}. `__cpp_lib_x` defined as `__glibcxx_x` takes its
    versions."""
    features: Dict = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for _ in scan(text, features=features, collect=features):
            pass
    return features


def _strip(text: str) -> str:
    """Comments and string contents blanked, line breaks kept (so line numbers stay right)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _COMMENT.sub(blank, text)
    return _STRING.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], text)


class Symbol(tuple):
    """(name, kind, line, signature, c_mask, cpp_mask)"""


def scan(text: str, features: Optional[Dict] = None, collect: Optional[Dict] = None) -> Iterator[Tuple]:
    """Every declaration in a header: (name, kind, line, signature, C versions, C++ versions, owner).
    owner is the class a member belongs to (methods, fields, member types), None at namespace level."""
    for item in _scan(text, features, collect):
        yield item if len(item) == 7 else (*item, None)


# a class declared through a macro: COM interfaces (DirectX, the Xbox SDK, the Windows SDK)
_MACRO_CLASS = re.compile(r"(?:DECLARE_INTERFACE(?:_IID)?_?\s*\(\s*([A-Za-z_]\w*)"
                          r"|MIDL_INTERFACE\s*\([^)]*\)\s*([A-Za-z_]\w*))")
_ACCESS = re.compile(r"^(?:\s*(?:public|private|protected|__published)\s*:)+")
_STDMETHOD = re.compile(r"\bSTDMETHOD_?\s*\(\s*(?:[^()]*?,\s*)?([A-Za-z_]\w*)\s*\)")


def _strip_template(head: str) -> str:
    """`template<...>` prefixes removed (angle brackets may nest)."""
    while True:
        m = re.match(r"\s*template\s*<", head)
        if not m:
            return head
        depth, i = 1, m.end()
        while i < len(head) and depth:
            depth += {"<": 1, ">": -1}.get(head[i], 0)
            i += 1
        head = head[i:]


def _members(code: str, start: int, end: int, owner: str) -> Iterator[Tuple[str, str, int, str]]:
    """What a class body declares: (name, kind, position of the name, signature), kind one of method,
    field, member_type, enum_value. Nested classes are read the same way, with their own owner."""
    pos, stmt_start = start, start
    while pos < end:
        ch = code[pos]
        if ch not in ";{}":
            pos += 1
            continue
        raw = code[stmt_start:pos]
        head = _ACCESS.sub("", raw).strip()
        offset = stmt_start + (len(raw) - len(raw.lstrip()))
        if ch == "}":
            stmt_start = pos = pos + 1
            continue
        if ch == "{":
            close = _matching(code, pos)
            kind_m = re.search(r"\b(struct|class|union|enum(?:\s+class|\s+struct)?)\s+([A-Za-z_]\w*)\s*(?:final\s*)?(?::[^{]*)?$", head)
            if kind_m:
                where = stmt_start + raw.find(kind_m.group(2), len(raw) - len(raw.lstrip()))
                yield kind_m.group(2), "member_type", where, _sig(head)
                if kind_m.group(1).startswith("enum"):
                    for v, off in _enum_values(code[pos + 1:close]):
                        yield v, "enum_value", pos + 1 + off, f"{owner}::{v}"
                else:
                    for m in _members(code, pos + 1, close, kind_m.group(2)):
                        yield m
            elif head.startswith(("enum", "typedef enum")) or re.match(r"enum\s*$", head):
                for v, off in _enum_values(code[pos + 1:close]):
                    yield v, "enum_value", pos + 1 + off, f"{owner}::{v}"
            elif head and not head.startswith(("namespace", "extern")):
                body = _strip_template(head)
                own = re.match(r"(?:(?:explicit|virtual|inline|constexpr|[A-Z_][A-Z0-9_]*)\s+)*(~?" + re.escape(owner) + r")\s*\(", body)
                fn = own.group(1) if own else _function(body)
                if fn:
                    yield fn, "method", _name_at(code, offset, pos, fn), _sig(head)
            stmt_start = pos = close + 1
            continue
        # ';': a declaration in the class
        stmt_start = pos + 1
        pos += 1
        if not head or head.startswith(("friend", "static_assert", "using namespace", "#")):
            continue
        sd = _STDMETHOD.search(head)
        if sd:
            yield sd.group(1), "method", _name_at(code, offset, pos, sd.group(1)), _sig(head)
            continue
        body = _strip_template(head)
        if body.startswith("typedef") or re.match(r"using\s+[A-Za-z_]\w*\s*=", body):
            for name, _, sig in _statement(body):
                yield name, "member_type", _name_at(code, offset, pos, name), sig
            continue
        if re.match(r"(?:struct|class|union|enum)\s+[A-Za-z_]\w*\s*$", body):
            continue   # a forward declaration
        if "(" in re.sub(r"\[[^\]]*\]", "", body.split("=")[0]):
            own = re.match(r"(?:(?:explicit|virtual|inline|constexpr|[A-Z_][A-Z0-9_]*)\s+)*(~?" + re.escape(owner) + r")\s*\(", body)
            fn = own.group(1) if own else _function(body)   # a constructor or destructor has no type before it
            if fn:
                yield fn, "method", _name_at(code, offset, pos, fn), _sig(head)
            continue
        # fields: `type a, *b, c[4] : 3 = 0`
        decl = re.sub(r"\s*=.*$", "", body, flags=re.S)
        parts = _top_split(decl)
        first = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?::\s*\w+\s*)?$", parts[0]) if parts else None
        if not first or not _IDENT.search(parts[0][:first.start()]):
            continue   # a lone name: a macro, not a field
        names = [first.group(1)] + [m.group(1) for p in parts[1:]
                                    for m in [re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?::\s*\w+\s*)?$", p)] if m]
        for name in names:
            if name not in KEYWORDS:
                yield name, "field", _name_at(code, offset, pos, name), _sig(head)


def _top_split(text: str) -> List[str]:
    out, depth, cur = [], 0, ""
    for c in text:
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    return out + [cur]


def _name_at(code: str, start: int, end: int, name: str) -> int:
    """Where a member's name is, between start and end (a function's name is the one before '(')."""
    seg = code[start:end]
    m = (re.search(r"(?<!\w)" + re.escape(name) + r"\s*\(", seg) or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", seg))
    return start + (m.start() if m else 0)


def _scan(text: str, features: Optional[Dict] = None, collect: Optional[Dict] = None) -> Iterator[Tuple]:
    text = _strip(text)
    lines = text.split("\n")
    # pass 1: preprocessor regions and #defines, by line
    stack: List[Tuple[int, int, Optional[Tuple[int, int]], bool]] = []   # (c, cpp, this branch's own masks, judged)
    cur = (ALL_C, ALL_CPP)
    line_mask: List[Tuple[int, int]] = []
    code_lines: List[str] = []
    continued = False   # inside a directive that runs on with a trailing backslash
    for i, raw in enumerate(lines):
        s = raw.strip()
        if continued:
            continued = s.endswith("\\")
            line_mask.append(cur)
            code_lines.append("")
            continue
        if s.startswith("#"):
            continued = s.endswith("\\")
            d = s[1:].strip()
            word = d.split(None, 1)[0] if d else ""
            rest = d[len(word):].strip()
            if word in ("if", "ifdef", "ifndef"):
                expr = rest if word == "if" else (f"defined({rest})" if word == "ifdef" else f"!defined({rest})")
                own = condition_masks(expr, features)
                stack.append((cur[0], cur[1], own, own is not None))
                if own:
                    cur = (cur[0] & own[0], cur[1] & own[1])
            elif word in ("elif", "else") and stack:
                parent_c, parent_cpp, own, judged = stack[-1]
                if word == "else" and judged and own is not None:
                    cur = (parent_c & ~own[0] & ALL_C, parent_cpp & ~own[1] & ALL_CPP)
                elif word == "elif":
                    nxt = condition_masks(rest, features)
                    if judged and own is not None and nxt is not None:
                        cur = (parent_c & ~own[0] & nxt[0], parent_cpp & ~own[1] & nxt[1])
                        stack[-1] = (parent_c, parent_cpp, (own[0] | nxt[0], own[1] | nxt[1]), True)
                    else:
                        cur = (parent_c, parent_cpp)
                        stack[-1] = (parent_c, parent_cpp, None, False)
                else:
                    cur = (parent_c, parent_cpp)
            elif word == "endif" and stack:
                parent_c, parent_cpp, _, _ = stack.pop()
                cur = (parent_c, parent_cpp)
            elif word == "define":
                m = re.match(r"([A-Za-z_]\w*)(\([^)]*\))?", rest)
                if collect is not None and m and re.match(r"__(?:cpp_lib|glibcxx|cpp)_", m.group(1)):
                    value = re.match(r"[A-Za-z_]\w*\s+(\d+)L?\s*$", rest)
                    alias = re.match(r"[A-Za-z_]\w*\s+([A-Za-z_]\w*)\s*$", rest)
                    if alias and alias.group(1) in collect:
                        collect.setdefault(m.group(1), []).extend(collect[alias.group(1)])
                    else:
                        collect.setdefault(m.group(1), []).append(
                            (int(value.group(1)) if value else None, cur[0], cur[1]))
                if m and cur != (0, 0):
                    yield m.group(1), "macro", i + 1, ("#define " + m.group(0)).strip(), cur[0], cur[1]
            line_mask.append(cur)
            code_lines.append("")
            # a directive continued with a backslash swallows its next lines too
            continue
        line_mask.append(cur)
        code_lines.append(raw)
    # pass 2: declarations, statement by statement, outside function and struct bodies
    code = "\n".join(code_lines)
    starts = [0]
    for ln in code_lines:
        starts.append(starts[-1] + len(ln) + 1)

    def line_of(pos: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def name_line(start: int, stmt: str, name: str, fallback: int) -> int:
        """The line the name itself is on (a declaration may start lines earlier)."""
        # a function's own name is the one followed by "(": prefer that, then any whole match
        m = (re.search(r"(?<!\w)" + re.escape(name) + r"\s*\(", stmt)
             or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", stmt))
        return line_of(start + m.start()) if m else fallback

    pos, n = 0, len(code)
    stmt_start = 0
    while pos < n:
        ch = code[pos]
        if ch in ";{}":
            stmt = code[stmt_start:pos]
            if ch == "{":
                head = stmt.strip()
                ln = line_of(stmt_start + len(stmt) - len(stmt.lstrip()))
                c_m, cpp_m = line_mask[min(ln, len(line_mask) - 1)]
                # (string contents are blanked, so `extern "C"` reads as `extern " "`)
                if re.match(r'(namespace\b|inline\s+namespace\b|extern\s+"[^"]*"\s*$)', head):
                    stmt_start = pos + 1   # a namespace or linkage block: its contents are top level
                    pos += 1
                    continue
                kind_m = re.search(r"\b(struct|class|union|enum(?:\s+class|\s+struct)?)\s+"
                                   r"(?:(?:[A-Z_][A-Z0-9_]*|__declspec\([^)]*\)|alignas\([^)]*\)|\[\[[^\]]*\]\])\s+)*"
                                   r"([A-Za-z_]\w*)\s*(?:final\s*)?(?::[^{]*)?$", head)
                end = _matching(code, pos)
                macro_class = _MACRO_CLASS.search(head) if not kind_m else None
                if macro_class:
                    kind_m = re.match(r"(class) (\w+)", "class " + (macro_class.group(1) or macro_class.group(2)))
                if kind_m:
                    yield kind_m.group(2), "type", name_line(stmt_start, stmt, kind_m.group(2), ln) + 1, head[:160], c_m, cpp_m
                    if not kind_m.group(1).startswith("enum"):
                        for name, kind, where, sig in _members(code, pos + 1, end, kind_m.group(2)):
                            mln = line_of(where)
                            # its own line's versions (they already include the #ifs around the class)
                            mc, mcpp = line_mask[min(mln, len(line_mask) - 1)]
                            if mc or mcpp:
                                yield name, kind, mln + 1, sig, mc, mcpp, kind_m.group(2)
                    if kind_m.group(1).startswith("enum"):
                        for v, off in _enum_values(code[pos + 1:end]):
                            yield v, "enum_value", line_of(pos + 1 + off) + 1, f"{kind_m.group(2)}::{v}", c_m, cpp_m
                    tail = re.match(r"\s*([A-Za-z_]\w*)?\s*(?:,\s*\*?\s*([A-Za-z_]\w*))*\s*;", code[end + 1:end + 200])
                    if head.startswith("typedef") and tail:
                        for t in re.findall(r"(?<![\w.])[A-Za-z_]\w*", tail.group(0)):
                            yield t, "type", name_line(end + 1, code[end + 1:end + 300], t, ln) + 1, head[:160], c_m, cpp_m
                elif re.match(r"(typedef\s+)?(struct|union|enum)\s*$", head) or head.startswith("typedef"):
                    if head.startswith(("enum", "typedef enum")):
                        for v, off in _enum_values(code[pos + 1:end]):
                            yield v, "enum_value", line_of(pos + 1 + off) + 1, v, c_m, cpp_m
                    tail = re.match(r"\s*\*?\s*([A-Za-z_]\w*)((?:\s*,\s*\*?\s*[A-Za-z_]\w*)*)\s*;", code[end + 1:end + 300])
                    if head.startswith("typedef") and tail:
                        for t in re.findall(r"(?<![\w.])[A-Za-z_]\w*", tail.group(0)):
                            yield t, "type", name_line(end + 1, code[end + 1:end + 300], t, ln) + 1, head[:160], c_m, cpp_m
                        # `typedef struct { ... } Name;`: its members belong to the name it is given
                        if re.search(r"\b(struct|union)\b", head):
                            owner = tail.group(1)
                            for name, kind, where, sig in _members(code, pos + 1, end, owner):
                                mln = line_of(where)
                                mc, mcpp = line_mask[min(mln, len(line_mask) - 1)]
                                if mc or mcpp:
                                    yield name, kind, mln + 1, sig, mc, mcpp, owner
                else:
                    fn = _function(head)
                    if fn:
                        yield fn, "function", name_line(stmt_start, stmt, fn, ln) + 1, _sig(head), c_m, cpp_m
                pos = end + 1
                stmt_start = pos
                continue
            if ch == ";":
                head = stmt.strip()
                if head:
                    ln = line_of(stmt_start + len(stmt) - len(stmt.lstrip()))
                    c_m, cpp_m = line_mask[min(ln, len(line_mask) - 1)]
                    for item in _statement(head):
                        yield item[0], item[1], name_line(stmt_start, stmt, item[0], ln) + 1, item[2], c_m, cpp_m
            stmt_start = pos + 1
        pos += 1


def _matching(code: str, pos: int) -> int:
    depth = 0
    for i in range(pos, len(code)):
        c = code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(code) - 1


def _enum_values(body: str) -> List[Tuple[str, int]]:
    """(name, offset in the body) for each enumerator."""
    out, at = [], 0
    for part in body.split(","):
        m = re.match(r"\s*([A-Za-z_]\w*)", part)
        if m:
            out.append((m.group(1), at + m.start(1)))
        at += len(part) + 1
    return out


def _sig(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:240]


_ANNOTATION = re.compile(r"^(?:[A-Z0-9_]+|_+[A-Z][A-Za-z0-9_]*_|__[a-z][a-z0-9_]*)$")


def _function(head: str) -> Optional[str]:
    """The function a declaration names: the identifier before a top-level '(' that has at least one
    identifier (its type) before it, skipping annotations written as calls (all-capitals macros such as
    a deprecation marker, SAL's _Name_(...), __drv_...(...)). Function pointers are not functions."""
    if re.search(r"\btypedef\b", head):
        return None
    depth, opens = 0, []
    for i, c in enumerate(head):
        if c == "(":
            if depth == 0:
                opens.append(i)
            depth += 1
        elif c == ")":
            depth -= 1
    for first in opens:
        before = head[:first].rstrip()
        m = re.search(r"(~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*$", before)
        if not m:
            return None   # '(' right after something that is not a name: a function pointer or an expression
        name = m.group(1).split("::")[-1]
        if name in KEYWORDS or _ANNOTATION.match(name):
            continue      # an annotation (or a keyword such as __declspec): the name comes later
        prefix = before[:m.start()]
        cleaned = re.sub(r"\b(?:[A-Z0-9_]+|_+[A-Z][A-Za-z0-9_]*_|__[a-z][a-z0-9_]*)\s*\([^()]*\)", " ", prefix)
        if not _IDENT.search(cleaned):
            return None   # nothing before it but annotations: a macro call, not a declaration
        return name
    return None


def _statement(head: str) -> Iterator[Tuple[str, str, str]]:
    """Declarations ending in ';': functions, typedefs, macro-made types, forward declarations."""
    if head.startswith("typedef"):
        fp = re.search(r"\(\s*(?:\w+\s+)*\*\s*([A-Za-z_]\w*)\s*\)\s*\(", head)
        if fp:
            yield fp.group(1), "type", _sig(head)
            return
        names = re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:,|$)", head)
        for n in names:
            if n not in ("typedef",):
                yield n, "type", _sig(head)
        return
    m = re.match(r"(?:[A-Z_][A-Z0-9_]*\s+)*(?:template\s*<[^;]*>\s*)?using\s+([A-Za-z_]\w*)\s*=", head)
    if m:
        yield m.group(1), "type", _sig(head)
        return
    m = re.search(r"\bconcept\s+([A-Za-z_]\w*)\s*=", head)
    if m:
        yield m.group(1), "concept", _sig(head)
        return
    m = re.match(r"DECLARE_HANDLE\s*\(\s*([A-Za-z_]\w*)\s*\)", head)
    if m:
        yield m.group(1), "type", _sig(head)
        return
    m = re.match(r"(?:struct|class|union|enum)\s+([A-Za-z_]\w*)\s*$", head)
    if m:
        yield m.group(1), "type", _sig(head)
        return
    fn = _function(head)
    if fn:
        yield fn, "function", _sig(head)


def scan_file(path: str, features: Optional[Dict] = None) -> List[Tuple[str, str, int, str, int, int]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return list(scan(fh.read(), features))
    except OSError:
        return []


def header_files(root: str, exts=(".h", ".hpp", ".hh", ".hxx", ".inl", "")) -> Iterator[str]:
    """Header files under a folder (extension-less files only inside a C++ library folder)."""
    for dirpath, _, files in os.walk(root):
        cpp_lib = bool({"vector", "iostream", "memory", "algorithm"} & set(files)) \
            or os.path.basename(dirpath) in ("bits", "ext", "tr1", "experimental")
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in exts and (ext or cpp_lib):
                yield os.path.join(dirpath, f)


def version_headers(roots: List[str]) -> List[str]:
    """A library's own version header(s), where its feature-test macros are defined."""
    out = []
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if (f == "version.h" and os.path.basename(dirpath) == "bits") or f in ("version", "yvals_core.h"):
                    out.append(os.path.join(dirpath, f))
    return out
