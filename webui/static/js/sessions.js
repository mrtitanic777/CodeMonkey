"use strict";
/* CodeMonkey page, sessions.js: The sessions list and opening a session.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- sessions panel ---------------- */

function pins() { var p = store("cm:pinned-sessions"); return Array.isArray(p) ? p : []; }
function togglePin(id) {
  var p = pins(), i = p.indexOf(id);
  if (i >= 0) p.splice(i, 1); else p.unshift(id);
  store("cm:pinned-sessions", p);
  renderSessions();
}

function refreshSessions() {
  return api("GET", "/api/sessions").then(function (list) {
    S.list = list;
    renderSessions();
    return list;
  });
}

function renderSessions() {
  var box = $("session-list");
  box.innerHTML = "";
  // as Claude Code's sidebar: Pinned, then Recents, newest first (the folder is in the tooltip)
  var pinned = pins(), top = [], recent = [];
  var archived = [];
  S.list.forEach(function (e) { (e.archived ? archived : pinned.indexOf(e.id) >= 0 ? top : recent).push(e); });
  function row(e) {
    var st = S.status[e.id] || "";
    var b = h("button", "it ses" + (e.id === S.current ? " on" : ""));
    b.setAttribute("role", "listitem");
    b.title = (e.title || "New session") + "\n" + (e.project_dir || "") + "\n" + shortAgo(e.last_active);
    b.appendChild(h("i", "st ses-st" + (st === "working" ? " live" : st === "permission" ? " wait" : st === "error" ? " err" : readyIds().indexOf(e.id) >= 0 ? " ready" : "")));
    b.appendChild(h("span", "", e.title || "New session"));
    if (st === "permission") b.appendChild(h("em", "needs", "Needs input"));
    else b.appendChild(h("small", "when", shortAgo(e.last_active)));   // how long ago, faint, on the right
    var more = h("span", "more");
    more.innerHTML = svg("more");
    more.title = "More";
    more.onclick = function (ev) {
      ev.stopPropagation();
      var r = more.getBoundingClientRect();
      openRowMenu(e, r.left, r.bottom + 4);
    };
    b.appendChild(more);
    b.onclick = function () { openSession(e.id); };
    b.oncontextmenu = function (ev) { ev.preventDefault(); openRowMenu(e, ev.clientX, ev.clientY); };
    return b;
  }
  if (top.length) {
    box.appendChild(h("div", "rh")).appendChild(h("span", "", "Pinned"));
    top.forEach(function (e) { box.appendChild(row(e)); });
  }
  var view = sidebarView(), shown = 0, more = [];
  if (view.sort === "name") recent.sort(function (x, y) { return (x.title || "").localeCompare(y.title || ""); });
  var groups = [];
  if (view.group === "folder") {
    var byKey = {};
    recent.forEach(function (e) {
      var key = e.project_dir || "";
      if (!byKey[key]) { byKey[key] = { label: folderName(key) || "No folder", items: [] }; groups.push(byKey[key]); }
      byKey[key].items.push(e);
    });
  } else {
    // Today, Yesterday, Older; the older ones wait behind "Show more"
    var midnight = new Date(); midnight.setHours(0, 0, 0, 0);
    var today = midnight.getTime(), yesterday = today - 86400000;
    ["Today", "Yesterday", "Older"].forEach(function (label, gi) {
      var items = recent.filter(function (e) {
        var t = new Date(e.last_active).getTime() || 0;
        return gi === 0 ? t >= today : gi === 1 ? t >= yesterday && t < today : t < yesterday;
      });
      if (items.length) groups.push({ label: label, items: items, older: gi === 2 });
    });
  }
  groups.forEach(function (g, gi) {
    var head = box.appendChild(h("div", "rh"));
    head.appendChild(h("span", "", g.label));
    if (gi === 0) {
      var gb = head.appendChild(h("button", "iconbtn grpbtn"));
      gb.type = "button"; gb.title = "Group and sort"; gb.setAttribute("aria-label", "Group and sort");
      gb.innerHTML = svg("sliders");
      gb.onclick = function (ev) { ev.stopPropagation(); var r = gb.getBoundingClientRect(); openGroupMenu(r.left, r.bottom + 4); };
    }
    g.items.forEach(function (e) {
      if (g.older && shown >= 12 && !S.allSessions && e.id !== S.current) { more.push(e); return; }
      box.appendChild(row(e)); shown++;
    });
  });
  if (archived.length && S.showArchived) {
    box.appendChild(h("div", "rh")).appendChild(h("span", "", "Archived"));
    archived.forEach(function (e) { var r = row(e); r.classList.add("archived"); box.appendChild(r); });
  }
  if (more.length) {
    var mb = box.appendChild(h("button", "it showmore"));
    mb.appendChild(h("span", "", "Show more"));
    mb.onclick = function () { S.allSessions = true; renderSessions(); };
  }
  if (!S.list.length) box.appendChild(h("div", "it quiet")).appendChild(h("span", "", "No sessions yet"));
  if (!$("hello").classList.contains("hidden")) renderHome();
  glide(box, box.querySelector(".it.ses.on"));
}

/* Right-click a file path in the chat or the diff, as in Claude Code: attach it to the next
   message, copy it, or show it in Explorer. */
function pathFrom(el) {
  var t = (el.textContent || "").trim().replace(/[:(]\d+(?:[:,-]\d+)?\)?$/, "");
  if (t.length > 260 || /\s/.test(t)) return "";
  return /^[\w@.~-]*([\\/][\w@.~ -]+)+$|^[\w@.-]+\.[A-Za-z0-9]{1,8}$/.test(t) ? t.replace(/\\/g, "/") : "";
}
function openPathMenu(path, x, y) {
  var m = $("row-menu");
  m.innerHTML = "";
  m.appendChild(h("div", "mh")).appendChild(h("span", "", path));
  function item(icon, label, fn) {
    var b = menuRow(icon, label, fn);
    m.appendChild(b);
  }
  item("file", "Open", function () { openFile(path); });
  item("plus", "Attach as context", function () {
    var input = $("input"), sep = input.value && !/\s$/.test(input.value) ? " " : "";
    input.value += sep + "@" + path + " "; autosize(); input.focus();
  });
  item("copy", "Copy relative path", function () { copyText(path, function () { toast("Copied " + path); }); });
  item("copy", "Copy filename", function () { var f = path.split("/").pop(); copyText(f, function () { toast("Copied " + f); }); });
  if (S.current) item("folder", "Show in Explorer", function () {
    api("POST", "/api/sessions/" + S.current + "/reveal", { path: path }).catch(toastError);
  });
  closeMenus();
  m.classList.add("open");
  var w = m.offsetWidth, ht = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - ht - 8)) + "px";
}

function renameSession(e) {
  askText("Rename session", e.title || "", "Save", function (t) {
    api("POST", "/api/sessions/" + e.id + "/rename", { title: t }).then(function () {
      if (e.id === S.current) setSessionTitle(t);
      return refreshSessions();
    }).catch(toastError);
  });
}

function openRowMenu(e, x, y) {
  var m = $("row-menu");
  m.innerHTML = "";
  var isPin = pins().indexOf(e.id) >= 0;
  function item(icon, label, fn, danger) {
    var b = h("button", "mi row1" + (danger ? " danger" : ""));
    b.innerHTML = svg(icon) + "<span><b></b></span>";
    b.querySelector("b").textContent = label;
    b.onclick = function () { closeMenus(); fn(); };
    m.appendChild(b);
  }
  item("pencil", "Rename", function () { renameSession(e); });
  item("star", isPin ? "Unpin" : "Pin to top", function () { togglePin(e.id); });
  item("copy", "Duplicate", function () {
    api("POST", "/api/sessions/" + e.id + "/duplicate", {}).then(function (snap) {
      return refreshSessions().then(function () { openSession(snap.id); });
    }).catch(toastError);
  });
  item("cube", e.archived ? "Unarchive" : "Archive", function () { archiveSession(e, !e.archived); });
  m.appendChild(h("div", "sep"));
  if (e.id !== S.current) item("split", "Open in split view", function () { openSplit(e); });
  item("popout", "Open in new window", function () { popOut(e.id); });
  m.appendChild(h("div", "sep"));
  item("trash", "Delete session", function () {
    askConfirm("Delete this session?",
      "“" + (e.title || "New session") + "” is removed from the list. Files in your folder are not touched.",
      "Delete", function () {
        api("DELETE", "/api/sessions/" + e.id).catch(function () {}).then(function () {
          delete S.status[e.id];
          if (S.current === e.id) newSession();
          refreshSessions();
        });
      });
  }, true);
  closeMenus();
  m.classList.add("open");
  var w = m.offsetWidth, ht = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - ht - 8)) + "px";
}

/* A new session exists only in the page until the first message is sent,
   so opening the app or pressing New never leaves empty sessions behind. */
function newSession() {
  if (S.es) { S.es.close(); S.es = null; }
  S.current = null; S.snap = null; S.pendingFolder = null;
  keepSession(""); restoreDraft();
  resetView();
  resetAgentBrowser();
  setSessionTitle("");
  document.title = "CodeMonkey";
  showHello(true);
  renderSessions(); renderPickers(); updateCtx();
  document.body.classList.remove("nav-open");
  $("input").focus();
}

function resetView() {
  S.todos = []; S.subagents = {}; S.turn = null; S.lastRetry = null;
  S.changes = {}; closeReview(); updateChangesBtn();
  closeSide(); $("side-log").innerHTML = "";
  S.comments = []; renderCommentBar();
  S.lastPlan = ""; S.file = null; S.term.seq = 0; $("term-out").innerHTML = "";
  S.git = null; $("gitbar").classList.add("hidden");
  // the session's own panes close; Activity stays if you left it open (it is about whichever session is open)
  closePanes(activityWanted() ? "inspector" : null);
  setSuggestion("");
  S.attachments = []; renderAttachments();
  $("chat").innerHTML = "";
  closePerm();
  setWorking(false);
  renderTodos(); renderSubagents(); renderCheckpoints([]);
  $("queue-bar").classList.add("hidden");
}

function showHello(on) {
  $("hello").classList.toggle("hidden", !on);
  $("chat").classList.toggle("hidden", on);
  if (on) renderHome();
}

function readyIds() { var r = store("cm:ready"); return Array.isArray(r) ? r : []; }
function markReady(id, on) {
  var r = readyIds().filter(function (x) { return x !== id; });
  if (on) r.unshift(id);
  store("cm:ready", r.slice(0, 50));
}
/* What a session needs from you, in Claude Code's words. */
function sessionTag(e) {
  var st = S.status[e.id] || "";
  if (st === "permission") return ["Needs input", "need"];
  if (st === "error") return ["Failed", "bad"];
  if (st === "working") return ["Working", "busy"];
  if (readyIds().indexOf(e.id) >= 0) return ["Ready for review", "ready"];
  return null;
}

/* The start screen, as Claude Code's home: the mascot, a welcome, and the sessions that need you
   first, then the latest few. */
function renderHome() {
  var box = $("home-list");
  if (!box) return;
  box.innerHTML = "";
  $("home-hi").textContent = S.list.length ? "Welcome back" : "Welcome to CodeMonkey";
  if (S.available === undefined) {   // the Get started card needs to know whether a model is connected
    S.available = null;
    api("GET", "/api/models/available").then(function (live) { S.available = live; renderHome(); })
      .catch(function () { S.available = []; renderHome(); });
  } else if (S.available !== null) {
    welcomeCard(box);
  }
  if (!document.querySelector(".welcome")) beginnerOffer(box);   // one card at a time: Get started first
  else if (document.querySelector(".beginoffer")) document.querySelector(".beginoffer").remove();
  var live = S.list.filter(function (e) { return !e.archived; });
  var tagged = live.filter(function (e) { return sessionTag(e); });
  var rest = live.filter(function (e) { return !sessionTag(e); });
  var rows = tagged.concat(rest).slice(0, 5);
  if (!rows.length) return;
  box.appendChild(h("div", "rh")).appendChild(h("span", "", "Sessions"));
  rows.forEach(function (e) {
    var tag = sessionTag(e), b = box.appendChild(h("button", "hs"));
    b.type = "button";
    b.title = (e.project_dir || "") + (e.last_active ? "\n" + shortAgo(e.last_active) : "");
    if (tag) b.appendChild(h("span", "htag " + tag[1], tag[0]));
    b.appendChild(h("span", "ht", e.title || "New session"));
    b.appendChild(h("em", "", tag ? "" : shortAgo(e.last_active)));
    b.onclick = function () { openSession(e.id); };
  });
}

/* An idea from Settings > Ideas, put in the input to send. */
function startIdea(idea) {
  api("POST", "/api/ideas/" + idea.idea_id, { status: "started" }).catch(function () {});
  var input = $("input");
  input.value = idea.prompt;
  input.focus(); autosize();
}

function openSession(id) {
  var entry = sessionEntry(id);
  document.body.classList.remove("nav-open");
  if (S.es) { S.es.close(); S.es = null; }
  S.current = id; S.snap = null;
  keepSession(id); restoreDraft();
  markReady(id, false);
  resetView();
  resetAgentBrowser();
  renderSessions();
  var ready = entry && !entry.live
    ? api("POST", "/api/sessions/" + id + "/reopen", {})
    : api("GET", "/api/sessions/" + id);
  return ready.then(function (snap) {
    if (S.current !== id) return;
    if (entry) entry.live = true;
    applySnapshot(snap);
    if (paneOpen("file-pane")) { showTree(true); loadTree(); }   // the new session's folder
    return api("GET", "/api/sessions/" + id + "/history").then(function (hist) {
      if (S.current !== id) return;
      var entries = hist.transcript || [];
      showHello(!entries.length);
      entries.forEach(replayEntry);
      endReplay();
      stickToEnd(1500);
      // pick the stream up exactly where the record ends, so nothing falls between the two
      SYNC.seq = hist.seq || 0;
      openEvents(id, SYNC.seq);
      restoreScroll(id); restorePane();   // as it was before CodeMonkey closed (keep.js)
    });
  }).catch(function (err) {
    if (S.current !== id) return;
    showHello(false);
    addError({ message: err.message || String(err) });
  });
}

function applySnapshot(snap) {
  S.snap = snap;
  // An answer being written right now: the events that follow carry only its newest words, so the
  // page needs what has been written up to this moment (webui/app.py _on_delta). Skip it when the
  // page already holds more than the snapshot — a snapshot can be older than the last event, and
  // taking it then would drop words the page has already been sent.
  if (snap.streaming != null && STREAM.text.indexOf(snap.streaming) !== 0) STREAM.text = snap.streaming;
  if (S.findOnOpen) setTimeout(findInChat, 200);
  S.status[snap.id] = snap.status;
  S.todos = snap.todos || [];
  S.processes = snap.processes || [];
  renderBackground();
  setSessionTitle(snap.title === "New session" ? "" : snap.title);
  document.title = snap.title && snap.title !== "New session" ? snap.title + " · CodeMonkey" : "CodeMonkey";
  var entry = sessionEntry(snap.id);
  if (entry) { entry.title = snap.title; entry.project_dir = snap.project_dir; }
  renderSessions(); renderTodos(); renderPickers(); updateCtx();
  renderCheckpoints(snap.checkpoints || []);
  if (!S.git) refreshGit(false);
  (snap.helpers || []).forEach(function (hp) {
    S.subagents[hp.id] = { brief: hp.brief, done: hp.status !== "running", ok: hp.status === "done",
      summary: hp.summary, steps: hp.steps, last: hp.last };
  });
  renderSubagents();
  setWorking(snap.status === "working" || snap.status === "permission");
  if (snap.pending_permission && (!S.perm || S.perm.request_id !== snap.pending_permission.request_id)) {
    onPermissionRequest(snap.pending_permission);
  }
  if (snap.pending_elicitation && (!S.elicit || S.elicit.request_id !== snap.pending_elicitation.request_id)) {
    onElicitation(snap.pending_elicitation);
  }
}
