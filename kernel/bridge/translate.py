"""Input translator: English -> controlled language. A transcriber, not a reasoner.

It knows the grammar, not the domain. Unknown or misspelled words are copied as written so the
kernel's matcher, not the translator, decides what they mean. It learns in situ: translations
confirmed in use become examples for later ones. No training.
"""
import json
import os
import re

from . import grammar as shapes
from .llm import chat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLACEHOLDERS = ("gorp", "blint", "snib", "blintt", "trisk", "wemble", "dreel", "triskk")  # worked examples' made-up words

SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
          "required": ["lines"], "additionalProperties": False}

RULES = """You translate English into the controlled input language specified below. You are a transcriber, not a reasoner.

Rules:
1. Output only statements of the language, one per list item. No commentary.
2. Copy every term (kinds of things, colors, units, names) exactly as the English spells it, even if it looks misspelled or unfamiliar. Never correct a spelling. Never replace a word with a similar known word. This includes colors and units.
3. State only what the English states. Never add a value, span, relation or fact the English does not give (for example, do not give something a pell unless the English gives one). Never work out answers.
4. Introduce each described thing with `let <name> = <term> ...`, using a short unique name. Keep numbers and units as written (7 Gdg stays `pell 7 Gdg`). "Not glimming" is `glim none`. What is observed or reported about a thing ("seen glimming blue", "reported glimming green") also goes on its `let` line.
5. Relations: `rel touches a b` (add `span <n> t` only if a path length is given); `rel wraps <fenn> <thing>`; `rel soaks <fluid> <fenn>`; `rel sight a b` for line of sight.
6. "Suppose X is a new kind of Y" is `teach X isa Y`. "X was confirmed to mean Y" is `confirm X means Y`.
7. One `ask` line per question, in the order asked. Use `estimate` instead of `ask` when the English asks for a best guess or estimate.
8. If a question fits no question form, still write it as `ask <key word of the question> <names>` so it is not lost. Never drop a question.
9. If the English refers to a name that was never described, use that name as written anyway.
10. The term after `=` is the single word naming the kind of thing. Leave out words for shape or form such as block, rod, piece, pool, puddle.
11. Starting values go on the `let` line. `shift` is only for a change the English describes: "rises by 50 dg" is `shift x pell 50 dg`, "drops by 50 dg" is `shift x pell -50 dg`.
12. Question forms, and only these:
    - what color does X glim -> `ask glim x`
    - does X glim / can X ever glim -> `ask has glim x`
    - what is X's pell -> `ask pell x`
    - what is the passage rate (between X and Y) -> `ask rate x y`
    - which way does thren flow (between X and Y), does thren reach Y from X -> `ask flows x y`
    - is X harmed -> `ask harmed x`
    - is there contact passage between X and Y -> `ask contact x y`
    - what is the <attribute> of <term> (conductance, harm threshold, resting pell) -> `ask value <term> <attribute>` with the attribute written conductance, harm_threshold or resting_pell
    - what class is <term> -> `ask class <term>`
    - which is higher, or how do X and Y compare in <quantity or attribute> -> `ask compare <quantity or attribute> x y`
13. `estimate` takes exactly the same question forms as `ask`.
14. Never write placeholders such as <n> or <unit>, and never relate a thing to itself. If a value is not given, leave it out.
15. Never replace the quantity or attribute a question names with a different one.

"""

EXAMPLES_A = """Worked examples (the words gorp, blint, snib and blintt are placeholders):

English: A gorp block at 3 Gdg touches a blint rod at 40 dg along a 2 tal path. Which way does thren flow, and what is the passage rate? What color does the gorp glim?
Lines: let g = gorp pell 3 Gdg | let b = blint pell 40 dg | rel touches g b span 2 t | ask flows g b | ask rate g b | ask glim g

English: Suppose a snib is a new kind of fluent substance. What class is snib? What is your best guess for the conductance of snib?
Lines: teach snib isa fluent | ask class snib | estimate value snib conductance

English: A gorp that is not glimming touches a blintt. Is the blintt harmed? Who made the gorp?
Lines: let g = gorp glim none | let b = blintt | rel touches g b | ask harmed b | ask maker g"""

# A second translator gets different worked examples, so the two do not share a prompt's mistakes.
EXAMPLES_B = """Worked examples (the words trisk, wemble, dreel and triskk are placeholders):

English: The wemble slab is at 7 Gdg and a trisk pebble sits against it at 300 dg with a 5 t contact path. How fast does thren move between them, and does the wemble glim?
Lines: let w = wemble pell 7 Gdg | let t = trisk pell 300 dg | rel touches w t span 5 t | ask rate w t | ask has glim w

English: A dreel was seen glimming red, then its pell fell by 200 dg. What is its pell now, and which is higher, the dreel's pell or that of a trisk at 800 dg?
Lines: let d = dreel glim red | shift d pell -200 dg | let t = trisk pell 800 dg | ask pell d | ask compare pell d t

English: Take it that triskk means trisk. A triskk rests against a dreel wrapped around a wemble. Is there contact passage between the triskk and the wemble? What is the wemble's melting point?
Lines: confirm triskk means trisk | let t = triskk | let d = dreel | let w = wemble | rel wraps d w | rel touches t d | ask contact t w | ask melting w"""


def grammar():
    """Sections 1-2 of LANGUAGE.md: the statements and questions. The translator needs nothing else."""
    with open(os.path.join(ROOT, "LANGUAGE.md"), encoding="utf-8") as f:
        text = f.read()
    return text.split("## 3.", 1)[0]


def _words(text):
    return set(re.findall(r"[a-z0-9.-]+", text.lower()))


class Translator:
    def __init__(self, model, examples=0, worked=None, domain=None):
        self.model = model
        self.examples = examples
        self.memory = []  # (english, controlled text, word set): confirmed translations, learned in use
        self.system = RULES + "\n\n" + (worked or EXAMPLES_A) + "\n\n" + grammar()
        # with the domain, each line is held to the language's shape while it is written (bridge/grammar.py)
        self.domain = domain
        self.schema = shapes.schema(domain) if domain else SCHEMA
        self.malformed = []   # lines of the last translation that do not have a statement's shape

    def translate(self, english):
        messages = [{"role": "system", "content": self.system}]
        if self.examples:
            words = _words(english)
            for past_english, past_text, _ in sorted(self.memory, key=lambda m: -len(words & m[2]))[:self.examples]:
                messages.append({"role": "user", "content": past_english})
                messages.append({"role": "assistant", "content": json.dumps({"lines": past_text.splitlines()})})
        messages.append({"role": "user", "content": english})
        lines = [line.strip() for line in json.loads(chat(self.model, messages, schema=self.schema))["lines"]
                 if line.strip()]
        self.malformed = shapes.malformed(self.domain, lines) if self.domain else []
        return "\n".join(lines)

    def remember(self, english, text):
        """A translation confirmed in use becomes an example for later ones."""
        self.memory.append((english, text, _words(english)))
