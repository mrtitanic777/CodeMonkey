"""Tests for project context packs (context/packs.py).

Run from the project root:
    python tests/test_packs.py

Stdlib only; builds fixture trees in a temp dir, no network.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from context import packs  # noqa: E402
from context.packs import (  # noqa: E402
    build_pack,
    load_pack,
    pack_stats,
    render_prefix,
)

MAIN_PY = '''"""Main entry point."""

class App:
    """The app."""

    def run(self):
        """Run it."""
        pass


def helper():
    pass
'''

EXPECTED_FILES = {
    ".gitignore",
    "main.py",
    "no_doc.py",
    "plain.txt",
    "script.sh",
    os.path.join("sub", "nested.py").replace(os.sep, "/"),
}


def make_fixture():
    tmp = tempfile.mkdtemp(prefix="packtest_")
    proj = os.path.join(tmp, "proj")
    cache = os.path.join(tmp, "cache")
    os.makedirs(os.path.join(proj, "sub"))
    os.makedirs(os.path.join(proj, "ignored"))
    files = {
        "main.py": MAIN_PY,
        "no_doc.py": "# does things\nx = 1\n",
        "plain.txt": "Just some text\nsecond line\n",
        "script.sh": "#!/bin/bash\n#\n# Deploy script\necho deploy\n",
        ".gitignore": "ignored/*\n*.log\n",
        "debug.log": "noise\n",
        "ignored/skip.py": "x = 1\n",
        "sub/nested.py": '"""Nested module."""\n',
    }
    for rel, content in files.items():
        path = os.path.join(proj, *rel.split("/"))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    with open(os.path.join(proj, "binary.dat"), "wb") as fh:
        fh.write(b"\x00\x01\x02binary-bytes")
    return tmp, proj, cache


def by_path(pack):
    return {f["path"]: f for f in pack["files"]}


def test_pack_builds_over_fixture():
    tmp, proj, cache = make_fixture()
    pack = build_pack(proj, cache)
    assert pack["version"] == 1, pack
    assert pack["project_dir"] == os.path.abspath(proj)
    got = set(by_path(pack))
    assert got == EXPECTED_FILES, got
    print("PASS test_pack_builds_over_fixture")


def test_summaries_extracted_correctly():
    tmp, proj, cache = make_fixture()
    entries = by_path(build_pack(proj, cache))
    # Python: module docstring first line
    assert entries["main.py"]["summary"] == "Main entry point.", entries["main.py"]
    # Python without docstring/symbols: first comment line
    assert entries["no_doc.py"]["summary"] == "does things", entries["no_doc.py"]
    # Plain text: first non-empty line
    assert entries["plain.txt"]["summary"] == "Just some text", entries["plain.txt"]
    # Shell: shebang skipped, empty comment skipped
    assert entries["script.sh"]["summary"] == "Deploy script", entries["script.sh"]
    # Nested module docstring
    nested = os.path.join("sub", "nested.py").replace(os.sep, "/")
    assert entries[nested]["summary"] == "Nested module.", entries[nested]
    print("PASS test_summaries_extracted_correctly")


def test_symbol_index_correct():
    tmp, proj, cache = make_fixture()
    entries = by_path(build_pack(proj, cache))
    syms = entries["main.py"]["symbols"]
    got = [(s["kind"], s["name"], s["line"]) for s in syms]
    assert got == [
        ("class", "App", 3),
        ("function", "App.run", 6),
        ("function", "helper", 11),
    ], got
    assert syms[0]["doc"] == "The app.", syms[0]
    assert syms[1]["doc"] == "Run it.", syms[1]
    # non-python files have no symbols
    assert entries["plain.txt"]["symbols"] == []
    print("PASS test_symbol_index_correct")


def test_render_prefix_byte_stable():
    tmp, proj, cache = make_fixture()
    first = render_prefix(build_pack(proj, cache))
    second = render_prefix(build_pack(proj, cache))
    assert first == second, "prefix must be byte-identical across builds"
    assert "## File tree" in first and "## Symbols" in first
    assert "built_at" not in first, "no timestamps in the render"
    assert "Main entry point." in first
    assert "main.py: App, App.run, helper" in first, first
    print("PASS test_render_prefix_byte_stable")


def test_mtime_invalidation_rescans_only_changed():
    tmp, proj, cache = make_fixture()
    pack1 = build_pack(proj, cache)
    stats1 = pack_stats(pack1)
    assert stats1["cache_hits"] == 0, stats1
    assert stats1["cache_misses"] == len(EXPECTED_FILES), stats1

    pack2 = build_pack(proj, cache)
    stats2 = pack_stats(pack2)
    assert stats2["cache_misses"] == 0, stats2
    assert stats2["cache_hits"] == len(EXPECTED_FILES), stats2

    # change one file (content + mtime)
    main_py = os.path.join(proj, "main.py")
    with open(main_py, "w", encoding="utf-8") as fh:
        fh.write('"""Changed entry point, longer text."""\n')
    pack3 = build_pack(proj, cache)
    stats3 = pack_stats(pack3)
    assert stats3["cache_misses"] == 1, stats3
    assert stats3["cache_hits"] == len(EXPECTED_FILES) - 1, stats3
    entries = by_path(pack3)
    assert entries["main.py"]["summary"] == "Changed entry point, longer text.", (
        entries["main.py"]
    )
    # unchanged file kept its cached entry object
    assert by_path(pack2)["plain.txt"]["summary"] == "Just some text"
    print("PASS test_mtime_invalidation_rescans_only_changed")


def test_cache_round_trips_as_json():
    tmp, proj, cache = make_fixture()
    pack = build_pack(proj, cache)
    cache_files = os.listdir(cache)
    assert len(cache_files) == 1 and cache_files[0].endswith(".json"), cache_files
    with open(os.path.join(cache, cache_files[0]), encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["version"] == 1
    assert "_cache_hits" not in raw, "ephemeral keys must not be serialized"
    # load_pack on a fresh project returns the cached pack
    pack2 = load_pack(proj, cache)
    assert pack_stats(pack2)["cache_misses"] == 0
    assert render_prefix(pack) == render_prefix(pack2)
    print("PASS test_cache_round_trips_as_json")


def test_binary_and_oversize_skipped():
    tmp, proj, cache = make_fixture()
    old_cap = packs.DEFAULT_MAX_FILE_BYTES
    packs.DEFAULT_MAX_FILE_BYTES = 50
    try:
        pack = build_pack(proj, cache)
    finally:
        packs.DEFAULT_MAX_FILE_BYTES = old_cap
    paths = set(by_path(pack))
    assert "binary.dat" not in paths, "binary files must be skipped"
    assert "main.py" not in paths, "oversize files must be skipped"
    print("PASS test_binary_and_oversize_skipped")


def test_broken_python_degrades_gracefully():
    tmp, proj, cache = make_fixture()
    bad = os.path.join(proj, "broken.py")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("# broken module\ndef oops(:\n")
    pack = build_pack(proj, cache)
    entries = by_path(pack)
    assert entries["broken.py"]["symbols"] == []
    assert entries["broken.py"]["summary"] == "broken module", entries["broken.py"]
    print("PASS test_broken_python_degrades_gracefully")


def test_pack_stats_shape():
    tmp, proj, cache = make_fixture()
    stats = pack_stats(build_pack(proj, cache))
    assert stats["files"] == len(EXPECTED_FILES), stats
    assert stats["symbols"] >= 3, stats
    assert stats["chars"] > 0, stats
    assert stats["cache_hits"] == 0 and stats["cache_misses"] == len(EXPECTED_FILES)
    print("PASS test_pack_stats_shape")


def test_big_project_keeps_symbols_within_budget():
    """A project too big for the pack must lose detail evenly, not lose symbols whole."""
    tmp = tempfile.mkdtemp(prefix="packbig_")
    proj, cache = os.path.join(tmp, "proj"), os.path.join(tmp, "cache")
    os.makedirs(proj)
    # 120 small modules: paths + summaries alone overflow a small budget.
    for i in range(120):
        with open(os.path.join(proj, f"m{i:03d}.py"), "w", encoding="utf-8") as fh:
            fh.write(f'"""Module number {i}, described at some length."""\n'
                     f"def func_{i}():\n    pass\n")
    pack = build_pack(proj, cache)
    assert len(pack["files"]) == 120, len(pack["files"])
    budget = 4000
    rendered = render_prefix(pack, max_chars=budget)
    assert len(rendered) <= budget, (len(rendered), budget)
    assert "## File tree" in rendered and "## Files" in rendered, rendered
    # The old render cut at the limit and lost whichever section came last.
    assert "## Symbols" in rendered, rendered
    assert "func_0" in rendered, rendered
    # Every file is still named, even where its summary had to go.
    assert "... and " in rendered or rendered.count(".py") >= 120, rendered
    assert render_prefix(pack, max_chars=budget) == rendered, "byte-stable"
    print("PASS test_big_project_keeps_symbols_within_budget")


def test_rank_orders_source_then_depth():
    """A capped pack must spend its slots on code first, and on the shallow code first."""
    tmp = tempfile.mkdtemp(prefix="packrank_")
    proj, cache = os.path.join(tmp, "proj"), os.path.join(tmp, "cache")
    deep = os.path.join(proj, "vendor", "fonts", "x", "y")
    os.makedirs(deep)
    for rel, body in {
        "app.py": '"""The app."""\n',
        "lib/deep/core.py": '"""Deep module."""\n',
    }.items():
        path = os.path.join(proj, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    for i in range(40):   # shallow junk: first alphabetically, and not code
        with open(os.path.join(proj, f"a{i:03d}.dat"), "w", encoding="utf-8") as fh:
            fh.write("noise\n")
    for i in range(40):   # deep code: last alphabetically, and deep
        with open(os.path.join(deep, f"z{i:03d}.css"), "w", encoding="utf-8") as fh:
            fh.write("/* noise */\n")

    paths = [f["path"] for f in build_pack(proj, cache, max_files=10)["files"]]
    assert paths[0] == "app.py", paths                 # code before shallow junk
    assert "lib/deep/core.py" in paths, paths          # deep code still beats shallow junk
    assert not any(p.endswith(".dat") for p in paths), paths
    assert all(p.endswith(".css") or "/" in p for p in paths[1:]), paths
    print("PASS test_rank_orders_source_then_depth")


def main():
    tests = [
        test_pack_builds_over_fixture,
        test_summaries_extracted_correctly,
        test_symbol_index_correct,
        test_render_prefix_byte_stable,
        test_big_project_keeps_symbols_within_budget,
        test_rank_orders_source_then_depth,
        test_mtime_invalidation_rescans_only_changed,
        test_cache_round_trips_as_json,
        test_binary_and_oversize_skipped,
        test_broken_python_degrades_gracefully,
        test_pack_stats_shape,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
