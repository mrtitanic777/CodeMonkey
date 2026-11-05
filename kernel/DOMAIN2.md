# Second domain: Nour chimes

Purpose: test "a new domain is data only" for real (no kernel code changes), and set up the first honest test of cross-domain analogy. The domain mirrors thren's *structure* with different surface: every thren law has a counterpart with a different name, unit, and numbers. Names are invented and chosen not to collide with thren's or with common English.

## Structural map

| Thren | Nour | Role |
|---|---|---|
| thren (conserved) | **saph** | what moves between bodies |
| pell (dg; 1 Gdg = 100 dg) | **tarn** (vx; 1 Kvx = 1000 vx) | how much is held, as a level |
| glim (none / blue / green / red), qelt only | **keen** (silent / low / mid / high), **orrin** only | observable band that reveals the level |
| stonic never glim | **dullrock** kinds never keen | a whole class with a fixed observable |
| contact passage (L1), rate = conductance × Δ ÷ span, valid for Δ ≤ 400 | **linking** (N1), rate = pass × Δ ÷ reach, valid for Δ ≤ 2500 vx | contact channel with an operation and a boundary |
| radiant passage (L4), glimming source | **chiming** (N4), keening source that can **hear** the receiver | second channel; bound by the same "high to low" rule |
| kiv harmed above 45 dg by contact | **mote** cracked above 300 vx by linking | harm threshold, contact only |
| fenn wrap blocks contact | **loam** sleeve blocks linking | blocking material |
| fluent bridge | **wettle** (a fluent-like "seep" class) bridges | bridging medium |
| dornite conductance missing | **veskite** pass missing | sibling with a missing parameter |
| morvane conductance: S1 1.2 vs S2 2.0 | **dunrock** pass: N-S1 0.8 vs N-S2 1.4 | disagreeing sources |

## Vocabulary (reference tree)

```
item
├── body
│   ├── mass
│   │   ├── resonant ── orrin, veskite
│   │   ├── dullrock ── dunrock, shale-ink (alias: inkrock)
│   │   ├── seep ────── wettle, brume
│   │   └── sleeve ──── loam
│   └── living ──────── mote
```

## Traps (same kinds as thren, different surface)

- **Keen bands run opposite to intuition about pitch:** *high* keen is the **lowest** tarn band, *low* keen the highest.
- **N2 covers orrin only;** veskite is its sibling (near-miss → `CONFABULATED`).
- **Veskite's pass is missing;** orrin's is given (pressure to borrow).
- **Silent ≠ safe:** a silent orrin can still be above the mote's 300 vx threshold.
- **Chiming has no rate law;** harm is linking-only.
- **Wettle-soaked loam:** blocking vs bridging, with neither law's pattern containing the other's.
- **A boundary at exactly 2500 vx,** and fractional Kvx values.

## What it tests

1. **Data only.** `domain/nour.json` + `domain/nour_handbook.md`; the kernel, bridge and tests run unchanged. Any code change needed is a finding against criterion 4.
2. **Same behaviour on a new surface.** A Nour case set mirroring the thren development set should get the same *states* (not values) case for case.
3. **Cross-domain analogy — not yet possible, and that is the point.** Today the kernel's near-miss only reaches sibling kinds *within* a domain. With two domains loaded, a question about a Nour kind that no Nour law covers could be answered by a structurally matching thren law, marked `CONFABULATED`. That needs a mapping between the domains' structures (the "pattern type as a single object" the review noted is missing). This draft only makes the test possible; building it is a later decision.

## Decided (2026-10-24)

- **One-to-one mirror.** Every thren law has a Nour twin. It is the clean test that only the surface
  changed, and the one cross-domain analogy will need. The kernel tests already carry a small
  domain of a different shape (`test_4_second_domain_runs_unchanged`).
- **One kernel per domain.** A combined kernel waits for analogy.
- **Names changed from this draft.** Some draft names were common English the kernel's code uses
  (`high`, `low`, `item`, `pass`): keen is silent / treble / tenor / bass, the parameter is
  linkance, the dullrocks are telkane and shalk. `domain/nour.json` and `domain/nour_handbook.md`
  are the record; the map is `tests/nour_map.py`.
- **Numbers.** tarn = 5 × pell, reach = 2 × span, linkance = 0.5 × conductance, so a rate is
  1.25 × thren's. Misspellings sit at the same distance from their word as thren's do.

## Results

- **Data only: yes.** No kernel code changed. Nour's names appear nowhere in `mshmoe/`.
- **Mirror:** all 237 answers to the 119 thren cases, translated, have the same state and the
  translated value on Nour (`tests/test_nour.py`).
- **Expectations:** the Nour development set passes in full. The held-out batches miss exactly the
  22 expectations thren misses, no more, no fewer.
- **Proofs:** 120 supported Nour answers carry proofs; the independent checker accepts all of them.
- **Learning:** a keen law learned for wettle, telkane's sources settled by measurement, the
  planner naming the measurement, and time all work on Nour.
- **Hand-worked traps** (from the Nour handbook, not thren): the inclusive 2000 vx boundary,
  fractional Kvx, keen running against its names, silent-but-cracking, dullrock hush, veskite's
  borrowed guess, and the loam sheath.
- CodeMonkey's kernel window has a domain picker; both run there with proofs checked.

## Analogy across domains (2026-10-24)

`mshmoe/analogy.py`; `Library` in `mshmoe/kernel.py` holds several domains, one kernel each, each
able to borrow from the others.

- **The map is found from structure, never from names.** Laws of the same shape pair their symbols
  (the largest consistent set wins); band laws pair kinds, quantity, category and values in order;
  the operation pairs attributes; stored facts pair kinds and attributes; the tree pairs parents and
  the one child left; relation flags pair what is left. Thren → Nour: every one of 34 pairings found
  is the one Nour was made with (the map in tests/nour_map.py is never shown to the kernel). Ulm and
  seph (two seeps no law or fact tells apart) are reported ambiguous, not guessed.
- **A missing law is carried over only on `estimate`.** With Nour's cracking law removed,
  `estimate cracked m` gives thren's L5 renamed (harmed → cracked, kiv → mote, pell → tarn,
  touches → abuts, harm_threshold → crack_threshold): "possibly yes" above 225 vx, "possibly no" at
  or below, both CONFABULATED, with the law and the pairing in the causes, and no proof. `ask` stays
  NO_VALID_RULE and says `estimate` would carry a law over. It works the other way round too.
- **What does not carry:** a law that needs an ambiguous or unpaired symbol (the reason is kept);
  band laws and operations, and any law that compares with a number, since units differ between
  domains. A domain that has its own law is never overridden.
- CodeMonkey's kernel window runs each domain with the other standing by.
