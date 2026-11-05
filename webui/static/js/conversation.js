"use strict";
/* CodeMonkey page, conversation.js: The conversation: turns, steps, replies, cards.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- conversation ---------------- */

function isAtBottom() {
  var w = $("chat-wrap");
  return w.scrollHeight - w.scrollTop - w.clientHeight < 80;
}
function scrollToEnd(smooth) {
  var w = $("chat-wrap"), far = w.scrollHeight - w.scrollTop - w.clientHeight > 1500;
  if (smooth === true && !far && w.scrollTo) {
    // a short glide; the arrow stays away while it moves instead of flickering back
    S.gliding = Date.now();
    w.scrollTo({ top: w.scrollHeight, behavior: "smooth" });
  } else {
    w.scrollTop = w.scrollHeight;
  }
  $("new-msg-pill").classList.add("hidden");
  $("new-msg-pill").classList.remove("has-new");
}
/* Opening a session: turns out of view are drawn at an estimated height until they come into view,
   so one jump to the end lands short. For a moment the view stays pinned to the end as the last
   turns take their real size, unless you scroll yourself. */
function stickToEnd(ms) {
  var w = $("chat-wrap"), until = Date.now() + (ms || 1500), stop = false;
  function quit() { stop = true; }
  ["wheel", "touchstart", "keydown", "mousedown"].forEach(function (t) { w.addEventListener(t, quit, { once: true, passive: true }); });
  (function pin() {
    if (stop) return;
    scrollToEnd();
    if (Date.now() < until) requestAnimationFrame(pin);
  })();
}
/* Follow new content only when the reader is already at the bottom; otherwise the arrow says
   there is something new below. */
function follow(wasAtBottom) {
  if (wasAtBottom) scrollToEnd();
  else { $("new-msg-pill").classList.remove("hidden"); $("new-msg-pill").classList.add("has-new"); }
}
/* The arrow shows whenever the latest is out of view: scrolled up, or pushed under a growing
   input area. */
function updateToBottom() {
  var at = isAtBottom(), pill = $("new-msg-pill");
  if (S.gliding && !at && Date.now() - S.gliding < 1500) return;
  S.gliding = 0;
  pill.classList.toggle("hidden", at || $("chat").classList.contains("hidden"));
  if (at) pill.classList.remove("has-new");
}

function newTurn() {
  showHello(false);
  var el = h("div", "turn");
  $("chat").appendChild(el);
  S.turn = { el: el, work: null, pending: [], n: 0, failed: 0, started: Date.now(), reply: null, errored: false };
  return S.turn;
}
function ensureTurn() { return S.turn || newTurn(); }

function addUserMsg(text) {
  var turn = newTurn();
  turn.lastUser = text;
  var el = h("div", "user");
  el.innerHTML = renderMarkdown(text);
  turn.el.appendChild(el);
  var back = turn.el.appendChild(h("button", "userback"));   // shown on hover: rewind to before this message
  back.type = "button";
  back.innerHTML = svg("undo");
  back.appendChild(h("span", "", "Go back to here"));
  back.title = "Go back to before this message: the conversation, and the files if you choose";
  back.onclick = function () {
    var all = Array.prototype.slice.call(document.querySelectorAll("#chat .user"));
    openRewind(all.indexOf(el), text);
  };
  scrollToEnd();
}

/* Tool names as Claude Code shows them (its own tools by their names; ours in the same style). */
var STEP_NAME = { read: "Read", glob: "Glob", grep: "Grep", edit: "Edit", write_file: "Write",
  bash: "Bash", todo_write: "Update Todos", todo_list: "Todos", delegate: "Task", subagent_spawn: "Task",
  subagent_list: "Tasks", subagent_send: "Task", subagent_close: "Task", memory_save: "Remember",
  memory_search: "Recall", web_search: "WebSearch", web_fetch: "WebFetch", browser_navigate: "Browser",
  browser_read: "Browser", browser_links: "Browser", browser_click: "Browser", browser_click_text: "Browser",
  browser_fill: "Browser", browser_screenshot: "Screenshot", cron_add: "Schedule", cron_list: "Schedule",
  cron_update: "Schedule", cron_remove: "Schedule", cron_run: "Schedule", cron_runs: "Schedule",
  git_status: "Git status", git_diff: "Git diff", process_start: "Start", process_logs: "Output", process_stop: "Stop",
  process_list: "Programs", test_focus: "Tests", notebook_edit: "NotebookEdit",
  diagnostics: "Diagnostics", lsp_definition: "Definition", lsp_references: "References", lsp_hover: "Hover",
  lsp_symbols: "Outline", tools_load: "Load tools" };

/* What a finished step came to, in a few words: "120 lines", "3 matches" (Claude Code's result line). */
function stepResult(name, res) {
  res = String(res || "");
  if (/^\s*ERROR/.test(res)) return res.replace(/^\s*ERROR:?\s*/, "").split("\n")[0].slice(0, 60);
  var lines = res.replace(/\n\(ignored arguments[^)]*\)$/, "").split("\n").filter(function (l) { return l.trim(); });
  if (name === "read") return lines.length + (lines.length === 1 ? " line" : " lines");
  if (name === "grep") return /^\(no |^No /.test(res) ? "no matches" : lines.length + (lines.length === 1 ? " match" : " matches");
  if (name === "glob") return /^\(no files/.test(res) ? "no files" : lines.length + (lines.length === 1 ? " file" : " files");
  return "";
}
var STEP_VERB = { read: "Reading", glob: "Finding", grep: "Searching for", edit: "Editing",
  write_file: "Writing", bash: "Running", delegate: "A helper is working on", todo_write: "Updating the to-do list",
  process_start: "Starting", process_logs: "Reading the output of", process_stop: "Stopping",
  test_focus: "Running the tests for what changed" };

function stepSummary(name, args) {
  args = args || {};
  if (name === "bash") return String(args.cmd || args.command || "");
  if (name === "process_start") return String(args.command || "");
  if (/^process_/.test(name)) return String(args.name || "");
  if (name === "test_focus") return String(args.paths || "changed files");
  if (name === "grep") return String(args.pattern || "") + (args.path && args.path !== "." ? " in " + args.path : "");
  if (name === "glob") return String(args.pattern || "");
  if (args.brief) return String(args.brief);
  if (args.fact) return String(args.fact);
  if (args.url) return String(args.url);
  if (args.query) return String(args.query);
  if (name === "todo_write") return (args.todos && args.todos.length ? "" : "") + "plan updated";
  if (args.path) return String(args.path);
  var keys = Object.keys(args);
  return keys.length ? keys[0] + "=" + String(args[keys[0]]) : "";
}

/* A step as one plain sentence, the way Claude Code lists tool calls. */
/* A step while it runs, said the way Claude Code says it: "Running web UI test files", "Reading
   app.js". A command's description is its words; without one it is just "Running a command". */
function gerund(verb) {
  var v = verb.toLowerCase();
  if (/ing$/.test(v) && v.length > 4) return verb;
  if (/ie$/.test(v)) return verb.slice(0, -2) + "ying";
  if (/[^aeiou]e$/.test(v) && v !== "be") return verb.slice(0, -1) + "ing";
  if (/^(run|get|set|put|stop|cut|let|hit|sit|plan|shut|ship|skip|drop|grep|chop|wrap|tap|map|zip|pin|log|tag|dig|swap)$/.test(v)) return verb + verb.slice(-1) + "ing";
  return verb + "ing";
}
function stepDoing(name, args) {
  args = args || {};
  var base = function (p) { return String(p || "").split(/[\/]/).pop() || "a file"; };
  if (name === "bash" || name === "process_start") {
    var desc = String(args.description || "").trim().split("\n")[0];
    if (!desc) return name === "bash" ? "Running a command" : "Starting a process";
    if (!/^[A-Z][a-z]+(\s|$)/.test(desc)) return desc;   // not a sentence ("npm test"): as written
    var m = /^(\S+)(.*)$/.exec(desc);
    var first = m[1].charAt(0).toUpperCase() + m[1].slice(1);
    return (/^[A-Za-z]+$/.test(m[1]) ? gerund(first) : first) + m[2];
  }
  if (name === "read") return "Reading " + base(args.path);
  if (name === "edit") return "Editing " + base(args.path);
  if (name === "write_file") return "Writing " + base(args.path);
  if (name === "grep" || name === "code_search") return "Searching the code";
  if (name === "glob") return "Finding files";
  if (name === "todo_write") return "Updating the to-do list";
  if (name === "web_search") return "Searching the web";
  if (name === "web_fetch") return "Reading a web page";
  if (name === "delegate" || name === "subagent_spawn") return "Asking a helper";
  if (name === "code_explore") return "Exploring the code";
  if (/^git_/.test(name)) return "Checking git";
  if (/^browser_/.test(name)) return "Using the browser";
  return "Using " + stepLabel(name).toLowerCase();
}

function stepTitle(name, args) {
  args = args || {};
  var one = function (t, n) { t = String(t || "").split("\n")[0]; return t.length > n ? t.slice(0, n - 1) + "\u2026" : t; };
  var where = args.path && args.path !== "." ? " in " + args.path : "";
  if (name === "bash") return args.description ? String(args.description) : "Ran " + one(args.cmd || args.command, 80);
  if (name === "read") return "Read " + (args.path || "a file");
  if (name === "grep") return "Searched for " + one(args.pattern, 60) + where;
  if (name === "glob") return "Found files matching " + one(args.pattern, 60);
  if (name === "edit") return "Edited " + (args.path || "a file");
  if (name === "write_file") return "Wrote " + (args.path || "a file");
  if (name === "todo_write") return "Updated the to-do list";
  if (name === "web_search") return "Searched the web for " + one(args.query, 60);
  if (name === "web_fetch") return "Fetched " + one(args.url, 70);
  if (name === "process_start") return "Started " + one(args.command || args.name, 70);
  if (name === "delegate" || name === "subagent_spawn") return "Asked a helper: " + one(args.brief, 70);
  if (name === "code_explore") return "Explored " + one(args.query || args.question || stepSummary(name, args), 70);
  if (name === "code_search") return "Searched code for " + one(args.query || stepSummary(name, args), 60);
  var summary = stepSummary(name, args);
  return stepLabel(name) + (summary ? " " + one(summary, 70) : "");
}

/* A step's label: the short word for a known tool, else its name made readable
   ("code_callers" -> "callers", "mcp__github__search_issues" -> "search issues"). */
function stepLabel(name) {
  if (STEP_NAME[name]) return STEP_NAME[name];
  var m = /^mcp__([^_]+)__(.+)$/.exec(String(name));
  var base = m ? m[2] : String(name).replace(/^code_/, "");
  base = base.replace(/_/g, " ");
  return (m ? m[1] + " \u00b7 " : "") + base.charAt(0).toUpperCase() + base.slice(1);
}

/* The steps of a turn, folded to one line until it is opened: what it is doing now while it
   works, and how long it took when done. */
function workBlock(turn) {
  if (turn.work) return turn.work;
  var det = h("details", "work");
  det.open = S.view === "verbose";   // Normal: tool calls folded into one line; Verbose: every step shown
  var sum = h("summary");
  sum.innerHTML = '<span class="wi"><i class="spin"></i></span><span class="wt">Working</span><span class="wnow"></span>' + svg("chev", "chev");
  det.appendChild(sum);
  det.appendChild(h("div", "steps"));
  // the reply, plan or error always comes after the activity line
  turn.el.insertBefore(det, turn.reply || null);
  turn.work = det;
  return det;
}

/* Text, then steps, then text: each piece stays where it was written. The words before a step are
   sealed in place, and the steps before them are summed up on their own line. */
function sealSegment(turn) {
  if (!turn.reply || !turn.reply._text) return;
  var cur = turn.reply.querySelector(".cursor");
  if (cur) cur.remove();
  turn.reply.classList.add("interim");
  drawDiagrams(turn.reply);
  markTerms(turn.reply);
  turn.reply = null;
  if (turn.work) {
    var keep = S.turn;
    S.turn = turn; finishWork(null); S.turn = keep;
    turn.work = null; turn.n = 0; turn.failed = 0;
  }
}

function stepStart(d, live) {
  var turn = ensureTurn(), bottom = isAtBottom();
  sealSegment(turn);
  var steps = workBlock(turn).querySelector(".steps");
  var summary = stepSummary(d.name, d.args);
  var row = h("button", "step");
  row.type = "button";
  row.innerHTML = '<i class="spin"></i><span class="t"></span><span class="d"></span>';
  row.querySelector(".t").textContent = stepTitle(d.name, d.args);
  row.title = stepLabel(d.name) + (summary ? ": " + summary : "");
  row._name = d.name; row._args = d.args || {}; row._t0 = Date.now(); row._live = !!live;
  row.onclick = function () { toggleStepDetail(row); };
  steps.appendChild(row);
  turn.pending.push(row);
  turn.n++;
  if (live) {
    S.liveTool = d.name;
    // what it is doing, in words ("Running web UI test files"); the command itself is in the step's detail
    var doing = stepDoing(d.name, d.args);
    S.liveStep = esc(doing);
    turn.work.querySelector(".wt").textContent = doing;
    turn.work.querySelector(".wt").title = turn.n + (turn.n === 1 ? " step" : " steps") + " so far";
    turn.work.querySelector(".wnow").textContent = "";
    tickLive();
    follow(bottom);
  }
}

function stepEnd(d) {
  var turn = S.turn;
  if (!turn) return;
  var row = null, bottom = isAtBottom();
  for (var i = 0; i < turn.pending.length; i++) {
    if (turn.pending[i]._name === d.name) { row = turn.pending.splice(i, 1)[0]; break; }
  }
  if (!row) {   // this page joined in the middle of the step
    stepStart({ name: d.name, args: d.args || {} }, false);
    row = turn.pending.pop();
  }
  var res = String(d.result || "");
  var failed = /^\s*ERROR/.test(res) || /^\[exit [^0\]]/.test(res);
  row._result = res + (d.truncated ? "\n… (cut short)" : "");
  var mark = h("span", "sic");
  if (failed) mark.innerHTML = svg("x", "bad");
  row.replaceChild(mark, row.firstChild);
  if (failed) { row.classList.add("failed"); turn.failed++; }
  var dur = row._live ? fmtElapsed(Date.now() - row._t0) : "";
  if ((d.name === "edit" || d.name === "write_file") && !failed && /^@@|^[+-]/m.test(res)) {
    var diff = renderDiff(res, row._args.path);
    if (row._args.path) diff.setAttribute("data-path", String(row._args.path));
    row.parentNode.insertBefore(diff, row.nextSibling);
    row._diff = diff;
    dur = "+" + diff._adds + " −" + diff._dels;
  } else if (d.name === "bash") {
    // the last line of the output is usually the verdict ("1 failed, 466 passed")
    var lines = res.replace(/^\[exit -?\d+\]\n?/, "").trim().split("\n");
    var last = lines[lines.length - 1] || "";
    if (last && last.length < 90 && last !== "(no output)") row._verdict = last.replace(/^=+\s*|\s*=+$/g, "");
  }
  var said = stepResult(d.name, res);
  row.querySelector(".d").textContent = row._diff ? dur : said || row._verdict || (row._live && Date.now() - row._t0 > 2000 ? dur : "");
  follow(bottom);
}

/* What a long step is doing as it happens (a tool that sends tool_progress): each stage in words, the
   code it writes typed out as it arrives, what the build said, and a picture. It stays under the step
   afterwards, as the record of what was done. */
function stepProgress(d) {
  var turn = S.turn;
  if (!turn) return;
  var row = null;
  for (var i = turn.pending.length - 1; i >= 0; i--) {
    if (turn.pending[i]._name === d.name) { row = turn.pending[i]; break; }
  }
  if (!row || !row.parentNode) return;
  var bottom = isAtBottom();
  var box = row._livebox;
  if (!box) {
    // after the turn's activity line, not inside it: the folded list of steps would hide the show
    box = h("div", "steplive");
    var work = turn.work || workBlock(turn);
    work.parentNode.insertBefore(box, work.nextSibling);
    row._livebox = box;
  }
  if (d.kind === "step") {
    box.appendChild(h("div", "sl-step", String(d.text || "")));
    turn.work.querySelector(".wt").textContent = String(d.text || "");
  } else if (d.kind === "log") {
    box.appendChild(h("pre", "sl-log", String(d.text || "")));
  } else if (d.kind === "code") {
    var file = h("div", "sl-code");
    file.appendChild(h("div", "sl-file", String(d.file || "")));
    var pre = h("pre");
    file.appendChild(pre);
    box.appendChild(file);
    typeOut(pre, String(d.text || ""), function () { follow(bottom); });
  } else if (d.kind === "image" && /^data:image\/png;base64,/.test(String(d.data || ""))) {
    var img = h("img", "sl-img");
    img.src = d.data;
    img.alt = "The program running in the emulator";
    box.appendChild(img);
  }
  follow(bottom);
}

/* Code shown as if being typed: all of it within about four seconds, however long it is. */
function typeOut(pre, text, tick) {
  var at = 0, per = Math.max(30, Math.ceil(text.length / 240));
  (function next() {
    at = Math.min(text.length, at + per);
    pre.innerHTML = highlight(text.slice(0, at)) + (at < text.length ? '<span class="sl-caret"></span>' : "");
    if (tick) tick();
    if (at < text.length) setTimeout(next, 16);   // a timer, not a frame: it carries on in a background tab
  })();
}

/* A turn's steps as one plain phrase: "Read 2 files, searched code, ran a command". */
var PHRASE = {
  read: ["read a file", "read # files"], edit: ["edited a file", "edited # files"], notebook_edit: ["edited a notebook", "edited # notebooks"],
  write_file: ["created a file", "created # files"], bash: ["ran a command", "ran # commands"],
  grep: ["searched code", "searched code"], glob: ["found files", "found files"], delegate: ["ran an agent", "ran # agents"],
  subagent_spawn: ["started an agent", "started # agents"], web_search: ["searched the web", "searched the web"],
  web_fetch: ["fetched a page", "fetched # pages"], todo_write: ["updated todos", "updated todos"],
  test_focus: ["ran the tests", "ran the tests"], process_start: ["started a program", "started # programs"],
  git_status: ["checked git", "checked git"], git_diff: ["checked git", "checked git"]
};
function phraseFor(name) {
  if (PHRASE[name]) return PHRASE[name];
  if (name === "code_explore") return ["explored the code", "explored the code"];
  if (name === "code_search") return ["searched code", "searched code"];
  if (/^code_|^lsp_/.test(name)) return ["looked up code", "looked up code"];
  if (name === "diagnostics") return ["checked for errors", "checked for errors"];
  if (/^memory_/.test(name)) return ["used memory", "used memory"];
  if (/^browser_/.test(name)) return ["used the browser", "used the browser"];
  if (/^process_/.test(name)) return ["checked a program", "checked programs"];
  if (/^cron_/.test(name)) return ["changed a schedule", "changed schedules"];
  var m = /^mcp__([^_]+)__/.exec(name);
  if (m) return ["used " + m[1], "used " + m[1]];
  return null;
}
function stepsPhrase(turn) {
  var rows = turn.work ? turn.work.querySelectorAll(".steps > .step") : [];
  var counts = {}, order = [];
  Array.prototype.forEach.call(rows, function (r) {
    var p = phraseFor(r._name), key = p ? p[1] : "used " + stepLabel(r._name).toLowerCase();
    if (!(key in counts)) { counts[key] = { n: 0, p: p }; order.push(key); }
    counts[key].n++;
  });
  var bits = order.map(function (k) {
    var c = counts[k];
    if (!c.p) return k;
    return c.n === 1 ? c.p[0] : c.p[1].replace("#", c.n);
  });
  if (!bits.length) return "";
  var text = bits.slice(0, 4).join(", ") + (bits.length > 4 ? ", and more" : "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function toggleStepDetail(row) {
  if (row._detail) { row._detail.remove(); row._detail = null; row.classList.remove("open"); return; }
  var box = h("div", "sdetail"), args = row._args || {};
  box.appendChild(h("div", "slbl", stepLabel(row._name)));
  var call = box.appendChild(h("pre", "scall"));
  call.textContent = row._name === "bash" ? "$ " + (args.cmd || args.command || "")
    : Object.keys(args).map(function (k) {
        var v = args[k];
        return k + ": " + (typeof v === "string" ? v : JSON.stringify(v));
      }).join("\n") || "(no arguments)";
  var out = box.appendChild(h("pre", "sout"));
  out.textContent = row._result != null ? String(row._result).replace(/^\[exit 0\]\n?/, "") || "(no output)" : "(still running)";
  row.parentNode.insertBefore(box, (row._diff || row).nextSibling);
  row._detail = box;
  row.classList.add("open");
}

/* Close the activity line: "Worked for 14 seconds · 4 steps · 1 failed". */
function finishWork(elapsedMs) {
  var turn = S.turn;
  if (!turn || !turn.work) return;
  turn.pending.forEach(function (row) {   // a step cut off by Stop
    var mark = document.createElement("span");
    mark.innerHTML = svg("x", "bad");
    row.replaceChild(mark.firstChild, row.firstChild);
    row.querySelector(".d").textContent = "stopped";
  });
  turn.pending = [];
  var wt = turn.work.querySelector(".wt");
  turn.work.querySelector(".wnow").textContent = "";
  turn.work.querySelector(".wi").innerHTML = turn.failed ? svg("x", "bad") : "";
  // what the turn did, said plainly ("Read 2 files, searched code, ran a command"), as Claude Code does
  wt.textContent = stepsPhrase(turn) || (turn.n + (turn.n === 1 ? " step" : " steps"));
  var info = [];
  if (elapsedMs != null) info.push("Worked for " + fmtDuration(elapsedMs / 1000));
  info.push(turn.n + (turn.n === 1 ? " step" : " steps"));
  if (elapsedMs != null && S.lastSpeed) info.push(S.lastSpeed + " tokens a second");
  if (elapsedMs != null && S.lastReuse != null) info.push(S.lastReuse + "% of the prompt already read");
  wt.title = info.join(" · ");
  if (turn.failed) {
    wt.appendChild(document.createTextNode(" · "));
    wt.appendChild(h("span", "warn", turn.failed + " failed"));
  }
  turn.work.open = S.view === "verbose";
}

/* The model's thinking, as Claude Code shows it: "Thinking" while it thinks (open it to read along),
   then "Thought for 12s". It is shown only; it never goes back to the model. */
function thoughtEl(turn) {
  if (turn.thought) return turn.thought;
  // thinking after words or steps: those are done, and what follows starts a new group below
  sealSegment(turn);
  if (turn.work && !turn.pending.length) {
    var keep = S.turn;
    S.turn = turn; finishWork(null); S.turn = keep;
    turn.work = null; turn.n = 0; turn.failed = 0;
  }
  var det = h("details", "thought live");
  det.appendChild(h("summary")).innerHTML = '<span class="tht">Thinking</span>' + svg("chev", "chev");
  det.appendChild(h("div", "thbody"));
  turn.el.appendChild(det);
  turn.thought = det;
  return det;
}
function settleThought(turn) {
  var det = turn && turn.thought;
  if (!det) return;
  det.classList.remove("live");
  det.querySelector(".tht").textContent = "Thought";
  turn.thought = null;
}
function onThinking(d) {
  var turn = ensureTurn(), bottom = isAtBottom();
  var det = thoughtEl(turn), body = det.querySelector(".thbody");
  body.textContent = d.text || "";
  if (det.open) body.scrollTop = body.scrollHeight;
  if (d.final) {
    det.classList.remove("live");
    det.querySelector(".tht").textContent = "Thought for " + ((d.secs || 1) < 60 ? (d.secs || 1) + "s" : fmtDuration(d.secs));
    turn.thought = null;   // the next thinking in this turn is a line of its own
  }
  follow(bottom);
}

function replyEl(turn) {
  if (!turn.reply) {
    turn.reply = h("div", "reply");
    turn.reply.appendChild(h("div", "md"));
    turn.el.appendChild(turn.reply);
  }
  return turn.reply;
}

function onToken(d) {
  var turn = ensureTurn(), text = d.text || "";
  if (turn.errored && /^\[Error contacting the model/.test(text)) return;  // shown as an error card
  if (!text.trim()) return;
  var bottom = isAtBottom();
  // words after a group of steps: that group is done, so it says what it did
  if (!turn.reply && turn.work && !turn.pending.length && turn === S.turn) finishWork(null);
  var el = replyEl(turn);
  el._text = text;
  var md = el.querySelector(".md");
  md.innerHTML = renderMarkdown(text);
  if (!d.final) {
    var last = md.lastElementChild;
    while (last && last.lastElementChild && /^(UL|OL|LI|BLOCKQUOTE)$/.test(last.tagName)) last = last.lastElementChild;
    (last && /^(P|LI|H[1-6]|BLOCKQUOTE)$/.test(last.tagName) ? last : md).appendChild(h("span", "cursor"));
  }
  follow(bottom);
}

function finishReply() {
  var turn = S.turn;
  if (!turn || !turn.reply || turn.reply.querySelector(".acts")) return;
  var cur = turn.reply.querySelector(".cursor");
  if (cur) cur.remove();
  drawDiagrams(turn.reply);
  markTerms(turn.reply);
  addReplyActions(turn.reply, turn.lastUser);
  addNextSteps(turn.reply);
}

function addReplyActions(el, lastUser) {
  var acts = h("div", "acts");
  var copy = h("button");
  copy.title = "Copy"; copy.setAttribute("aria-label", "Copy reply");
  copy.innerHTML = svg("copy");
  copy.onclick = function () {
    copyText(el._text || el.querySelector(".md").innerText, function () {
      copy.classList.add("did");
      setTimeout(function () { copy.classList.remove("did"); }, 1200);
    });
  };
  acts.appendChild(copy);
  if (lastUser) {
    var retry = h("button");
    retry.title = "Ask again"; retry.setAttribute("aria-label", "Ask again");
    retry.innerHTML = svg("retry");
    retry.onclick = function () { $("input").value = lastUser; sendMessage(); };
    acts.appendChild(retry);
  }
  el.appendChild(acts);
}

/* What the reply is based on: the kernel's states, in plain words. */
var BASIS_WORD = { KNOWN: "checked", DERIVED: "seen, not opened", CONFABULATED: "guess",
  CONFLICTED: "contradicts a result", UNKNOWN_REFERENCE: "not found" };
var BASIS_ORDER = ["CONFLICTED", "UNKNOWN_REFERENCE", "CONFABULATED", "DERIVED", "KNOWN"];

function basisEl(c) {
  if (!c) return null;
  var det = h("details", "basis s-" + c.state);
  var sum = h("summary");
  sum.appendChild(h("i", "dot"));
  var counts = c.counts || {}, bits = [];
  BASIS_ORDER.forEach(function (k) {
    if (counts[k]) bits.push(counts[k] + " " + (k === "CONFABULATED" && counts[k] > 1 ? "guesses" : BASIS_WORD[k]));
  });
  var claims = c.claims || [];
  var label;
  if (!claims.length) {
    label = c.state === "KNOWN" ? "Nothing here needed checking" : "From the model’s own knowledge, not checked against your files";
  } else {
    // one plain line: how much it checked, then only what should worry you; the full split on hover
    var total = 0;
    BASIS_ORDER.forEach(function (k) { total += counts[k] || 0; });
    var parts = ["Checked " + (counts.KNOWN || 0) + " of " + total + " things against your files"];
    if (counts.CONFLICTED) parts.push(counts.CONFLICTED + " contradict" + (counts.CONFLICTED > 1 ? "" : "s") + " what it found");
    if (counts.UNKNOWN_REFERENCE) parts.push(counts.UNKNOWN_REFERENCE + " not found");
    if (counts.CONFABULATED) parts.push(counts.CONFABULATED + (counts.CONFABULATED > 1 ? " guesses" : " guess"));
    label = parts.join(" · ");
    sum.title = bits.join(" · ") + ". Click for the list.";
  }
  sum.appendChild(h("span", "", label));
  det.appendChild(sum);
  var list = h("div", "blist");
  claims.slice().sort(function (a, b) { return BASIS_ORDER.indexOf(a.state) - BASIS_ORDER.indexOf(b.state); })
    .forEach(function (cl) {
      var row = h("div", "brow");
      var lab = h("span", "lab");
      lab.appendChild(h("i", "dot s-" + cl.state));
      lab.appendChild(document.createTextNode(cl.label));
      var what = h("span", "what");
      what.appendChild(h("code", "", cl.text));
      row.appendChild(lab); row.appendChild(what);
      row.appendChild(h("span", "why", cl.why.charAt(0).toUpperCase() + cl.why.slice(1) + "."));
      list.appendChild(row);
    });
  var ca = c.checked_against || {};
  list.appendChild(h("div", "bfoot",
    "Checked against this session: " + (ca.files_read || 0) + " files read, " + (ca.commands_run || 0) +
    " commands run. “Checked” means the agent saw it, not that it is correct."));
  det.appendChild(list);
  return det;
}

function onClaims(c) {
  var turn = S.turn;
  if (!turn || !turn.reply || turn.reply.querySelector(".basis")) return;
  var el = basisEl(c);
  if (el) turn.reply.insertBefore(el, turn.reply.querySelector(".acts"));
}

/* Something the agent made for you: a card that opens it, not a wall of pasted text. */
function addArtifactCard(d) {
  var turn = ensureTurn(), bottom = isAtBottom();
  var card = h("div", "artcard");
  card.innerHTML = svg(d.type === "page" ? "image" : "file");
  var info = h("div");
  info.appendChild(h("b", "", d.title));
  info.appendChild(h("i", "", (d.type === "page" ? "Page" : "Document") + " · " + d.filename + " · kept in your Library"));
  card.appendChild(info);
  var open = h("a", "btn", "Open");
  open.href = d.url;
  open.target = "_blank";
  open.rel = "noopener";
  card.appendChild(open);
  turn.el.insertBefore(card, turn.reply || null);
  follow(bottom);
}

function addNote(text) {
  if (!text) return;
  showHello(false);
  var bottom = isAtBottom();
  var el = h("div", "note"), inner = h("span");
  inner.innerHTML = renderMarkdown(text);
  el.appendChild(inner);
  (S.turn ? S.turn.el : $("chat")).appendChild(el);
  follow(bottom);
}

/* Errors appear in the conversation where they happened, with a way forward. */
function addError(d) {
  var turn = ensureTurn(), bottom = isAtBottom();
  turn.errored = true;
  var message = d.message || "error";
  pageError("Shown: " + message);
  var card = h("div", "errcard");
  card.setAttribute("role", "alert");
  var title = h("h4"), p = h("p"), row = h("div", "btnrow");
  function button(label, fn, primary) {
    var b = h("button", "btn" + (primary ? " primary" : ""), label);
    b.onclick = fn;
    row.appendChild(b);
  }
  var retry = S.lastRetry;
  if (d.kind === "model") {
    title.innerHTML = svg("warn") + "<span>The model did not answer</span>";
    p.appendChild(document.createTextNode("CodeMonkey could not reach "));
    p.appendChild(h("b", "", d.model || d.provider || "the model"));
    p.appendChild(document.createTextNode(" (" + (d.provider || "provider") + "). The model server may not be running. Nothing in your project was changed."));
    if (retry) button("Try again", function () { card.remove(); retry(); }, true);
    button("Set up a model", function () { openModelSetup(); });
    button("Switch model", function () { openMenu("model-menu"); });
    button("Show details", function () {
      var pre = card.querySelector("pre");
      if (pre) { pre.remove(); return; }
      card.appendChild(h("pre", "", message));
    });
  } else if (d.kind === "memory") {
    title.innerHTML = svg("warn") + "<span>Stopped to protect this computer</span>";
    p.textContent = message;
    button("Hardware settings", function () { openSettings("hardware"); }, true);
    button("Dismiss", function () { card.remove(); });
  } else {
    title.innerHTML = svg("warn") + "<span>Something went wrong</span>";
    p.textContent = message;
    if (retry) button("Try again", function () { card.remove(); retry(); }, true);
    button("Report this", function () { openReport(message); });
    button("Dismiss", function () { card.remove(); });
  }
  card.appendChild(title); card.appendChild(p); card.appendChild(row);
  turn.el.appendChild(card);
  if (S.current) { S.status[S.current] = "error"; renderSessions(); }
  follow(bottom);
}
function toastError(err) { addError({ message: err && err.message ? err.message : String(err) }); }

/* A word that comes and goes at the bottom of the window ("Verbose: every tool call shown"). */
function toast(text) {
  var t = $("toast");
  if (!t) { t = h("div", "toast"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = text;
  t.classList.add("on");
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(function () { t.classList.remove("on"); }, 1800);
}

/* ---- replaying a stored conversation ---- */

var replay = { plan: null };
function replayEntry(e) {
  if (!e) return;
  if (e.kind === "task") { addTaskCard(e); return; }
  if (e.kind === "thinking") { onThinking({ text: e.text, secs: e.secs, final: true }); return; }
  if (e.kind === "user") {
    if (S.turn) { finishWork(null); }
    S.replayLastUser = e.text;   // what "Try again" sends if this turn failed
    addUserMsg(e.text);
  } else if (e.kind === "tool") {
    stepStart({ name: e.name, args: e.args }, false);
    stepEnd({ name: e.name, result: e.result, truncated: e.truncated });
  } else if (e.kind === "assistant" && e.interim) {
    var t0 = ensureTurn();
    sealSegment(t0);
    var lead = replyEl(t0);
    lead._text = e.text;
    lead.querySelector(".md").innerHTML = renderMarkdown(e.text);
  } else if (e.kind === "assistant") {
    var turn = ensureTurn();
    finishWork(null);
    if (/^\[Error contacting the model/.test(e.text || "")) {
      var again = S.replayLastUser;
      S.lastRetry = again ? function () { $("input").value = again; autosize(); sendMessage(); } : null;
      var st = (S.snap && S.snap.settings) || {};
      addError({ kind: "model", message: e.text, provider: st.provider, model: st.model });
      S.turn = null;
      return;
    }
    var el = replyEl(turn);
    el._text = e.text;
    el.querySelector(".md").innerHTML = renderMarkdown(e.text);
    drawDiagrams(el);
    markTerms(el);
    el.querySelectorAll(".choices").forEach(function (c) { c.classList.add("done"); });   // answered then, or passed by
    var basis = basisEl(e.claims);
    if (basis) el.appendChild(basis);
    addReplyActions(el, turn.lastUser);
    S.turn = null;
  } else if (e.kind === "artifact") {
    addArtifactCard(e);
  } else if (e.kind === "notice") {
    addNote(e.text);
  } else if (e.kind === "changes") {
    addChangesCard(e);
  } else if (e.kind === "plan") {
    replay.plan = renderPlan(e.plan, false);
  } else if (e.kind === "subagent") {
    S.subagents[e.id || ("r" + Object.keys(S.subagents).length)] = { brief: e.brief, summary: e.summary, ok: true, done: true };
  }
}
function endReplay() {
  var busy = S.snap && S.snap.status !== "idle";
  if (S.turn && !busy) { finishWork(null); S.turn = null; }   // a turn still running stays open
  if (replay.plan && S.snap && S.snap.has_pending_plan) addPlanActions(replay.plan);
  replay.plan = null;
  renderSubagents();
}

/* ---------------- plan review ---------------- */

function renderPlan(planText, pending) {
  S.lastPlan = planText || ""; renderPlanPane();
  var turn = ensureTurn(), bottom = isAtBottom();
  var old = $("chat").querySelector(".plan.pending");
  if (old) { old.classList.remove("pending"); var a = old.querySelector(".planact"); if (a) a.remove(); }
  var card = h("div", "plan");
  var hd = h("div", "planhd");
  hd.innerHTML = svg("list") + "<span>Plan for your review</span>";
  card.appendChild(hd);
  var md = h("div", "md");
  md.innerHTML = renderMarkdown(planText || "");
  card.appendChild(md);
  turn.el.appendChild(card);
  if (pending) addPlanActions(card);
  follow(bottom);
  return card;
}

function addPlanActions(card) {
  card.classList.add("pending");
  card.querySelector(".planhd").appendChild(h("em", "", "nothing has been changed yet"));
  var act = h("div", "planact");
  var note = h("textarea", "field");
  note.rows = 1;
  note.placeholder = "Ask for changes to the plan (optional)";
  var row = h("div", "btnrow");
  function button(label, action, primary) {
    var b = h("button", "btn" + (primary ? " primary" : ""), label);
    b.onclick = function () {
      if (action === "reject" && !note.value.trim()) { note.focus(); note.placeholder = "Say what to change, then press Revise"; return; }
      api("POST", "/api/sessions/" + S.current + "/plan", { action: action, feedback: note.value })
        .catch(toastError);
    };
    row.appendChild(b);
  }
  button("Approve and build", "approve", true);
  button("Revise", "reject");
  button("Discard", "discard");
  act.appendChild(note); act.appendChild(row);
  card.appendChild(act);
}

function resolvePlanCard(action) {
  var card = $("chat").querySelector(".plan.pending");
  if (!card) return;
  card.classList.remove("pending");
  var act = card.querySelector(".planact");
  if (act) act.remove();
  var em = card.querySelector(".planhd em");
  if (em) em.textContent = action === "approve" ? "approved" : action === "reject" ? "sent back for changes" : "discarded";
}

/* ---------------- Activity panel ---------------- */

/* ---------------- what a turn changed: keep or undo each file (the Changes pane) ---------------- */

S.changes = {};   // turn id -> its files

function addChangesCard(r) {
  if (!r || !r.files || !r.files.length) return;
  S.changes[r.turn] = r;
  var el = document.querySelector('.chg[data-turn="' + r.turn + '"]');
  var fresh = !el;
  if (fresh) { el = h("div", "chg"); el.dataset.turn = r.turn; }
  // Claude Code's card: "Edited 3 files" and View changes, then a row per file with its counts
  el.innerHTML = "";
  var pending = 0, undone = 0, undoable = 0;
  r.files.forEach(function (f) {
    if (f.state === "pending") { pending++; if (f.undoable) undoable++; }
    if (f.state === "undone") undone++;
  });
  var n = r.files.length, files = n === 1 ? " file" : " files";
  var head = el.appendChild(h("div", "chghd"));
  head.appendChild(h("span", "", undone === n ? "Undid " + n + files + ": they are back as they were"
    : "Edited " + n + files + (pending ? "" : " \u00b7 reviewed")));
  var acts = head.appendChild(h("span", "chgacts"));
  // one click puts every file this reply changed back as it was (one at a time in View changes)
  if (undoable) acts.appendChild(button("Undo", function () {
    askConfirm("Undo these changes?", (undoable === 1 ? "The file goes back to how it was" : "The " + undoable +
      " files go back to how they were") + " before this reply. The conversation stays as it is.", "Undo", function () {
      decideChange(r.turn, "", "undo_all");
    });
  }));
  acts.appendChild(button("View changes", function () { openReview(r.turn); }));
  r.files.forEach(function (f) {
    var row = el.appendChild(h("button", "chgrow"));
    row.type = "button";
    row.title = f.path;
    row.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/></svg>';
    row.appendChild(h("span", "p", f.path));
    row.appendChild(h("span", "num-add", "+" + f.added));
    row.appendChild(h("span", "num-del", "-" + f.removed));
    row.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24" class="chev"><path d="M9 6l6 6-6 6"/></svg>');
    row.onclick = function () { openReview(r.turn); };
  });
  if (fresh) {
    var bottom = isAtBottom();
    (S.turn ? S.turn.el : $("chat")).appendChild(el);
    follow(bottom);
  }
  updateChangesBtn();
}

function openReview(turn) {
  S.reviewTurn = turn;
  closePanes("review");
  $("review").classList.remove("hidden");
  renderReview();
  // the page may have been opened after the files were kept or undone elsewhere
  api("GET", "/api/sessions/" + S.current + "/changes/" + turn).then(function (r) {
    addChangesCard(r);
    if (S.reviewTurn === turn) renderReview();
  }).catch(function () {});
}

var EFFORT_WORDS = { auto: "Chooses for each request", low: "Fastest: little thinking, first good approach",
  medium: "Balanced", high: "Thinks longer and checks before editing", max: "As long as it takes" };

/* The sidebar's group and sort menu (Claude Code's icon beside the first heading). */
function sidebarView() {
  var v = store("cm:sidebar") || {};
  return { group: v.group === "folder" ? "folder" : "date", sort: v.sort === "name" ? "name" : "recent" };
}
function openGroupMenu(x, y) {
  var m = $("row-menu"), v = sidebarView();
  m.innerHTML = "";
  function pick(title, list, key) {
    m.appendChild(h("div", "mh")).appendChild(h("span", "", title));
    list.forEach(function (o) {
      m.appendChild(menuItem(o[1], "", v[key] === o[0], function () {
        v[key] = o[0]; store("cm:sidebar", v); renderSessions();
      }));
    });
  }
  pick("Group by", [["date", "Date"], ["folder", "Folder"]], "group");
  m.appendChild(h("div", "sep"));
  pick("Sort by", [["recent", "Recent activity"], ["name", "Name"]], "sort");
  m.appendChild(h("div", "sep"));
  m.appendChild(menuItem("View archived", "Sessions you archived, at the end of the list", !!S.showArchived, function () {
    S.showArchived = !S.showArchived; renderSessions();
  }));
  closeMenus();
  m.classList.add("open");
  var w = m.offsetWidth, ht = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - ht - 8)) + "px";
}

/* Panes resize from their left edge; each remembers its width. */
function paneWidths() { var w = store("cm:pane-w"); return w && typeof w === "object" ? w : {}; }
function applyPaneWidth(id) {
  var w = paneWidths()[id], el = $(id);
  if (el && w) el.style.width = Math.max(280, Math.min(w, Math.round(window.innerWidth * 0.7))) + "px";
}
function makeResizable(id) {
  var el = $(id);
  if (!el || el.querySelector(".grip")) return;
  var grip = h("div", "grip");
  grip.title = "Drag to resize";
  el.insertBefore(grip, el.firstChild);
  grip.addEventListener("mousedown", function (e) {
    e.preventDefault();
    document.body.classList.add("resizing");
    function move(ev) {
      var w = Math.max(280, Math.min(window.innerWidth - ev.clientX, Math.round(window.innerWidth * 0.7)));
      el.style.width = w + "px";
    }
    function up() {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("resizing");
      var all = paneWidths(); all[id] = parseInt(el.style.width, 10) || 0; store("cm:pane-w", all);
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  grip.addEventListener("dblclick", function () {   // back to its usual width
    el.style.width = ""; var all = paneWidths(); delete all[id]; store("cm:pane-w", all);
  });
  applyPaneWidth(id);
}

/* ---------------- the Code review card (/review, /security-review) ----------------
   The review ends with a ```findings block (a JSON list); the chat shows it as Claude Code's card:
   grouped by file, most severe first, with Apply fixes, Fix this one and Dismiss. */
var SEVERITY = { high: 0, medium: 1, low: 2 };
function reviewCardHtml(list) {
  var items = (Array.isArray(list) ? list : []).filter(function (f) { return f && (f.title || f.fix); })
    .map(function (f, i) { return { i: i, file: String(f.file || ""), line: f.line, sev: SEVERITY[String(f.severity).toLowerCase()] != null ? String(f.severity).toLowerCase() : "medium", title: String(f.title || ""), fix: String(f.fix || "") }; });
  items.sort(function (a, b) { return SEVERITY[a.sev] - SEVERITY[b.sev]; });
  var html = '<div class="rvcard" data-findings="' + esc(JSON.stringify(items)) + '"><div class="rvhd"><b>Code review</b><em>' +
    (items.length ? items.length + (items.length === 1 ? " finding" : " findings") : "Nothing to fix") + "</em>" +
    (items.length ? '<button class="btn primary" type="button" data-rv="apply">Apply fixes</button>' : "") + "</div>";
  var files = [];
  items.forEach(function (f) { if (files.indexOf(f.file) < 0) files.push(f.file); });
  files.forEach(function (file) {
    html += '<div class="rvfile"><code>' + esc(file || "general") + "</code></div>";
    items.filter(function (f) { return f.file === file; }).forEach(function (f) {
      html += '<div class="rvf" data-i="' + f.i + '"><span class="sev ' + f.sev + '">' + f.sev.charAt(0).toUpperCase() + f.sev.slice(1) + "</span>" +
        '<div class="rvt"><b>' + esc(f.title) + "</b><small>" + (f.line ? "line " + esc(String(f.line)) + (f.fix ? " · " : "") : "") + esc(f.fix) + "</small></div>" +
        '<button class="btn" type="button" data-rv="fix">Fix this one</button>' +
        '<button class="iconbtn" type="button" data-rv="dismiss" title="Dismiss" aria-label="Dismiss">' + svg("x") + "</button></div>";
    });
  });
  return html + "</div>";
}
function findingText(f) {
  return "- `" + (f.file || "?") + (f.line ? ":" + f.line : "") + "` (" + f.sev + "): " + f.title.replace(/[.\s]+$/, "") + (f.fix ? ". Fix: " + f.fix : "");
}
function onReviewClick(e) {
  var b = e.target.closest("[data-rv]");
  if (!b) return;
  var card = b.closest(".rvcard"), all = [];
  try { all = JSON.parse(card.getAttribute("data-findings") || "[]"); } catch (err) { return; }
  var act = b.getAttribute("data-rv"), row = b.closest(".rvf");
  if (act === "dismiss") { row.classList.add("dismissed"); return; }
  var pick = act === "fix"
    ? all.filter(function (f) { return String(f.i) === row.getAttribute("data-i"); })
    : all.filter(function (f) { var r = card.querySelector('.rvf[data-i="' + f.i + '"]'); return r && !r.classList.contains("dismissed"); });
  if (!pick.length) { toast("Every finding is dismissed"); return; }
  $("input").value = "Please fix " + (pick.length === 1 ? "this code review finding" : "these code review findings") + ":\n\n" + pick.map(findingText).join("\n");
  sendMessage();
}

/* Archive: out of the sidebar and the home screen, kept on disk; View archived brings them back. */
function archiveSession(e, on) {
  api("POST", "/api/sessions/" + e.id + "/archive", { archived: on }).then(function () {
    if (on && e.id === S.current) newSession();
    toast(on ? "Archived “" + (e.title || "New session") + "”" : "Back in the sidebar");
    return refreshSessions();
  }).catch(toastError);
}

/* ---------------- the Context card (Claude Code's): one drawing for the ring's popover, the card
   in Activity and /context ---------------- */
var CTX_COLORS = { "Messages": "#4a8af4", "System tools": "#e2763b", "MCP tools": "#1fa58c", "System prompt": "#d9a032",
  "Skills": "#9b9b9b", "Memory files": "#c4c4c4" };
function fmtTok(n) {
  n = Math.max(0, Math.round(n || 0));
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
function renderContext(box, d, opts) {
  opts = opts || {};
  box.innerHTML = "";
  box.classList.add("ctxv");
  var win = Math.max(1, d.window), pct = d.used / win * 100;
  if (opts.title) {
    var t = box.appendChild(h("div", "ctxv-title"));
    t.appendChild(h("b", "", "Context"));
  }
  var head = box.appendChild(h("div", "ctxv-head"));
  head.appendChild(h("b", "", Math.round(pct) + "% used"));
  head.appendChild(h("span", "", fmtTok(d.used) + " / " + fmtTok(d.window)));
  var bar = box.appendChild(h("div", "ctxv-bar"));
  d.parts.forEach(function (p) {
    if (!p.tokens) return;
    var seg = bar.appendChild(h("i"));
    seg.style.width = Math.max(.4, p.tokens / win * 100) + "%";
    seg.style.background = CTX_COLORS[p.name] || "var(--muted)";
  });
  function line(name, tokens, color, dim, dash) {
    var r = box.appendChild(h("div", "ctxv-row" + (dim ? " dim" : "")));
    var sq = r.appendChild(h("u"));
    sq.style.background = color;
    r.appendChild(h("span", "n", name)).title = name;
    r.appendChild(h("span", "t", fmtTok(tokens)));
    r.appendChild(h("span", "p", dash ? "—" : (tokens / win * 100).toFixed(1) + "%"));
  }
  d.parts.forEach(function (p) { line(p.name, p.tokens, CTX_COLORS[p.name] || "var(--muted)"); });
  line("Free space", d.free, "rgba(var(--tint), .14)", true);
  if ((d.deferred || []).length) {
    box.appendChild(h("div", "ctxv-gap"));
    d.deferred.forEach(function (p) { line(p.name, p.tokens, "rgba(var(--tint), .22)", true, true); });
  }
  Object.keys(d.lists || {}).forEach(function (name) {
    var items = d.lists[name];
    if (!items.length) return;
    var det = box.appendChild(h("details", "ctxv-list"));
    var sum = det.appendChild(h("summary"));
    sum.appendChild(h("span", "", name));
    sum.appendChild(h("em", "", String(items.length)));
    var ul = det.appendChild(h("div", "ctxv-items"));
    items.forEach(function (it) { ul.appendChild(h("div", "", it)).title = it; });
  });
  var foot = box.appendChild(h("div", "ctxv-foot"));
  foot.appendChild(h("span", "", fmtTok(d.until_compact != null ? d.until_compact : d.free) + " until auto-compact"));
  var b = foot.appendChild(h("button", "btn", "Compact session"));
  b.type = "button";
  b.disabled = !S.current || !!S.workStart || !d.parts[0] || !d.parts[0].tokens;
  b.onclick = function () {
    closeMenus();
    api("POST", "/api/sessions/" + S.current + "/message", { text: "/compact", attachments: [], client_id: "" }).catch(toastError);
  };
}
function loadContext(box, opts) {
  if (!S.current) {
    box.innerHTML = "";
    box.appendChild(h("div", "ctxv-empty", "The context fills from your first message."));
    return;
  }
  if (!box.children.length) box.appendChild(h("div", "ctxv-empty", "Counting…"));
  api("GET", "/api/sessions/" + S.current + "/context").then(function (d) { renderContext(box, d, opts); })
    .catch(function () { box.innerHTML = ""; box.appendChild(h("div", "ctxv-empty", "Could not count the context.")); });
}
