"""The SDKs' own documentation as a look-up source: the help files on this computer, made searchable.

The Xbox 360 SDK and the DirectX SDK ship their documentation as .chm help files. Each is unpacked
once (with 7-Zip; nothing is downloaded), every page's text is read out of its HTML (code blocks keep
their lines), cut into sections at its headings, and stored in a SQLite full-text index. A search
ranks the page titles highest, so an API's own page comes first; a page can then be read whole.

    python -m codekb.docs build [<workspace>]      unpack and index what this computer has
    python -m codekb.docs search "<words>" [--platform xbox360]
"""
from __future__ import annotations

import glob
import html
import os
import re
import sqlite3
import subprocess
import sys
import time
from html.parser import HTMLParser
from typing import Dict, List, Optional

PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
FORMAT = "5"
SECTION_CHARS = 1600


def sources() -> List[Dict]:
    """The help files on this computer: {name, chm, platforms, note}."""
    out = []
    xedk = os.environ.get("XEDK") or os.path.join(PF86, "Microsoft Xbox 360 SDK")
    for chm in sorted(glob.glob(os.path.join(xedk, "doc", "*", "xbox360sdk.chm")))[:1]:
        out.append({"name": "xdk", "chm": chm, "platforms": ["xbox360"], "note": "Xbox 360 SDK documentation"})
    dx = os.environ.get("DXSDK_DIR") or os.path.join(PF86, "Microsoft DirectX SDK (June 2010)")
    for name, file, note in (("dxsdk", "directx_sdk.chm", "DirectX SDK (June 2010) documentation"),
                             ("wingfx", "windows_graphics.chm", "Windows graphics (Direct3D 9-11) documentation")):
        chm = os.path.join(dx, "Documentation", "DirectX9", file)
        if os.path.isfile(chm):
            out.append({"name": name, "chm": chm, "platforms": ["win32", "win64"], "note": note})
    return out


def seven_zip() -> Optional[str]:
    for p in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "7-Zip", "7z.exe"),
              os.path.join(PF86, "7-Zip", "7z.exe")):
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------------------- reading HTML ---

class _Text(HTMLParser):
    SKIP = {"script", "style", "xml", "head", "title"}
    BLOCK = {"p", "div", "br", "li", "tr", "table", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "pre", "blockquote"}
    HEAD = {"h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: List[List[str]] = [["", ""]]    # [heading, text]
        self._skip = 0
        self._pre = 0
        self._head: Optional[List[str]] = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "pre":
            self._pre += 1
            self._emit("\n")
        elif tag in self.HEAD:
            self._head = []
        elif tag in self.BLOCK:
            self._emit("\n")
        elif tag == "td":
            self._emit("  ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._emit("\n")
        elif tag in self.HEAD and self._head is not None:
            heading = " ".join("".join(self._head).split())
            self._head = None
            if heading:
                self.sections.append([heading, ""])
        elif tag in self.BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if self._head is not None:
            self._head.append(data)
            return
        self._emit(data if self._pre else re.sub(r"\s+", " ", data))

    def _emit(self, text):
        self.sections[-1][1] += text


# the help viewer's own words on every page
_CHROME = {"See Also", "Collapse All", "Expand All", "Collapse All Expand All", "Visual Basic (Usage)",
           "Visual Basic (Declaration)", "Visual Basic", "C#", "C++", "J#", "JScript", "JavaScript", "Copy Code",
           "Language Filter: All", "Send comments about this topic to Microsoft."}


_FOOTER = re.compile(r"(©|Copyright ©|Send feedback|Send comments|Version: \d|Build date:"
                     r"|((See Also|Members|Example|Requirements|Remarks)\s*)+$)")   # the page's link bar
_STOP = {"a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "be", "it", "its", "i", "do", "does",
         "how", "what", "which", "when", "where", "why", "can", "with", "from", "by", "at", "as", "this", "that", "my",
         "me", "you", "your", "we", "there", "any", "about", "into", "should", "would", "could", "get", "use", "using"}


def read_page(path: str) -> Dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    m = re.search(rb'charset=([\w-]+)', raw[:2000])
    try:
        text = raw.decode(m.group(1).decode() if m else "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    p = _Text()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001 - a broken page is read as far as it goes
        pass
    keywords = re.findall(r'<MSHelp:Keyword Index="[KF]" Term="([^"]+)"', text)
    sections = []
    for heading, body in p.sections:
        body = "\n".join(ln for ln in body.split("\n") if " ".join(ln.split()) not in _CHROME
                         and not _FOOTER.match(ln.strip()))
        body = re.sub(r"[ \t]+\n", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        while body:
            cut = body[:SECTION_CHARS]
            if len(body) > SECTION_CHARS:
                at = cut.rfind("\n")
                cut = body[:at] if at > SECTION_CHARS // 2 else cut
            sections.append((heading, cut.strip()))
            body = body[len(cut):].strip()
    return {"title": html.unescape(" ".join(p.title.split())), "keywords": keywords[:20], "sections": sections}


# --------------------------------------------------------------- the index ---

class Docs:
    def __init__(self, home: str):
        self.home = home
        os.makedirs(home, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.home, f"docs-{name}.db")

    def ready(self, name: str) -> bool:
        if not os.path.isfile(self.path(name)):
            return False
        con = None
        try:
            con = sqlite3.connect(f"file:{self.path(name)}?mode=ro", uri=True)
            return con.execute("select value from meta where key='format'").fetchone() == (FORMAT,)
        except sqlite3.Error:
            return False
        finally:
            if con is not None:
                con.close()

    def unpack(self, src: Dict) -> str:
        out = os.path.join(self.home, "raw", src["name"])
        if glob.glob(os.path.join(out, "*.htm*")):
            return out
        exe = seven_zip()
        if not exe:
            raise RuntimeError("7-Zip is needed to unpack the help files (7-Zip's 7z.exe was not found)")
        os.makedirs(out, exist_ok=True)
        subprocess.run([exe, "x", "-y", "-bd", "-o" + out, src["chm"]], capture_output=True, timeout=1800, check=False)
        return out

    def build(self, src: Dict, progress=None) -> Dict:
        folder = self.unpack(src)
        pages = sorted(glob.glob(os.path.join(folder, "**", "*.htm*"), recursive=True))
        tmp = self.path(src["name"]) + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        con = sqlite3.connect(tmp)
        con.executescript("""
            create table meta(key text primary key, value text);
            create table pages(id integer primary key, path text, title text, keywords text);
            create virtual table sections using fts5(title, heading, body, page unindexed,
                                                     tokenize='porter unicode61');
        """)
        t0, n_sec = time.time(), 0
        try:
            for i, path in enumerate(pages):
                page = read_page(path)
                if not page["title"] or not page["sections"]:
                    continue
                rel = os.path.relpath(path, folder).replace("\\", "/")
                cur = con.execute("insert into pages(path, title, keywords) values (?,?,?)",
                                  (rel, page["title"], " ".join(page["keywords"])))
                for heading, body in page["sections"]:
                    con.execute("insert into sections(title, heading, body, page) values (?,?,?,?)",
                                (page["title"] + " " + " ".join(page["keywords"][:3]), heading, body, cur.lastrowid))
                    n_sec += 1
                if progress and i % 2000 == 0:
                    progress(src["name"], i, len(pages))
            con.executemany("insert into meta values (?,?)", [("format", FORMAT), ("name", src["name"]),
                                                              ("note", src["note"]),
                                                              ("platforms", ",".join(src["platforms"])), ("chm", src["chm"])])
            con.commit()
            con.execute("insert into sections(sections) values ('optimize')")
            con.commit()
        finally:
            con.close()   # one page that will not read must not leave the pack half-built and held open
        os.replace(tmp, self.path(src["name"]))
        return {"name": src["name"], "pages": len(pages), "sections": n_sec, "seconds": round(time.time() - t0)}

    def build_all(self, progress=None) -> List[Dict]:
        return [self.build(s, progress) for s in sources() if not self.ready(s["name"])]

    def _for(self, platform: Optional[str]) -> List[str]:
        names = []
        for s in sources():
            if self.ready(s["name"]) and (not platform or platform in s["platforms"]):
                names.append(s["name"])
        return names

    def search(self, text: str, platform: Optional[str] = None, limit: int = 6) -> List[Dict]:
        """The best sections for the words, titles weighted highest; one hit per page."""
        words = re.findall(r"[A-Za-z0-9_]+", text)
        exact = {w.lower() for w in words}
        words = [w for w in words if w.lower() not in _STOP][:12]
        if not words:
            return []
        hits = self._search(words, " AND ".join(f'"{w}"' for w in words), exact, platform)
        if not hits and len(words) > 1:      # no page has every word: the best of any of them
            hits = self._search(words, " OR ".join(f'"{w}"' for w in words), exact, platform)
        out, seen = [], set()
        for h in hits:
            if (h["source"], h["path"]) not in seen:
                seen.add((h["source"], h["path"]))
                out.append(h)
            if len(out) >= limit:
                break
        for h in out:   # a page about exactly what was asked: show its opening, not just the matching part
            if h.get("exact"):
                h["heading"], h["text"] = "", self._opening(h["source"], h["path"]) or h["text"]
        return out

    def _opening(self, name: str, path: str, chars: int = 900) -> str:
        con = sqlite3.connect(self.path(name))
        try:
            rows = con.execute("select s.heading, s.body from sections s join pages p on p.id = s.page "
                               "where p.path = ? order by s.rowid limit 4", (path,)).fetchall()
        finally:
            con.close()
        text = "\n\n".join((f"## {h}\n" if h else "") + b for h, b in rows)
        return text[:chars]

    def _search(self, words, query, exact, platform) -> List[Dict]:
        hits = []
        for name in self._for(platform):
            con = sqlite3.connect(self.path(name))
            try:
                rows = con.execute(
                    "select p.title, p.path, s.heading, s.body, bm25(sections, 10.0, 3.0, 1.0) as score "
                    "from sections s join pages p on p.id = s.page where sections match ? order by score limit 40",
                    (query,)).fetchall()
            except sqlite3.Error:
                rows = []
            con.close()
            for title, path, heading, body, score in rows:
                # the page about exactly this name: "XInputGetState", "IDirect3DDevice9::Present Method"
                bare = re.sub(r" (Function|Method|Macro|Structure|Enumeration|Interface|Class|Property)$", "", title).lower()
                is_exact = (bare in exact or bare.split("::")[-1] in exact
                            or title.lower().replace(" ", "") == "".join(words).lower())
                if is_exact:
                    score -= 50
                hits.append({"source": name, "title": title, "path": path, "heading": heading, "text": body,
                             "score": score, "exact": is_exact})
        hits.sort(key=lambda h: h["score"])
        return hits

    def page(self, title_or_path: str, platform: Optional[str] = None) -> Optional[Dict]:
        """One page whole: by its title (exactly, any case) or its path."""
        for name in self._for(platform):
            con = sqlite3.connect(self.path(name))
            row = con.execute("select id, title, path from pages where lower(title) = lower(?) or path = ? limit 1",
                              (title_or_path, title_or_path)).fetchone()
            if row:
                secs = con.execute("select heading, body from sections where page = ? order by rowid", (row[0],)).fetchall()
                con.close()
                text = "\n\n".join((f"## {h}\n" if h else "") + b for h, b in secs)
                return {"source": name, "title": row[1], "path": row[2], "text": text}
            con.close()
        return None

    def status(self) -> List[Dict]:
        out = []
        for s in sources():
            entry = {"name": s["name"], "note": s["note"], "ready": self.ready(s["name"])}
            if entry["ready"]:
                con = sqlite3.connect(self.path(s["name"]))
                entry["pages"] = con.execute("select count(*) from pages").fetchone()[0]
                con.close()
            out.append(entry)
        return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        ws = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "agent-workspace")
        d = Docs(os.path.join(ws, "codekb"))
        for r in d.build_all(lambda n, i, t: print(f"  {n}: {i}/{t} pages", flush=True)):
            print(r)
        print(d.status())
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "search":
        plat = sys.argv[sys.argv.index("--platform") + 1] if "--platform" in sys.argv else None
        d = Docs(os.path.join(root, "agent-workspace", "codekb"))
        for h in d.search(sys.argv[2], plat):
            print(f"[{h['source']}] {h['title']} ({h['path']}) / {h['heading']}\n{h['text'][:400]}\n")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
