"""Packs: one SQLite file per language, library or SDK, with the indexes lookups need.

    symbols   every declaration: name, kind, header, line, signature, two version masks (one bit
              per C version, one per C++ version: which versions see it), and for a class member
              (method, field, member_type) the class that owns it
    names     the distinct names in a trigram index, for "did you mean" on a near-miss
    files     every header read, with its size and time, so a pack is rebuilt only when one changes
    meta      what the pack is (its name, its source folder, how it was built)

Indexes: name (exact lookup), header (what a header declares, which header declares a name), and in
memory a Bloom filter of every name, so the common answer "not in this pack" needs no disk read.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Tuple

from .headers import ALL_C, ALL_CPP, C_VERSIONS, CPP_VERSIONS, collect_features, header_files, scan_file, version_headers

# headers and names are stored once and referred to by number; the trigram index keeps no copy of
# the names (external content), so a pack is about the size of what it says, not three times that
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, size INTEGER, mtime REAL);
CREATE TABLE IF NOT EXISTS name_list(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS symbols(
    name_id INTEGER NOT NULL, kind TEXT, file_id INTEGER, line INTEGER, signature TEXT,
    c_mask INTEGER, cpp_mask INTEGER, owner_id INTEGER);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name_id);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS symbols_owner ON symbols(owner_id);
CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(name, tokenize='trigram', content='name_list', content_rowid='id');
"""
KINDS_WITH_SIGNATURE = ("function", "type", "concept", "method")
FORMAT = "2"   # 2: class members, each with the class that owns it


class Bloom:
    """A Bloom filter: 'certainly not here' without a disk read (about 1 MB per million names)."""

    def __init__(self, count: int, bits_per_item: int = 10, hashes: int = 7):
        self.size = max(64, count * bits_per_item)
        self.hashes = hashes
        self.bits = bytearray(self.size // 8 + 1)

    def _spots(self, item: str):
        h = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        a, b = int.from_bytes(h[:8], "little"), int.from_bytes(h[8:], "little")
        return ((a + i * b) % self.size for i in range(self.hashes))

    def add(self, item: str) -> None:
        for s in self._spots(item):
            self.bits[s >> 3] |= 1 << (s & 7)

    def __contains__(self, item: str) -> bool:
        return all(self.bits[s >> 3] & (1 << (s & 7)) for s in self._spots(item))


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca.lower() != cb.lower()))
        prev = cur
    return prev[-1]


class Pack:
    """One pack, opened read-only for lookups."""

    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.meta = dict(self.db.execute("SELECT key, value FROM meta"))
        self.name = self.meta.get("name", os.path.splitext(os.path.basename(path))[0])
        names = [r[0] for r in self.db.execute("SELECT name FROM name_list")]
        self.bloom = Bloom(len(names))
        for n in names:
            self.bloom.add(n)
        self.count = len(names)

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    _COLUMNS = ("name", "kind", "header", "line", "signature", "c_mask", "cpp_mask", "owner")
    _SELECT = ("SELECT n.name, s.kind, f.path, s.line, s.signature, s.c_mask, s.cpp_mask, o.name FROM name_list n "
               "JOIN symbols s ON s.name_id = n.id JOIN files f ON f.id = s.file_id "
               "LEFT JOIN name_list o ON o.id = s.owner_id ")

    def _rows(self, where: str, args: tuple, limit: int = 20) -> List[Dict]:
        rows = self.db.execute(self._SELECT + where + f" LIMIT {int(limit)}", args)
        return [dict(zip(self._COLUMNS, r), pack=self.name) for r in rows]

    def lookup(self, name: str, members: bool = False) -> List[Dict]:
        """Declarations of a name at namespace level (members=False) or as a class member (True)."""
        if name not in self.bloom:
            return []
        return self._rows("WHERE n.name = ? AND s.owner_id IS " + ("NOT NULL" if members else "NULL"), (name,))

    def member(self, owner: str, name: str) -> List[Dict]:
        """A member of one class."""
        if name not in self.bloom or owner not in self.bloom:
            return []
        return self._rows("WHERE n.name = ? AND o.name = ?", (name, owner))

    def search(self, text: str, limit: int = 60) -> List[Dict]:
        """Declarations whose name contains `text` (any case): the trigram index for three letters or
        more, a scan for fewer. One row per name, namespace level first."""
        text = text.strip()
        if not text:
            return []
        if len(text) >= 3:
            ids = [r[0] for r in self.db.execute(
                "SELECT rowid FROM names WHERE names MATCH ? LIMIT ?", ('"' + text.replace('"', '""') + '"', limit * 4))]
        else:
            ids = [r[0] for r in self.db.execute("SELECT id FROM name_list WHERE name LIKE ? LIMIT ?",
                                                 ("%" + text + "%", limit * 4))]
        out = []
        for nid in ids:
            rows = self._rows("WHERE n.id = ? ORDER BY s.owner_id IS NOT NULL", (nid,), limit=1)
            if rows:
                out.append(rows[0])
        out.sort(key=lambda r: (r["owner"] is not None, len(r["name"]), r["name"]))
        return out[:limit]

    def members_of(self, owner: str, limit: int = 400) -> List[str]:
        if owner not in self.bloom:
            return []
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT n.name FROM symbols s JOIN name_list n ON n.id = s.name_id "
            "JOIN name_list o ON o.id = s.owner_id WHERE o.name = ? LIMIT ?", (owner, limit))]

    def near(self, name: str, limit: int = 3, members: Optional[bool] = None) -> List[str]:
        """Names a few edits away: what a misremembered name probably meant. members=False keeps names
        declared at namespace level, True names declared in a class, None either."""
        found = self._near(name, limit * 4 if members is not None else limit)
        if members is None:
            return found
        test = "NOT NULL" if members else "NULL"
        return [n for n in found if self.db.execute(
            "SELECT 1 FROM symbols s JOIN name_list n ON n.id = s.name_id WHERE n.name = ? AND s.owner_id IS "
            + test + " LIMIT 1", (n,)).fetchone()][:limit]

    def _near(self, name: str, limit: int) -> List[str]:
        if len(name) < 4:
            return []
        grams = {name[i:i + 3] for i in range(len(name) - 2)}
        query = " OR ".join('"' + g.replace('"', '""') + '"' for g in list(grams)[:12])
        try:
            # ranked by how many pieces they share (bm25), so the likely meaning is among the first
            rows = [r[0] for r in self.db.execute(
                "SELECT n.name FROM names JOIN name_list n ON n.id = names.rowid WHERE names MATCH ? "
                "ORDER BY rank LIMIT 200", (query,))]
        except sqlite3.OperationalError:
            return []
        allow = max(2, len(name) // 4)   # longer names tolerate a few more slips
        def shared_start(n):
            k = 0
            while k < min(len(n), len(name)) and n[k].lower() == name[k].lower():
                k += 1
            return k
        scored = [(d, n) for n in rows for d in [_edit_distance(name, n, allow)] if 0 < d <= allow]
        scored = sorted(scored, key=lambda dn: (dn[0], -shared_start(dn[1]), dn[1]))
        # a name that is the start of the other (XMemAllocate / XMemAlloc) is a likely meaning too
        prefix = sorted((abs(len(n) - len(name)), n) for n in rows
                        if n != name and min(len(n), len(name)) >= 6 and (n.startswith(name) or name.startswith(n)))
        out = []
        for _, n in scored + prefix:
            if n not in out:
                out.append(n)
        return out[:limit]

    def first_version(self, row: Dict, language: str = "c++") -> Optional[str]:
        """The first language version a declaration is visible in (no older than the pack's oldest mode)."""
        versions = CPP_VERSIONS if language == "c++" else C_VERSIONS
        mask = row["cpp_mask"] if language == "c++" else row["c_mask"]
        seen = versions_of(mask, versions)
        if not seen:
            return None
        floor = self.meta.get("cpp_floor") if language == "c++" else None
        if floor and versions.index(seen[0]) < versions.index(floor):
            return floor
        return seen[0]

    def declares(self, header: str, limit: int = 200) -> List[str]:
        """What a header declares (matched on the end of its path)."""
        rows = self.db.execute(
            "SELECT DISTINCT n.name FROM files f JOIN symbols s ON s.file_id = f.id JOIN name_list n ON n.id = s.name_id "
            "WHERE f.path LIKE ? LIMIT ?", ("%" + header.replace("\\", "/").lstrip("/"), limit))
        return [r[0] for r in rows]

    def has_header(self, header: str) -> Optional[str]:
        h = header.replace("\\", "/").lstrip("/").lower()
        for (path,) in self.db.execute("SELECT path FROM files"):   # a few thousand rows at most
            p = path.replace("\\", "/").lower()
            if p == h or p.endswith("/" + h):
                return path
        return None


def build(path: str, name: str, roots: Iterable[str], language: str = "c/c++", note: str = "",
          progress=None, before_swap=None) -> Dict:
    """Build (or rebuild) a pack from header folders. Rebuilds only if a header changed.

    The old pack stays readable while the new one is built beside it; before_swap() is called just
    before it is replaced, so whoever has it open can close it (Windows will not replace an open file)."""
    roots = [r for r in roots if os.path.isdir(r)]
    pairs = [(f, os.path.relpath(f, r).replace("\\", "/")) for r in roots for f in header_files(r)]
    files = [f for f, _ in pairs]
    rel_of = dict(pairs)
    stamp = {rel_of[f]: (os.path.getsize(f), os.path.getmtime(f)) for f in files}
    if os.path.exists(path):
        try:
            old = sqlite3.connect(path)
            have = {p: (s, m) for p, s, m in old.execute("SELECT path, size, mtime FROM files")} \
                if old.execute("SELECT 1 FROM sqlite_master WHERE name='name_list'").fetchone() else {}
            old.close()
            fmt = old_format(path)
            if have == stamp and fmt == FORMAT:
                return {"pack": name, "rebuilt": False, "files": len(files)}
        except sqlite3.Error:
            pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    t0 = time.time()
    tmp = path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    name_ids: Dict[str, int] = {}
    count = 0
    # the library's feature-test macros first, so `#ifdef __cpp_lib_optional` reads as "C++17 and later"
    features = collect_features(version_headers(roots))
    for i, f in enumerate(files):
        rel = rel_of[f]
        file_id = i + 1
        db.execute("INSERT INTO files VALUES (?,?,?,?)", (file_id, rel, *stamp[rel]))
        seen = set()
        rows = []
        for n, k, ln, sig, c, cpp, owner in scan_file(f, features):
            if (n, k, ln, owner) in seen:
                continue
            seen.add((n, k, ln, owner))
            nid = name_ids.get(n)
            if nid is None:
                nid = name_ids[n] = len(name_ids) + 1
            oid = None
            if owner:
                oid = name_ids.get(owner)
                if oid is None:
                    oid = name_ids[owner] = len(name_ids) + 1
            rows.append((nid, k, file_id, ln, sig[:200] if k in KINDS_WITH_SIGNATURE else None, c, cpp, oid))
        db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)", rows)
        count += len(rows)
        if progress and i % 200 == 0:
            progress(i, len(files))
    db.executemany("INSERT INTO name_list VALUES (?,?)", ((i, n) for n, i in name_ids.items()))
    db.execute("INSERT INTO names(names) VALUES ('rebuild')")   # the trigram index over name_list
    meta = {"name": name, "language": language, "roots": json.dumps(roots), "note": note,
            "c_versions": json.dumps(C_VERSIONS), "cpp_versions": json.dumps(CPP_VERSIONS),
            "built": time.strftime("%Y-%m-%d %H:%M:%S"), "declarations": str(count), "names": str(len(name_ids)),
            "feature_macros": str(len(features)), "format": FORMAT}
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    if before_swap:
        before_swap()
    os.replace(tmp, path)
    return {"pack": name, "rebuilt": True, "files": len(files), "declarations": count, "names": len(name_ids),
            "seconds": round(time.time() - t0, 1), "bytes": os.path.getsize(path)}  # noqa: E501


def old_format(path: str) -> Optional[str]:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        finally:
            db.close()
        return row[0] if row else "1"
    except sqlite3.Error:
        return None


def versions_of(mask: int, versions: Tuple[str, ...]) -> List[str]:
    return [v for i, v in enumerate(versions) if mask & (1 << i)]


__all__ = ["Pack", "build", "Bloom", "versions_of", "ALL_C", "ALL_CPP"]
