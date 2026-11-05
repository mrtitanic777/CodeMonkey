"""Facts at points in time (kernel item 8).

Any line may start with `at <t>`; a line without it is at time 0. A question `at <t> ask ...` sees
every fact as it stands at t: each holds from its time until a later line changes it (a `shift`
changes a value and, as always, discards earlier observations of it; `unrel <relation> <a> <b>` ends
a relation). Each moment asked about is answered on its own, as an ordinary input of the facts that
hold then, and the answers are labelled with their moment.

What it does not do: say where things go next on their own. That needs a law for how fast a quantity
changes, and the domain may not have one; the kernel says so rather than guessing.
"""

QUESTIONS = ("ask", "estimate")


def is_timed(text):
    return any(line.strip().split()[:1] == ["at"] or line.strip().startswith("unrel ")
               for line in text.splitlines() if line.strip())


def _parse(text):
    """[(time, order, tokens)] for each statement; (time, tokens) for each question."""
    lines, asks = [], []
    for order, raw in enumerate(text.splitlines()):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        t = 0.0
        if tokens[0] == "at":
            try:
                t = float(tokens[1])
            except (IndexError, ValueError):
                t = None   # a malformed time: the line is passed on as written, to fail where it would
            tokens = tokens[2:] if t is not None else tokens
            t = 0.0 if t is None else t
        if not tokens:
            continue
        if tokens[0] in QUESTIONS:
            asks.append((t, tokens))
        else:
            lines.append((t, order, tokens))
    return lines, asks


def compile_at(lines, moment):
    """The ordinary input that holds at `moment`: statements up to then, in time order, minus the
    relations that ended."""
    kept = []
    for t, order, tokens in sorted((x for x in lines if x[0] <= moment), key=lambda x: (x[0], x[1])):
        if tokens[0] == "unrel" and len(tokens) >= 4:
            ended = ("rel",) + tuple(tokens[1:4])
            kept = [k for k in kept if tuple(k[:4]) != ended]
            continue
        kept.append(tokens)
    return kept


def _fmt(t):
    return str(int(t)) if float(t).is_integer() else str(t)


def run(kernel, text, learn=True):
    """Answer every moment asked about; the answers in the order the questions were written."""
    from .kernel import Answer, Result
    lines, asks = _parse(text)
    moments = sorted({t for t, tok in asks if tok[1:2] != ["next"]})
    answers, texts, steps, op_calls = [], [], [], 0
    by_moment = {}
    for moment in moments:
        compiled = "\n".join(" ".join(tok) for tok in compile_at(lines, moment))
        questions = "\n".join(" ".join(tok) for t, tok in asks if t == moment and tok[1:2] != ["next"])
        source = (compiled + "\n" + questions).strip()
        result = kernel.run(source, learn=False)
        by_moment[moment] = (source, list(result.answers))
        texts.append(f"at {_fmt(moment)}:\n{result.text}")
        steps = steps or result.steps
        op_calls += result.op_calls
    cursor = {m: 0 for m in moments}
    for t, tokens in asks:
        if tokens[1:2] == ["next"]:   # where it goes on its own: needs a law the domain may lack
            from .states import State
            from .values import failure
            cause = ("the domain has no law for how fast a quantity changes over time, so where things go "
                     "next cannot be worked out; ask about a moment instead (at <t> ask ...)")
            if kernel.domain.get("dynamics"):
                cause = "prediction from the domain's dynamics is not built yet"
            answers.append(Answer(f"{' '.join(tokens[1:])} after {_fmt(t)}", failure(State.MISSING_KNOWLEDGE, cause),
                                  "plan"))
            continue
        source, got = by_moment[t]
        a = got[cursor[t]]
        cursor[t] += 1
        answers.append(Answer(f"{a.goal} at {_fmt(t)}", a.value, a.step, a.rests_on, a.clashes, a.proof, source))
    if learn:   # every observation and measurement is learned once, from all the statements
        kernel.run("\n".join(" ".join(tok) for tok in compile_at(lines, float("inf"))), learn=True)
    return Result(answers, "\n\n".join(texts), op_calls, {}, steps, None, ())
