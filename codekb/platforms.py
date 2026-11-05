"""Platforms: what each target is, so one clue (an include, a project setting) settles the rest.

Each entry: the CPU and its byte order, the compiler(s), which language versions those compilers
accept (with the C++11 features an older compiler did and did not have), the SDK packs that hold its
headers, and the clues that point to it. Facts here are CodeMonkey's own table, written from the
vendors' documentation; anything a compiler on this computer can check is checked by it instead.
"""

PLATFORMS = {
    "xbox360": {
        "name": "Xbox 360",
        "cpu": "PowerPC Xenon: 3 cores, 2 threads each, VMX128 vector units",
        "endian": "big",
        "pointer_bits": 32,
        "compiler": "Xbox 360 SDK cl 16.00 for PowerPC (Visual C++ 2010 generation)",
        "c": ["c89"],                 # Visual C++ 2010's C: C89 with // comments; no mixed declarations
        "c_extras": ["line comments"],
        "cpp": ["c++98", "c++03"],
        # its library is Visual C++ 2010's, whose C++0x parts (_HAS_CPP0X) are on by default:
        # shrink_to_fit, emplace_back, unique_ptr, std::move are there although the language is C++03
        "library_cpp": "c++11",
        "cpp11_has": ["auto", "lambda", "decltype", "nullptr", "static_assert", "rvalue references",
                      "trailing return types"],
        "cpp11_lacks": ["range-based for", "enum class", "variadic templates", "initializer lists",
                        "constexpr", "noexcept", "delegating constructors", "= default / = delete",
                        "override and final keywords", "alias templates (using X = ...)", "thread_local"],
        "packs": ["xdk"],
        "defines": ["_XBOX", "XBOX", "_M_PPCBE"],
        "includes": ["xtl.h", "xboxmath.h", "xbdm.h", "xgraphics.h", "xaudio2.h", "xbox.h"],
        "project_platforms": ["Xbox 360"],
    },
    "xbox": {
        "name": "Xbox (original)",
        "cpu": "Intel Pentium III (Coppermine) 733 MHz, x86",
        "endian": "little",
        "pointer_bits": 32,
        "compiler": "Visual C++ .NET 2003 (7.1) with the Xbox Development Kit",
        "c": ["c89"],
        "cpp": ["c++98", "c++03"],
        "cpp11_has": [],
        "cpp11_lacks": ["every C++11 feature"],
        "packs": ["xdk-original"],
        "defines": ["_XBOX"],
        "includes": ["xtl.h", "xgraphics.h", "xbdm.h"],
        "project_platforms": ["Xbox"],
    },
    "win32": {
        "name": "Windows x86 (32-bit)",
        "cpu": "x86",
        "endian": "little",
        "pointer_bits": 32,
        "compiler": "Visual C++ (cl), Clang, or MinGW GCC",
        "c": ["c89", "c99", "c11", "c17", "c23"],
        "cpp": ["c++98", "c++03", "c++11", "c++14", "c++17", "c++20", "c++23"],
        "packs": ["winsdk", "msvc", "libstdcxx"],
        "defines": ["_WIN32", "WIN32"],
        "includes": ["windows.h", "winbase.h", "winuser.h"],
        "project_platforms": ["Win32"],
    },
    "win64": {
        "name": "Windows x64",
        "cpu": "x86-64",
        "endian": "little",
        "pointer_bits": 64,
        "compiler": "Visual C++ (cl), Clang, or MinGW-w64 GCC",
        "c": ["c89", "c99", "c11", "c17", "c23"],
        "cpp": ["c++98", "c++03", "c++11", "c++14", "c++17", "c++20", "c++23"],
        "packs": ["winsdk", "msvc", "libstdcxx"],
        "defines": ["_WIN64"],
        "includes": ["windows.h"],
        "project_platforms": ["x64"],
    },
    "posix": {
        "name": "Linux / POSIX",
        "cpu": "x86-64 or ARM64",
        "endian": "little",
        "pointer_bits": 64,
        "compiler": "GCC or Clang",
        "c": ["c89", "c99", "c11", "c17", "c23"],
        "cpp": ["c++98", "c++03", "c++11", "c++14", "c++17", "c++20", "c++23"],
        "packs": ["libstdcxx"],
        "defines": ["__linux__", "__unix__"],
        "includes": ["unistd.h", "pthread.h", "sys/types.h"],
        "project_platforms": [],
    },
}


def describe(key):
    p = PLATFORMS[key]
    lines = [f"{p['name']}: {p['cpu']}, {p['endian']}-endian, {p['pointer_bits']}-bit pointers",
             f"compiler: {p['compiler']}",
             f"C: {', '.join(p['c'])}; C++: {', '.join(p['cpp'])}"]
    if p.get("cpp11_has"):
        lines.append("C++11 features it has: " + ", ".join(p["cpp11_has"]))
    if p.get("cpp11_lacks"):
        lines.append("C++11 features it lacks: " + ", ".join(p["cpp11_lacks"]))
    return "\n".join(lines)
