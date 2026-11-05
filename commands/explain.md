---
name: explain
description: Explain how the given code works, grounded in actual reads.
---

Explain this code: $ARGUMENTS

Rules for a good explanation:

1. **Read before explaining.** Use `read` (and `grep` for callers) on the
   actual files. Never explain from the name alone. Cite `file:line`
   when pointing at specific behavior.
2. **Top-down.** Start with what it does in one sentence, then how it's
   organized (the main pieces and their responsibilities), then the
   interesting details (the non-obvious decision, the subtle invariant).
3. **Follow the data.** Trace one concrete example through the code:
   what goes in, what happens at each step, what comes out.
4. **Name the why.** For every surprising choice, explain the reason
   (or say it's unknown). "It retries 3 times" is trivia; "it retries
   because the hook server can cold-start" is understanding.
5. **End with the mental model.** One paragraph the reader can carry
   away: how should they *think* about this piece from now on?

Stay grounded: every claim should trace back to something you actually read.
