"""Real tasks, run the way CodeMonkey runs them, on a model server on this computer.

The model check in Settings (webui/bench.py) asks short questions. This asks for work: build a
small package, fix a failing test, follow a traceback, rename across files, change one function
in a long file. Each task runs in a throwaway folder through a real CodeMonkey session (the same
prompt, tools and loop as the app), then is graded by tests the model never saw.

What it records, per task: passed or not, seconds, tool calls, tool calls that failed, edits that
did not apply, model requests, and (for Strata) prompt tokens read again vs reused and tokens
written. The point is a number before and after each change, on this machine:

    python evals/realtasks.py --out before.json
    python evals/realtasks.py --out after.json
    python evals/realtasks.py --compare before.json after.json

Options: --base-url (default Strata, http://127.0.0.1:8080/v1), --model (default: the one the
server has loaded), --only name,name, --effort auto|low|medium|high, --minutes per task (10).
Nothing here touches your own sessions or settings: it runs its own app on a temporary folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _long_module() -> str:
    """About 600 lines: many small functions, one of which the task asks to change."""
    parts = ['"""Unit conversions and small helpers for the shop."""\n\nimport math\n']
    for i in range(1, 120):
        parts.append(_d(f'''
            def helper_{i:03d}(value):
                """Scale value by {i}, rounded to two places."""
                result = value * {i}
                return round(result, 2)
            '''))
        if i == 77:
            parts.append(_d('''
                def shipping_cost(weight_kg, express=False):
                    """Cost to ship a parcel: 4.00 base plus 1.50 a kilo, rounded up to whole kilos."""
                    kilos = math.ceil(weight_kg)
                    cost = 4.00 + 1.50 * kilos
                    return round(cost, 2)
                '''))
    return "\n".join(parts)


#: name -> setup files, the request, hidden tests (graded with pytest), optional extra check command
TASKS: List[Dict[str, Any]] = [
    {
        "name": "new-package",
        "files": {},
        "prompt": "Create a Python module `inventory.py` with a class `Inventory`: add(name, qty), "
                  "remove(name, qty) which raises ValueError if there is not enough, count(name) (0 for an "
                  "unknown item) and total() (all items together). Add pytest tests for it and run them.",
        "hidden": _d('''
            import pytest
            from inventory import Inventory
            def test_flow():
                inv = Inventory(); inv.add("bolt", 5); inv.add("nut", 2); inv.add("bolt", 1)
                assert inv.count("bolt") == 6 and inv.count("nut") == 2 and inv.count("gear") == 0
                assert inv.total() == 8
                inv.remove("bolt", 6); assert inv.count("bolt") == 0 and inv.total() == 2
            def test_not_enough():
                inv = Inventory(); inv.add("nut", 1)
                with pytest.raises(ValueError):
                    inv.remove("nut", 2)
                assert inv.count("nut") == 1
            '''),
    },
    {
        "name": "fix-failing-test",
        "files": {
            "dates.py": _d('''
                from datetime import date
                def days_between(a, b):
                    """Whole days from a to b, counting both ends (1 Jan to 3 Jan is 3 days)."""
                    return (b - a).days
                def is_weekend(d):
                    return d.weekday() > 5
                '''),
            "test_dates.py": _d('''
                from datetime import date
                from dates import days_between, is_weekend
                def test_days():
                    assert days_between(date(2024, 1, 1), date(2024, 1, 3)) == 3
                def test_weekend():
                    assert is_weekend(date(2024, 1, 6)) and is_weekend(date(2024, 1, 7))
                    assert not is_weekend(date(2024, 1, 5))
                '''),
        },
        "prompt": "The tests in test_dates.py fail. Find out why and fix the code (not the tests).",
        "hidden": _d('''
            from datetime import date
            from dates import days_between, is_weekend
            def test_days():
                assert days_between(date(2024, 3, 1), date(2024, 3, 1)) == 1
                assert days_between(date(2024, 2, 28), date(2024, 3, 1)) == 3
            def test_weekend():
                assert is_weekend(date(2024, 3, 9)) and is_weekend(date(2024, 3, 10)) and not is_weekend(date(2024, 3, 8))
            '''),
    },
    {
        "name": "follow-traceback",
        "files": {
            "report.py": _d('''
                import json, sys
                def summary(orders):
                    total = sum(o["price"] * o["qty"] for o in orders)
                    lines = [f"{o['item']}: {o['qty']} x {o['price']:.2f}" for o in orders]
                    return "\\n".join(lines + [f"Total: {total:.2f}"])
                if __name__ == "__main__":
                    print(summary(json.load(open(sys.argv[1]))))
                '''),
            "orders.json": json.dumps([{"item": "tea", "qty": 2, "price": 3.5}, {"item": "cake", "price": 4.0}]),
        },
        "prompt": "Running `python report.py orders.json` crashes:\n\nTraceback (most recent call last):\n"
                  "  File \"report.py\", line 3, in summary\n    total = sum(o[\"price\"] * o[\"qty\"] for o in orders)\n"
                  "KeyError: 'qty'\n\nAn order without a qty means one of it. Fix report.py so it handles that.",
        "hidden": _d('''
            from report import summary
            def test_missing_qty_is_one():
                out = summary([{"item": "tea", "qty": 2, "price": 3.5}, {"item": "cake", "price": 4.0}])
                assert "Total: 11.00" in out and "cake: 1 x 4.00" in out
            '''),
    },
    {
        "name": "rename-across-files",
        "files": {
            "pricing.py": "def calc(items):\n    return sum(p for _, p in items)\n",
            "cart.py": "from pricing import calc\n\n\ndef cart_total(cart):\n    return calc(cart)\n",
            "invoice.py": "import pricing\n\n\ndef invoice(items):\n    return f\"Due: {pricing.calc(items):.2f}\"\n",
        },
        "prompt": "Rename the function `calc` in pricing.py to `calculate_total`, and update every place that uses it.",
        "hidden": _d('''
            import pathlib, re
            def test_renamed():
                import pricing, cart, invoice
                assert hasattr(pricing, "calculate_total") and not hasattr(pricing, "calc")
                assert cart.cart_total([("a", 1.0), ("b", 2.5)]) == 3.5
                assert invoice.invoice([("a", 1.0)]) == "Due: 1.00"
                for f in ("pricing.py", "cart.py", "invoice.py"):
                    assert not re.search(r"\\bcalc\\b", pathlib.Path(f).read_text())
            '''),
    },
    {
        "name": "long-file-edit",
        "files": {"shop_utils.py": _long_module()},
        "prompt": "In shop_utils.py, shipping_cost should add 6.00 when express=True. It ignores express now. "
                  "Change only that function.",
        "hidden": _d('''
            from shop_utils import shipping_cost, helper_001, helper_119
            def test_express():
                assert shipping_cost(2.2) == 8.5
                assert shipping_cost(2.2, express=True) == 14.5
                assert helper_001(2) == 2 and helper_119(1) == 119
            '''),
    },
    {
        "name": "add-cli-flag",
        "files": {
            "wordfreq.py": _d('''
                import argparse, collections, re
                def counts(text):
                    return collections.Counter(re.findall(r"[a-z']+", text.lower()))
                def main(argv=None):
                    p = argparse.ArgumentParser()
                    p.add_argument("file")
                    p.add_argument("--top", type=int, default=3)
                    a = p.parse_args(argv)
                    for word, n in counts(open(a.file).read()).most_common(a.top):
                        print(f"{word} {n}")
                if __name__ == "__main__":
                    main()
                '''),
            "sample.txt": "the cat and the hat and the bat\n",
        },
        "prompt": "Add a --json flag to wordfreq.py: with it, print the top words as one JSON object "
                  "(word to count, most common first) instead of lines. Keep the line output as the default.",
        "hidden": _d('''
            import json, subprocess, sys
            def run(*a):
                return subprocess.run([sys.executable, "wordfreq.py", "sample.txt", *a], capture_output=True, text=True).stdout
            def test_json():
                assert json.loads(run("--json", "--top", "2")) == {"the": 3, "and": 2}
            def test_lines_still():
                assert run("--top", "1").strip() == "the 3"
            '''),
    },
    {
        "name": "javascript-fix",
        "files": {
            "slug.js": _d('''
                // Turn a title into a URL slug: lower case, words joined by single dashes, nothing else.
                function slugify(title) {
                  return title.toLowerCase().replace(" ", "-");
                }
                module.exports = { slugify };
                '''),
        },
        "prompt": "slugify in slug.js is wrong for most titles (try \"Hello,  World! 2024\"). Fix it so it does "
                  "what its comment says, and check it with node.",
        "check": ["node", "-e", "const {slugify}=require('./slug.js');"
                  "const ok=slugify('Hello,  World! 2024')==='hello-world-2024'&&slugify('  A  B -- c ')==='a-b-c';"
                  "process.exit(ok?0:1)"]
    },
    {
        "name": "two-failures",
        "files": {
            "stats.py": _d('''
                def mean(xs):
                    return sum(xs) / len(xs)
                def median(xs):
                    s = sorted(xs)
                    return s[len(s) // 2]
                def spread(xs):
                    return max(xs) - min(xs)
                '''),
            "test_stats.py": _d('''
                import pytest
                from stats import mean, median, spread
                def test_mean():
                    assert mean([1, 2, 3, 4]) == 2.5
                def test_mean_empty():
                    with pytest.raises(ValueError):
                        mean([])
                def test_median_odd():
                    assert median([3, 1, 2]) == 2
                def test_median_even():
                    assert median([4, 1, 3, 2]) == 2.5
                def test_spread():
                    assert spread([5, 1, 9]) == 8
                '''),
        },
        "prompt": "Run the tests in this folder and fix whatever makes them fail.",
        "hidden": _d('''
            import pytest
            from stats import mean, median
            def test_all():
                assert median([10, 20, 30, 40]) == 25 and median([7]) == 7 and mean([2, 4]) == 3
                with pytest.raises(ValueError):
                    mean([])
            '''),
    },
    {
        "name": "use-existing-helper",
        "files": {
            "cache.py": _d('''
                import time
                class TTLCache:
                    """Remembers values for ttl seconds."""
                    def __init__(self, ttl):
                        self.ttl, self._d = ttl, {}
                    def get(self, key):
                        hit = self._d.get(key)
                        if hit and time.monotonic() - hit[1] < self.ttl:
                            return hit[0]
                        return None
                    def put(self, key, value):
                        self._d[key] = (value, time.monotonic())
                '''),
            "weather.py": _d('''
                CALLS = []
                def _fetch(city):
                    CALLS.append(city)          # stands in for a slow web request
                    return {"city": city, "temp": 20}
                def forecast(city):
                    return _fetch(city)
                '''),
        },
        "prompt": "forecast() in weather.py fetches every time. Make it reuse a result for 60 seconds, "
                  "using the cache this project already has rather than writing a new one.",
        "hidden": _d('''
            import pathlib
            import weather
            def test_cached():
                weather.CALLS.clear()
                a = weather.forecast("Oslo"); b = weather.forecast("Oslo"); weather.forecast("Rome")
                assert a == b == {"city": "Oslo", "temp": 20} and weather.CALLS == ["Oslo", "Rome"]
                assert "TTLCache" in pathlib.Path("weather.py").read_text()
            '''),
    },
    {
        "name": "parser-edge-cases",
        "files": {},
        "prompt": "Write `settings.py` with parse(text) for a simple settings format: `key = value` lines, "
                  "`[section]` headers that prefix keys as `section.key`, lines starting with # or ; are "
                  "comments, blank lines are ignored, spaces around keys and values are trimmed, and a value "
                  "may itself contain `=`. Return a dict. Raise ValueError naming the line number for a line "
                  "that is none of those. Test it.",
        "hidden": _d('''
            import pytest
            from settings import parse
            def test_parse():
                text = "# top\\nname = shop\\n\\n[db]\\n host = localhost \\nurl = a=b=c\\n; note\\n[cache]\\nttl=5\\n"
                assert parse(text) == {"name": "shop", "db.host": "localhost", "db.url": "a=b=c", "cache.ttl": "5"}
            def test_bad_line():
                with pytest.raises(ValueError) as e:
                    parse("a = 1\\nthis is wrong\\n")
                assert "2" in str(e.value)
            '''),
    },
]


# ------------------------------------------------------------------ the server's own numbers ---

class StrataMeter:
    """Collects the server's per-request records while a task runs (it keeps only the last few)."""

    def __init__(self, base_url: str):
        root = base_url.rstrip("/")
        self.url = (root[:-3] if root.endswith("/v1") else root) + "/metrics"
        self.seen: Dict[str, Dict] = {}
        self._stop = threading.Event()
        self.works = self._poll()

    def _poll(self) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=5) as r:
                for row in json.loads(r.read()).get("requests") or []:
                    if row.get("id"):
                        self.seen.setdefault(row["id"], row)
            return True
        except Exception:  # noqa: BLE001 - not Strata, or not answering: no server numbers
            return False

    def start(self) -> None:
        self.before = set(self.seen)

        def run():
            while not self._stop.wait(2.0):
                self._poll()
        self._stop.clear()
        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        self._poll()
        rows = [r for k, r in self.seen.items() if k not in self.before]
        prompt = sum(int(r.get("prompt_tokens") or 0) for r in rows)
        reused = sum(int(r.get("reused") or 0) for r in rows)
        return {"server_requests": len(rows), "prompt_tokens": prompt, "reread_tokens": prompt - reused,
                "reused_pct": round(100.0 * reused / prompt, 1) if prompt else None,
                "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
                "server_seconds": round(sum(float(r.get("duration_s") or 0) for r in rows), 1)}


# --------------------------------------------------------------------------- one task ---

_FAILED = re.compile(r"^(ERROR|\[exit [1-9]\d*\]|Traceback|Not run\.)")


def _grade(proj: str, task: Dict[str, Any]) -> tuple:
    if task.get("hidden"):
        path = os.path.join(proj, "_hidden_grade_test.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(task["hidden"])
        try:
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path],
                                  cwd=proj, capture_output=True, text=True, timeout=120)
        finally:
            os.remove(path)
        return done.returncode == 0, (done.stdout + done.stderr)[-600:]
    done = subprocess.run(task["check"], cwd=proj, capture_output=True, text=True, timeout=60)
    return done.returncode == 0, (done.stdout + done.stderr)[-600:]


def run_task(app, task: Dict[str, Any], provider: str, model: str, effort: str, minutes: float,
             meter: Optional[StrataMeter]) -> Dict[str, Any]:
    proj = tempfile.mkdtemp(prefix=f"cm-eval-{task['name']}-")
    for rel, body in task["files"].items():
        with open(os.path.join(proj, rel), "w", encoding="utf-8") as fh:
            fh.write(body)
    session = app.create_session(proj)
    session.settings.update(provider=provider, model=model, permission_mode="bypass", effort=effort)
    if meter and meter.works:
        meter.start()
    started = time.time()
    session.enqueue_message(task["prompt"], [])
    time.sleep(2)
    timed_out = False
    while session.status not in ("idle", "error"):
        if time.time() - started > minutes * 60:
            timed_out = True
            session.cancel()
            break
        time.sleep(1)
    seconds = round(time.time() - started, 1)
    server = meter.stop() if meter and meter.works else {}
    tools = [e for e in session.transcript if e.get("kind") == "tool"]
    failed = [e for e in tools if _FAILED.match(str(e.get("result") or "").lstrip())]
    edits_missed = [e for e in tools if e.get("name") in ("edit", "multi_edit")
                    and str(e.get("result") or "").startswith("ERROR")]
    replies = [e for e in session.transcript if e.get("kind") == "assistant" and e.get("text")]
    try:
        passed, grade_out = _grade(proj, task)
    except Exception as exc:  # noqa: BLE001
        passed, grade_out = False, f"grading failed: {exc}"
    result = {
        "task": task["name"], "passed": passed, "seconds": seconds, "timed_out": timed_out,
        "status": session.status, "tool_calls": len(tools), "failed_calls": len(failed),
        "edits_missed": len(edits_missed),
        "tools_used": sorted({e.get("name") for e in tools}),
        "failures": [f"{e.get('name')}: {str(e.get('result'))[:160]}" for e in failed][:8],
        "last_reply": (replies[-1]["text"][:400] if replies else ""),
        "grade": grade_out if not passed else "",
        **server,
    }
    try:
        session.close()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(proj, ignore_errors=True)
    return result


def _served_model(base_url: str) -> str:
    with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=10) as r:
        return json.loads(r.read())["data"][0]["id"]


def run_all(base_url: str, model: str, only: List[str], effort: str, minutes: float,
            log: Callable[[str], None] = print) -> Dict[str, Any]:
    os.environ.setdefault("CODEMONKEY_NO_MCP", "1")
    from webui.app import WebApp

    tmp = tempfile.mkdtemp(prefix="cm-eval-")
    cfg = os.path.join(tmp, "config.yaml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(f'agent_name: Eval\nworkspace: "{os.path.join(tmp, "ws")}"\nprovider: "local"\n'
                 f'providers:\n  local:\n    base_url: "{base_url}"\n    api_key_env: ""\n'
                 f'    models:\n      - "{model}"\n')
    app = WebApp(cfg, default_project=tmp)
    meter = StrataMeter(base_url)
    results = []
    for task in TASKS:
        if only and task["name"] not in only:
            continue
        log(f"- {task['name']} ...")
        r = run_task(app, task, "local", model, effort, minutes, meter)
        log(f"  {'PASS' if r['passed'] else 'FAIL'} in {r['seconds']}s, {r['tool_calls']} tool calls "
            f"({r['failed_calls']} failed, {r['edits_missed']} edits missed)"
            + (f", reread {r['reread_tokens']} of {r['prompt_tokens']} prompt tokens" if r.get("prompt_tokens") else ""))
        results.append(r)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"model": model, "base_url": base_url, "effort": effort, "when": time.strftime("%Y-%m-%d %H:%M"),
            "summary": summarize(results), "tasks": results}


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(results) or 1
    tot = lambda k: sum(r.get(k) or 0 for r in results)  # noqa: E731
    return {"passed": sum(1 for r in results if r["passed"]), "tasks": len(results),
            "seconds": round(tot("seconds"), 1), "avg_seconds": round(tot("seconds") / n, 1),
            "tool_calls": tot("tool_calls"), "failed_calls": tot("failed_calls"), "edits_missed": tot("edits_missed"),
            "prompt_tokens": tot("prompt_tokens"), "reread_tokens": tot("reread_tokens"), "output_tokens": tot("output_tokens")}


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    sa, sb = a["summary"], b["summary"]
    lines = [f"{'':16}{'before':>12}{'after':>12}"]
    for k in ("passed", "seconds", "tool_calls", "failed_calls", "edits_missed", "reread_tokens", "output_tokens"):
        lines.append(f"{k:16}{sa.get(k, 0):>12}{sb.get(k, 0):>12}")
    lines.append("")
    by = {r["task"]: r for r in a["tasks"]}
    for r in b["tasks"]:
        o = by.get(r["task"])
        if o:
            lines.append(f"{r['task']:22} {'PASS' if o['passed'] else 'fail'} {o['seconds']:>6}s -> "
                         f"{'PASS' if r['passed'] else 'fail'} {r['seconds']:>6}s")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    p.add_argument("--model", default="")
    p.add_argument("--only", default="")
    p.add_argument("--effort", default="auto")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--out", default="")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    a = p.parse_args(argv)
    if a.compare:
        with open(a.compare[0], encoding="utf-8") as f1, open(a.compare[1], encoding="utf-8") as f2:
            print(compare(json.load(f1), json.load(f2)))
        return 0
    model = a.model or _served_model(a.base_url)
    print(f"Running {len(TASKS)} tasks on {model} at {a.base_url} (effort {a.effort})", flush=True)
    out = run_all(a.base_url, model, [s for s in a.only.split(",") if s], a.effort, a.minutes,
                  log=lambda s: print(s, flush=True))
    s = out["summary"]
    print(f"\n{s['passed']}/{s['tasks']} passed in {s['seconds']}s; {s['tool_calls']} tool calls, "
          f"{s['failed_calls']} failed, {s['edits_missed']} edits missed; reread {s['reread_tokens']} tokens, "
          f"wrote {s['output_tokens']}", flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
