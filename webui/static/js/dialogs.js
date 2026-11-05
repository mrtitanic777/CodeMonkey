"use strict";
/* CodeMonkey page, dialogs.js: Side chat, rewind, memory, permission and other dialogs.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- side chat: Claude Code's Ctrl+; and /btw ---------------- */

function openSide(question) {
  if (!S.current) { toast("Open a session first: a side chat reads its conversation"); return; }
  closePanes("side");
  $("side").classList.remove("hidden");
  if (!$("side-log").children.length) {
    $("side-log").appendChild(h("p", "sidehint", "Ask about this session without adding to it. The side chat reads the conversation so far; nothing here goes back into it, and it is gone when you close the app."));
  }
  $("side-input").focus();
  if (question) askSide(question);
}
function closeSide() { $("side").classList.add("hidden"); }
function askSide(question) {
  question = String(question || "").trim();
  if (!question || !S.current) return;
  var log = $("side-log"), hint = log.querySelector(".sidehint");
  if (hint) hint.remove();
  log.appendChild(h("div", "sq", question));
  var a = log.appendChild(h("div", "sa pending"));
  a.innerHTML = '<span class="spark">✻</span> Thinking';
  log.scrollTop = log.scrollHeight;
  api("POST", "/api/sessions/" + S.current + "/side", { question: question }).then(function (r) {
    a.classList.remove("pending");
    a.innerHTML = renderMarkdown(r.answer || "(no answer)");
  }).catch(function (err) {
    a.classList.remove("pending"); a.classList.add("err");
    a.textContent = err.message || "The model did not answer";
  }).then(function () { log.scrollTop = log.scrollHeight; });
}

function closeReview() {
  $("review").classList.add("hidden");
  S.reviewTurn = null;
}

function renderReview() {
  var r = S.changes[S.reviewTurn], box = $("review-files");
  if (!r) return;
  var open = {};
  box.querySelectorAll(".cf.open").forEach(function (el) { open[el.dataset.path] = true; });
  box.innerHTML = "";
  var pending = 0;
  r.files.forEach(function (f) {
    if (f.state === "pending") pending++;
    var item = h("div", "cf " + f.state);
    item.dataset.path = f.path;
    var row = h("div", "it");
    row.innerHTML = svg("chev");
    row.appendChild(h("span", "", f.path));
    var nums = h("em");
    nums.appendChild(h("span", "num-add", "+" + f.added));
    nums.appendChild(document.createTextNode(" "));
    nums.appendChild(h("span", "num-del", "\u2212" + f.removed));
    row.appendChild(nums);
    var acts = h("div", "acts");
    if (f.state === "pending") {
      acts.appendChild(iconButton("check", "Keep", function () { decideChange(r.turn, f.path, "keep"); }));
      if (f.undoable) acts.appendChild(iconButton("undo", f.created ? "Undo (delete the new file)" : "Undo",
        function () { decideChange(r.turn, f.path, "undo"); }));
    } else {
      acts.appendChild(h("em", "", f.state === "kept" ? "Kept" : "Undone"));
    }
    row.appendChild(acts);
    row.onclick = function (ev) {
      if (ev.target.closest(".acts")) return;
      toggleChangeDiff(item, r.turn, f.path);
    };
    item.appendChild(row);
    box.appendChild(item);
    if (open[f.path]) toggleChangeDiff(item, r.turn, f.path);
  });
  $("review-foot").classList.toggle("hidden", !pending);
}

function iconButton(icon, title, fn) {
  var b = h("button", "iconbtn");
  b.type = "button";
  b.title = title;
  b.setAttribute("aria-label", title);
  b.innerHTML = svg(icon);
  b.onclick = function (ev) { ev.stopPropagation(); fn(); };
  return b;
}

function diffView() { return store("cm:diff-view") === "split" ? "split" : "unified"; }
function setDiffView(v) {
  store("cm:diff-view", v);
  $("diff-unified").setAttribute("aria-selected", v === "unified" ? "true" : "false");
  $("diff-split").setAttribute("aria-selected", v === "split" ? "true" : "false");
  // side by side needs room: the pane widens if it is narrow
  var pane = $("review");
  if (v === "split" && pane.getBoundingClientRect().width < 760 && window.innerWidth > 1300) pane.style.width = Math.min(1000, Math.round(window.innerWidth * 0.55)) + "px";
  // open files are drawn again in the new layout
  pane.querySelectorAll(".cf.open").forEach(function (item) {
    var d = item.querySelector(".diff"); if (d) d.remove();
    item.classList.remove("open");
    toggleChangeDiff(item, S.reviewTurn, item.dataset.path);
  });
}

function toggleChangeDiff(item, turn, path) {
  var shown = item.querySelector(".diff");
  if (shown) { shown.remove(); item.classList.remove("open"); return; }
  item.classList.add("open");
  api("GET", "/api/sessions/" + S.current + "/changes/" + turn + "?path=" + encodeURIComponent(path)).then(function (r) {
    if (!item.classList.contains("open") || item.querySelector(".diff")) return;
    if (!r.diff) { item.appendChild(h("div", "err", "No difference now.")); return; }
    // Split: before and after side by side; Unified (the default) is the one you can comment on
    item.appendChild(diffView() === "split" ? renderSplitDiff(r.diff, path) : commentable(renderDiff(r.diff, path), path));
  }).catch(function (err) { item.appendChild(h("div", "err", err.message || String(err))); });
}

/* Comments on diff lines, as in Claude Code: click a line, write, Enter adds it; Ctrl+Enter sends
   every comment as one message, and the agent makes the changes asked for. */
S.comments = [];
function commentable(diff, path) {
  diff.querySelectorAll(".l:not(.hunk)").forEach(function (row) {
    row.title = "Click to comment on this line";
    row.onclick = function () { openComment(row, path); };
  });
  S.comments.filter(function (c) { return c.path === path; }).forEach(function (c) {
    var row = diff.querySelectorAll(".l:not(.hunk)")[c.index];
    if (row) showComment(row, c);
  });
  return diff;
}
function openComment(row, path) {
  if (row.nextSibling && row.nextSibling.classList && row.nextSibling.classList.contains("cmtbox")) {
    row.nextSibling.querySelector("textarea").focus(); return;
  }
  var rows = Array.prototype.slice.call(row.parentNode.querySelectorAll(".l:not(.hunk)"));
  var box = h("div", "cmtbox"), ta = h("textarea");
  ta.rows = 2; ta.placeholder = "Comment on this line. Enter adds it, Esc cancels";
  box.appendChild(ta);
  row.parentNode.insertBefore(box, row.nextSibling);
  ta.focus();
  ta.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addOne(); sendComments(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); addOne(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); box.remove(); }
  });
  function addOne() {
    var text = ta.value.trim();
    box.remove();
    if (!text) return;
    var spans = row.querySelectorAll("span");
    var c = { path: path, index: rows.indexOf(row), line: (spans[0] && spans[0].textContent) || "",
      sign: (spans[1] && spans[1].textContent) || "", code: ((spans[2] && spans[2].textContent) || "").trim(), text: text };
    S.comments.push(c);
    showComment(row, c);
    renderCommentBar();
  }
}
function showComment(row, c) {
  var el = h("div", "cmt");
  el.appendChild(h("span", "", c.text));
  var x = h("button", "iconbtn");
  x.type = "button"; x.title = "Remove comment"; x.innerHTML = svg("x");
  x.onclick = function (e) {
    e.stopPropagation();
    S.comments.splice(S.comments.indexOf(c), 1); el.remove(); renderCommentBar();
  };
  el.appendChild(x);
  row.parentNode.insertBefore(el, row.nextSibling);
}
function renderCommentBar() {
  var bar = $("review-comments"), n = S.comments.length;
  bar.classList.toggle("hidden", !n);
  $("review-comments-n").textContent = n + (n === 1 ? " comment" : " comments");
}
function sendComments() {
  if (!S.comments.length) return;
  var lines = S.comments.map(function (c) {
    var where = "`" + c.path + "`" + (c.line ? " line " + c.line : "") + (c.sign === "−" ? " (removed)" : "");
    return "- " + where + (c.code ? ": `" + c.code.slice(0, 120).replace(/`/g, "'") + "`" : "") + "\n  " + c.text;
  });
  $("input").value = "Comments on your changes. Please make these changes:\n\n" + lines.join("\n");
  S.comments = [];
  document.querySelectorAll("#review .cmt, #review .cmtbox").forEach(function (el) { el.remove(); });
  renderCommentBar();
  sendMessage();
}

function decideChange(turn, path, action) {
  api("POST", "/api/sessions/" + S.current + "/changes/" + turn, { action: action, path: path })
    .then(function (r) {
      addChangesCard(r);
      renderReview();
      if (r.errors && r.errors.length) toastError(new Error(r.errors.join("; ")));
    })
    .catch(function (err) {
      var item = $("review-files").querySelector('.cf[data-path="' + CSS.escape(path || "") + '"]');
      if (item) item.appendChild(h("div", "err", err.message || String(err)));
      else toastError(err);
    });
}

/* Activity stays the way you leave it, across messages and restarts; nothing opens it for you.
   A window too narrow to show it beside the chat hides it without forgetting your choice. */
function activityWanted() {
  return store("cm:activity") === true && window.innerWidth > 1000 && !HASH.pane && !HASH.solo;
}
function showActivity(on, passing) {
  if (on) closePanes("inspector");
  $("inspector").classList.toggle("hidden", !on);
  $("activity-btn").classList.toggle("on", on);
  if (!passing) store("cm:activity", on);
}

function renderTodos() {
  // the list itself is in the Plan pane and pinned above the input while the agent works through it
  var done = S.todos.filter(function (t) { return t.status === "completed"; }).length;
  renderTaskPin(done);
  renderPlanPane();
}

/* The agent's task list, pinned above the message box while it works through it (as Claude Code
   pins it under the latest message). Gone once every task is done. */
function renderTaskPin(done) {
  var pin = $("taskpin");
  var open = S.todos.length && done < S.todos.length;
  pin.classList.toggle("hidden", !open);
  if (!open) return;
  pin.innerHTML = "";
  var now = S.todos.filter(function (t) { return t.status === "in_progress"; })[0];
  var head = h("button", "tphd");
  head.type = "button";
  head.innerHTML = svg("chev");
  head.appendChild(h("span", "", now ? now.title : "Tasks"));
  head.appendChild(h("em", "", done + " of " + S.todos.length));
  head.onclick = function () { S.taskpinOpen = !S.taskpinOpen; renderTaskPin(done); };
  pin.appendChild(head);
  pin.classList.toggle("open", !!S.taskpinOpen);
  if (!S.taskpinOpen) return;
  var list = h("div", "tplist");
  S.todos.forEach(function (t) {
    var row = h("div", "tp " + t.status);
    if (t.status === "completed") row.innerHTML = svg("check");
    else row.appendChild(h("i", "st " + (t.status === "in_progress" ? "live" : "ring")));
    row.appendChild(h("span", "", t.title));
    list.appendChild(row);
  });
  pin.appendChild(list);
}

/* Transcript view (Claude Code's Ctrl+O): Normal folds a turn's tool calls, Verbose shows them all. */
S.view = store("cm:view") === "verbose" ? "verbose" : "normal";
function cycleView() {
  S.view = S.view === "verbose" ? "normal" : "verbose";
  store("cm:view", S.view);
  document.querySelectorAll("details.work").forEach(function (d) { d.open = S.view === "verbose"; });
  toast(S.view === "verbose" ? "Verbose: every tool call shown" : "Normal: tool calls folded");
}

/* The title bar's Changes button: the newest turn that changed files, with its counts. */
function updateChangesBtn() {
  var turns = Object.keys(S.changes || {}), last = turns.length ? S.changes[turns[turns.length - 1]] : null;
  var b = $("changes-btn");
  b.classList.toggle("hidden", !last);
  renderDetails();
  if (!last) return;
  var add = 0, del = 0;
  last.files.forEach(function (f) { add += f.added; del += f.removed; });
  $("changes-add").textContent = "+" + add;
  $("changes-del").textContent = "\u2212" + del;
  b.onclick = toggleChanges;
}
function toggleChanges() {
  var turns = Object.keys(S.changes || {});
  if (!$("review").classList.contains("hidden")) { closeReview(); return; }
  if (turns.length) openReview(turns[turns.length - 1]); else toast("Nothing has changed in this session yet");
}

/* ---------------- rewind: back to before one of your messages ---------------- */

function openRewind(atUser, atText) {   // atUser: the index of one of your messages on the page, to go straight to it
  if (!S.current) return;
  if (S.workStart) { toastError(new Error("Stop the agent first, then rewind.")); return; }
  var list = $("rewind-list");
  list.innerHTML = "";
  $("rewind-opts").classList.add("hidden");
  list.classList.remove("hidden");
  $("rewind-sub").textContent = "Go back to before one of your messages.";
  $("rewind-modal").classList.add("open");
  api("GET", "/api/sessions/" + S.current + "/rewind").then(function (r) {
    if (!r.points.length) { list.appendChild(h("div", "empty", "No messages to go back to yet.")); return; }
    // points are newest first, messages on the page oldest first
    var at = typeof atUser === "number" && atUser >= 0 ? r.points[r.points.length - 1 - atUser] : null;
    var head = function (t) { return String(t || "").trim().slice(0, 60); };
    if (at && atText != null && head(at.text) !== head(atText)) {   // a message with no turn shifts the count
      at = r.points.filter(function (p) { return head(p.text) === head(atText); })[0] || null;
    }
    if (at) { chooseRewind(at); return; }
    r.points.forEach(function (p, i) {
      var b = h("button", "it rw");
      b.type = "button";
      b.appendChild(h("span", "", p.text.split("\n")[0] || "(empty)"));
      if (p.files) b.appendChild(h("em", "", p.files + (p.files === 1 ? " file" : " files")));
      b.onclick = function () { chooseRewind(p); };
      list.appendChild(b);
      if (i === 0) b.focus();
    });
  }).catch(function (err) { closeDialog("rewind-modal"); toastError(err); });
}

function chooseRewind(p) {
  $("rewind-list").classList.add("hidden");
  $("rewind-sub").textContent = "Before: " + (p.text.split("\n")[0] || "").slice(0, 140);
  var opts = $("rewind-opts");
  opts.classList.remove("hidden");
  $("rw-both-sub").textContent = p.files ? p.files + (p.files === 1 ? " file goes" : " files go") + " back too" : "No files were changed after it";
  opts.querySelectorAll(".opt").forEach(function (b) {
    var r = b.dataset.restore;
    b.classList.toggle("hidden", r === "code" && !p.files);
    b.onclick = function () { doRewind(p, r); };
  });
  opts.querySelector(".opt").focus();
}

function exportChat() {
  if (!S.current) return;
  var a = document.createElement("a");
  a.href = "/api/sessions/" + S.current + "/export";
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function forkFrom(p) {
  api("POST", "/api/sessions/" + S.current + "/duplicate", { turn: p.turn }).then(function (snap) {
    closeDialog("rewind-modal");
    return refreshSessions().then(function () { return openSession(snap.id); }).then(function () {
      $("input").value = p.text || ""; autosize(); $("input").focus();
      addNote("Forked. The original session is unchanged.");
    });
  }).catch(toastError);
}

function doRewind(p, restore) {
  if (restore === "fork") { forkFrom(p); return; }
  S.rewinding = true;
  api("POST", "/api/sessions/" + S.current + "/rewind", { confirm: true, turn: p.turn, restore: restore })
    .then(function (r) {
      closeDialog("rewind-modal");
      return Promise.resolve(restore === "code" ? null : openSession(S.current)).then(function () {
        var c = r.code || {}, n = (c.restored || []).length + (c.removed || []).length;
        if (restore !== "code") { $("input").value = r.text || ""; autosize(); $("input").focus(); }
        addNote(restore === "chat" ? "Rewound the conversation." :
          "Rewound" + (restore === "both" ? " the conversation and " : " ") + n + (n === 1 ? " file." : " files.") +
          ((c.skipped || []).length ? " Not restored (too large): " + c.skipped.join(", ") : ""));
      });
    })
    .catch(toastError)
    .then(function () { S.rewinding = false; });
}

/* ---------------- /context: what the next request carries ---------------- */

function showContext() {
  if (!S.current) return;
  api("GET", "/api/sessions/" + S.current + "/context").then(addContextCard).catch(toastError);
}

function addContextCard(d) {
  var card = h("div", "ctxcard");
  renderContext(card, d, { title: true });
  var bottom = isAtBottom();
  showHello(false);
  (S.turn ? S.turn.el : $("chat")).appendChild(card);
  follow(bottom);
}

/* Programs running in the background (started by the agent, or moved there with Ctrl+B). */
function renderBackground() { renderTasks(); }   // background commands are listed in the Tasks pane

function toBackground() {
  if (!S.current) return;
  api("POST", "/api/sessions/" + S.current + "/background", {}).then(function (r) {
    if (!r.moved) addNote("Only a running command can go to the background (Ctrl+B).");
  }).catch(toastError);
}

/* ---------------- your earlier messages: Up / Down, and Ctrl+R to search them ---------------- */

function sentHistory() { return store("cm:history") || []; }

function rememberSent(text) {
  if (!text.trim()) return;
  var list = sentHistory().filter(function (t) { return t !== text; });
  list.unshift(text);
  store("cm:history", list.slice(0, 200));
  S.histPos = -1;
}

/* Up on the first line goes back through what you sent, Down on the last line comes forward again
   to what you were typing. Returns true when it moved. */
function historyStep(dir) {
  var input = $("input"), v = input.value, pos = input.selectionStart, list = sentHistory();
  if (dir > 0 && v.slice(0, pos).indexOf("\n") >= 0) return false;
  if (dir < 0 && (S.histPos === undefined || S.histPos < 0 || v.slice(pos).indexOf("\n") >= 0)) return false;
  if (S.histPos === undefined || S.histPos < 0) { S.histPos = -1; S.histDraft = v; }
  var next = S.histPos + dir;
  if (next >= list.length) return false;
  S.histPos = next;
  input.value = next < 0 ? (S.histDraft || "") : list[next];
  input.selectionStart = input.selectionEnd = input.value.length;
  autosize();
  return true;
}

function openHistorySearch() {
  S.historySearch = true;
  showHistoryMatches();
}

function showHistoryMatches() {
  var q = $("input").value.toLowerCase().trim();
  var items = sentHistory().filter(function (t) { return !q || t.toLowerCase().indexOf(q) >= 0; }).slice(0, 30)
    .map(function (t) { return { kind: "history", name: t.split("\n")[0].slice(0, 160), full: t }; });
  S.palette = { open: true, items: items, index: 0, title: items.length ? "Earlier messages" : "Nothing you sent matches" };
  S.historySearch = true;
  renderPalette();
}

/* ---------------- memory: "#" keeps a note, /memory edits the files ---------------- */

function askRemember(note) {
  if (!S.current) { toastError(new Error("Open or start a session first.")); return; }
  $("remember-text").textContent = note;
  $("remember-modal").classList.add("open");
  $("remember-modal").querySelectorAll(".opt").forEach(function (b, i) {
    if (i === 0) b.focus();
    b.onclick = function () {
      api("POST", "/api/sessions/" + S.current + "/memory", { scope: b.dataset.scope, text: note, append: true })
        .then(function () {
          closeDialog("remember-modal");
          $("input").value = ""; autosize();
          addNote("Remembered in " + (b.dataset.scope === "user" ? "your memory." : "this project's memory."));
        }).catch(toastError);
    };
  });
}

function openMemory(scope) {
  if (!S.current) { openSettings("context"); return; }
  api("GET", "/api/sessions/" + S.current + "/memory").then(function (m) {
    var which = scope || "project";
    function show() {
      $("memory-path").textContent = which === "project" ? m.project.path : "About you: read at the start of every session, in every project.";
      $("memory-text").value = m[which].text;
    }
    var tabs = $("memory-tabs");
    tabs.innerHTML = "";
    tabs.appendChild(segControl([["project", "Project"], ["user", "Yours"]], which, function (v) { which = v; show(); }));
    show();
    $("memory-save").onclick = function () {
      api("POST", "/api/sessions/" + S.current + "/memory", { scope: which, text: $("memory-text").value })
        .then(function (r) { m = r; closeDialog("memory-modal"); addNote("Memory saved."); }).catch(toastError);
    };
    $("memory-modal").classList.add("open");
    $("memory-text").focus();
  }).catch(toastError);
}

function renderSubagents() { renderTasks(); }   // helpers are listed in the Tasks pane

/* A helper's run in a pane beside the chat (Claude Code's subagent pane): what it was asked, its
   steps as they happen, then its report. While it works the pane follows it. */
S.helperOpen = null;
function openHelper(id) {
  S.helperOpen = id;
  showPane("helper-pane");
  drawHelper(true);
}
function drawHelper(first) {
  var id = S.helperOpen;
  if (!id || !S.current || !paneOpen("helper-pane")) { S.helperOpen = null; return; }
  api("GET", "/api/sessions/" + S.current + "/agents/" + id).then(function (r) {
    if (S.helperOpen !== id) return;
    var running = r.status === "running";
    $("helper-title").textContent = (r.agent ? r.agent : "Helper") + (running ? " \u00b7 working" : "");
    var body = $("helper-body"), keep = body.scrollTop, atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    body.innerHTML = "";
    body.appendChild(h("p", "helperbrief", r.brief.split("\n")[0].slice(0, 400)));
    var steps = r.items.filter(function (e) { return e.kind === "tool"; });
    if (steps.length) {
      var list = renderStepList(steps);
      list.classList.add("helpersteps");
      body.appendChild(list);
    }
    if (r.report) {
      var rep = body.appendChild(h("div", "reply"));
      rep.appendChild(h("div", "md")).innerHTML = renderMarkdown(r.report);
    } else if (running) {
      var w = body.appendChild(h("div", "sa pending"));
      w.innerHTML = '<span class="spark">\u273B</span> Working \u00b7 ' + steps.length + (steps.length === 1 ? " step" : " steps");
    } else if (!steps.length) body.appendChild(h("div", "panehint", "Nothing yet."));
    body.scrollTop = first || atEnd ? body.scrollHeight : keep;
    if (running) setTimeout(function () { drawHelper(false); }, 2000);
  }).catch(toastError);
}

/* Finished steps as the chat shows them: one line each, opened for the detail; edits with their diff. */
function renderStepList(items) {
  var box = h("div", "steps");
  items.forEach(function (e) {
    var res = String(e.result || ""), failed = /^\s*ERROR/.test(res) || /^\[exit [^0\]]/.test(res);
    var row = h("button", "step" + (failed ? " failed" : ""));
    row.type = "button";
    row.innerHTML = '<span class="sic">' + (failed ? svg("x", "bad") : "") + '</span><span class="t"></span><span class="d"></span>';
    row.querySelector(".t").textContent = stepTitle(e.name, e.args);
    row._name = e.name; row._args = e.args || {}; row._result = res + (e.truncated ? "\n… (cut short)" : "");
    row.onclick = function () { toggleStepDetail(row); };
    row.querySelector(".d").textContent = stepResult(e.name, res);
    box.appendChild(row);
    if ((e.name === "edit" || e.name === "write_file") && !failed && /^@@|^[+-]/m.test(res)) {
      var diff = renderDiff(res, (e.args || {}).path);
      box.appendChild(diff);
      row._diff = diff;
      row.querySelector(".d").textContent = "+" + diff._adds + " −" + diff._dels;
    }
  });
  return box;
}

/* The newest checkpoint is the one Rewind goes back to (the agent keeps one marker). */
function renderCheckpoints(list) {
  var box = $("checkpoints");
  box.innerHTML = "";
  list.slice().reverse().slice(0, 8).forEach(function (cp, i) {
    var row = h("div", "it cpr");
    row.innerHTML = svg("retry");
    row.appendChild(h("span", "", cp.label || "Checkpoint"));
    if (i === 0) {
      var b = h("button", "mini", "Rewind");
      b.title = "Undo what the agent changed since this checkpoint";
      b.onclick = rewind;
      row.appendChild(b);
    } else {
      row.appendChild(h("em", "", shortAgo(cp.ts)));
    }
    box.appendChild(row);
  });
}

function rewind() {
  if (!S.current) return;
  askConfirm("Rewind to the last checkpoint?",
    "Every file change the agent made since then is undone. Your own edits to the same files may be lost too.",
    "Rewind", function () {
      api("POST", "/api/sessions/" + S.current + "/rewind", { confirm: true }).catch(toastError);
    });
}

/* ---------------- permission dialog ---------------- */

var PERM_TEXT = {
  bash: ["CodeMonkey wants to run a command", "In "],
  edit: ["CodeMonkey wants to edit a file", "In "],
  write_file: ["CodeMonkey wants to write a file", "Creates or replaces it in "],
  read: ["CodeMonkey wants to read a file", "In "],
  glob: ["CodeMonkey wants to list files", "In "],
  grep: ["CodeMonkey wants to search the files", "In "],
  delegate: ["CodeMonkey wants to run an agent", "A second copy of itself, working in "],
  subagent_spawn: ["CodeMonkey wants to start an agent", "It works in the background in "],
  web_search: ["CodeMonkey wants to search the web", "The search goes to the internet; working in "],
  web_fetch: ["CodeMonkey wants to fetch a web page", "Working in "],
  browser_navigate: ["CodeMonkey wants to open a page in a browser", "Working in "],
  cron_add: ["CodeMonkey wants to schedule work", "It runs by itself, without you watching, in "],
  process_start: ["CodeMonkey wants to start a background program", "It keeps running in "],
  test_focus: ["CodeMonkey wants to run the tests for what changed", "In "],
  notebook_edit: ["CodeMonkey wants to edit a notebook", "In "],
  mcp_sampling: ["An MCP server wants to use your model", "A server the agent is using asks for an answer, while working in "]
};

/* ---------------- an MCP server asks for a few answers (elicitation) ---------------- */

function onElicitation(d) {
  S.elicit = d;
  notify("A server is asking", d.message);
  $("elicit-title").textContent = d.server + " is asking";
  $("elicit-message").textContent = d.message || "Fill in what it asks for, or decline.";
  $("elicit-error").textContent = "";
  var box = $("elicit-fields");
  box.innerHTML = "";
  d.fields.forEach(function (f) {
    var id = "elicit-f-" + f.name;
    var lab = h("label", "lab", f.title + (f.required ? "" : " (optional)") + (f.description ? ": " + f.description : ""));
    lab.htmlFor = id;
    var input;
    if (f.type === "boolean") {
      input = h("input", "");
      input.type = "checkbox";
      input.checked = !!f.default;
    } else if (f.enum.length) {
      input = selectControl(f.enum.map(function (v, i) { return [v, f.enum_names[i] || v]; }), f.default == null ? f.enum[0] : f.default);
    } else {
      input = textField("", f.format === "email" ? "email" : f.type === "string" ? "text" : "number");
      if (f.default != null) input.value = f.default;
    }
    input.id = id;
    input.dataset.name = f.name;
    box.appendChild(lab);
    box.appendChild(input);
  });
  $("elicit-modal").classList.add("open");
  var first = box.querySelector("input,select");
  if (first) first.focus();
}

function closeElicitation() {
  $("elicit-modal").classList.remove("open");
  S.elicit = null;
}

function answerElicitation(action) {
  if (!S.elicit) return;
  var content = {};
  $("elicit-fields").querySelectorAll("[data-name]").forEach(function (el) {
    content[el.dataset.name] = el.type === "checkbox" ? el.checked : el.value;
  });
  api("POST", "/api/sessions/" + S.current + "/elicitation",
    { request_id: S.elicit.request_id, action: action, content: action === "accept" ? content : undefined })
    .then(closeElicitation)
    .catch(function (err) { $("elicit-error").textContent = err.message || String(err); });
}

/* A desktop notification, only when asked for and only while the window is in the background. */
function notify(title, body, opts) {
  opts = opts || {};
  try {
    // when the window is hidden, or (anyway) for a session you are not looking at
    if ((!document.hidden && !opts.anyway) || !S.settings || !S.settings.ui || !S.settings.ui.notify) return;
    if (!window.Notification || Notification.permission !== "granted") return;
    var n = new Notification(title, { body: String(body || "").slice(0, 160), tag: "codemonkey" + (opts.open || "") });
    n.onclick = function () { window.focus(); if (opts.open) openSession(opts.open); n.close(); };
  } catch (e) {}
}

function onPermissionRequest(d) {
  S.perm = d;
  notify("CodeMonkey is asking", (PERM_TEXT[d.tool] || ["Allow " + d.tool + "?"])[0] + " " + ((d.args || {}).cmd || (d.args || {}).path || ""));
  setStatus("permission");
  var a = d.args || {}, t = PERM_TEXT[d.tool] || ["CodeMonkey wants to use " + stepLabel(d.tool), "In "];
  $("perm-title").textContent = t[0];
  var cmd = d.tool === "bash" ? (a.cmd || "") : d.tool === "process_start" ? (a.command || "") : a.brief ? a.brief : a.url ? a.url : a.query ? a.query
    : a.path ? a.path : JSON.stringify(a, null, 2);
  $("perm-cmd").textContent = cmd;
  var diff = $("perm-diff");
  diff.innerHTML = "";
  if (d.preview) diff.appendChild(renderDiff(d.preview, a.path));
  else if (d.tool === "write_file" && a.content) diff.appendChild(h("pre", "askcmd", String(a.content).slice(0, 1500)));
  var why = $("perm-why");
  why.innerHTML = "";
  why.appendChild(document.createTextNode(t[1]));
  why.appendChild(h("b", "", d.folder || "this folder"));
  why.appendChild(document.createTextNode(d.helper ? ". Asked by a helper working on: " + d.helper : "."));
  if (d.rule) {
    why.appendChild(document.createTextNode(" Asked because of the rule "));
    why.appendChild(h("code", "", d.rule));
    why.appendChild(document.createTextNode("."));
  }
  var risky = $("perm-risky");   // a command that can destroy work: said plainly, whatever the mode
  risky.classList.toggle("hidden", !d.risky);
  if (d.risky) {
    var rt = $("perm-risky-text");
    rt.innerHTML = "";
    rt.appendChild(h("b", "", "Careful: "));
    rt.appendChild(document.createTextNode(d.risky.charAt(0).toUpperCase() + d.risky.slice(1) + ". Allow it only if that is what you want."));
  }
  var guard = $("perm-guard");
  guard.classList.toggle("hidden", !d.guard);
  if (d.guard) {
    var gt = $("perm-guard-text");
    gt.innerHTML = "";
    gt.appendChild(h("b", "", d.guard.label + ": "));
    gt.appendChild(document.createTextNode(d.guard.reasons.join("; ") + ". You are asked even when permissions are set to allow."));
  }
  var always = $("perm-always");
  always.classList.toggle("hidden", !d.always_label);
  if (d.always_label) {
    var lab = $("perm-always-label");
    lab.innerHTML = "";
    if (d.always_scope === "folder") {
      lab.appendChild(document.createTextNode("Always allow "));
      lab.appendChild(h("code", "", d.always_label));
      lab.appendChild(document.createTextNode(" in this folder"));
      $("perm-always-sub").textContent = "Stop asking for this command here. Remove it any time in Settings.";
    } else {
      lab.textContent = "Always allow this in this session";
      $("perm-always-sub").textContent = "Stop asking for " + (STEP_NAME[d.tool] || d.tool) + " until the session ends";
    }
  }
  $("perm-message").value = "";
  $("perm-modal").classList.add("open");
  $("perm-allow").focus();
}

function closePerm() {
  $("perm-modal").classList.remove("open");
  S.perm = null;
}

function resolvePermission(decision, message) {
  if (!S.perm) return;
  // the dialog stays until the server confirms (permission_resolved)
  api("POST", "/api/sessions/" + S.current + "/permission",
    { request_id: S.perm.request_id, decision: decision, message: message || undefined })
    .catch(function (err) { closePerm(); toastError(err); });
}

/* ---------------- small dialogs ---------------- */

function askConfirm(title, text, okLabel, cb) {
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-ok").textContent = okLabel;
  S.confirmCb = cb;
  $("confirm-modal").classList.add("open");
  $("confirm-ok").focus();
}
function askLongText(title, hint, value, cb) {
  $("text-title").textContent = title;
  $("text-hint").textContent = hint;
  $("text-body").value = value || "";
  S.textCb = cb;
  $("text-modal").classList.add("open");
  $("text-body").focus();
}
function askText(title, value, okLabel, cb) {
  $("prompt-title").textContent = title;
  $("prompt-input").value = value || "";
  $("prompt-ok").textContent = okLabel;
  S.promptCb = cb;
  $("prompt-modal").classList.add("open");
  $("prompt-input").focus();
  $("prompt-input").select();
}
/* ---------------- Set up a model ----------------
   For someone who has never run a model: what is on this computer, which model fits it, and the
   steps, one at a time. Ollama is the simplest; LM Studio and a hosted key are the other ways.
   Nothing is downloaded or started until a button is pressed. */
function openModelSetup() {
  closeMenus();
  $("setup-modal").classList.add("open");
  $("setup-body").innerHTML = "";
  $("setup-body").appendChild(h("p", "pgsub", "Checking this computer\u2026"));
  S.setupShown = "";
  drawModelSetup();
}
function drawModelSetup() {
  clearTimeout(S.setupTimer);
  if (!$("setup-modal").classList.contains("open")) return;
  api("GET", "/api/setup").then(function (st) {
    if (!$("setup-modal").classList.contains("open")) return;
    var shown = $("setup-body"), body = document.createElement("div");   // drawn aside, then swapped in
    var c = st.computer || {};
    body.appendChild(h("p", "pgsub", "CodeMonkey needs a model to talk to. Pick one of the ways below; the first is the simplest."));
    body.appendChild(h("div", "setupcomp", "This computer: " + (c.gpu ? c.gpu + (c.vram_gb ? " (" + c.vram_gb + " GB)" : "") : "no NVIDIA graphics card found") +
      (c.ram_gb ? " \u00b7 " + Math.round(c.ram_gb) + " GB memory" : "")));

    function card(title, sub, badge) {
      var el = body.appendChild(h("div", "setupcard"));
      var hd = el.appendChild(h("div", "sthd"));
      hd.appendChild(h("b", "", title));
      if (badge) hd.appendChild(h("span", "badge", badge));
      el.appendChild(h("p", "stsub", sub));
      return el;
    }
    function stepRow(el, n, done, text, action) {
      var row = el.appendChild(h("div", "ststep" + (done ? " done" : "")));
      row.appendChild(h("i", "stn", done ? "\u2713" : String(n)));
      row.appendChild(h("span", "", text));
      if (action && !done) row.appendChild(action);
      return row;
    }
    function btn(label, fn, primary) {
      var b = h("button", "btn" + (primary ? " primary" : ""), label);
      b.type = "button";
      b.onclick = function () { b.disabled = true; Promise.resolve(fn()).catch(toastError).then(function () { b.disabled = false; setTimeout(drawModelSetup, 600); }); };
      return b;
    }
    function useRow(el, provider, models) {
      var cur = S.settings || {};
      models.slice(0, 6).forEach(function (m) {
        var row = el.appendChild(h("div", "stmodel"));
        row.appendChild(h("code", "", m));
        if (cur.default_provider === provider && cur.default_model === m) row.appendChild(h("em", "", "In use"));
        else row.appendChild(btn("Use this", function () {
          return saveSetting({ default_provider: provider, default_model: m }).then(function () {
            if (S.current) sessionSetting({ provider: provider, model: m });
            toast("Using " + m);
          });
        }, true));
      });
    }

    // Ollama
    var o = st.ollama, ol = card("Ollama", "Free, and runs on this computer. Nothing you write leaves it.", "Simplest");
    stepRow(ol, 1, o.installed, o.installed ? "Ollama is installed" : "Install Ollama, then come back here",
      btn("Download Ollama", function () { window.open("https://ollama.com/download", "_blank"); }, true));
    stepRow(ol, 2, o.running, o.running ? "Ollama is running" : "Start Ollama",
      o.installed ? btn("Start Ollama", function () { return api("POST", "/api/setup/ollama-start", {}); }, true) : null);
    var p = st.pull || {};
    if (o.models.length) {
      stepRow(ol, 3, true, "You have " + o.models.length + (o.models.length === 1 ? " model" : " models") + ": pick one");
      useRow(ol, "ollama", o.models);
    } else stepRow(ol, 3, false, "Download a model", null);
    if (o.installed && o.running) {
      var picks = ol.appendChild(h("div", "stpicks"));
      (st.choices || []).slice(0, 3).forEach(function (ch) {
        if (o.models.indexOf(ch.model) >= 0) return;
        var row = picks.appendChild(h("div", "stpick"));
        var t = row.appendChild(h("div"));
        t.appendChild(h("code", "", ch.model));
        if (ch.recommended) t.appendChild(h("span", "badge", "Fits this computer best"));
        t.appendChild(h("div", "stsub", ch.about + " \u00b7 " + ch.gb + " GB download"));
        if (p.running && p.model === ch.model) {
          var bar = row.appendChild(h("div", "stbar"));
          bar.appendChild(h("i")).style.width = (p.pct || 0) + "%";
          row.appendChild(h("em", "", (p.pct || 0) + "%"));
        } else row.appendChild(btn("Download", function () { return api("POST", "/api/setup/pull", { model: ch.model }); }, ch.recommended));
      });
      if (p.error) picks.appendChild(h("div", "err", "The download stopped: " + p.error));
    }

    // LM Studio
    var l = st.lmstudio, lm = card("LM Studio", "Free, runs on this computer, and lets you browse and try many models.");
    stepRow(lm, 1, l.installed, l.installed ? "LM Studio is installed" : "Install LM Studio, then come back here",
      btn("Download LM Studio", function () { window.open("https://lmstudio.ai", "_blank"); }, true));
    stepRow(lm, 2, l.running && l.models.length, l.running && l.models.length ? "Its server is on"
      : "In LM Studio: download a model, then turn on the server (Developer tab, Status: Running)",
      l.installed ? btn("Open LM Studio", function () { return api("POST", "/api/setup/lmstudio-open", {}); }) : null);
    if (l.running && l.models.length) useRow(lm, "lmstudio", l.models);

    // a hosted model
    var k = st.keys || {}, any = Object.keys(k).some(function (n) { return k[n]; });
    var ho = card("Claude or OpenAI", "The strongest models, on their servers. Needs an account and a key; you pay for what you use.");
    stepRow(ho, 1, any, any ? "A key is added: pick the model in the model menu" : "Add a key in Settings",
      btn("Add a key", function () { closeDialog("setup-modal"); openSettings("models"); }, true));

    if (st.ready) body.appendChild(h("p", "stready", "\u2713 A model is ready. Close this and say what you want in the message box."));
    // Swapped in only when something changed: redrawing the same guide every few seconds made it
    // flicker and lose the pointer's hover, the focused button and the scroll position.
    if (body.innerHTML !== S.setupShown) {
      S.setupShown = body.innerHTML;
      shown.replaceChildren.apply(shown, Array.prototype.slice.call(body.childNodes));
    }
    if (p.running || !st.ready) S.setupTimer = setTimeout(drawModelSetup, p.running ? 1500 : 4000);
  }).catch(function (err) { S.setupShown = ""; $("setup-body").innerHTML = ""; $("setup-body").appendChild(h("div", "err", err.message || String(err))); });
}

function closeDialog(id) { $(id).classList.remove("open"); }
function openDialogs() { return document.querySelectorAll(".scrim.open"); }
