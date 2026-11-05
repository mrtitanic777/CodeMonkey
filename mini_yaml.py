"""Minimal YAML-subset parser (standard library only).

The project avoids third-party dependencies, so this module implements just
enough YAML for the config files and skill frontmatter used here:

  - mappings via ``key: value`` (nested through indentation)
  - sequences via ``- item``
  - scalars: plain/quoted strings, ints, floats, booleans, null
  - block scalars: ``>`` (folded, joined with spaces) and ``|`` (literal,
    joined with newlines) for multi-line values
  - ``#`` comments (outside of quoted strings)

This is NOT a full YAML implementation. Do not use it for untrusted or
complex YAML documents.
"""

from __future__ import annotations


def _strip_comment(line: str) -> str:
    """Remove a ``#`` comment, ignoring ``#`` inside quotes."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in " \t":
                return line[:i]
    return line


def _scalar(text: str):
    """Convert a scalar string to int/float/bool/None/str."""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] == "'":
        return s[1:-1].replace("''", "'")   # YAML: a quote inside single quotes is doubled
    if len(s) >= 2 and s[0] == s[-1] == '"':
        if "\\" in s:
            import json

            try:   # YAML's double-quoted escapes (\" \\ \n ...) are JSON's
                return json.loads(s)
            except ValueError:
                pass   # e.g. a Windows path written "C:\work": kept as written
        return s[1:-1]
    low = s.lower()
    if low in ("", "null", "~", "none"):
        return None
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def parse(text: str):
    """Parse a YAML-subset document into Python objects."""
    prepared = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code = _strip_comment(raw).rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        prepared.append((indent, code.strip()))

    pos = 0

    def peek():
        return prepared[pos] if pos < len(prepared) else None

    def parse_block(indent):
        first = peek()
        if first is None or first[0] != indent:
            return None
        if first[1] == "-" or first[1].startswith("- "):
            return parse_list(indent)
        return parse_map(indent)

    def parse_block_scalar(parent_indent, style):
        """Consume indented lines for ``>`` (folded) / ``|`` (literal)."""
        nonlocal pos
        chunks = []
        while True:
            cur = peek()
            if cur is None or cur[0] <= parent_indent:
                break
            chunks.append(cur[1])
            pos += 1
        if style == "|":
            return "\n".join(chunks)
        return " ".join(chunks)

    def parse_map(indent):
        nonlocal pos
        mapping = {}
        while True:
            cur = peek()
            if cur is None or cur[0] != indent:
                break
            text = cur[1]
            if text == "-" or text.startswith("- "):
                break
            pos += 1
            if ":" not in text:
                raise ValueError(f"Invalid mapping line: {text!r}")
            key, _, value = text.partition(":")
            key, value = key.strip(), value.strip()
            nxt = peek()
            if value in (">", "|"):
                mapping[key] = parse_block_scalar(indent, value)
            elif value == "":
                mapping[key] = (
                    parse_block(nxt[0]) if (nxt and nxt[0] > indent) else None
                )
            else:
                mapping[key] = _scalar(value)
        return mapping

    def parse_list(indent):
        nonlocal pos
        items = []
        while True:
            cur = peek()
            if cur is None or cur[0] != indent:
                break
            text = cur[1]
            if not (text == "-" or text.startswith("- ")):
                break
            pos += 1
            rest = text[1:].strip()
            if rest == "":
                nxt = peek()
                items.append(
                    parse_block(nxt[0]) if (nxt and nxt[0] > indent) else None
                )
            elif ": " in rest or rest.endswith(":"):
                # Inline mapping item, e.g. "- name: foo"
                item = {}
                key, _, value = rest.partition(":")
                key, value = key.strip(), value.strip()
                item[key] = _scalar(value) if value else None
                while True:
                    nxt = peek()
                    if (
                        nxt
                        and nxt[0] > indent
                        and not (nxt[1] == "-" or nxt[1].startswith("- "))
                        and ":" in nxt[1]
                    ):
                        k2, _, v2 = nxt[1].partition(":")
                        pos += 1
                        k2, v2 = k2.strip(), v2.strip()
                        after = peek()
                        if v2 in (">", "|"):
                            item[k2] = parse_block_scalar(nxt[0], v2)
                        elif v2 == "" and after and after[0] > nxt[0]:
                            item[k2] = parse_block(after[0])
                        else:
                            item[k2] = _scalar(v2) if v2 else None
                    else:
                        break
                items.append(item)
            else:
                items.append(_scalar(rest))
        return items

    if not prepared:
        return {}
    return parse_block(prepared[0][0])


def parse_file(path: str):
    """Parse a YAML-subset file."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse(fh.read())
