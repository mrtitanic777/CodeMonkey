"use strict";
/* CodeMonkey page, composer.js: The message box.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- composer ---------------- */

function autosize() {
  var t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(220, Math.max(46, t.scrollHeight)) + "px";
}

function setWorktreeChip() {
  var b = $("env-worktree");
  b.classList.toggle("on", !!S.pendingWorktree);
  b.setAttribute("aria-pressed", S.pendingWorktree ? "true" : "false");
}

function ensureSession() {
  if (S.current) return Promise.resolve(S.current);
  var body = {};
  if (S.pendingFolder === "") body.no_project = true;
  else if (S.pendingFolder) body.project_dir = S.pendingFolder;
  return api("POST", "/api/sessions", body).then(function (snap) {
    S.current = snap.id;
    S.pendingFolder = null;
    S.list.unshift({ id: snap.id, title: snap.title, project_dir: snap.project_dir, last_active: snap.last_active, live: true });
    S.snap = snap; S.status[snap.id] = "idle";
    SYNC.seq = snap.event_seq || 0;
    renderSessions(); renderPickers();
    var pending = S.pendingSettings || {};
    S.pendingSettings = {};
    var settle = Object.keys(pending).length
      ? api("POST", "/api/sessions/" + snap.id + "/settings", pending).then(function (r) { snap.settings = r.settings; })
      : Promise.resolve();
    // the Worktree switch: a copy of the repository on a branch of its own, before the first message
    if (S.pendingWorktree) {
      var name = S.worktreeName || "";
      settle = settle.then(function () {
        return api("POST", "/api/sessions/" + snap.id + "/worktree", { name: name }).then(function (r) {
          toast("Working in a worktree on " + (r.branch || "a branch of its own"));
          return refreshSnapshot();
        }).catch(function (err) { toastError(new Error("Stayed in the folder itself: " + (err.message || err))); });
      });
    }
    S.pendingWorktree = false; setWorktreeChip();
    return settle.then(function () { return openEvents(snap.id, SYNC.seq); }).then(function () { return snap.id; });
  });
}

function sendMessage() {
  var input = $("input");
  var text = input.value;
  if (!text.trim() && !S.attachments.length) return;
  if (/^\/kernel\b/.test(text.trim())) { input.value = ""; autosize(); closePalette(); openKernel(); return; }
  if (/^\/remote(-control)?\b/.test(text.trim())) { input.value = ""; autosize(); closePalette(); openRemote(); return; }
  if (/^\/memory\s*$/.test(text.trim())) { input.value = ""; autosize(); closePalette(); openMemory(); return; }
  if (/^\/hooks\s*$/.test(text.trim())) { input.value = ""; autosize(); closePalette(); openSettings("hooks"); return; }
  if (/^\/export\s*$/.test(text.trim())) { input.value = ""; autosize(); closePalette(); exportChat(); return; }
  if (/^\/btw\b/.test(text.trim())) { input.value = ""; autosize(); closePalette(); openSide(text.trim().replace(/^\/btw\s*/, "")); return; }
  if (/^#\s+\S/.test(text) && !S.attachments.length) { askRemember(text.replace(/^#\s+/, "")); return; }
  // a new install with no model chosen and none running: say so, rather than send into an error
  if (modelLabel(effective()) === "No models" && Array.isArray(S.available) && !modelConnected()) { noModelCard(); return; }
  setSuggestion("");
  if (!S.current) S.worktreeName = text.trim().split("\n")[0].slice(0, 40);
  rememberSent(text);
  var atts = S.attachments.slice();
  S.attachments = []; renderAttachments();
  input.value = ""; autosize(); closePalette(); clearDraft();
  var shown = text + (atts.length ? "\n\n*" + atts.map(function (a) { return a.filename; }).join(", ") + " attached*" : "");
  var payload = { text: text, attachments: atts, client_id: "" };
  function deliver(id, triesLeft) {
    return api("POST", "/api/sessions/" + id + "/message", payload).catch(function (err) {
      // a network failure (not a refusal from the server): the same id makes a repeat harmless
      if (triesLeft > 0 && !/^HTTP |empty message|unknown session/.test(err.message || "")) {
        setLink(true);
        return new Promise(function (r) { setTimeout(r, 1200 * (4 - triesLeft)); })
          .then(function () { return deliver(id, triesLeft - 1); });
      }
      throw err;
    });
  }
  function post(first) {
    if (!first) addUserMsg(shown);
    payload.client_id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    S.sentIds[payload.client_id] = 1;
    return ensureSession().then(function (id) {
      return deliver(id, 3);
    }).then(function (r) {
      var qb = $("queue-bar");
      if (r.position > 1) {
        qb.textContent = r.position + " messages are waiting. They run in order.";
        qb.classList.remove("hidden");
      } else { qb.classList.add("hidden"); }
    }).catch(toastError);
  }
  // Shown at once, before the server answers.
  addUserMsg(shown);
  S.lastRetry = function () { post(false); };
  post(true);
}

function cancelTurn() {
  if (!S.current) return;
  api("POST", "/api/sessions/" + S.current + "/cancel", {}).catch(toastError);
}

/* typing / or @ opens the matching list above the composer */

var BUILTIN_COMMANDS = [
  ["plan", "Draft a plan and wait for approval before building"],
  ["build", "Carry out the plan in plan.md"],
  ["checkpoint", "Save a point you can rewind to"],
  ["compact", "Summarise older messages to free up context"],
  ["todo", "Show the to-do list"],
  ["show", "Show a tool output that was shortened"],
  ["mcp", "List connected tool servers"],
  ["agent", "Hand a task to a named agent: /agent reviewer check my last change"],
  ["agents", "List the agents you can hand work to"],
  ["init", "Write or improve this project's AGENTS.md from what it really does"],
  ["kernel", "Open the Logic lab"],
  ["remote-control", "Use CodeMonkey from your phone: QR code, pairing code, switch"],
  ["loop", "Repeat a check into this conversation: /loop 10m <what to check>; /loop stop ends it"],
  ["rewind", "Go back to before one of your messages, or fork from one"],
  ["review", "Review the uncommitted changes (or a branch: /review feature-x)"],
  ["security-review", "Look for security problems in the uncommitted changes"],
  ["btw", "Ask a side question that reads this session but adds nothing to it (Ctrl ;)"],
  ["export", "Save this conversation as a Markdown file"],
  ["context", "Show what the context holds"],
  ["memory", "Edit this project's memory (AGENTS.md) or yours"],
  ["hooks", "Commands that run before or after tools, and at other points"],
  ["help", "List what you can type"]
];

function currentWord() {
  var input = $("input");
  var v = input.value.slice(0, input.selectionStart);
  var mSlash = v.match(/^\/([\w-]*)$/);
  if (mSlash) return { kind: "slash", frag: mSlash[1].toLowerCase() };
  var mAt = v.match(/(^|\s)@([\w\-./]*)$/);
  if (mAt) return { kind: "at", frag: mAt[2] };
  return null;
}

function updatePalette() {
  if (S.historySearch) { showHistoryMatches(); return; }
  var w = currentWord();
  if (!w) { closePalette(); return; }
  if (w.kind === "slash") {
    // grouped by where each comes from: built in, yours, a plugin's, an MCP server's prompts
    var items = BUILTIN_COMMANDS.map(function (c) { return { kind: "slash", name: c[0], desc: c[1], group: "Built in" }; })
      .concat(S.commands.map(function (c) {
        return { kind: "slash", name: c.name, desc: c.description, group: c.name.indexOf(":") > 0 ? "From plugins" : "Your commands" };
      }))
      .concat(((S.mcp && S.mcp.prompts) || []).map(function (p) {
        return { kind: "slash", name: p.server + ":" + p.name, desc: p.description || "", group: "MCP prompts" };
      }))
      .filter(function (c) { return c.name.toLowerCase().indexOf(w.frag.toLowerCase()) === 0; });
    showPalette(items, "Commands");
  } else if (S.current) {
    api("GET", "/api/sessions/" + S.current + "/files?prefix=" + encodeURIComponent(w.frag)).then(function (files) {
      var again = currentWord();
      if (!again || again.kind !== "at" || again.frag !== w.frag) return;
      showPalette(files.slice(0, 10).map(function (f) { return { kind: "at", name: f }; }),
        "Files in " + folderName(currentFolder()));
    }).catch(closePalette);
  } else {
    showPalette([], "");
  }
}

function showPalette(items, title) {
  S.palette = { open: items.length > 0, items: items, index: 0, title: title };
  renderPalette();
}

function renderPalette() {
  var pal = $("palette");
  pal.innerHTML = "";
  pal.classList.toggle("open", S.palette.open);
  if (!S.palette.open) return;
  var grouped = false, last = null;
  var bare = S.palette.items.length && S.palette.items[0].kind === "slash";   // Claude Code's list has no heading
  if (!grouped && !bare) pal.appendChild(h("div", "mh")).appendChild(h("span", "", S.palette.title));
  S.palette.items.forEach(function (it, i) {
    if (grouped && it.group !== last) {
      pal.appendChild(h("div", "mh")).appendChild(h("span", "", it.group));
      last = it.group;
    }
    var b = h("button", "mi row1" + (i === S.palette.index ? " sel1" : ""));
    b.type = "button";
    b.setAttribute("role", "option");
    if (it.kind === "history") {
      b.innerHTML = svg("clock") + "<span></span>";
      b.querySelector("span").textContent = it.name;
    } else if (it.kind === "slash") {
      // Claude Code's list: the bare name; what it does shows beside the chosen one
      b.classList.add("slash");
      b.innerHTML = "<span></span>";
      b.querySelector("span").textContent = it.name;
      b.title = it.desc || "";
    } else {
      var slash = it.name.lastIndexOf("/");
      b.innerHTML = svg("file") + "<span><b></b></span><em></em>";
      b.querySelector("b").textContent = it.name.slice(slash + 1);
      b.querySelector("em").textContent = slash >= 0 ? it.name.slice(0, slash + 1) : "project root";
    }
    b.onmousedown = function (e) { e.preventDefault(); pickPalette(i); };
    pal.appendChild(b);
  });
  var sel = pal.querySelector(".sel1");
  if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
  var tip = $("palette-tip"), chosen = S.palette.items[S.palette.index];
  if (!tip) { tip = h("div", "paltip"); tip.id = "palette-tip"; $("composer").appendChild(tip); }
  tip.classList.toggle("on", !!(sel && chosen && chosen.kind === "slash" && chosen.desc));
  if (sel && chosen && chosen.desc) {
    tip.textContent = chosen.desc;
    var pr = pal.getBoundingClientRect(), cr = $("composer").getBoundingClientRect(), sr = sel.getBoundingClientRect();
    tip.style.left = (pr.right - cr.left + 8) + "px";
    tip.style.top = (sr.top - cr.top + sr.height / 2) + "px";
  }
}

function closePalette() {
  S.palette.open = false; S.palette.items = []; S.historySearch = false;
  if ($("palette-tip")) $("palette-tip").classList.remove("on");
  $("palette").classList.remove("open");
}

function pickPalette(i) {
  var it = S.palette.items[i === undefined ? S.palette.index : i];
  if (!it) return;
  if (it.kind === "history") {
    var box = $("input");
    box.value = it.full;
    box.selectionStart = box.selectionEnd = box.value.length;
    box.focus(); closePalette(); autosize();
    return;
  }
  var input = $("input"), v = input.value, pos = input.selectionStart;
  var before = v.slice(0, pos), after = v.slice(pos);
  before = it.kind === "slash"
    ? "/" + it.name + " "
    : before.replace(/(^|\s)@[\w\-./]*$/, "$1@" + it.name + " ");
  input.value = before + after;
  input.selectionStart = input.selectionEnd = before.length;
  input.focus();
  closePalette();
  autosize();
}

/* attachments sit above the text as chips */

function renderAttachments() {
  var bar = $("attach-preview");
  bar.innerHTML = "";
  bar.classList.toggle("hidden", !S.attachments.length);
  S.attachments.forEach(function (a, i) {
    var chip = h("span", "fchip");
    chip.innerHTML = svg(a.kind === "image" ? "image" : "file");
    chip.appendChild(h("span", "", a.filename));
    var x = h("button");
    x.title = "Remove"; x.setAttribute("aria-label", "Remove " + a.filename);
    x.innerHTML = svg("x");
    x.onclick = function () { S.attachments.splice(i, 1); renderAttachments(); };
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}

function handleFiles(files) {
  Array.prototype.forEach.call(files, function (f) {
    if (f.size > 2 * 1024 * 1024) { addNote("Skipped " + f.name + ": it is over 2 MB."); return; }
    var reader = new FileReader();
    var isImg = /^image\//.test(f.type);
    reader.onload = function () {
      S.attachments.push({ filename: f.name, kind: isImg ? "image" : "text", content: String(reader.result) });
      renderAttachments();
    };
    if (isImg) reader.readAsDataURL(f); else reader.readAsText(f);
  });
}
