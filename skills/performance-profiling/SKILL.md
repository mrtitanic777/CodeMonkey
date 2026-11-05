---
name: performance-profiling
description: Use when a program, request, or job is slower than it should be, or before you change anything for speed, so the work starts from measurements instead of guesses.
keywords: [performance, slow, profile, latency, benchmark]
---

# Performance Profiling

You never optimize from intuition. Intuition about bottlenecks is wrong
most of the time; the profile is right. The loop is: measure, find the
hotspot, fix one thing, measure again.

## Measure first

1. **Write the benchmark before changing anything.** A small script
   that performs the slow operation and prints wall time. Run it three
   times; take the median. This number is your baseline and your
   definition of done.
2. **Make it reproducible.** Same machine, same data, quiet system.
   Note the inputs — "slow" on 10 rows and "slow" on 10 million rows
   are different problems.

## Profile, don't guess

- **CPU:** `cProfile` + `pstats` for a quick picture; `py-spy` when
  you need production-safe sampling without restarting.
- **Memory:** `tracemalloc` for allocation hotspots; watch for
  growth over time (leak) vs a single spike (peak usage).
- **I/O:** if CPU is idle while it's slow, you're waiting on
  something — disk, network, a lock. Time the waits, not the code.
- **Frontend:** the browser's performance panel; measure paint and
  script time separately.

Read the profile top-down: the single hottest frame is where the time
goes (Amdahl's law — a 2x speedup on code that's 5% of runtime is a
2.5% win; don't bother). Ignore everything that isn't the hotspot
until the hotspot is fixed.

## The usual suspects

Check these before inventing exotic theories:

- **N+1 queries:** one query per row in a loop. Fix: batch/join/eager load.
- **Repeated work:** serialization, parsing, or hashing recomputed per
  iteration instead of once. Hoist it out of the loop.
- **Accidental quadratic:** nested loops over the same collection,
  `list` membership tests (`in`) on large lists — use a set.
- **Sync I/O in hot paths:** one network call per item; fix with
  batching or concurrency.
- **String building:** `+=` in a loop; use `"".join`.
- **Import-time cost:** heavy imports at module top for code paths
  that rarely run.
- **Regex backtracking:** nested quantifiers on untrusted-length input.

## Fix and verify

Change one thing, re-run the benchmark, compare medians. If the number
didn't move, revert — a "cleanup" that doesn't help is just risk.
When it does move, convert the benchmark into a regression test with
a generous time bound so the slowness can't creep back silently.

Know when to stop: "fast enough for the requirement" is done. Say
what the final numbers are and what the remaining hotspot is, so the
next person starts from your profile instead of their intuition.
