---
name: deep-research
description: Use when the user asks a hard question needing thorough multi-step investigation — comparisons, "what's the best X", or anything where being wrong is costly.
keywords: [research, investigate, investigation, compare, analysis, report, look into, find out]
---

# Deep Research

Use this playbook when the user asks a question that can't be answered from
a single lookup: comparisons, buying decisions, technical investigations,
"what's the best X", or anything where being wrong is costly.

## The method

### 1. Clarify the real question
Restate what the user actually wants to know and what a good answer looks
like. If the question is ambiguous (budget? timeline? constraints?), ask
one round of clarifying questions before burning effort.

### 2. Decompose into sub-questions
Break the investigation into 2–5 independent sub-questions. Examples:
- "What are the leading options and their trade-offs?"
- "What do recent reviews / changelogs / docs say?"
- "What are the costs, risks, or gotchas?"

### 3. Fan out with `delegate`
Hand each sub-question to the `delegate` tool as a separate child agent
with a crisp brief: the sub-question, what output format you want back
(bullets, a short table, key facts with sources), and a reminder to say
when evidence is thin. Children work in parallel; you synthesize.

Keep delegations independent. Don't chain them unless step B truly needs
step A's answer.

### 4. Gather from multiple angles
Each investigator (you or a child) should:
- Use `web_fetch` on primary sources first (official docs, specs,
  changelogs), then secondary ones (reviews, discussions).
- Fetch at least two independent sources for any contested claim.
- Prefer recent sources; note the date of anything time-sensitive.

### 5. Verify before synthesizing
- If two sources disagree, say so explicitly. Don't smooth it over.
- Distinguish *fact* ("the docs say X") from *inference* ("so X probably
  means Y for your case").
- If evidence is thin, say "I couldn't verify this" rather than guessing.

### 6. Synthesize
Write the final answer with:
- A 2–3 sentence bottom line up front.
- The key findings, organized by sub-question.
- Trade-offs or a comparison table when options are involved.
- A short "what I'd do" recommendation *only* if the user asked for one.
- Caveats: what you couldn't verify, and what would change the answer.

## Anti-patterns

- Don't delegate the whole investigation as one blob; decomposition is the
  point.
- Don't present a single source's marketing copy as a conclusion.
- Don't keep researching past the point of diminishing returns: 3–6 solid
  sources beat 20 shallow ones.
