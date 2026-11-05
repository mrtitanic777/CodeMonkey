# codekb: what the C and C++ on this computer actually declare

CodeMonkey checks C and C++ against the SDK headers already installed here, so a model cannot call a
function that does not exist, or use a C++14 name in a C++03 project, without being told.

## Packs

One SQLite file per SDK or library, built from its headers (`kb.sources()` finds them):
xdk (Xbox 360 SDK), winsdk, msvc, vc10 (Visual C++ 2010), dxsdk (DirectX June 2010), libstdcxx and
mingw (MSYS2's GCC). They live in the workspace's `codekb/` folder (about 240 MB), are built in the
background when the app starts, and are rebuilt only when a header changes (a check of a second or
two). The old pack stays readable while a new one is built.

Each pack holds `name_list` (each name once), `files`, and `symbols` (name, kind, file, line,
signature, and two bitmasks: which C and which C++ versions see it). A Bloom filter in memory answers
"not here" without touching the disk; an FTS5 trigram index and an edit distance give near names
for a misspelling.

## Versions

A declaration's versions come from the preprocessor conditions around it. `__cplusplus` and
`__STDC_VERSION__` comparisons are read directly; feature-test macros (`_HAS_CXX17`,
`__cpp_lib_make_unique`, ...) are learned from the library's own `yvals_core.h`, `version` and
`bits/version.h`, so a pack knows what its library says, not what a table says.

## Targets

`target.detect()` reads, in order: the request's words ("for the original Xbox", "in C89"), the
project files (.vcxproj platforms and LanguageStandard, CMake, -std= flags in Makefiles and
compile_commands.json), then includes only one platform has. A platform settles the rest through
`platforms.py` (CPU, endianness, compiler, language versions, which C++11 features its compiler had
early). With nothing to go on, the target is unknown and every pack is used. Nothing asks.

## Checking

`check.check()` returns findings in the kernel's states, each with a proof `verify()` can re-check
from the header file. `compile_check()` runs the target's compiler with syntax only. In CodeMonkey
(webui/codeknow.py): notes on an edit (only what the edit brought in), the compiler's errors after
it is written, and the agent's `code_lookup` tool. Settings > "Check C and C++ against your SDKs" and
"Compile C and C++ after each change" switch them off.

## Not covered

Members declared inside a class body are not indexed (namespace-scope names, out-of-class
definitions, types, macros, enumerators are). Assembly has no pack yet; NASM is installed and could
judge it the way the compiler judges C.
