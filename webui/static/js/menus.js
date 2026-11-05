"use strict";
/* CodeMonkey page, menus.js: Menus, shared controls, the folder chooser, Ctrl K.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- menus: folder, model, permissions, context ---------------- */

function closeMenus() {
  Array.prototype.forEach.call(document.querySelectorAll(".menu.open"), function (m) {
    if (m.id !== "palette") m.classList.remove("open");
  });
}
var MENU_BUILDERS = {};
function openMenu(id) {
  closeMenus();
  if (MENU_BUILDERS[id]) MENU_BUILDERS[id]($(id));
  $(id).classList.add("open");
}

/* Settings of the open session, or what a new one will start with. */
function effective() {
  if (S.snap) return S.snap.settings;
  var st = S.settings || {};
  var base = {
    provider: st.default_provider, model: st.default_model, effort: st.effort || "auto",
    permission_mode: st.permission_mode || "ask", context_budget: st.context_budget || 128000
  };
  Object.keys(S.pendingSettings || {}).forEach(function (k) { base[k] = S.pendingSettings[k]; });
  return base;
}
function sessionSetting(patch) {
  if (S.current) {
    return api("POST", "/api/sessions/" + S.current + "/settings", patch).then(function (r) {
      if (S.snap) S.snap.settings = r.settings;
      renderPickers(); updateCtx();
    }).catch(toastError);
  }
  S.pendingSettings = S.pendingSettings || {};
  Object.keys(patch).forEach(function (k) { S.pendingSettings[k] = patch[k]; });
  renderPickers(); updateCtx();
  return Promise.resolve();
}

/* The title bar: "folder / session", as Claude Code shows it. */
function setSessionTitle(title) {
  var el = $("session-title"), folder = folderName(currentFolder() || "");
  el.innerHTML = "";
  $("session-menu-btn").classList.toggle("hidden", !S.current);
  el.classList.toggle("renamable", !!S.current);
  el.title = S.current ? "Click to rename" : "";
  el.onclick = S.current ? function () { renameSession(sessionEntry(S.current) || { id: S.current, title: title }); } : null;
  if (!title && !folder) return;
  // as Claude Code: the session's title, then its folder as a small tag
  el.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M2.5 19h19"/></svg>');
  el.appendChild(h("span", "ttl", title || folder));
  if (title && folder) el.appendChild(h("span", "tag", folder));
}

/* The model button: the chosen model, or "No models" until one is chosen (a new install). */
function modelLabel(eff) {
  return eff.model && eff.model !== "default" ? eff.model : "No models";
}
function needsKey(p) { return (p.needs_key || p.custom) && !(p.key && p.key.source); }

/* "provider/model" for every model that is running now, plus any in *keep* already chosen. */
function liveRefs(keep) {
  var out = [];
  (S.available || []).forEach(function (g) { g.models.forEach(function (m) { out.push(g.provider + "/" + m); }); });
  (keep || []).forEach(function (r) { if (r && out.indexOf(r) < 0) out.push(r); });
  return out;
}

/* Models to choose from: the live list (what the servers have now), plus the one in *keep* when it
   is not reachable, so a choice already made is shown rather than silently replaced. */
function liveChoices(keep) {
  var out = [];
  (S.available || []).forEach(function (g) {
    g.models.forEach(function (m) { out.push({ provider: g.provider, model: String(m), live: true }); });
  });
  if (keep && keep.model && !out.some(function (c) { return c.provider === keep.provider && c.model === keep.model; })) {
    out.unshift({ provider: keep.provider, model: keep.model, live: false });
  }
  return out;
}

function renderPickers() {
  var eff = effective();
  var folder = currentFolder();
  $("folder-label").textContent = folderName(folder);
  $("folder-btn").title = folder ? "Working in " + folder : "No folder: the agent only chats";
  var efforts = (S.config && S.config.efforts) || {}, effLabel = (efforts[eff.effort] || {}).label;
  $("model-label").textContent = modelLabel(eff);
  $("model-label").appendChild(h("span", "eff", effLabel || "Auto"));
  if (S.snap) setSessionTitle(S.snap.title === "New session" ? "" : S.snap.title);
  var pm = PERM_LABEL[eff.permission_mode] ? eff.permission_mode : "ask";
  $("perm-label").textContent = PERM_PLAIN[pm];
  $("perm-btn").title = PERM_LABEL[pm][0] + ": " + PERM_LABEL[pm][1] + " (Shift Tab to switch, Ctrl Shift M for the menu)";
  $("perm-btn").setAttribute("data-mode", eff.permission_mode || "ask");
  // where the work happens: shown above the input until the first message, as Claude Code does
  $("envbar").classList.toggle("hidden", !!(S.snap && S.snap.id && $("chat").children.length));
  $("env-branch").textContent = (S.snap && S.snap.git_branch) || "";
  $("env-branch").parentNode.classList.toggle("hidden", !(S.snap && S.snap.git_branch));
}

function menuItem(title, sub, on, fn) {
  var b = h("button", "mi" + (on ? " on" : ""));
  b.type = "button";
  b.setAttribute("role", "menuitem");
  var span = h("span");
  span.appendChild(h("b", "", title));
  if (sub) span.appendChild(h("i", "", sub));
  b.appendChild(span);
  if (on) b.insertAdjacentHTML("beforeend", svg("check", "tick"));
  b.onclick = function () { closeMenus(); fn(); };
  return b;
}
function menuRow(icon, title, fn) {
  var b = h("button", "mi row1");
  b.type = "button";
  b.innerHTML = svg(icon) + "<span><b></b></span>";
  b.querySelector("b").textContent = title;
  b.onclick = function () { closeMenus(); fn(); };
  return b;
}

function setFolder(path) {
  if (S.current) {
    var body = path ? { project_dir: path } : { no_project: true };
    api("POST", "/api/sessions/" + S.current + "/folder", body).then(function (snap) {
      applySnapshot(snap);
      refreshSessions();
    }).catch(function (err) { addNote("The folder was not changed: " + err.message + "."); });
  } else {
    S.pendingFolder = path || "";
    renderPickers();
    if (!$("hello").classList.contains("hidden")) renderHome();   // Get started ticks off the folder
  }
}

MENU_BUILDERS["folder-menu"] = function (m) {
  function build() {
    var cur = currentFolder();
    m.innerHTML = "";
    var mh = h("div", "mh");
    mh.appendChild(h("span", "", "Work in a folder"));
    if (S.snap && S.snap.git_branch) mh.appendChild(h("span", "", "branch " + S.snap.git_branch));
    m.appendChild(mh);
    var seen = {};
    var list = (cur ? [{ name: baseName(cur), path: cur }] : []).concat(S.folders);
    list.forEach(function (f) {
      if (seen[f.path]) return;
      seen[f.path] = 1;
      m.appendChild(menuItem(f.name, f.path, f.path === cur, function () { setFolder(f.path); }));
    });
    m.appendChild(h("div", "sep"));
    var wt = S.snap && S.snap.worktree;
    if (wt) {
      m.appendChild(menuRow("branch", "Merge " + wt.branch + " into " + wt.base + "…", function () { worktreeMerge(wt); }));
      m.appendChild(menuRow("x", "Discard this worktree…", function () { worktreeDiscard(wt); }));
    } else if (S.current && S.snap && S.snap.is_git_repo) {
      m.appendChild(menuRow("branch", "Work on a new branch (worktree)", function () {
        api("POST", "/api/sessions/" + S.current + "/worktree", {}).then(function (e) {
          addNote("Now on " + e.branch + " in its own folder. Merge or discard it from this menu or Settings > Worktrees.");
          return refreshSnap();
        }).catch(toastError);
      }));
    }
    m.appendChild(menuRow("folderPlus", "Choose a folder…", function () { openBrowse(cur); }));
    var none = menuRow("x", "No folder (just chat)", function () { setFolder(""); });
    if (!cur) none.insertAdjacentHTML("beforeend", svg("check", "tick"));
    m.appendChild(none);
  }
  build();
  api("GET", "/api/folders").then(function (f) { S.folders = f; if (m.classList.contains("open")) build(); }).catch(function () {});
};

/* A row that opens a menu beside its menu (Claude Code's "Effort  Medium  >"). */
function flyoutRow(m, label, value, build) {
  var b = h("button", "mi row1 flyrow");
  b.type = "button";
  b.innerHTML = "<span><b></b></span>";
  b.querySelector("b").textContent = label;
  if (value) b.appendChild(h("em", "", value));
  b.insertAdjacentHTML("beforeend", svg("chev", "flychev"));
  function open(ev) {
    if (ev) ev.stopPropagation();
    if (window.innerWidth <= 560) {   // a phone: the side menu opens in place, with a way back
      if (ev && ev.type === "mouseenter") return;
      var keep = Array.prototype.slice.call(m.childNodes);
      m.innerHTML = "";
      var back = h("button", "mi row1 backrow");
      back.type = "button";
      back.innerHTML = "<span><b>\u2039 " + esc(label) + "</b></span>";
      back.onclick = function (e2) { e2.stopPropagation(); m.innerHTML = ""; keep.forEach(function (n) { m.appendChild(n); }); };
      m.appendChild(back);
      build(m);
      return;
    }
    var f = $("flyout");
    if (!f) { f = h("div", "menu flyout"); f.id = "flyout"; f.setAttribute("role", "menu"); document.body.appendChild(f); }
    f.innerHTML = "";
    build(f);
    f.classList.add("open");
    var row = b.getBoundingClientRect(), menu = m.getBoundingClientRect(), w = f.offsetWidth, ht = f.offsetHeight;
    var left = menu.right + 6 + w <= window.innerWidth - 8 ? menu.right + 6 : menu.left - w - 6;
    f.style.left = Math.max(8, left) + "px";
    f.style.top = Math.max(8, Math.min(row.top - 6, window.innerHeight - ht - 8)) + "px";
    m.querySelectorAll(".flyrow").forEach(function (r) { r.classList.toggle("sel1", r === b); });
  }
  b.onclick = open;
  b.onmouseenter = open;
  b._open = open;
  m.appendChild(b);
  return b;
}

/* The model menu, as Claude Code's: the models (a tick on the one in use, 1-4 to pick), then Effort
   and More models beside it. Only models that are running are listed (each server is asked). */
MENU_BUILDERS["model-menu"] = function (m) {
  var eff = effective(), current = eff.model && eff.model !== "default" ? eff.model : "";
  var efforts = (S.config && S.config.efforts) || {};
  function draw(list, checking) {
    m.innerHTML = "";
    var rows = [];
    list.forEach(function (g) { g.models.forEach(function (model) { rows.push({ provider: g.provider, model: String(model) }); }); });
    // the provider in use first, as Claude lists its own models first
    rows.sort(function (x, y) { return (y.provider === eff.provider) - (x.provider === eff.provider); });
    var main = rows.slice(0, 4), more = rows.slice(4);
    var chosen = rows.some(function (r) { return r.provider === eff.provider && (r.model === eff.model || r.model === current); });
    if (!chosen && current && !checking) {
      var gone = menuItem(current, "Not reachable right now", true, function () {});
      gone.classList.add("unavailable");
      m.appendChild(gone);
    }
    main.forEach(function (r, i) {
      var on = r.provider === eff.provider && (r.model === eff.model || r.model === current);
      var b = menuItem(r.model, "", on, function () { sessionSetting({ provider: r.provider, model: r.model }); });
      b.appendChild(h("em", "", String(i + 1)));
      m.appendChild(b);
    });
    if (checking && !rows.length) m.appendChild(h("div", "mh")).appendChild(h("span", "", "Checking which models are running\u2026"));
    if (!checking && !rows.length) {
      m.appendChild(h("div", "mh")).appendChild(h("span", "", "No model is running yet."));
      m.appendChild(menuRow("plus", "Set up a model", function () { openModelSetup(); }));
    }
    m.appendChild(h("div", "sep"));
    m._effort = flyoutRow(m, "Effort", (efforts[eff.effort] || {}).label || "Auto", function (f) {
      Object.keys(efforts).forEach(function (k, i) {
        var b = menuItem(efforts[k].label, "", (eff.effort || "auto") === k, function () { sessionSetting({ effort: k }); });
        b.title = EFFORT_WORDS[k] || "";
        b.appendChild(h("em", "", String(i + 1)));
        f.appendChild(b);
      });
    });
    flyoutRow(m, "More models", "", function (f) {
      var last = null;
      more.forEach(function (r) {
        if (r.provider !== last) { f.appendChild(h("div", "mh")).appendChild(h("span", "", r.provider)); last = r.provider; }
        f.appendChild(menuItem(r.model, "", false, function () { sessionSetting({ provider: r.provider, model: r.model }); }));
      });
      if (!more.length) f.appendChild(h("div", "mh")).appendChild(h("span", "", "Every running model is listed already."));
      f.appendChild(h("div", "sep"));
      f.appendChild(menuRow("sliders", "Manage models", function () { openSettings("models"); }));
    });
  }
  draw(S.available || [], !S.available);
  api("GET", "/api/models/available").then(function (list) {
    S.available = list;
    if (m.classList.contains("open") && !m.querySelector(".backrow")) draw(list, false);   // not over an open side menu
  }).catch(function () { if (m.classList.contains("open") && !m.querySelector(".backrow")) draw([], false); });
};

/* The session menu from the caret beside the title, as in Claude Code: rename, how much of the
   transcript to show, and what else can be done with this session. */
MENU_BUILDERS["session-menu"] = function (m) {
  m.innerHTML = "";
  if (!S.current) return;
  var entry = sessionEntry(S.current) || { id: S.current, title: S.snap && S.snap.title };
  m.appendChild(menuRow("pencil", "Rename", function () { renameSession(entry); }));
  m.appendChild(h("div", "sep"));
  var tv = m.appendChild(h("div", "mh"));
  tv.appendChild(h("span", "", "Transcript view"));
  tv.appendChild(h("span", "", "Ctrl O"));
  [["normal", "Normal", "Tool calls folded into one line"], ["verbose", "Verbose", "Every tool call and step"]].forEach(function (v) {
    m.appendChild(menuItem(v[1], v[2], S.view === v[0], function () { if (S.view !== v[0]) cycleView(); }));
  });
  m.appendChild(h("div", "sep"));
  m.appendChild(h("div", "mh")).appendChild(h("span", "", "Reply style"));
  var styleBox = m.appendChild(h("div"));
  var style = effective().output_style || (S.settings && S.settings.output_style) || "default";
  api("GET", "/api/styles?project_dir=" + encodeURIComponent(currentFolder() || "")).then(function (list) {
    list.forEach(function (st) {
      styleBox.appendChild(menuItem(st.name.charAt(0).toUpperCase() + st.name.slice(1), st.description, st.name === style,
        function () { sessionSetting({ output_style: st.name }); }));
    });
  }).catch(function () {});
  m.appendChild(h("div", "sep"));
  m.appendChild(menuRow("chat", "Side chat", function () { openSide(); }));
  m.appendChild(menuRow("undo", "Rewind", openRewind));
  m.appendChild(menuRow("copy", "Fork", function () { forkFrom({}); }));
  m.appendChild(menuRow("file", "Export as Markdown", exportChat));
  m.appendChild(menuRow("folder", "Show in Explorer", function () {
    api("POST", "/api/sessions/" + S.current + "/reveal", {}).catch(toastError);
  }));
};

/* The panes beside the conversation, as Claude Code's layout menu lists them: an icon, a tick when
   open, and the shortcut. */
MENU_BUILDERS["pane-menu"] = function (m) {
  m.innerHTML = "";
  [["Changes", '<path d="M12 4v6M9 7h6M9 17h6"/><rect x="4" y="3" width="16" height="18" rx="2.5"/>',
    !$("review").classList.contains("hidden"), "Ctrl Shift D", toggleChanges],
   ["Activity", '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="5" cy="6" r="1"/><circle cx="5" cy="12" r="1"/><circle cx="5" cy="18" r="1"/>',
    !$("inspector").classList.contains("hidden"), "Ctrl .", function () { showActivity($("inspector").classList.contains("hidden")); }],
   ["Tasks", '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    paneOpen("tasks-pane"), "", toggleTasks],
   ["Plan", '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2"/>', paneOpen("plan-pane"), "", togglePlan],
   ["Files", '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>', paneOpen("file-pane"), "", toggleFiles],
   ["Terminal", '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 10l3 2-3 2M12 15h5"/>', paneOpen("term-pane"), "Ctrl `", toggleTerm],
   ["Browser", '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 2.5 14.4 0 17M12 3.5c-2.5 2.6-2.5 14.4 0 17"/>', paneOpen("browser-pane"), "", function () { toggleBrowser(); }],
   ["Side chat", '<path d="M4 5h16v11H9l-5 4z"/>', !$("side").classList.contains("hidden"), "Ctrl ;",
    function () { if ($("side").classList.contains("hidden")) openSide(); else closeSide(); }]
  ].forEach(function (p) {
    var b = h("button", "mi row1 pane");
    b.type = "button";
    b.setAttribute("role", "menuitemcheckbox");
    b.setAttribute("aria-checked", p[2] ? "true" : "false");
    b.innerHTML = '<svg viewBox="0 0 24 24">' + p[1] + "</svg><span><b></b></span>";
    b.querySelector("b").textContent = p[0];
    if (p[2]) b.insertAdjacentHTML("beforeend", svg("check", "tick"));
    b.appendChild(h("em", "", p[3]));
    b.onclick = function () { closeMenus(); p[4](); };
    m.appendChild(b);
  });
};

/* The mode menu, as Claude Code's: Mode, then the five modes with their keys (1-5 pick one). */
MENU_BUILDERS["perm-menu"] = function (m) {
  var eff = effective();
  m.innerHTML = "";
  m.appendChild(h("div", "mh")).appendChild(h("span", "", "Mode"));
  MODE_ORDER.forEach(function (mode, i) {
    var b = menuItem(PERM_LABEL[mode][0], PERM_LABEL[mode][1], eff.permission_mode === mode,
      function () { sessionSetting({ permission_mode: mode }); });
    if (mode === "auto") b.querySelector("b").appendChild(h("span", "badge", "Recommended"));
    b.appendChild(h("em", "", String(i + 1)));
    m.appendChild(b);
  });
};

function contextParts() {
  var eff = effective(), budget = +eff.context_budget || 128000;
  var b = (S.snap && S.snap.context_breakdown) || { yours: 0, attached: 0, replies: 0, fixed: 0 };
  var parts = [
    ["Your messages", b.yours / 4, "var(--info)"],
    ["Attached files", b.attached / 4, "var(--warn)"],
    ["Replies", b.replies / 4, "var(--accent)"],
    ["Instructions, tools and memory", b.fixed / 4, "var(--ok)"]
  ];
  var used = parts.reduce(function (n, p) { return n + p[1]; }, 0);
  return { parts: parts, used: used, budget: budget, pct: Math.min(100, used / budget * 100) };
}

function updateCtx() {
  var c = contextParts(), el = $("ctx-meter");
  // a small ring, as in Claude Code; the number is in its tooltip and its menu
  var r = 6.5, len = 2 * Math.PI * r, used = Math.max(0.02, Math.min(1, c.pct / 100));
  // drawn once, then only the arc's length changes, so it fills smoothly as the context grows
  if (!el.querySelector(".used")) {
    el.innerHTML = '<svg viewBox="0 0 18 18" class="ring"><circle cx="9" cy="9" r="' + r + '" class="track"/>' +
      '<circle cx="9" cy="9" r="' + r + '" class="used" stroke-dasharray="0 ' + len.toFixed(1) + '" transform="rotate(-90 9 9)"/></svg>';
    el.querySelector(".used").getBoundingClientRect();   // the first value animates in from empty
  }
  el.querySelector(".used").style.strokeDasharray = (len * used).toFixed(2) + " " + len.toFixed(2);
  var pct = el.querySelector(".pct") || el.appendChild(h("span", "pct"));   // the number beside the ring, so it isn't a mystery
  pct.textContent = Math.round(c.pct) + "%";
  el.title = Math.round(c.pct) + "% of the conversation's memory used (the context). Click for what fills it";
  el.setAttribute("aria-label", el.title);
  el.classList.toggle("high", c.pct >= 80 && c.pct < 95);
  el.classList.toggle("full", c.pct >= 95);
  renderStatusline();
  renderDetails();
}

/* The session details cards at the top of Activity, as Claude Code's session panel shows them. */
function renderDetails() {
  if (!$("dcard-env")) return;
  var snap = S.snap || {}, folder = baseName(currentFolder() || "") || "No folder";
  $("dcard-env").textContent = "Local · " + folder + (snap.git_branch ? " · " + snap.git_branch : "");
  var turns = Object.keys(S.changes || {}), add = 0, del = 0;
  turns.forEach(function (t) { S.changes[t].files.forEach(function (f) { add += f.added; del += f.removed; }); });
  var n = $("dcard-chg-n");
  n.innerHTML = "";
  if (turns.length) {
    n.appendChild(h("span", "num-add", "+" + add)); n.appendChild(document.createTextNode(" "));
    n.appendChild(h("span", "num-del", "-" + del));
  } else n.textContent = "None yet";
  $("dcard-ctx-pct").textContent = Math.round(contextParts().pct) + "%";
  if (!$("dcard-ctx-body").classList.contains("hidden")) loadContext($("dcard-ctx-body"));
}

/* The status line: the fields chosen in Settings > General, and what your own command prints. */
var STATUS_FIELDS = [["model", "Model"], ["branch", "Git branch"], ["folder", "Folder"], ["context", "Context used"],
  ["tokens", "Tokens in and out"], ["time", "Time in this session"], ["mode", "Permission mode"], ["style", "Reply style"]];
function statusFields() {
  var ui = (S.settings && S.settings.ui) || {};
  return Array.isArray(ui.statusline) ? ui.statusline : [];   // off until chosen, as in Claude Code's desktop app
}
function renderStatusline() {
  var bar = $("statusline");
  if (!bar) return;
  var on = statusFields(), eff = effective(), snap = S.snap || {}, parts = [];
  function add(label, value) { if (value) parts.push([label, value]); }
  on.forEach(function (f) {
    if (f === "model") add("model", modelLabel(eff));
    else if (f === "branch") add("branch", snap.worktree ? snap.worktree.branch + " (worktree)" : snap.git_branch);
    else if (f === "folder") add("in", baseName(currentFolder() || "") || "no folder");
    else if (f === "context") add("context", Math.round(contextParts().pct) + "%");
    else if (f === "tokens" && snap.id && (snap.tokens_in || snap.tokens_out)) add("tokens", fmtNum(snap.tokens_in || 0) + " in · " + fmtNum(snap.tokens_out || 0) + " out");
    else if (f === "time" && snap.elapsed_s != null) add("session", fmtDuration(snap.elapsed_s));
    else if (f === "mode") add("mode", (PERM_LABEL[eff.permission_mode] || PERM_LABEL.ask)[0]);
    else if (f === "style") add("style", eff.output_style || (S.settings && S.settings.output_style) || "default");
  });
  bar.innerHTML = "";
  parts.forEach(function (p) {
    var span = h("span");
    span.appendChild(document.createTextNode(p[0] + " "));
    span.appendChild(h("b", "", p[1]));
    bar.appendChild(span);
  });
  if (S.statusOwn) bar.appendChild(h("span", "own", S.statusOwn));
  bar.classList.toggle("hidden", !parts.length && !S.statusOwn);
}
function pollStatusCommand() {
  var secs = Math.max(2, ((S.settings && S.settings.ui && S.settings.ui.statusline_seconds) || 10));
  setTimeout(pollStatusCommand, secs * 1000);
  if (!S.current || document.hidden || !(S.settings && S.settings.statusline_command)) {
    if (S.statusOwn) { S.statusOwn = ""; renderStatusline(); }
    return;
  }
  api("GET", "/api/sessions/" + S.current + "/statusline").then(function (r) {
    S.statusOwn = r.text || ""; renderStatusline();
  }).catch(function () {});
}

MENU_BUILDERS["ctx-menu"] = function (m) { loadContext(m, { title: true }); };

/* ---------------- shared controls ---------------- */

function segControl(options, current, onPick, cls) {
  var seg = h("div", "seg" + (cls ? " " + cls : ""));
  options.forEach(function (o) {
    var b = h("button", String(o[0]) === String(current) ? "on" : "", o[1]);
    b.type = "button";
    b.onclick = function (ev) {
      ev.stopPropagation();
      Array.prototype.forEach.call(seg.children, function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      onPick(o[0]);
    };
    seg.appendChild(b);
  });
  return seg;
}
function toggleControl(on, onChange) {
  var t = h("button", "tog" + (on ? " on" : ""));
  t.type = "button";
  t.setAttribute("role", "switch");
  t.setAttribute("aria-checked", on ? "true" : "false");
  t.onclick = function () {
    var now = !t.classList.contains("on");
    t.classList.toggle("on", now);
    t.setAttribute("aria-checked", now ? "true" : "false");
    onChange(now);
  };
  return t;
}
function selectControl(options, current, onChange) {
  var s = h("select", "sel");
  options.forEach(function (o) {
    var opt = h("option", "", o[1]);
    opt.value = o[0];
    if (String(o[0]) === String(current)) opt.selected = true;
    s.appendChild(opt);
  });
  s.onchange = function () { onChange(s.value); };
  return s;
}
function row(cls, title, sub) {
  var r = h("div", cls), d = h("div");
  if (title instanceof Node) d.appendChild(title); else d.appendChild(h("b", "", title));
  if (sub) d.appendChild(h("i", "", sub));
  r.appendChild(d);
  for (var i = 3; i < arguments.length; i++) if (arguments[i]) r.appendChild(arguments[i]);
  return r;
}
function usageRow(title, sub, pct) {
  var r = h("div", "urow"), d = h("div");
  d.appendChild(h("b", "", title));
  d.appendChild(h("i", "", sub));
  var track = h("span", "track"), u = h("u");
  if (pct == null) pct = 0;
  u.style.width = Math.max(0, Math.min(100, pct)) + "%";
  if (pct >= 95) u.className = "full"; else if (pct >= 80) u.className = "high";
  track.appendChild(u);
  var em = h("em", "", Math.round(pct) + "%");
  r.appendChild(d); r.appendChild(track); r.appendChild(em);
  // update the same row instead of rebuilding it, so the bar glides and nothing flickers
  r._set = function (newSub, newPct) {
    newPct = Math.max(0, Math.min(100, newPct || 0));
    d.lastChild.textContent = newSub;
    u.style.width = newPct + "%";
    u.className = newPct >= 95 ? "full" : newPct >= 80 ? "high" : "";
    em.textContent = Math.round(newPct) + "%";
  };
  return r;
}

/* Live meters for this computer. Rows are made once and then only updated. */
function liveMeters(box, onData) {
  var rows = {};
  function meter(key, title, sub, pct) {
    if (!rows[key]) { rows[key] = usageRow(title, sub, pct); box.appendChild(rows[key]); }
    else rows[key]._set(sub, pct);
  }
  function draw() {
    api("GET", "/api/hardware").then(function (hw) {
      if (hw.gpu) meter("gpu", "Graphics memory", hw.gpu.used_gb + " of " + hw.gpu.total_gb + " GB · " + hw.gpu.name, hw.gpu.used_gb / hw.gpu.total_gb * 100);
      else if (!rows.gpu) { rows.gpu = row("srow", "Graphics memory", "No NVIDIA card was found, so this cannot be read"); box.appendChild(rows.gpu); }
      if (hw.memory.total_gb) {
        var used = hw.memory.total_gb - hw.memory.free_gb;
        meter("mem", "Memory", used.toFixed(1) + " of " + hw.memory.total_gb + " GB · " + hw.memory.free_gb + " GB free", used / hw.memory.total_gb * 100);
      }
      meter("cpu", "Processor", hw.cpu.cores + " cores" + (hw.cpu.percent == null ? " · measuring" : " · " + Math.round(hw.cpu.percent) + "% busy"), hw.cpu.percent || 0);
      if (onData) onData(hw);
    }).catch(function () {});
  }
  var every = Math.max(500, ((S.settings.ui && S.settings.ui.meter_seconds) || 1) * 1000);
  draw();
  clearInterval(hwTimer);
  hwTimer = setInterval(draw, every);
}
function button(label, fn, cls) {
  var b = h("button", "btn" + (cls ? " " + cls : ""), label);
  b.type = "button";
  b.onclick = fn;
  return b;
}
function buttonRow() {
  var r = h("div", "btnrow");
  for (var i = 0; i < arguments.length; i++) if (arguments[i]) r.appendChild(arguments[i]);
  return r;
}
function codeTitle(text) {
  var b = h("b");
  b.appendChild(h("code", "", text));
  return b;
}

/* ---------------- folder chooser ---------------- */

/* Choosing the folder to work in. In the desktop app this is the system's own folder picker (what
   everyone knows); "Browse here instead" and a browser or phone get CodeMonkey's chooser: places on
   the left (recent folders, Desktop, Documents, Downloads, drives), the path as steps you can click,
   folders that look like projects marked, a filter, New folder, and a button that names the folder. */
var browse = { path: "", dirs: [] };
function openBrowse(start, inPage) {
  if (!inPage && window.desktop && window.desktop.pickFolder) {
    window.desktop.pickFolder(start || "").then(function (picked) {
      if (picked) { setFolder(picked); toast("Working in " + baseName(picked)); }
    }).catch(function () { openBrowse(start, true); });
    return;
  }
  $("folder-modal").classList.add("open");
  $("browse-filter").value = "";
  $("browse-native").classList.toggle("hidden", !(window.desktop && window.desktop.pickFolder));
  $("browse-native").textContent = /Win/i.test(navigator.platform || "") ? "Use the Windows folder picker" : "Use the system folder picker";
  browseTo(start || "");
}
function chooseFolder(path) {
  closeDialog("folder-modal");
  setFolder(path);
  toast("Working in " + baseName(path));
}
function browseCrumbs(path) {
  var box = $("browse-crumbs");
  box.innerHTML = "";
  var sep = path.indexOf("\\") >= 0 ? "\\" : "/";
  var parts = path.split(/[\\/]+/).filter(Boolean), acc = path.charAt(0) === "/" ? "/" : "";
  parts.forEach(function (p, i) {
    acc = i === 0 && /^[A-Za-z]:$/.test(p) ? p + sep : (acc && !/[\\/]$/.test(acc) ? acc + sep : acc) + p;
    var target = acc;
    if (i) box.appendChild(h("span", "csep", "›"));
    var b = box.appendChild(h("button", i === parts.length - 1 ? "on" : "", i === 0 && /^[A-Za-z]:$/.test(p) ? p + "\\" : p));
    b.type = "button";
    b.onclick = function () { browseTo(target); };
  });
  var edit = box.appendChild(h("button", "cedit", "Type a path"));
  edit.type = "button";
  edit.onclick = function () {
    box.classList.add("hidden");
    var f = $("browse-path");
    f.classList.remove("hidden");
    f.value = browse.path;
    f.focus(); f.select();
  };
  box.scrollLeft = box.scrollWidth;
}
function browseList() {
  var list = $("browse-list"), q = $("browse-filter").value.trim().toLowerCase();
  list.innerHTML = "";
  var shown = browse.dirs.filter(function (d) { return !q || d.name.toLowerCase().indexOf(q) >= 0; });
  shown.forEach(function (d) {
    var full = browse.path.replace(/[\\/]+$/, "") + (browse.path.indexOf("\\") >= 0 ? "\\" : "/") + d.name;
    var r = h("div", "brow");
    r.tabIndex = 0;
    r.setAttribute("role", "button");
    r.innerHTML = svg("folder");
    r.appendChild(h("span", "bname", d.name));
    if (d.project) r.appendChild(h("span", "tag", "project")).title = "Looks like a project: it has code or a git history";
    var pick = r.appendChild(h("button", "btn bpick", "Choose"));
    pick.type = "button";
    pick.title = "Work in " + d.name;
    pick.onclick = function (e) { e.stopPropagation(); chooseFolder(full); };
    r.insertAdjacentHTML("beforeend", svg("chev"));
    r.onclick = function () { browseTo(full); };
    r.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); browseTo(full); } };
    list.appendChild(r);
  });
  if (!shown.length) list.appendChild(h("div", "empty", q ? "No folder here matches." : "No folders inside this one. You can work here, or make a New folder."));
}
function browseTo(path) {
  api("GET", "/api/browse?path=" + encodeURIComponent(path || "")).then(function (d) {
    browse.path = d.path; browse.parent = d.parent; browse.dirs = d.dirs;
    $("browse-path").classList.add("hidden");
    $("browse-crumbs").classList.remove("hidden");
    browseCrumbs(d.path);
    $("browse-up").disabled = !d.parent;
    var places = $("browse-roots");
    places.innerHTML = "";
    function place(name, p, icon) {
      var b = places.appendChild(h("button", "bplace" + (p === d.path ? " on" : "")));
      b.type = "button";
      b.innerHTML = svg(icon);
      b.appendChild(h("span", "", name));
      b.title = p;
      b.onclick = function () { browseTo(p); };
    }
    if (d.recent && d.recent.length) {
      places.appendChild(h("div", "bhd", "Recent"));
      d.recent.slice(0, 4).forEach(function (f) { place(f.name, f.path, "clock"); });
    }
    places.appendChild(h("div", "bhd", "Places"));
    (d.places || []).forEach(function (f) { place(f.name, f.path, "folder"); });
    var name = baseName(d.path) || d.path;
    $("browse-choose").textContent = "Work in " + (name.length > 24 ? name.slice(0, 23) + "…" : name);
    $("browse-choose").title = d.path;
    $("browse-note").textContent = d.project ? "This folder looks like a project." : "";
    $("browse-filter").value = "";
    browseList();
    $("browse-list").scrollTop = 0;
  }).catch(function (err) {
    var list = $("browse-list");
    list.innerHTML = "";
    list.appendChild(h("div", "empty", err.message));
  });
}
function browseNewFolder() {   // a row at the top of the list to type the name in
  var list = $("browse-list"), old = list.querySelector(".bnew");
  if (old) { old.querySelector("input").focus(); return; }
  var r = h("div", "brow bnew");
  r.innerHTML = svg("folderPlus");
  var f = r.appendChild(h("input", "field"));
  f.placeholder = "Name of the new folder";
  f.spellcheck = false;
  var make = r.appendChild(h("button", "btn primary", "Make it"));
  make.type = "button";
  function go() {
    if (!f.value.trim()) { f.focus(); return; }
    api("POST", "/api/browse/new", { parent: browse.path, name: f.value }).then(function (res) { browseTo(res.path); })
      .catch(function (err) { f.setCustomValidity(err.message); f.reportValidity(); setTimeout(function () { f.setCustomValidity(""); }, 2500); });
  }
  make.onclick = go;
  f.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } if (e.key === "Escape") { e.stopPropagation(); r.remove(); } };
  list.insertBefore(r, list.firstChild);
  list.scrollTop = 0;
  f.focus();
}

/* ---------------- search everything (Ctrl K) ---------------- */

function fuzzyScore(label, q) {
  var l = label.toLowerCase(), query = q.toLowerCase();
  if (!query) return 0;
  if (l === query) return 100;
  if (l.indexOf(query) === 0) return 80;
  var words = l.split(/[^a-z0-9]+/);
  for (var i = 0; i < words.length; i++) if (words[i].indexOf(query) === 0) return 60;
  if (l.indexOf(query) >= 0) return 40;
  var qi = 0;
  for (var j = 0; j < l.length && qi < query.length; j++) if (l[j] === query[qi]) qi++;
  return qi === query.length ? 20 : -1;
}

function cmdkActions() {
  var acts = [
    ["plus", "New session", "Alt N", newSession],
    ["folderPlus", "Change folder…", "Ctrl Shift O", function () { openBrowse(currentFolder()); }],
    ["list", "Transcript: normal or verbose", "Ctrl O", cycleView],
    ["retry", "New checkpoint", "Ctrl S", checkpoint],
    ["list", "Show or hide Activity", "Ctrl .", function () { showActivity($("inspector").classList.contains("hidden")); }],
    ["sliders", "Settings", "Ctrl ,", function () { openSettings(); }],
    ["cube", "Open the kernel", "", openKernel],
    ["file", "Agents", "", function () { openSettings("agents"); }],
    ["branch", "Worktrees", "", function () { openSettings("worktrees"); }],
    ["branch", "Work on a new branch (worktree)", "", function () {
      if (!S.current) { addNote("Open or start a session in a git project first."); return; }
      api("POST", "/api/sessions/" + S.current + "/worktree", {}).then(function (e) {
        addNote("Now on " + e.branch + " in its own folder."); return refreshSnap();
      }).catch(toastError);
    }],
    ["star", "Plugins", "", function () { openSettings("plugins"); }],
    ["term", "MCP servers", "", function () { openSettings("mcp"); }],
    ["sliders", "Permission rules and sandbox", "", function () { openSettings("permissions"); }],
    ["image", "Remote control: use from your phone", "", openRemote],
    ["x", "Stop the agent", "Esc", cancelTurn]
  ];
  MODE_ORDER.forEach(function (mode) {
    acts.push(["term", "Permissions: " + PERM_LABEL[mode][0], MODE_CYCLE.indexOf(mode) >= 0 ? "Shift Tab" : "",
      function () { sessionSetting({ permission_mode: mode }); }]);
  });
  acts.push(["retry", "Rewind to an earlier message", "Esc Esc", openRewind]);
  acts.push(["file", "Memory", "", function () { openMemory(); }]);
  acts.push(["file", "Export this conversation", "", exportChat]);
  acts.push(["list", "Keyboard shortcuts", "?", openShortcuts]);
  acts.push(["term", "Hooks", "", function () { openSettings("hooks"); }]);
  acts.push(["list", "What the context holds", "", showContext]);
  acts.push(["warn", "Report a problem", "", function () { openReport(); }]);
  acts.push(["retry", "Check for updates", "", function () { openSettings("whatsnew"); }]);
  return acts.map(function (a) { return { group: "Actions", icon: a[0], label: a[1], hint: a[2], kbd: true, run: a[3] }; });
}

function cmdkItems() {
  var items = S.list.map(function (e) {
    return { group: "Sessions", icon: "chat", label: e.title || "New session", hint: folderName(e.project_dir),
      run: function () { openSession(e.id); } };
  }).concat(cmdkActions());
  BUILTIN_COMMANDS.concat(S.commands.map(function (c) { return [c.name, c.description]; })).forEach(function (c) {
    items.push({ group: "Commands", icon: "term", label: "/" + c[0], hint: c[1], run: function () {
      $("input").value = "/" + c[0] + " ";
      $("input").focus(); autosize();
    } });
  });
  (S.cmdk.messages || []).forEach(function (r) {
    items.push({ group: "Messages", icon: "chat", label: r.snippet, hint: r.title, exact: true,
      run: function () { S.findOnOpen = r.snippet.replace(/^\u2026|\u2026$/g, "").slice(0, 60); if (S.current === r.id) findInChat(); else openSession(r.id); } });
  });
  S.cmdk.files.forEach(function (f) {
    items.push({ group: "Files", icon: "file", label: f, hint: "mention in your message", run: function () {
      var input = $("input");
      input.value += (input.value && !/\s$/.test(input.value) ? " " : "") + "@" + f + " ";
      input.focus(); autosize();
    } });
  });
  return items;
}

function renderCmdk() {
  var q = $("cmdk-input").value.trim();
  var scored = cmdkItems().map(function (it, i) { return { it: it, s: it.exact ? 1 : fuzzyScore(it.label, q), i: i }; })
    .filter(function (x) { return !q || x.s >= 0; });
  var order = ["Sessions", "Messages", "Actions", "Commands", "Files"];
  scored.sort(function (a, b) {
    var g = order.indexOf(a.it.group) - order.indexOf(b.it.group);
    return g || (b.s - a.s) || (a.i - b.i);
  });
  var perGroup = {};
  S.cmdk.results = scored.map(function (x) { return x.it; }).filter(function (it) {
    perGroup[it.group] = (perGroup[it.group] || 0) + 1;
    return perGroup[it.group] <= (q ? 8 : 6);
  });
  S.cmdk.index = 0;
  var list = $("cmdk-list");
  list.innerHTML = "";
  if (!S.cmdk.results.length) { list.appendChild(h("div", "none", "Nothing matches.")); return; }
  var last = null;
  S.cmdk.results.forEach(function (it, idx) {
    if (it.group !== last) {
      last = it.group;
      list.appendChild(h("div", "mh")).appendChild(h("span", "", last));
    }
    var b = h("button", "mi row1" + (idx === 0 ? " sel1" : ""));
    b.type = "button";
    b.setAttribute("role", "option");
    b.innerHTML = svg(it.icon) + "<span><b></b></span>";
    b.querySelector("b").textContent = it.label;
    if (it.hint) b.appendChild(h(it.kbd ? "kbd" : "em", "", it.hint));
    b.onmousemove = function () { if (S.cmdk.index !== idx) { S.cmdk.index = idx; paintCmdk(); } };
    b.onclick = function () { runCmdk(idx); };
    list.appendChild(b);
  });
}
function paintCmdk() {
  var rows = $("cmdk-list").querySelectorAll(".mi");
  Array.prototype.forEach.call(rows, function (r, i) {
    r.classList.toggle("sel1", i === S.cmdk.index);
    if (i === S.cmdk.index && r.scrollIntoView) r.scrollIntoView({ block: "nearest" });
  });
}
function runCmdk(idx) {
  var it = S.cmdk.results[idx == null ? S.cmdk.index : idx];
  if (!it) return;
  closeDialog("cmdk-modal");
  it.run();
}
/* After a search, the message found: scrolled to and lit up for a moment. */
function findInChat(tries) {
  // the snippet is the stored text (with **, `, # ...) and may start mid-word: match a few whole words
  var words = (S.findOnOpen || "").toLowerCase().replace(/[*_`#>|\[\]()~]/g, " ").split(/\s+/).filter(Boolean);
  var want = words.slice(words.length > 4 ? 1 : 0, 6).join(" ");
  if (!want) return;
  var hit = null;
  Array.prototype.some.call(document.querySelectorAll("#chat .user, #chat .reply"), function (el) {
    if (el.textContent.toLowerCase().replace(/[*_`#>|\[\]()~]/g, " ").replace(/\s+/g, " ").indexOf(want) >= 0) { hit = el; return true; }
    return false;
  });
  if (!hit) {
    if ((tries || 0) < 20) setTimeout(function () { findInChat((tries || 0) + 1); }, 150);
    else S.findOnOpen = "";
    return;
  }
  S.findOnOpen = "";
  hit.scrollIntoView({ block: "center" });
  hit.classList.add("found");
  setTimeout(function () { hit.classList.remove("found"); }, 2200);
}

function openCmdk() {
  closeMenus();
  S.cmdk.files = []; S.cmdk.messages = [];
  $("cmdk-modal").classList.add("open");
  $("cmdk-input").value = "";
  renderCmdk();
  $("cmdk-input").focus();
}
var cmdkTimer = null;
function cmdkInput() {
  renderCmdk();
  var q = $("cmdk-input").value.trim();
  clearTimeout(cmdkTimer);
  if (q.length < 2 || q.charAt(0) === "/") { S.cmdk.files = []; S.cmdk.messages = []; return; }
  clearTimeout(S.cmdk.msgTimer);
  S.cmdk.msgTimer = setTimeout(function () {
    api("GET", "/api/search?q=" + encodeURIComponent(q)).then(function (rows) {
      if ($("cmdk-input").value.trim() !== q) return;
      S.cmdk.messages = rows.slice(0, 6);
      renderCmdk();
    }).catch(function () {});
  }, 220);
  if (!S.current) { S.cmdk.files = []; return; }
  cmdkTimer = setTimeout(function () {
    api("GET", "/api/sessions/" + S.current + "/files?prefix=" + encodeURIComponent(q)).then(function (files) {
      if ($("cmdk-input").value.trim() !== q) return;
      S.cmdk.files = files.slice(0, 8);
      renderCmdk();
    }).catch(function () {});
  }, 150);
}

function checkpoint() {
  if (!S.current) { addNote("Send a message first: a checkpoint belongs to a session."); return; }
  api("POST", "/api/sessions/" + S.current + "/checkpoint", {}).catch(toastError);
}
