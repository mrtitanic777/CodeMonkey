# Controlled Input Language and Answer Semantics (Phase 0)

**Revision 2 (2026-10-24).** Operations run on ranges: a pell known only as a range goes into an operation as a range, and the answer is the range of every value the operation can give (`DERIVED`, or `CONFLICTED` per source). A boundary must hold for every value in the range; if it holds for none the answer is `OUT_OF_BOUNDARY`, if for only part, `MISSING_KNOWLEDGE` (an exact value is needed). Held-out B2-10's rate was the case for this (proposal 6).

**Revision 1 (2026-09-11).** Batch 2 was measured against the previous version, kept in `results/frozen_spec_batch2/`. Changes: failures stay with the questions that depend on them; specificity by pattern containment; contact and radiant passage as separate channels, both bound by the direction rule; headspace reported, not enforced; a value after `shift` is `DERIVED`.

The public interface of the Phase 0 kernel. The domain itself is described in [domain/thren_handbook.md](domain/thren_handbook.md). This file says how questions are written and what answer each situation must produce.

## 1. Statements

One statement per line. `#` starts a comment.

| Statement | Meaning |
|---|---|
| `let <name> = <term> [pell <n> <unit>] [glim <color>]` | Introduce an entity of a kind, optionally with its pell and/or observed glim. `glim none` means observed not glimming. |
| `shift <name> pell <n> <unit>` | Change an entity's exact pell by n (may be negative). Earlier glim observations of it are discarded. |
| `rel <relation> <a> <b> [span <n> t]` | A given relation between two introduced entities. `span` is allowed on `touches` only. |
| `teach <term> isa <class>` | Add a new term under an existing node of the reference tree. Usable in the same input and afterwards. |
| `confirm <term> means <term>` | Feedback that a misspelled term means a known one. Takes effect at once, for the whole input (wherever the line appears) and in every later input. |
| `observe <term> pell <n> <unit> glim <color>` | Record an observation of a kind (evidence for learning). |
| `observe <name> [pell <n> <unit>] [glim <color>]` | An observation of an introduced entity: added to what is given about it. A value that disagrees with one already given makes that value `CONFLICTED`, listing both. |
| `measure <term> <attribute> <n> [unit]` | A direct measurement of a store value (e.g. a conductance). Evidence for consolidation: repeated agreeing measurements refute a source that disagrees, and every measurement adds to each source's record. Never settles anything during the run itself. |
| `ask <question>` | A question. Several `ask` lines may appear in one input. |
| `at <t> <statement>` | The statement happens at time t (a line without `at` is at time 0). A question `at <t> ask ...` sees every fact as it stands at t: each holds until a later line changes it. Each moment asked about is answered on its own, labelled with it. |
| `unrel <relation> <a> <b>` | Ends a relation from then on (use with `at`). |
| `ask next <quantity> <name>` | Where a value goes on its own. Answered `MISSING_KNOWLEDGE` unless the domain has a law for how fast it changes (thren has none). |
| `estimate <question>` | Same as `ask`, but explicitly asks for a best guess. |

Units: pell in `dg` or `Gdg` (1 Gdg = 100 dg); span in `t`.

Given relations: `touches` (symmetric), `wraps` (`rel wraps <fenn> <body>`: the first wraps the second), `soaks` (`rel soaks <fluent> <fenn>`: the first soaks the second), `sight` (symmetric line of sight).

## 2. Questions

| Question | Answers |
|---|---|
| `value <term or name> <attribute>` | A measured value of a kind (or of an entity's kind): `conductance`, `harm_threshold`, `resting_pell` |
| `class <term or name>` | The kind's parent in the reference tree |
| `compare <quantity-or-attribute> <a> <b>` | `a > b`, `a < b`, or `a = b`. `pell` compares introduced entities; attributes compare kinds or entities' kinds |
| `pell <name>` | The entity's pell, exact or as a range |
| `glim <name>` | The entity's glim color (`none` = not glimming) |
| `has glim <name>` | `yes` / `no`: does it glim? |
| `rate <a> <b>` | Contact passage rate between two entities (L1) |
| `flows <a> <b>` | Direction of thren flow between two entities, by either channel: `a → b` or `b → a` |
| `contact_flow <a> <b>` / `radiant_flow <a> <b>` | The same, for one channel only |
| `harmed <name>` | `yes` / `no` for a kiv entity |
| `contact <a> <b>` | `yes` / `no`: is there contact passage between them? |
| anything else | `UNKNOWN_STRUCTURE` |

A name used in a statement or question without being introduced is read as a term and introduced as an entity of that kind (so `ask has glim brask` asks about a brask; a name that is not a term fails as `UNKNOWN_REFERENCE` or `LOW_CONFIDENCE`).

The goal label in the answer is `<question word>(<args as written, comma-separated>)`, e.g. `rate(a, b)`, `has(glim, t)`, `value(q-metal, conductance)`, `compare(pell, x, y)`.

## 3. Answer format

One answer per question:

```
<goal> = <value> [<STATE>]
  from: ...
  because: ...
  alternative: ...
  experiment: ...
```

Value text:

| Case | Text |
|---|---|
| Number | `<n> <unit>`; whole numbers without decimals (`3 Q·t/(dg·tick)`, `420 Q/tick`, `45 dg`) |
| Range | `below 500 dg`, `at least 900 dg`, `from 500 to below 700 dg` |
| Yes/no, color, class | `yes`, `no`, `blue`, `stonic` |
| Direction / comparison | `a → b`; `x > y` |
| Any failure state | `unknown` |
| `CONFLICTED` between sources | `either <v1> or <v2>`; each also listed as an `alternative` line with its source |
| `CONFLICTED` observation vs. law | `unknown` |
| `CONFABULATED` | `possibly ` + the value, e.g. `possibly blue` |

Rate units are `Q/tick`. Conductance units are `Q·t/(dg·tick)`.

## 4. Epistemic states: when each one is produced

| State | Produced when |
|---|---|
| `KNOWN` | The value is given in the input or stated in the handbook's measured values (incl. `class`). A value after `shift` is computed, so it is `DERIVED`. |
| `DERIVED` | A law settles it from supported facts. |
| `UNKNOWN_REFERENCE` | A term, name, unit, relation or color can't be bound and has no close spelling. The failure goes to **the questions that depend on it**: questions about the entity the failing statement names, or about anything connected to that entity by a relation. Other questions are answered. |
| `LOW_CONFIDENCE` | A term isn't known but is a close spelling of a known term (edit distance ≤ 30% of the longer word). It fails the dependent questions in the same way. |
| `MISSING_KNOWLEDGE` | A law applies, but a needed fact is absent or too imprecise (e.g. a pell range that is only partly inside an operation's boundary; a missing conductance; a missing span). |
| `CONFLICTED` | Sources disagree, or an observation contradicts a law or a given value. **Low confidence, not a failure:** the answer is usable, is always marked, lists every alternative with its source, and carries an `experiment` that would settle it. If a result comes out the same under every source, the disagreement does not matter and the result is `DERIVED`. |
| `UNKNOWN_STRUCTURE` | The question word or attribute isn't part of the language. |
| `AMBIGUOUS_PATTERN` | Two laws reach opposite conclusions and neither law's pattern contains the other's. Law A is more specific than law B only when, with their conclusions aligned, every premise and condition of B appears in A and A has more. How many facts the input mentions does not matter. |
| `NO_VALID_RULE` | No law concludes anything about the question from the given situation. |
| `OUT_OF_BOUNDARY` | An operation's validity condition fails on the actual values (rate formula: pell difference > 400 dg, or span ≤ 0). |
| `CONFABULATED` | See below. |

### Detailed conventions

- **Glim laws.** L2 covers qelt only; L3 covers all stonics. A given pell and an observed glim that disagree under L2 are `CONFLICTED`; an observed glim on a stonic other than `none` is `CONFLICTED` (and so is asking for that entity's pell).
- **Facts about kinds are not entity values.** A kiv entity's pell is unknown unless given; the resting pell is not filled in automatically.
- **Rate** requires: contact between the two (direct `touches`, or a fluent bridge per L7); a pell for both (exact, or a range: the rate is then a range); the conductance of the higher-pell body's kind; and the `span` given on the `touches` relation (a fluent-bridge contact has no span, so its rate is `MISSING_KNOWLEDGE`). Boundaries are checked before computing. If the conductance sources disagree, the answer is `CONFLICTED` and lists one `alternative` per source.
- **Flows come in two channels.** Contact (L1: contact and a decidable pell difference) and radiant (L4: a glimming source with line of sight). The handbook's "thren only ever flows from a body with higher pell to a body with lower pell" binds **both**: a radiant path needs a glimming source *and* a receiver known to be at lower pell. `flows` holds if either channel does. If a channel's law applies but can't be decided (e.g. the receiver's pell is unknown), `MISSING_KNOWLEDGE`.
- **Harm** is contact-only (L5): `touches` between the kiv and a body; above 45 dg `yes`, at or below `no`, undecidable `MISSING_KNOWLEDGE`, no touching body `NO_VALID_RULE`.
- **Fenn wrap** (L6) blocks contact passage between the wrapped body and anything touching the wrap.
- **Interpretation convention (not in the handbook):** a fluent that soaks a fenn wrap touches the body the wrap encloses and anything that touches the wrap.
- **Headspace** is reported with the result (chunks, limit 4, and whether the input is "wide"); entities joined by `wraps` or `soaks` count as one chunk. Exceeding it no longer fails anything.

### Confabulation

- If the strict answer to a `glim` or `has glim` question is `NO_VALID_RULE`, and a glim law covers a **sibling** kind (same parent in the reference tree), that law is applied by analogy. The answer is `CONFABULATED`, prefixed `possibly`, with an `experiment`.
- Numbers are borrowed from a sibling kind **only** for `estimate value ...` questions whose strict answer is `MISSING_KNOWLEDGE`.
- Nothing else is ever confabulated.
