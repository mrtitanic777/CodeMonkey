"use strict";
/* CodeMonkey page, panes.js: Split view, pop-outs, the panes beside the chat, the branch bar.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- split view and windows of their own (Claude Code's Open in → Split view / New window) ----------------
   A second session is the same page in a frame, told by its address to open that session alone:
   #session=<id>&solo. A pane popped out is #session=<id>&pane=<id>: that pane, filling the window. */

var HASH = (function () {
  var o = {};
  location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
    if (!kv) return;
    var i = kv.indexOf("=");
    o[i < 0 ? kv : kv.slice(0, i)] = i < 0 ? true : decodeURIComponent(kv.slice(i + 1));
  });
  return o;
})();

function sessionUrl(id, extra) { return location.pathname + "#session=" + encodeURIComponent(id) + (extra || ""); }
function popOut(id, pane) {
  var w = window.open(sessionUrl(id, pane ? "&pane=" + pane : "&solo"), "_blank",
                      pane ? "width=720,height=820" : "width=1100,height=860");
  if (!w) toast("Your browser blocked the new window");
}
function openSplit(e) {
  if (window.innerWidth < 1000) { popOut(e.id); return; }
  S.splitId = e.id;
  $("split-frame").src = sessionUrl(e.id, "&solo&_=" + Date.now());
  $("split").classList.remove("hidden");
  document.querySelector(".app").classList.add("has-split");
}
function closeSplit() {
  S.splitId = null;
  $("split").classList.add("hidden");
  $("split-frame").src = "about:blank";
  document.querySelector(".app").classList.remove("has-split");
}
// the pane a pop-out button sits on goes to a window of its own, and closes here
function popPane(btn) {
  var pane = btn.closest("aside");
  if (!pane || !S.current) return;
  var id = pane.id;
  if (id === "review" && S.reviewTurn) id += ":" + S.reviewTurn;
  popOut(S.current, id);
  closePanes(null);
}
// a popped-out pane: open it once the session is here
function openPoppedPane(spec) {
  var parts = String(spec).split(":"), id = parts[0];
  document.body.classList.add("popout");
  if (id === "tasks-pane") toggleTasks();
  else if (id === "plan-pane") togglePlan();
  else if (id === "term-pane") toggleTerm();
  else if (id === "review") openReview(isNaN(+parts[1]) ? parts[1] : +parts[1]);
  document.title = (S.snap && S.snap.title ? S.snap.title + " \u00b7 " : "") + ({ "tasks-pane": "Tasks", "plan-pane": "Plan", "term-pane": "Terminal", review: "Changes" }[id] || "CodeMonkey");
}

/* ---------------- panes beside the chat: one at a time, as Claude Code docks them ---------------- */

var PANES = ["review", "inspector", "side", "tasks-pane", "plan-pane", "file-pane", "term-pane", "helper-pane", "browser-pane"];
function closePanes(except) {
  PANES.forEach(function (id) {
    if (id === except || !$(id)) return;
    if (id === "review") { $("review").classList.add("hidden"); S.reviewTurn = null; }
    else if (id === "inspector") {
      if (!$("inspector").classList.contains("hidden")) store("cm:activity", false);   // you moved on from it
      $("inspector").classList.add("hidden"); $("activity-btn").classList.remove("on");
    }
    else $(id).classList.add("hidden");
  });
  if (except !== "term-pane") stopTermPoll();
  keepPane(except);   // kept for the next start (keep.js): the terminal or the files, or nothing
}
function paneOpen(id) { return !!$(id) && !$(id).classList.contains("hidden"); }
function showPane(id) { closePanes(id); $(id).classList.remove("hidden"); applyPaneWidth(id); }

/* Tasks: the background work inside this session, running first (helpers and background commands). */
function renderTasks() {
  if (!paneOpen("tasks-pane")) return;
  var box = $("tasks-body"), running = [], done = [];
  Object.keys(S.subagents || {}).forEach(function (id) {
    var s = S.subagents[id];
    (s.done ? done : running).push({ kind: "Agent", title: s.brief || "Helper", id: id, s: s,
      sub: s.done ? (s.ok === false ? "Failed" : "Done") : "Running" + (s.steps ? " · step " + s.steps : "") });
  });
  (S.processes || []).forEach(function (p) {
    var live = p.state === "running";
    (live ? running : done).push({ kind: "Bash", title: p.command, p: p,
      sub: live ? "Running" : p.state.replace("exited with code ", "Exit ") });
  });
  box.innerHTML = "";
  if (!running.length && !done.length) {
    box.appendChild(h("p", "panehint", "Nothing running. Helpers and background commands (Ctrl+B) show here."));
    return;
  }
  [["Running", running], ["Completed", done]].forEach(function (g) {
    if (!g[1].length) return;
    box.appendChild(h("div", "rh")).appendChild(h("span", "", g[0]));
    g[1].forEach(function (t) {
      var item = h("div", "task" + (g[0] === "Running" ? " live" : ""));
      var row = item.appendChild(h("div", "taskrow"));
      row.appendChild(h("i", "tdot"));
      var text = row.appendChild(h("div", "ttext"));
      text.appendChild(h("b", "", t.title));
      text.appendChild(h("small", "", t.sub));
      row.appendChild(h("span", "ttag", t.kind));
      if (t.p && t.p.state === "running") {
        row.appendChild(iconButton("x", "Stop", function () {
          api("POST", "/api/sessions/" + S.current + "/processes/" + encodeURIComponent(t.p.name) + "/stop", {}).catch(toastError);
        }));
      }
      row.onclick = function (ev) {
        if (ev.target.closest(".iconbtn")) return;
        if (t.id && /^[0-9a-f]+$/.test(t.id)) { openHelper(t.id); return; }
        if (!t.p) return;
        var shown = item.querySelector("pre");
        if (shown) { shown.remove(); return; }
        api("GET", "/api/sessions/" + S.current + "/processes/" + encodeURIComponent(t.p.name) + "/logs").then(function (r) {
          var pre = h("pre", "logtail");
          pre.textContent = r.lines.join("\n") || "(no output yet)";
          item.appendChild(pre);
          pre.scrollTop = pre.scrollHeight;
        }).catch(toastError);
      };
      box.appendChild(item);
    });
  });
}
function toggleTasks() { if (paneOpen("tasks-pane")) closePanes(null); else { showPane("tasks-pane"); renderTasks(); } }

/* Plan: the latest plan of this session, and the to-do list it turned into. */
function renderPlanPane() {
  if (!paneOpen("plan-pane")) return;
  var box = $("plan-body");
  box.innerHTML = "";
  if (S.lastPlan) {
    var md = box.appendChild(h("div", "md"));
    md.innerHTML = renderMarkdown(S.lastPlan);
  } else {
    box.appendChild(h("p", "panehint", "No plan yet. Switch to Plan (Shift+Tab or the mode menu) and ask for one: the agent explores, then proposes a plan here before changing anything."));
  }
  if (S.todos && S.todos.length) {
    box.appendChild(h("div", "rh")).appendChild(h("span", "", "To do"));
    S.todos.forEach(function (t) {
      var row = box.appendChild(h("div", "todo " + t.status));
      row.appendChild(h("i", "tdot"));
      row.appendChild(h("span", "", t.title));
    });
  }
}
function togglePlan() { if (paneOpen("plan-pane")) closePanes(null); else { showPane("plan-pane"); renderPlanPane(); } }

/* Files: open a file from the session's folder, edit it, save it (Ctrl+S). */
S.file = null;
function openFile(path) {
  if (!S.current) { toast("Open a session first"); return; }
  if (S.file && S.file.dirty && S.file.path !== path && !confirm("Discard your unsaved changes to " + S.file.path + "?")) return;
  api("GET", "/api/sessions/" + S.current + "/file?path=" + encodeURIComponent(path)).then(function (r) {
    S.file = { path: r.path, mtime: r.mtime, text: r.text, dirty: false, viewOnly: r.view_only };
    showPane("file-pane");
    showTree(false);
    $("file-title").textContent = r.path;
    $("file-title").title = r.path;
    var ta = $("file-text");
    ta.value = r.text;
    ta.readOnly = !!r.view_only;
    $("file-note").textContent = r.view_only ? "View-only here. Open it in another app to change it." : fmtNum(r.size) + " bytes";
    renderFileState();
    ta.scrollTop = 0;
  }).catch(toastError);
}
function renderFileState() {
  var f = S.file;
  $("file-save").classList.toggle("hidden", !f || !f.dirty);
  $("file-dot").classList.toggle("hidden", !f || !f.dirty);
}
function saveFile(force) {
  var f = S.file;
  if (!f || f.viewOnly || !S.current) return;
  var text = $("file-text").value;
  api("POST", "/api/sessions/" + S.current + "/file", { path: f.path, text: text, mtime: f.mtime, force: !!force }).then(function (r) {
    f.mtime = r.mtime; f.text = text; f.dirty = false;
    $("file-note").textContent = "Saved · " + fmtNum(r.size) + " bytes";
    renderFileState();
  }).catch(function (err) {
    if (/changed on disk/.test(err.message || "")) {
      askConfirm("The file changed on disk", f.path + " changed since you opened it. Override writes your version over it; Cancel keeps the pane as it is so you can reopen the file.",
        "Override", function () { saveFile(true); });
    } else toastError(err);
  });
}
function toggleFiles() {
  if (paneOpen("file-pane")) { closePanes(null); return; }
  if (!S.current) { toast("Open a session first"); return; }
  showPane("file-pane");
  showTree(!S.file);
  loadTree();
}
function backToTree() {
  if (S.file && S.file.dirty && !confirm("Discard your unsaved changes to " + S.file.path + "?")) return;
  S.file = null;
  showTree(true);
  renderTree();
  var on = $("file-tree").querySelector(".trow");
  if (on) on.focus();
}

/* The Files pane's tree: the project's folders and files, what git sees changed, a filter.
   Click a file to view it; @ mentions it in your message. */
S.tree = { files: [], changed: {}, open: {}, folder: "", filter: "" };
function treeKey() { return "cm:tree:" + (currentFolder() || ""); }
function showTree(on) {
  $("file-pane").classList.toggle("treeview", on);
  if (on) { $("file-title").textContent = S.tree.folder || "Files"; $("file-title").title = currentFolder() || ""; renderFileState(); }
}
function loadTree() {
  if (!S.current) return Promise.resolve();
  return api("GET", "/api/sessions/" + S.current + "/tree").then(function (t) {
    S.tree.files = t.files; S.tree.changed = t.changed || {}; S.tree.folder = t.folder; S.tree.truncated = t.truncated;
    S.tree.open = store(treeKey()) || {};
    if ($("file-pane").classList.contains("treeview")) $("file-title").textContent = t.folder || "Files";
    renderTree();
  }).catch(toastError);
}
function mentionFile(path) {
  var input = $("input");
  input.value += (input.value && !/\s$/.test(input.value) ? " " : "") + "@" + path + " ";
  input.focus(); autosize();
  toast("Mentioned " + path.split("/").pop());
}
/* A filter's ranking, as the @ list ranks: the file's name first, then its path, then letters in order. */
function treeScore(path, q) {
  var low = path.toLowerCase(), base = low.slice(low.lastIndexOf("/") + 1);
  function inOrder(t) { var i = 0; for (var j = 0; j < t.length && i < q.length; j++) if (t[j] === q[i]) i++; return i === q.length; }
  var s = base.indexOf(q) === 0 ? 100 : base.indexOf(q) > 0 ? 80 : low.indexOf(q) >= 0 ? 60 : inOrder(base) ? 40 : inOrder(low) ? 20 : -1;
  return s < 0 ? s : s - Math.min(15, path.split("/").length) - path.length / 1000;
}
function treeRow(name, path, depth, isDir) {
  var r = h("div", "trow" + (isDir ? " dir" : "") + (S.file && S.file.path === path ? " on" : ""));
  r.style.paddingLeft = (8 + depth * 14) + "px";
  r.setAttribute("role", "treeitem");
  r.tabIndex = 0;
  var mark = isDir ? '<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>' : '<span class="chev"></span>';
  r.innerHTML = mark + svg(isDir ? "folder" : "file");
  r.appendChild(h("span", "tname", name));
  var change = isDir ? "" : S.tree.changed[path];
  if (change) {
    var c = r.appendChild(h("span", "tchg c" + (change === "?" ? "n" : change.toLowerCase()), change === "?" ? "U" : change));
    c.title = { M: "Changed", A: "Added", D: "Deleted", "?": "New, not added to git yet" }[change] || "";
  } else if (isDir && S.tree.dirty[path]) {
    r.appendChild(h("span", "tdot")).title = "Has changes";
  }
  if (!isDir) {
    var at = r.appendChild(h("button", "iconbtn tat", "@"));
    at.type = "button"; at.title = "Mention in your message"; at.setAttribute("aria-label", "Mention " + path);
    at.onclick = function (e) { e.stopPropagation(); mentionFile(path); };
  }
  return r;
}
function renderTree() {
  var box = $("file-tree");
  if (!box) return;
  box.innerHTML = "";
  var q = S.tree.filter.trim().toLowerCase();
  var frag = document.createDocumentFragment();
  if (q) {   // a filter: matching files, flat, best first
    var hits = S.tree.files.map(function (f) { return { f: f, s: treeScore(f, q) }; })
      .filter(function (x) { return x.s >= 0; }).sort(function (a, b) { return b.s - a.s; }).slice(0, 300);
    hits.forEach(function (x) {
      var r = treeRow(x.f, x.f, 0, false);
      r.onclick = function () { openFile(x.f); };
      frag.appendChild(r);
    });
    if (!hits.length) frag.appendChild(h("div", "empty", "No file matches."));
    box.appendChild(frag);
    return;
  }
  // folders hold folders and files; a folder is marked when something under it changed
  var root = { dirs: {}, files: [] };
  S.tree.dirty = {};
  Object.keys(S.tree.changed).forEach(function (p) {
    var parts = p.split("/");
    for (var i = 1; i < parts.length; i++) S.tree.dirty[parts.slice(0, i).join("/")] = true;
  });
  S.tree.files.forEach(function (f) {
    var parts = f.split("/"), node = root;
    for (var i = 0; i < parts.length - 1; i++) node = node.dirs[parts[i]] || (node.dirs[parts[i]] = { dirs: {}, files: [] });
    node.files.push(parts[parts.length - 1]);
  });
  function walk(node, prefix, depth) {
    Object.keys(node.dirs).sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : 1; }).forEach(function (d) {
      var path = prefix + d, open = !!S.tree.open[path];
      var r = treeRow(d, path, depth, true);
      r.classList.toggle("open", open);
      r.setAttribute("aria-expanded", open ? "true" : "false");
      r.onclick = function () {
        if (S.tree.open[path]) delete S.tree.open[path]; else S.tree.open[path] = true;
        store(treeKey(), S.tree.open);
        renderTree();
      };
      frag.appendChild(r);
      if (open) walk(node.dirs[d], path + "/", depth + 1);
    });
    node.files.sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : 1; }).forEach(function (f) {
      var path = prefix + f, r = treeRow(f, path, depth, false);
      r.onclick = function () { openFile(path); };
      frag.appendChild(r);
    });
  }
  walk(root, "", 0);
  if (!S.tree.files.length) frag.appendChild(h("div", "empty", "No files in this folder yet."));
  if (S.tree.truncated) frag.appendChild(h("div", "empty", "A very large folder: only the first 20,000 files are listed. Filter to find others."));
  box.appendChild(frag);
}
function treeKeys(e) {   // arrows move between rows, Enter opens, Right/Left open and close a folder
  var rows = Array.prototype.slice.call($("file-tree").querySelectorAll(".trow")), i = rows.indexOf(document.activeElement);
  if (i < 0) return;
  var r = rows[i];
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); var n = rows[i + (e.key === "ArrowDown" ? 1 : -1)]; if (n) n.focus(); }
  else if (e.key === "Enter" || (e.key === "ArrowRight" && r.classList.contains("dir") && !r.classList.contains("open")) ||
           (e.key === "ArrowLeft" && r.classList.contains("open"))) {
    e.preventDefault();
    var label = r.querySelector(".tname").textContent;
    r.click();
    var again = Array.prototype.filter.call($("file-tree").querySelectorAll(".trow"), function (x) { return x.querySelector(".tname").textContent === label; })[0];
    if (again && $("file-pane").classList.contains("treeview")) again.focus();
  }
}

/* Browser: the app being built, running, beside the chat. Its address is remembered per folder,
   and a local address a command prints (a dev server's "Local: http://localhost:5173") is picked up. */
S.br = { back: [], fwd: [], url: "" };
function brKey() { return "cm:browser:" + (currentFolder() || ""); }
function brNormal(u) {
  u = String(u || "").trim();
  if (!u) return "";
  if (/^\d+$/.test(u)) u = "localhost:" + u;
  if (!/^[a-z]+:\/\//i.test(u)) u = (/^(localhost|127\.|\[::1\]|[\w.-]+\.(localhost|test))/i.test(u) ? "http://" : "https://") + u;
  return u;
}
function brGo(u, how) {
  u = brNormal(u);
  if (!u) return;
  if (S.br.url && how !== "hist") { S.br.back.push(S.br.url); S.br.fwd = []; }
  S.br.url = u;
  $("br-url").value = u;
  $("br-frame").src = u;
  $("br-empty").classList.add("hidden");
  $("br-back").disabled = !S.br.back.length;
  $("br-fwd").disabled = !S.br.fwd.length;
  try { localStorage.setItem(brKey(), u); } catch (e) {}
}
/* The agent's browser: it runs out of sight, so the pane shows a picture of its page after each step. */
function brMode(agent) {
  S.br.mode = agent ? "agent" : "you";
  $("br-tab-you").setAttribute("aria-selected", agent ? "false" : "true");
  $("br-tab-agent").setAttribute("aria-selected", agent ? "true" : "false");
  $("br-frame").classList.toggle("hidden", agent);
  $("br-agent").classList.toggle("hidden", !agent);
  ["br-back", "br-fwd", "br-reload"].forEach(function (id) { $(id).classList.toggle("hidden", agent); });
  $("br-url").readOnly = agent;
  if (agent) {
    $("br-empty").classList.add("hidden");
    var a = S.br.agent;
    $("br-url").value = a ? a.url : "";
    if (a && S.current) $("br-shot").src = "/api/sessions/" + S.current + "/browser-view.png?n=" + a.n;
  } else {
    $("br-url").value = S.br.url || "";
    $("br-empty").classList.toggle("hidden", !!S.br.url);
  }
}
function resetAgentBrowser() {
  S.br.agent = null;
  $("br-tab-agent").disabled = true;
  if (S.br.mode === "agent") brMode(false);
}
function onBrowserView(d) {
  S.br.agent = d;
  $("br-tab-agent").disabled = false;
  if (!paneOpen("browser-pane")) return;   // the pane is not opened for you; open it to watch
  brMode(true);
}

function toggleBrowser(url) {
  if (paneOpen("browser-pane") && !url) { closePanes(null); return; }
  showPane("browser-pane");
  if (!url && S.br.agent && !S.br.url) { brMode(true); return; }
  brMode(false);
  var u = url || S.br.url || S.br.seen || (function () { try { return localStorage.getItem(brKey()); } catch (e) { return ""; } })();
  if (u) { if (brNormal(u) !== S.br.url) brGo(u); }
  else { $("br-empty").classList.remove("hidden"); $("br-url").focus(); }
}
function noticeLocalUrl(text) {
  var m = /https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\]):\d{2,5}[^\s"'<>)\]]*/.exec(String(text || ""));
  if (!m || m[0].indexOf(location.host) >= 0) return;   // not CodeMonkey itself
  S.br.seen = m[0].replace(/[.,;]+$/, "");
}
function wireBrowser() {
  $("br-url").onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); brGo(this.value); } };
  $("br-reload").onclick = function () { if (S.br.url) $("br-frame").src = S.br.url; };
  $("br-back").onclick = function () { if (!S.br.back.length) return; S.br.fwd.push(S.br.url); brGo(S.br.back.pop(), "hist"); };
  $("br-fwd").onclick = function () { if (!S.br.fwd.length) return; S.br.back.push(S.br.url); brGo(S.br.fwd.pop(), "hist"); };
  $("br-ext").onclick = function () { var u = S.br.mode === "agent" ? (S.br.agent || {}).url : S.br.url; if (u) window.open(u, "_blank"); };
  $("br-tab-you").onclick = function () { brMode(false); if (!S.br.url) $("br-url").focus(); };
  $("br-tab-agent").onclick = function () { brMode(true); };
}

/* Terminal: commands you type, run in the session's folder (this computer only). */
S.term = { seq: 0, timer: null, hist: [], at: 0 };
function toggleTerm() {
  if (paneOpen("term-pane")) { closePanes(null); return; }
  if (!S.current) { toast("Open a session first: the terminal runs in its folder"); return; }
  showPane("term-pane");
  $("term-input").focus();
  pollTerm();
}
function stopTermPoll() { clearTimeout(S.term.timer); S.term.timer = null; }
function pollTerm() {
  stopTermPoll();
  if (!paneOpen("term-pane") || !S.current) return;
  var id = S.current;
  api("GET", "/api/sessions/" + id + "/terminal?after=" + S.term.seq).then(function (r) {
    if (S.current !== id) return;
    var out = $("term-out"), atEnd = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    r.lines.forEach(function (l) {
      S.term.seq = Math.max(S.term.seq, l.n);
      if (l.kind === "fail") { out.appendChild(fixCard(l)); return; }
      if (l.kind === "end") fixChecked(l);
      if (l.kind !== "cmd") noticeLocalUrl(l.text);
      var line = h("div", "tl " + l.kind);
      line.textContent = l.kind === "cmd" ? "› " + l.text : l.text;
      out.appendChild(line);
    });
    if (r.lines.length && atEnd) out.scrollTop = out.scrollHeight;
    $("term-cwd").textContent = r.cwd;
    $("term-stop").classList.toggle("hidden", !r.running);
    if (r.running) fixStillRunning();
    S.term.timer = setTimeout(pollTerm, r.running ? 400 : 1200);
  }).catch(function (err) {
    $("term-out").appendChild(h("div", "tl err", err.message || "The terminal is not available"));
  });
}
function runTerm(command) {
  if (!command.trim() || !S.current) return;
  S.term.hist.push(command); S.term.at = S.term.hist.length;
  if (/^(clear|cls)$/.test(command.trim())) { $("term-out").innerHTML = ""; }
  api("POST", "/api/sessions/" + S.current + "/terminal", { command: command }).then(pollTerm).catch(toastError);
}

/* Fix it for me: a command in the terminal failed. The card says why in plain words (webui/explain.py);
   "Fix it" hands the error to the agent, which fixes the code and re-runs the command itself (a few
   tries). When its turn ends the command runs here once more to check, and the card says how it went.
   A command that is still running after a while with no error (a game, a server) counts as working. */
S.fix = null;   // {card, command, tries, phase: "fixing"|"checking", after, started}
var FIX_TRIES = 3;
function fixCard(l) {
  var why = l.why || {}, tries = S.fix && S.fix.command === l.command ? S.fix.tries : 0;
  if (S.fix && S.fix.phase === "checking") {
    fixState(S.fix.card, "bad", "Still not working", "It still fails after the fix. See below.");
    S.fix.card.querySelector(".btnrow").classList.add("hidden");   // the new card below takes over
    S.fix.phase = "failed";
  }
  var card = h("div", "fixcard");
  card.setAttribute("role", "alert");
  var title = card.appendChild(h("h4"));
  title.innerHTML = svg("warn") + "<span></span>";
  title.lastChild.textContent = why.title || "It stopped with an error";
  card.appendChild(h("p", "", why.plain || "The command ended with an error."));
  if (tries) card.appendChild(h("p", "fixnote", tries >= FIX_TRIES
    ? "CodeMonkey has tried " + tries + " times. It may need a hand: show the full error, or say in the chat what the program should do."
    : "Tried " + tries + (tries === 1 ? " time" : " times") + " so far."));
  var row = card.appendChild(h("div", "btnrow"));
  row.appendChild(button(tries ? "Try again" : "Fix it", function () { fixIt(card, l, tries); }, "primary"));
  row.appendChild(button("Show the full error", function () {
    var pre = card.querySelector("pre");
    if (pre) { pre.remove(); this.textContent = "Show the full error"; return; }
    card.appendChild(h("pre", "", l.output || l.text));
    this.textContent = "Hide the error";
  }));
  if (why.file) row.appendChild(button("Open " + baseName(why.file), function () { openFile(why.file); }));
  row.appendChild(button("Dismiss", function () { card.remove(); if (S.fix && S.fix.card === card) S.fix = null; }));
  return card;
}
function fixState(card, kind, title, text) {
  card.className = "fixcard " + kind;
  card.querySelector("h4").innerHTML = svg(kind === "good" ? "check" : kind === "busy" ? "retry" : "warn") + "<span></span>";
  card.querySelector("h4 span").textContent = title;
  card.querySelector("p").textContent = text;
  var note = card.querySelector(".fixnote");
  if (note) note.remove();
  card.querySelector(".btnrow").classList.toggle("hidden", kind === "busy" || kind === "good");
}
function fixIt(card, l, tries) {
  if (S.workStart) { toast("Wait for CodeMonkey to finish what it's doing"); return; }
  var output = String(l.output || "").replace(/```/g, "'''");
  S.fix = { card: card, command: l.command, tries: tries + 1, phase: "fixing", sid: S.current };
  fixState(card, "busy", "Fixing it…", "CodeMonkey is looking at the error and changing the code. Follow along in the chat.");
  sendText("Fix this error for me.\n\nI ran `" + l.command + "` and it failed (exit " + l.code + "):\n\n```\n" + output + "\n```\n\n" +
    "Find what caused it and fix the code, without changing what the program is meant to do. Then run `" + l.command +
    "` again to check (if it's a program that keeps running, like a game or a server, give it a short time limit), and keep going" +
    " if it still fails, up to " + FIX_TRIES + " tries. End with one short line in plain words that starts with \"Fixed:\" or \"Couldn't fix:\".");
}
/* turn_end calls this with the turn that just finished. */
function fixTurnEnded(turn) {
  var f = S.fix;
  if (!f || f.phase !== "fixing") return;
  if (f.sid !== S.current) { S.fix = null; return; }
  var text = turn && turn.reply ? (turn.reply._text || (turn.reply.querySelector(".md") || {}).innerText || "") : "";
  var said = (text.match(/^\s*\**(Fixed|Couldn't fix|Could not fix):?\**:?\s*(.+)$/im) || [])[0];
  f.report = said ? said.replace(/\*\*/g, "").trim() : "";
  f.phase = "checking";
  fixState(f.card, "busy", "Checking…", "Running " + f.command + " again to see if it works now.");
  if (!paneOpen("term-pane")) { showPane("term-pane"); }
  api("GET", "/api/sessions/" + S.current + "/terminal?after=" + S.term.seq).then(function (r) {
    if (r.running) { fixState(f.card, "bad", "Couldn't check", "Something else is running in the terminal. Run " + f.command + " again yourself when it's done."); S.fix = null; return; }
    f.after = S.term.seq;
    f.started = Date.now();
    runTerm(f.command);
  }).catch(function () { S.fix = null; });
}
function fixChecked(l) {
  var f = S.fix;
  if (!f || f.phase !== "checking" || f.after == null || l.n <= f.after) return;
  if (l.text === "exit 0") {
    fixState(f.card, "good", "Fixed", fixReport(f) + "It runs without errors now.");
    S.fix = null;
  }   // a failure: its "fail" line follows, and fixCard takes it from there
}
function fixStillRunning() {
  var f = S.fix;
  if (!f || f.phase !== "checking" || !f.started || Date.now() - f.started < 10000) return;
  fixState(f.card, "good", "Looks fixed", fixReport(f) + "It has been running for 10 seconds with no error. Stop it when you're done.");
  S.fix = null;
}
/* The agent's "Fixed: ..." line, as a sentence to start the card with. */
function fixReport(f) {
  var s = (f.report || "").replace(/^Fixed:?\s*/i, "").replace(/`/g, "").trim();
  if (!s || /^Couldn/i.test(s)) return "";
  s = s.charAt(0).toUpperCase() + s.slice(1);
  return s + (/[.!?]$/.test(s) ? " " : ". ");
}

/* ---------------- the branch bar: git and GitHub, as Claude Code shows them above the input ----------------
   The branch (base <- branch), what it changes, then Commit, Push or Create PR; once a pull request is
   open, its number and checks. Messages are written by the model from the changes, for you to edit. */
S.git = null;
function refreshGit(fresh) {
  var bar = $("gitbar");
  if (!S.current) { bar.classList.add("hidden"); S.git = null; return Promise.resolve(); }
  var id = S.current;
  return api("GET", "/api/sessions/" + id + "/git" + (fresh ? "?fresh=1" : "")).then(function (r) {
    if (S.current !== id) return;
    S.git = r;
    renderGitbar();
  }).catch(function () {});
}
function renderGitbar() {
  var r = S.git, bar = $("gitbar");
  bar.innerHTML = "";
  bar.classList.toggle("hidden", !(r && r.repo && r.branch));
  if (!r || !r.repo || !r.branch) return;
  var left = bar.appendChild(h("span", "gb-branch"));
  left.innerHTML = svg("branch");
  left.appendChild(h("span", "", r.base && r.branch !== r.base ? r.base + " ← " + r.branch : r.branch));
  left.title = (r.upstream ? "Tracks " + r.upstream : "Not pushed yet") + (r.ahead ? " · " + r.ahead + " to push" : "") +
    (r.behind ? " · " + r.behind + " behind" : "");
  bar.appendChild(h("span", "sp"));
  var add = (r.added || 0) + (r.branch_added || 0), del = (r.removed || 0) + (r.branch_removed || 0);
  if (add || del) {
    var n = bar.appendChild(h("span", "gb-num"));
    n.appendChild(h("span", "num-add", "+" + add)); n.appendChild(h("span", "num-del", "-" + del));
    n.title = (r.files ? r.files + (r.files === 1 ? " file" : " files") + " not committed" : "Everything is committed") +
      (r.branch_added || r.branch_removed ? " · the branch adds +" + r.branch_added + " -" + r.branch_removed + " to " + r.base : "");
  }
  if (r.pr) {
    var c = r.pr.checks || {}, pr = bar.appendChild(h("button", "gb-pr"));
    pr.type = "button";
    var said = c.failed ? "✕ " + c.failed + " failing" : c.pending ? "● " + c.pending + " running"
      : c.passed ? "✓ Checks passed" : (r.pr.draft ? "Draft" : String(r.pr.state || "").toLowerCase());
    pr.appendChild(h("b", "", "#" + r.pr.number));
    pr.appendChild(h("span", c.failed ? "bad" : c.pending ? "run" : "ok", said));
    if (r.autofix) pr.appendChild(h("i", "gb-auto", "Auto-fix"));
    pr.title = r.pr.title || "";
    pr.onclick = function (ev) { ev.stopPropagation(); var b = pr.getBoundingClientRect(); openPrMenu(b.left, b.top); };
  }
  if (r.files) bar.appendChild(gitButton("Commit", openCommitDialog));
  if ((r.pr || !r.github) && r.remote && r.ahead > 0 && !r.files) {
    bar.appendChild(gitButton("Push", function (b) {
      b.disabled = true; b.textContent = "Pushing…";
      api("POST", "/api/sessions/" + S.current + "/git/push", {}).then(function () { toast("Pushed"); })
        .catch(toastError).then(function () { refreshGit(true); });
    }));
  }
  if (!r.pr && r.github) {
    var create = bar.appendChild(gitButton("Create PR", function () { openPrDialog(); }));
    if (!r.gh) { create.disabled = true; create.title = "Install the GitHub CLI (gh) and sign in with: gh auth login"; }
  }
}
/* The pull request's menu, as Claude Code's: open it, let the agent fix failing checks and review
   comments (Auto-fix), or have GitHub merge it once it may (Merge when ready). */
function openPrMenu(x, y) {
  var r = S.git || {}, m = $("row-menu"), id = S.current;
  if (!r.pr) return;
  m.innerHTML = "";
  m.appendChild(h("div", "mh")).appendChild(h("span", "", "#" + r.pr.number + " " + (r.pr.title || "")));
  m.appendChild(menuRow("chev", "Open on GitHub", function () { window.open(r.pr.url, "_blank"); }));
  m.appendChild(menuItem("Auto-fix", "The agent fixes failing checks and answers review comments, then pushes", !!r.autofix, function () {
    api("POST", "/api/sessions/" + id + "/git/autofix", { on: !r.autofix }).then(function (a) {
      toast(a.autofix ? "Auto-fix is on for #" + r.pr.number : "Auto-fix is off");
    }).catch(toastError).then(function () { refreshGit(false); });
  }));
  m.appendChild(menuItem("Merge when ready", "GitHub merges it (squashed) once checks pass and reviews approve", false, function () {
    api("POST", "/api/sessions/" + id + "/git/merge", {}).then(function () { toast("#" + r.pr.number + " merges when ready"); })
      .catch(toastError).then(function () { refreshGit(true); });
  }));
  m.appendChild(h("div", "sep"));
  m.appendChild(menuRow("retry", "Check again", function () { refreshGit(true); }));
  closeMenus();
  m.classList.add("open");
  var w = m.offsetWidth, ht = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + "px";
  m.style.top = Math.max(8, y - ht - 6) + "px";
}

function gitButton(label, fn) {
  var b = h("button", "btn gb-btn", label);
  b.type = "button";
  b.onclick = function () { fn(b); };
  return b;
}

/* One dialog for both: Commit (a message), or Create PR (a branch when on the base, a title, a
   description, draft or not). */
function openGitDialog(kind) {
  var r = S.git || {}, pr = kind === "pr";
  S.gitKind = kind;
  $("git-title").textContent = pr ? "Create pull request" : "Commit";
  $("git-hint").className = "sub";
  $("git-hint").textContent = pr
    ? (r.files ? "Your uncommitted changes are committed first, then the branch is pushed and the pull request opened into " + (r.base || "the default branch") + "."
       : "The branch is pushed and the pull request opened into " + (r.base || "the default branch") + ".")
    : r.files + (r.files === 1 ? " file" : " files") + " on " + r.branch + ". Everything is added and committed.";
  var needBranch = pr && (!r.base || r.branch === r.base);
  $("git-branch-row").classList.toggle("hidden", !needBranch);
  $("git-draft-row").classList.toggle("hidden", !pr);
  $("git-body-label").textContent = pr ? "Description" : "Details (optional)";
  $("git-go").textContent = pr ? "Create PR" : "Commit";
  ["git-branch", "git-subject", "git-body"].forEach(function (f) { $(f).value = ""; $(f).dataset.touched = ""; });
  $("git-subject").placeholder = "Writing a " + (pr ? "title" : "message") + " from your changes…";
  $("git-draft").checked = false;
  $("git-go").disabled = false;
  $("git-modal").classList.add("open");
  $("git-subject").focus();
  api("POST", "/api/sessions/" + S.current + "/git/message", { kind: kind }).then(function (m) {
    if (S.gitKind !== kind || !$("git-modal").classList.contains("open")) return;
    if (!$("git-subject").dataset.touched) $("git-subject").value = m.title || "";
    if (!$("git-body").dataset.touched) $("git-body").value = m.body || "";
    if (needBranch && !$("git-branch").dataset.touched) $("git-branch").value = m.branch || "";
  }).catch(function () {}).then(function () { $("git-subject").placeholder = pr ? "Title" : "Summary"; });
}
function openCommitDialog() { openGitDialog("commit"); }
function openPrDialog() { openGitDialog("pr"); }
function submitGit() {
  var pr = S.gitKind === "pr", subject = $("git-subject").value.trim(), body = $("git-body").value.trim();
  if (!subject) { $("git-subject").focus(); return; }
  var go = $("git-go");
  go.disabled = true; go.textContent = pr ? "Opening…" : "Committing…";
  var call = pr
    ? api("POST", "/api/sessions/" + S.current + "/git/pr", { title: subject, body: body, draft: $("git-draft").checked,
        branch: $("git-branch").value.trim(), message: subject + (body ? "\n\n" + body : "") })
    : api("POST", "/api/sessions/" + S.current + "/git/commit", { message: subject + (body ? "\n\n" + body : "") });
  call.then(function (r) {
    closeDialog("git-modal");
    toast(pr ? "Pull request opened" : "Committed " + r.commit);
    if (pr && r.url) window.open(r.url, "_blank");
  }).catch(function (err) {
    $("git-hint").className = "sub bad";
    $("git-hint").textContent = err.message || "That did not work";
    go.disabled = false; go.textContent = pr ? "Create PR" : "Commit";
  }).then(function () { refreshGit(true); });
}

/* A task the agent suggests (Claude Code's card): Start runs it in a new session in this folder,
   Do it here sends it to this one. */
function addTaskCard(t) {
  var turn = S.turn, bottom = isAtBottom();
  var card = h("div", "taskcard");
  var hd = card.appendChild(h("div", "tkhd"));
  hd.innerHTML = svg("list");
  hd.appendChild(h("span", "", "Suggested task"));
  card.appendChild(h("b", "", t.title));
  var p = card.appendChild(h("p", "", t.prompt));
  p.title = t.prompt;
  var row = card.appendChild(h("div", "btnrow"));
  var start = row.appendChild(h("button", "btn primary", "Start"));
  var here = row.appendChild(h("button", "btn", "Do it here"));
  start.onclick = function () {
    start.disabled = here.disabled = true; start.textContent = "Starting\u2026";
    var body = currentFolder() ? { project_dir: currentFolder() } : { no_project: true };
    api("POST", "/api/sessions", body).then(function (snap) {
      return api("POST", "/api/sessions/" + snap.id + "/message", { text: t.prompt, attachments: [], client_id: "" }).then(function () {
        card.classList.add("started");
        start.textContent = "Started";
        toast("Started \u201c" + t.title + "\u201d in a new session");
        return refreshSessions();
      });
    }).catch(function (err) { start.disabled = here.disabled = false; start.textContent = "Start"; toastError(err); });
  };
  here.onclick = function () { $("input").value = t.prompt; autosize(); sendMessage(); card.classList.add("started"); };
  (turn ? turn.el : $("chat")).appendChild(card);
  follow(bottom);
}

/* ---------------- suggested next prompt (grey text in the empty box) ---------------- */

var PLACEHOLDER = "Ask CodeMonkey to build or fix something   ·   / for commands";
function setSuggestion(text) {
  S.suggestion = text || "";
  var input = $("input");
  input.placeholder = S.suggestion ? S.suggestion + "   ⇥ Tab" : PLACEHOLDER;
  input.classList.toggle("suggesting", !!S.suggestion);
}
function fetchSuggestion() {
  var ui = (S.settings && S.settings.ui) || {};
  if (ui.prompt_suggestions === false || !S.current) return;
  var id = S.current;
  api("POST", "/api/sessions/" + id + "/suggest", {}).then(function (r) {
    // only if nothing moved on meanwhile: same session, empty box, not working
    if (S.current === id && !$("input").value && !S.workStart) setSuggestion(r.text);
  }).catch(function () {});
}
