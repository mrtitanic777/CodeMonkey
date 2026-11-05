/* Beginner mode (Settings > General): for someone who has never programmed.

   - Words: a programming word in a reply is underlined the first time; tapping it says what it means.
     The words come from GLOSSARY below, or from the model writing [[word|what it means]].
   - Choices: a ```choices block in a reply is a row of answer buttons.
   - Next steps: under the latest reply, "Next step", "Say that more simply" and "Why?".
   - What you've learned: every word tapped or met is kept (Settings > General).
   The model's side is BEGINNER_TEXT in webui/app.py. */

var GLOSSARY = {
  "variable": "A named box that holds a value, like score = 10.",
  "function": "A named set of steps you can run again and again by its name.",
  "loop": "Steps that repeat, for example once for every enemy on the screen.",
  "if statement": "A choice: do something only when a condition is true.",
  "condition": "A question with a yes or no answer, like \"is the score over 100?\".",
  "string": "A piece of text, like \"Game over\".",
  "integer": "A whole number, like 3 or -12.",
  "float": "A number with a decimal point, like 2.5.",
  "boolean": "A value that is either true or false.",
  "array": "A list of values kept in order, reached by their position.",
  "list": "Several values kept together in order.",
  "parameter": "A value a function asks for when you run it.",
  "argument": "The value you hand to a function when you run it.",
  "return": "What a function hands back when it finishes.",
  "class": "A blueprint for making things of the same kind, like a blueprint for enemies.",
  "object": "One thing made from a class, with its own values.",
  "method": "A function that belongs to an object.",
  "bug": "A mistake in the code that makes it do the wrong thing.",
  "debug": "Finding out why the code does the wrong thing, and fixing it.",
  "compile": "Turning the code you write into a program the computer can run.",
  "compiler": "The program that turns your code into a program the computer can run.",
  "build": "Making the finished program from the code (compiling and putting the pieces together).",
  "syntax": "The spelling and punctuation rules of a programming language.",
  "syntax error": "The code breaks the language's spelling or punctuation rules, so it cannot run.",
  "error": "A message saying something went wrong, and usually where.",
  "exception": "An error that happens while the program is running.",
  "library": "Code someone else wrote that you can use in your program.",
  "import": "Bringing in code from another file or library so you can use it.",
  "module": "A file of code that other code can use.",
  "terminal": "A window where you type commands for the computer instead of clicking.",
  "command": "An instruction typed in the terminal.",
  "repository": "A project's folder together with the history of every change, kept by git.",
  "git": "A tool that keeps a history of every change to a project, so nothing is lost.",
  "commit": "A saved point in a project's history, with a note of what changed.",
  "branch": "A separate line of changes, so you can try something without touching the main version.",
  "API": "The set of commands one program offers to other programs.",
  "SDK": "A kit of tools, code and documents for making programs for one system, like the Xbox 360.",
  "framework": "Ready-made structure that a program is built on.",
  "pointer": "A value that says where in memory something is kept.",
  "memory": "Where a running program keeps its values while it works.",
  "header file": "A file that lists what functions and types a piece of code offers (C and C++).",
  "type": "What kind of value something is: a number, text, a list and so on.",
  "comment": "A note in the code for people to read; the computer skips it.",
  "indentation": "Spaces at the start of a line that show which code belongs together.",
  "frame": "One picture of a game; games draw many frames every second.",
  "sprite": "A small picture in a game, like a character or a coin.",
  "texture": "A picture painted onto something the game draws.",
  "render": "Drawing the picture on the screen.",
  "emulator": "A program that pretends to be another machine, so its programs run here.",
  "port": "Making a program that runs on one system run on another (as a verb).",
  "refactor": "Tidying code without changing what it does.",
  "test": "A small program that checks your code does what it should.",
  "JSON": "A simple way of writing data as text, with names and values.",
  "HTML": "The language web pages are written in.",
  "CSS": "The language that sets how a web page looks: colours, sizes, layout.",
  "JavaScript": "The programming language web pages run.",
  "Python": "A popular programming language known for being easy to read.",
  "C++": "A fast programming language used for games and systems.",
  "server": "A program that waits for requests and answers them.",
  "localhost": "This computer itself, as an address.",
  "dependency": "Another piece of software your program needs in order to work.",
  "path": "Where a file is, written as folders and a name, like src/game.cpp."
};
var GLOSSARY_RE = null;

function beginnerOn() { return !!(S.settings && S.settings.beginner); }

function learned() { return (S.settings && S.settings.ui && S.settings.ui.learned) || {}; }
function learn(word) {
  var key = word.toLowerCase(), have = learned();
  if (have[key]) return;
  var next = {};
  Object.keys(have).forEach(function (k) { next[k] = have[k]; });
  next[key] = new Date().toISOString().slice(0, 10);
  saveUi("learned", next);
}

/* [[word|meaning]] in the model's text: an underlined word (renderInline calls this). */
function termHtml(word, meaning) {
  return '<span class="term" tabindex="0" role="button" data-tip="' + meaning + '">' + word + "</span>";
}

/* The glossary's words in a finished reply: the first of each is underlined. */
function markTerms(root) {
  if (!beginnerOn() || !root || root._terms) return;
  root._terms = true;
  if (!GLOSSARY_RE) {
    var words = Object.keys(GLOSSARY).sort(function (a, b) { return b.length - a.length; })
      .map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
    GLOSSARY_RE = new RegExp("(^|[^\\w+])(" + words.join("|") + ")(?=$|[^\\w+])", "i");
  }
  var seen = {};
  Array.prototype.forEach.call(root.querySelectorAll(".term"), function (t) { seen[t.textContent.toLowerCase()] = true; });
  var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: function (n) {
      return n.parentNode.closest("code, pre, a, .term, .acts, .choices, .codeblock, h3") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    }
  });
  var nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(function (node) {
    var text = node.nodeValue, m, guard = 0;
    while ((m = GLOSSARY_RE.exec(text)) && guard++ < 20) {
      var word = m[2], key = word.toLowerCase(), at = m.index + m[1].length;
      var meaning = GLOSSARY[key] || GLOSSARY[Object.keys(GLOSSARY).filter(function (k) { return k.toLowerCase() === key; })[0]];
      if (seen[key] || !meaning) { text = text.slice(0, at) + "\u0000".repeat(word.length) + text.slice(at + word.length); continue; }
      seen[key] = true;
      var after = node.splitText(at);
      node = after.splitText(word.length);
      var span = h("span", "term", word);
      span.tabIndex = 0;
      span.setAttribute("role", "button");
      span.setAttribute("data-tip", meaning);
      after.parentNode.replaceChild(span, after);
      text = node.nodeValue;
    }
  });
}

/* A word tapped: its meaning in a bubble beside it (and kept as learned). */
function showTerm(el) {
  var old = $("termtip");
  if (old) { old.remove(); if (old._for === el) return; }
  var tip = h("div", "termtip");
  tip.id = "termtip";
  tip._for = el;
  tip.setAttribute("role", "tooltip");
  tip.appendChild(h("b", "", el.textContent));
  tip.appendChild(h("span", "", el.getAttribute("data-tip")));
  document.body.appendChild(tip);
  var r = el.getBoundingClientRect(), w = tip.offsetWidth;
  tip.style.left = Math.max(12, Math.min(innerWidth - w - 12, r.left + r.width / 2 - w / 2)) + "px";
  var below = r.bottom + 8 + tip.offsetHeight < innerHeight;
  tip.style.top = (below ? r.bottom + 8 : r.top - tip.offsetHeight - 8) + "px";
  learn(el.textContent);
}
document.addEventListener("click", function (e) {
  var t = e.target.closest && e.target.closest(".term");
  if (t) { e.preventDefault(); showTerm(t); return; }
  var tip = $("termtip");
  if (tip && !e.target.closest(".termtip")) tip.remove();
  var c = e.target.closest && e.target.closest(".choices button");
  if (c) answerChoice(c);
});
document.addEventListener("keydown", function (e) {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("term")) { e.preventDefault(); showTerm(e.target); }
  if (e.key === "Escape" && $("termtip")) $("termtip").remove();
});

/* ```choices: one answer per line, as buttons (renderMarkdown calls this). */
function choicesHtml(lines) {
  var items = lines.map(function (l) { return l.replace(/^\s*(?:[-*]|\d+[.)])\s*/, "").trim(); }).filter(Boolean).slice(0, 6);
  if (!items.length) return "";
  return '<div class="choices" role="group" aria-label="Choose an answer">' +
    items.map(function (t) { return '<button type="button" class="btn">' + renderInline(t) + "</button>"; }).join("") + "</div>";
}
function sendText(text) {
  var input = $("input");
  input.value = text;
  autosize();
  sendMessage();
}
function answerChoice(btn) {
  var box = btn.parentNode;
  if (box.classList.contains("done")) return;
  if (S.turn && !S.turn.done && S.turn.reply && !S.turn.reply.contains(box)) { toast("Wait for the answer to finish"); return; }
  box.classList.add("done");
  btn.classList.add("picked");
  Array.prototype.forEach.call(box.querySelectorAll("button"), function (b) { b.disabled = true; });
  sendText(btn.textContent);
}

/* Under the latest finished reply: the easy ways to carry on. */
var NEXT_STEPS = [["Next step", "Next step, please."], ["Say that more simply", "Could you explain that more simply?"], ["Why?", "Why does that work?"]];
function addNextSteps(reply) {
  document.querySelectorAll(".nextsteps").forEach(function (n) { n.remove(); });
  if (!beginnerOn() || !reply || reply.querySelector(".choices:not(.done)")) return;
  var row = h("div", "nextsteps");
  NEXT_STEPS.forEach(function (s) {
    var b = row.appendChild(h("button", "btn", s[0]));
    b.type = "button";
    b.onclick = function () { row.remove(); sendText(s[1]); };
  });
  reply.appendChild(row);
}

/* Settings > General: the switch, and what you've learned so far. */
function beginnerSettings(pane) {
  pane.appendChild(row("srow", "Beginner mode", "New to programming? Plain words, one small step at a time, tap an underlined word to see what it means, and easy questions with buttons",
    toggleControl(beginnerOn(), function (on) {
      saveSetting({ beginner: on }).then(function () { showPage("general"); });
    })));
  if (!beginnerOn()) return;
  var words = Object.keys(learned()).sort();
  var box = h("div", "learned");
  box.appendChild(h("b", "", words.length ? "Words you've learned: " + words.length : "Words you've learned"));
  if (!words.length) box.appendChild(h("i", "", "Tap an underlined word in a reply and it is added here."));
  var chips = box.appendChild(h("div", "chips"));
  words.forEach(function (w) {
    var key = Object.keys(GLOSSARY).filter(function (k) { return k.toLowerCase() === w; })[0];
    var c = chips.appendChild(h("span", "term", key || w));
    c.tabIndex = 0;
    c.setAttribute("role", "button");
    c.setAttribute("data-tip", GLOSSARY[key] || "A word from your conversations.");
  });
  pane.appendChild(box);
}

/* The start screen, once on a new install: an offer to switch it on. */
function beginnerOffer(box) {
  var shown = document.querySelector(".beginoffer");
  if (shown) shown.remove();
  if (beginnerOn() || store("cm:beginner-asked") || (S.list && S.list.length > 2)) return;
  var card = h("div", "beginoffer");
  card.appendChild(h("b", "", "New to programming?"));
  card.appendChild(h("span", "", "Beginner mode explains in plain words, one step at a time."));
  var row2 = card.appendChild(h("div", "btnrow"));
  row2.appendChild(button("Turn it on", function () {
    store("cm:beginner-asked", true);
    saveSetting({ beginner: true }).then(function () { card.remove(); toast("Beginner mode is on"); });
  }, "primary"));
  row2.appendChild(button("No thanks", function () { store("cm:beginner-asked", true); card.remove(); }));
  box.parentNode.insertBefore(card, box);
}
