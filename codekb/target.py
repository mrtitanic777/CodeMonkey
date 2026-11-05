"""Working out what a project is built for, without asking.

Read from what is already there: project files (a .vcxproj's platforms, CMake's language standard,
-std= flags in a Makefile or compile_commands.json), the code (its includes and file types), and
words in the request ("for the original Xbox", "in C89"). A clue that names a platform settles the
rest through the platform table. When nothing points anywhere, the target is unknown and code is
checked against everything, with findings rather than refusals.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from .platforms import PLATFORMS

_STD = re.compile(r"-std[=:]\s*(?:gnu|c)(\+\+)?(\d\d|0x|1y|1z|2a|2b|2c)\b", re.I)
_STD_ALIAS = {"0x": "11", "1y": "14", "1z": "17", "2a": "20", "2b": "23", "2c": "26"}
_CPP_STD = {"98": "c++98", "03": "c++03", "11": "c++11", "14": "c++14", "17": "c++17", "20": "c++20", "23": "c++23"}
_C_STD = {"89": "c89", "90": "c89", "99": "c99", "11": "c11", "17": "c17", "18": "c17", "23": "c23"}


class Target(dict):
    """{platform, c, cpp, evidence: [why]} — platform and versions may be None (unknown)."""


def _std_from_flags(text: str, found: Dict) -> None:
    for m in _STD.finditer(text):
        n = _STD_ALIAS.get(m.group(2), m.group(2))
        if m.group(1):
            found.setdefault("cpp", (_CPP_STD.get(n), f"-std=c++{n}"))
        else:
            found.setdefault("c", (_C_STD.get(n), f"-std=c{n}"))


def detect(project_dir: str, code: str = "", path: str = "", words: str = "") -> Target:
    found: Dict = {}
    evidence: List[str] = []
    # 1. words in the request win: they are what the person said
    low = words.lower()
    for key, spec in PLATFORMS.items():
        if spec["name"].lower() in low or (key == "xbox" and "original xbox" in low):
            found.setdefault("platform", (key, f"the request says {spec['name']}"))
    m = re.search(r"\bc\+\+\s?(98|03|11|14|17|20|23)\b|\bc(89|90|99|11|17|23)\b", low)
    if m:
        if m.group(1):
            found.setdefault("cpp", (_CPP_STD[m.group(1)], f"the request says C++{m.group(1)}"))
        else:
            found.setdefault("c", (_C_STD[m.group(2)], f"the request says C{m.group(2)}"))
    # 2. project files
    # the project files in the file's own folder first (a subproject), then the project's
    folders = []
    if path:
        here = os.path.dirname(os.path.join(project_dir, path))
        if os.path.abspath(here) != os.path.abspath(project_dir):
            folders.append(here)
    folders.append(project_dir)
    entries = []
    for folder in folders:
        try:
            entries += [os.path.join(folder, n) for n in os.listdir(folder)]
        except OSError:
            pass
    for full in entries:
        name = os.path.basename(full)
        low_name = name.lower()
        try:
            if low_name.endswith((".vcxproj", ".vcproj")):
                text = open(full, encoding="utf-8", errors="replace").read()
                plats = set(re.findall(r"\|([^'\"<]+?)['\"<]", text)) | set(re.findall(r"<Platform>([^<]+)</Platform>", text))
                for key, spec in PLATFORMS.items():
                    if any(p.strip() in spec["project_platforms"] for p in plats):
                        found.setdefault("platform", (key, f"{name} builds for {spec['name']}"))
                m = re.search(r"<LanguageStandard>stdcpp(\d\d|latest)</LanguageStandard>", text)
                if m and m.group(1) != "latest":
                    found.setdefault("cpp", (_CPP_STD.get(m.group(1)), f"{name}: LanguageStandard stdcpp{m.group(1)}"))
            elif low_name == "cmakelists.txt":
                text = open(full, encoding="utf-8", errors="replace").read()
                m = re.search(r"CMAKE_CXX_STANDARD\s+(\d\d)", text)
                if m:
                    found.setdefault("cpp", (_CPP_STD.get(m.group(1)), f"CMakeLists.txt: CMAKE_CXX_STANDARD {m.group(1)}"))
                m = re.search(r"CMAKE_C_STANDARD\s+(\d\d)", text)
                if m:
                    found.setdefault("c", (_C_STD.get(m.group(1)), f"CMakeLists.txt: CMAKE_C_STANDARD {m.group(1)}"))
                _std_from_flags(text, found)
            elif low_name in ("makefile", "gnumakefile") or low_name.endswith(".mk"):
                _std_from_flags(open(full, encoding="utf-8", errors="replace").read(), found)
            elif low_name == "compile_commands.json":
                for entry in json.load(open(full, encoding="utf-8"))[:50]:
                    _std_from_flags(entry.get("command", "") + " " + " ".join(entry.get("arguments", [])), found)
        except (OSError, ValueError):
            continue
    # 3. the code: includes that only one platform has
    includes = [i.lower() for i in re.findall(r'#\s*include\s*[<"]([^>"]+)[>"]', code)]
    for key, spec in PLATFORMS.items():
        hit = [i for i in includes if i in spec["includes"]]
        if hit and "platform" not in found:
            if key == "xbox" and os.environ.get("XEDK"):
                continue   # xtl.h on a machine with the Xbox 360 SDK: the 360, unless something says otherwise
            found["platform"] = (key, f"the code includes <{hit[0]}>")
    why: Dict[str, str] = {}
    target = Target(platform=None, c=None, cpp=None, evidence=evidence, why=why)
    for k in ("platform", "c", "cpp"):
        if k in found and found[k][0]:
            target[k] = found[k][0]
            evidence.append(found[k][1])
            why[k] = found[k][1]
    # a platform with one language version settles it when nothing else did
    if target["platform"]:
        spec = PLATFORMS[target["platform"]]
        if target["c"] is None and len(spec["c"]) == 1:
            target["c"] = spec["c"][0]
            evidence.append(f"{spec['name']}'s compiler takes {spec['c'][0]}")
            why["c"] = evidence[-1]
        if target["cpp"] is None and spec["cpp"][-1] in ("c++03",):
            target["cpp"] = "c++03"
            evidence.append(f"{spec['name']}'s compiler is C++03 with some C++11 (see its table)")
            why["cpp"] = evidence[-1]
        if spec.get("library_cpp") and target["cpp"] == spec["cpp"][-1]:
            target["library_cpp"] = spec["library_cpp"]   # what its own library offers beyond the language
    target["language"] = language_of(path, code)
    return target


def language_of(path: str, code: str = "") -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".c":
        return "c"
    if ext in (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".inl"):
        return "c++"
    if ext == ".h":
        return "c++" if re.search(r"\b(class|namespace|template|public:|private:)\b", code) else "c"
    if ext in (".asm", ".s", ".inc", ".nasm"):
        return "asm"
    return None
