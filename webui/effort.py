"""How hard to think, from the message itself, when the effort is set to Auto.

A local model that always thinks its hardest takes a long time to answer "hi". With Auto, each
message gets a level from what it asks: a greeting or a thank-you none, a short question a little,
ordinary work a fair amount, and a long task, a bug hunt or an error to explain a lot. The message
can also say it in words: "think hard" or "ultrathink" for the most, "quickly" or "no need to think"
for the least. Anything else set in the effort menu (Low, Medium, High) is used as it is.
"""

from __future__ import annotations

import re
from typing import Tuple

_MOST = re.compile(r"\b(ultrathink|think (?:very |really )?(?:hard|deeply|carefully)|take your time|think it through)\b", re.I)
_LEAST = re.compile(r"\b(quick(?:ly)?|briefly|in short|no need to think|don'?t think|just answer|short answer)\b", re.I)
_SMALL_TALK = re.compile(r"^\s*(hi|hello|hey|yo|thanks?|thank you|thx|ok(?:ay)?|cool|great|nice|good (?:job|morning|night)|"
                         r"yes|no|yep|nope|sure|got it|perfect|awesome)\b[\s!.,?]*", re.I)
_WORK = re.compile(r"\b(fix|debug|implement|build|refactor|port|write|create|add|make|optimi[sz]e|rewrite|design|"
                   r"migrate|review|test|convert|integrate|update|change|remove|rename|install|set up|deploy)\b", re.I)
_TROUBLE = re.compile(r"(traceback|exception|error[: ]|failed|crash|segfault|stack trace|doesn'?t work|not working|broken|bug\b)",
                      re.I)
_CODE = re.compile(r"```|\b\w+\.(py|js|ts|cpp|c|h|hpp|cs|rs|go|java|json|yaml|toml|html|css)\b")


def choose(text: str) -> Tuple[str, str]:
    """(effort, why) for one message: effort is off, low, medium or high."""
    text = str(text or "")
    words = len(text.split())
    if _MOST.search(text):
        return "high", "you asked it to think hard"
    if _LEAST.search(text) and words < 60:
        return "low", "you asked for a quick answer"
    if _SMALL_TALK.match(text) and words <= 8:
        return "off", "small talk"
    trouble = bool(_TROUBLE.search(text))
    work = bool(_WORK.search(text))
    code = bool(_CODE.search(text))
    if trouble or words > 80 or (work and (code or words > 30)):
        return "high", "an error to look into" if trouble else "a bigger piece of work"
    if work or code:
        return "medium", "a piece of work"
    if words <= 15:
        return "low", "a short question"
    return "medium", "a question"


LABELS = {"off": "no thinking", "low": "light", "medium": "medium", "high": "deep"}
