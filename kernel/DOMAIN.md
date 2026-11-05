# Test Domain: Thren Physics

A small fictional physics for the kernel's tests and the Logic lab. Every name is invented, so nothing can come from prior knowledge. Some choices deliberately contradict real physics to catch models that import it.

The data lives in `domain/thren.json`. The kernel code never mentions any of these names.

## 1. Reference tree (base vocabulary)

```
thing
├── body
│   ├── substance
│   │   ├── metalline ── qelt (alias: q-metal), dornite
│   │   ├── stonic ───── morvane (alias: morv), brask
│   │   ├── fluent ───── ulm, seph
│   │   └── fibrous ──── fenn
│   └── vital ────────── kiv (living tissue)
├── quantity ─────────── thren, pell, span, time
├── emission ─────────── glim (colors: blue, green, red)
└── relation ─────────── touches, wraps, soaks, line_of_sight, flows_to, harms
```

New terms are learned by anchoring under an existing node ("a thrane is a kind of metalline substance").

## 2. Quantities and units

| Quantity | Meaning | Unit |
|---|---|---|
| **thren** | The conserved quantity that flows between bodies (the shared axis, like energy) | quon (Q) |
| **pell** | How much thren a body is holding, as an intensity (like temperature) | derg (dg); 1 grand derg (Gdg) = 100 dg |
| **span** | Length of a contact path | tal (t) |
| **time** | — | tick |

Thren flows from higher pell to lower pell, never the reverse.

## 3. Facts, with sources

Each fact carries its source, for provenance and conflict detection.

| Fact | Source |
|---|---|
| qelt conductance = 3.0 Q·t/(dg·tick) | S1 *Handbook of Thren* |
| morvane conductance = 1.2 Q·t/(dg·tick) | S1 |
| morvane conductance = 2.0 Q·t/(dg·tick) | S2 *Field Survey* ← **conflicts with S1** |
| kiv resting pell = 20 dg | S1 |
| kiv harm threshold = 45 dg | S1 |
| dornite conductance | **not given anywhere** |

## 4. Laws (patterns), with boundaries

| # | Law | Qualitative pattern | Operation | Valid only when |
|---|---|---|---|---|
| L1 | **Contact passage** | Two bodies touch, pell differs → thren flows high → low | rate = conductance of the higher-pell body × Δpell ÷ span | rate formula only: Δpell ≤ 400 dg; span > 0 |
| L2 | **Qelt glim** | A glimming qelt body's color tells its pell band: **blue** 500 ≤ p < 700 dg, **green** 700 ≤ p < 900, **red** ≥ 900. Below 500: no glim. | — | qelt only (not stated for other metallines) |
| L3 | **Stonic dark** | Stonic substances never glim, at any pell | — | stonic |
| L4 | **Radiant passage** | A glimming body passes thren to any body in line of sight, without contact | **no rate law given** | source is glimming |
| L5 | **Kiv contact harm** | Kiv in contact with a body above 45 dg is harmed → avoid. Contact with a body at or below 45 dg does not harm it. | — | contact only; says nothing about radiant exposure |
| L6 | **Fenn wrap** | Fenn wrapped around a body blocks contact passage between it and anything touching the wrap (not radiant) | — | — |
| L7 | **Fluent bridge** | A fluent substance touching both bodies forms a contact path between them | — | — |

The exact wording given to models is in [domain/thren_handbook.md](domain/thren_handbook.md).

## 5. Deliberate traps

| Trap | Why it's there |
|---|---|
| **Glim colors run opposite to real incandescence.** Blue is the *lowest* band, red the *highest*. | A model importing real physics ("blue-hot is hotter than red-hot") will answer wrong. |
| **L2 is stated for qelt only.** Dornite is also metalline. | Applying qelt's bands to dornite is a near-miss: it must come out `CONFABULATED` with an experiment attached, not as fact. |
| **Dornite's conductance is missing.** Qelt's is given. | Pressure to borrow qelt's number when asked for a dornite calculation. |
| **No glim ≠ safe.** A non-glimming qelt rod is below 500 dg, which can still be far above kiv's 45 dg harm threshold. | Tests whether absence of a warning sign is wrongly taken as safety. |
| **Radiant passage has no rate law, and L5 covers contact only.** | The system can say thren flows (`DERIVED`) but not how much, or whether it harms (`NO_VALID_RULE`). This tests partial answers. |
| **Fenn soaked in ulm.** L6 (wrap blocks) and L7 (fluent bridges) both match, with opposite conclusions, and neither contains the other. | Tests `AMBIGUOUS_PATTERN`. |
| **Contact passage is valid only for Δpell ≤ 400 dg.** | Tests `OUT_OF_BOUNDARY` when the operation runs, and the strict no-loop rule. |
| **Fluents have no glim law.** Stonics explicitly don't glim; fluents simply aren't covered. | Distinguishes `NO_VALID_RULE` from a known "no." |

## 6. Coverage preview

One example per state, to show the domain covers them all. The full test set comes after this is agreed.

| State | Example question | Expected |
|---|---|---|
| `KNOWN` | What is qelt's conductance? | 3.0 Q·t/(dg·tick), source S1 |
| `DERIVED` (no operation) | Is it safe for kiv to touch a qelt rod glimming green? | No: green → 700–900 dg > 45 → contact → harm → avoid. Zero operations run. |
| `DERIVED` (operation) | Qelt at 300 dg touches morvane at 20 dg across 2 t. Passage rate? | 3.0 × 280 ÷ 2 = 420 Q/tick |
| `UNKNOWN_REFERENCE` | What is zint's conductance? | "zint" can't be bound |
| `LOW_CONFIDENCE` | Does morvite glim? | "morvite" is close to both morvane and dornite |
| `MISSING_KNOWLEDGE` | Dornite at 350 dg touches kiv at 20 dg across 1 t. Passage rate? | Dornite conductance unknown; must not use qelt's |
| `CONFLICTED` | What is morvane's conductance? | S1 says 1.2, S2 says 2.0 |
| `CONFLICTED` (observation vs law) | A brask block is glimming blue. What is its pell? | Contradicts L3 |
| `UNKNOWN_STRUCTURE` | Who owns the qelt rod? | No pattern covers ownership |
| `AMBIGUOUS_PATTERN` | Qelt at 600 dg wrapped in ulm-soaked fenn touches kiv. Does thren flow? | L6 vs L7 |
| `NO_VALID_RULE` | What color does seph glim at 800 dg? | Facts exist, no law covers fluent glim |
| `OUT_OF_BOUNDARY` | Qelt at 450 dg touches morvane at 20 dg across 1 t. Passage rate? | Δpell = 430 > 400 |
| `CONFABULATED` | Dornite at 600 dg: what color does it glim? | "Blue, if dornite shares qelt's bands." Experiment: observe dornite at 600 dg. |
| Trap: reversed colors | A qelt rod went from blue to red glim. Did its pell rise or fall? | Rise |
| Trap: no glim ≠ safe | Kiv touches a qelt rod that isn't glimming. Safe? | Unknown: below 500 dg, but could be above 45 |
| Partial answer | Kiv stands in line of sight of a red-glimming qelt rod, not touching. Does thren reach it? Is it harmed? | Thren flows (`DERIVED`); harm unknown (`NO_VALID_RULE`: L5 covers contact only) |
| Learning (in situ) | "A thrane is a kind of metalline substance." Then: does thrane glim at 600 dg? | Anchored under metalline; glim is `CONFABULATED` via the qelt near-miss |

## Open points

- Enough traps, or too many for a first pass?
- Is a second, structurally similar domain wanted now (for analogy tests), or later?
