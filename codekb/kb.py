"""The knowledge base: the packs this computer has, built from its own SDKs and compilers' headers.

Sources are found on this computer (nothing is downloaded): the Xbox 360 SDK, the Windows SDK, Visual
C++'s headers (current and 2010), the DirectX SDK, MinGW-w64 GCC's C and C++ headers. Each becomes a
pack, built once and rebuilt only when one of its headers changes.
"""
from __future__ import annotations

import glob
import os
import threading
from typing import Dict, List, Optional

from .store import Pack, build

PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
PF = os.environ.get("ProgramFiles", r"C:\Program Files")


def _latest(pattern: str) -> Optional[str]:
    found = sorted(glob.glob(pattern), key=lambda p: [int(x) if x.isdigit() else x for x in
                                                      os.path.basename(p).replace("-", ".").split(".")])
    return found[-1] if found else None


def sources() -> List[Dict]:
    """Every header source on this computer: {name, roots, platforms, note}."""
    out = []
    xedk = os.environ.get("XEDK") or os.path.join(PF86, "Microsoft Xbox 360 SDK")
    if os.path.isdir(os.path.join(xedk, "include", "xbox")):
        out.append({"name": "xdk", "roots": [os.path.join(xedk, "include", "xbox")], "platforms": ["xbox360"],
                    "note": "Xbox 360 SDK (its C runtime, C++ library, Direct3D, XAudio2, system APIs)",
                    "examples": [os.path.join(xedk, "Source", "Samples")]})
    kit = _latest(os.path.join(PF86, "Windows Kits", "10", "Include", "10.*"))
    if kit:
        out.append({"name": "winsdk", "roots": [os.path.join(kit, d) for d in ("um", "shared", "ucrt")],
                    "platforms": ["win32", "win64"], "note": f"Windows SDK {os.path.basename(kit)} (user mode, shared, C runtime)"})
    msvc = _latest(os.path.join(PF, "Microsoft Visual Studio", "*", "*", "VC", "Tools", "MSVC", "*"))
    if msvc and os.path.isdir(os.path.join(msvc, "include")):
        out.append({"name": "msvc", "roots": [os.path.join(msvc, "include")], "platforms": ["win32", "win64"],
                    "note": f"Visual C++ {os.path.basename(msvc)} (C++ standard library, MSVC headers)",
                    "cpp_floor": "c++14"})   # its oldest mode: /std:c++14
    vc10 = os.path.join(PF86, "Microsoft Visual Studio 10.0", "VC", "include")
    if os.path.isdir(vc10):
        out.append({"name": "vc10", "roots": [vc10], "platforms": ["win32-vc2010"],
                    "note": "Visual C++ 2010 (its C runtime and C++ library)"})
    dx = os.environ.get("DXSDK_DIR")
    if dx and os.path.isdir(os.path.join(dx, "Include")):
        out.append({"name": "dxsdk", "roots": [os.path.join(dx, "Include")], "platforms": ["win32", "win64"],
                    "note": "DirectX SDK (June 2010)", "examples": [os.path.join(dx, "Samples", "C++")]})
    for root in (r"C:\msys64\mingw64", r"C:\msys64\ucrt64", r"C:\mingw64"):
        cxx = _latest(os.path.join(root, "include", "c++", "*"))
        if cxx:
            out.append({"name": "libstdcxx", "roots": [cxx], "platforms": ["win32", "win64", "posix"],
                        "note": f"GCC's C++ standard library {os.path.basename(cxx)} ({root})"})
            out.append({"name": "mingw", "roots": [os.path.join(root, "include")], "platforms": ["win32", "win64", "posix"],
                        "note": f"MinGW-w64 C runtime and headers ({root})", "skip": ["c++"]})
            break
    from .asm import assemblers   # assembly: one pack per assembler, asked of the assembler itself
    out += assemblers()
    return out


class KnowledgeBase:
    """The packs, opened on demand; built in the background the first time they are needed."""

    def __init__(self, home: str):
        self.home = home
        self.sources = {s["name"]: s for s in sources()}
        self._packs: Dict[str, Pack] = {}
        self._lock = threading.Lock()
        self.building: Dict[str, str] = {}   # pack -> what it is doing

    def path(self, name: str) -> str:
        return os.path.join(self.home, name + ".db")

    def ready(self, name: str) -> bool:
        return os.path.exists(self.path(name))

    def build(self, name: str) -> Dict:
        s = self.sources[name]
        if s.get("kind") == "asm":
            from .asm import build_pack
            result = build_pack(self.path(name), s, before_swap=lambda: self._close(name))
            self._close(name)
            return result
        roots = s["roots"]
        if s.get("skip"):   # a folder whose subfolders are their own pack
            from . import headers as _h
            original = _h.header_files

            def filtered(root, *a, **k):
                for f in original(root, *a, **k):
                    rel = os.path.relpath(f, root).replace("\\", "/")
                    if not any(rel.startswith(x + "/") for x in s["skip"]):
                        yield f
            import codekb.store as _store
            _store.header_files, saved = filtered, _store.header_files
            try:
                result = build(self.path(name), name, roots, note=s["note"], before_swap=lambda: self._close(name))
            finally:
                _store.header_files = saved
        else:
            result = build(self.path(name), name, roots, note=s["note"], before_swap=lambda: self._close(name))
        if s.get("cpp_floor"):
            import sqlite3
            db = sqlite3.connect(self.path(name))
            try:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('cpp_floor', ?)", (s["cpp_floor"],))
                db.commit()
            finally:
                db.close()   # a locked or half-written pack must not leave the file held open
        self._close(name)
        return result

    def _close(self, name: str) -> None:
        with self._lock:
            p = self._packs.pop(name, None)
        if p is not None:
            p.close()

    def build_all(self, names: Optional[List[str]] = None, progress=None) -> List[Dict]:
        out = []
        for name in names or list(self.sources):
            self.building[name] = "building"
            try:
                out.append(self.build(name))
            finally:
                self.building.pop(name, None)
            if progress:
                progress(name, out[-1])
        return out

    def pack(self, name: str) -> Optional[Pack]:
        with self._lock:
            if name in self._packs:
                return self._packs[name]
            if not self.ready(name):
                return None
            p = self._packs[name] = Pack(self.path(name))
            return p

    def packs_for(self, platform: Optional[str], kind: str = "c") -> List[Pack]:
        """The packs a platform's code may use (C and C++ headers, or kind="asm" for assemblers); every
        ready pack of that kind when the platform is unknown."""
        names = [n for n, s in self.sources.items() if s.get("kind", "c") == kind
                 and (platform is None or platform in s["platforms"])]
        return [p for p in (self.pack(n) for n in names) if p is not None]

    def status(self) -> List[Dict]:
        out = []
        for name, s in self.sources.items():
            p = self.pack(name)
            out.append({"pack": name, "note": s["note"], "platforms": s["platforms"], "ready": p is not None,
                        "names": p.count if p else 0, "building": name in self.building})
        return out
