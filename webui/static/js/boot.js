"use strict";
/* CodeMonkey page, boot.js: The walkthrough, the kernel, wiring and start-up.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- the first-run walkthrough ----------------
   Six short stops, each pointing at the thing it names. It runs once, on a new session, and again
   from Keyboard shortcuts → Show me around. A stop whose control is not on screen is skipped. */
var TOUR = [
  ["folder-btn", "Pick a folder", "This is the project the agent works in. It reads and changes files only inside it."],
  ["input", "Say what you want", "Describe the change in plain words, like you would to a colleague. Type / for commands and @ to point at a file."],
  ["perm-btn", "Choose how much it asks", "Manual asks before every change. Auto lets CodeMonkey decide what is safe. Switch any time with Shift Tab."],
  ["model-btn", "Pick a model", "Only the models that are running right now are listed. Effort sets how long it thinks before answering."],
  ["activity-btn", "Look beside the chat", "Changes, Terminal, Browser and more open here, next to the conversation."],
  ["settings-btn", "Settings", "Models, permissions and the look live here. The defaults are fine to start with."]
];
function startTour(force) {
  if (!force && store("cm:toured")) return;
  store("cm:toured", true);
  // the walkthrough starts from a new session, where every control it points at is on screen
  if (!visible0("folder-btn")) { newSession(); setTimeout(function () { runTour(); }, 350); } else runTour();
}
function visible0(id) { var el = $(id); return !!(el && el.offsetParent && el.getBoundingClientRect().width > 0); }
function runTour() {
  var at = -1, tip = h("div", "tour"), ring = h("div", "tourring");
  document.body.appendChild(ring);
  document.body.appendChild(tip);
  function end() { tip.remove(); ring.remove(); window.removeEventListener("resize", place); document.removeEventListener("keydown", keys, true); }
  var visible = visible0;
  function go(step) {
    var n = at + step;
    while (n >= 0 && n < TOUR.length && !visible(TOUR[n][0])) n += step;
    if (n < 0) return;
    if (n >= TOUR.length) { end(); return; }
    at = n;
    var t = TOUR[at], last = TOUR.slice(at + 1).every(function (x) { return !visible(x[0]); });
    tip.innerHTML = "";
    tip.appendChild(h("div", "tourn", (at + 1) + " of " + TOUR.length));
    tip.appendChild(h("b", "", t[1]));
    tip.appendChild(h("p", "", t[2]));
    var row = tip.appendChild(h("div", "btnrow"));
    row.appendChild(h("button", "btn link", "Skip")).onclick = end;
    if (at > 0) row.appendChild(h("button", "btn", "Back")).onclick = function () { go(-1); };
    var next = row.appendChild(h("button", "btn primary", last ? "Done" : "Next"));
    next.onclick = function () { go(1); };
    place();
    next.focus();
  }
  function place() {
    var el = $(TOUR[at][0]);
    if (!el) return;
    var r = el.getBoundingClientRect(), pad = 6;
    ring.style.cssText = "left:" + (r.left - pad) + "px;top:" + (r.top - pad) + "px;width:" + (r.width + pad * 2) + "px;height:" + (r.height + pad * 2) + "px";
    var tw = Math.min(340, window.innerWidth - 24), th = tip.offsetHeight;
    var below = r.bottom + 14 + th < window.innerHeight;
    var top = below ? r.bottom + 14 : r.top - 14 - th;
    var left = Math.max(12, Math.min(window.innerWidth - tw - 12, r.left + r.width / 2 - tw / 2));
    tip.style.cssText = "width:" + tw + "px;left:" + left + "px;top:" + Math.max(12, top) + "px";
  }
  function keys(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); end(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); go(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); }
  }
  window.addEventListener("resize", place);
  document.addEventListener("keydown", keys, true);
  go(1);
}

function openShortcuts() {
  var list = $("keys-list");
  list.innerHTML = "";
  list.appendChild(shortcutGrid());
  $("keys-modal").classList.add("open");
}

PAGES.advanced = function (pane) {
  pageHead(pane, "Advanced", "You should rarely need these.");
  // code intelligence: which language servers the agent can ask (coder/lsp.py)
  pane.appendChild(h("h3", "", "Code intelligence"));
  pane.appendChild(h("p", "pgsub", "A language server tells the agent about errors, definitions and uses the way an editor does. " +
    "After each change the agent sees the file's errors. Without one, Python, JavaScript and JSON still get a built-in check."));
  var lspBox = h("div");
  pane.appendChild(lspBox);
  api("GET", "/api/lsp?project_dir=" + encodeURIComponent(currentFolder() || "")).then(function (list) {
    list.forEach(function (l) {
      var tag = l.running ? h("span", "tag ok", "Running") : l.server ? h("span", "tag ok", "Found") : h("span", "tag", "Not installed");
      lspBox.appendChild(row("mrow", l.language, l.server ? l.server : "To add it: " + l.install + (l.error ? " · " + l.error : ""), tag));
    });
  }).catch(function () {});
  var info = h("div");
  pane.appendChild(info);
  function pathRow(title, path) {
    var b = button("Copy path", function () { copyText(path, function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy path"; }, 1200); }); });
    info.appendChild(row("srow", title, path, b));
  }
  api("GET", "/api/about").then(function (a) {
    pathRow("Settings file", a.config_path);
    pathRow("Workspace", a.workspace);
    pathRow("Commands folder", a.commands_dir);
    info.appendChild(row("srow", "Hooks", "Scripts that run before or after the agent uses a tool · " +
      (a.hooks ? a.hooks + " installed" : "none installed") + " · set up in hooks.yaml"));
    info.appendChild(row("srow", "Tool servers", "Extra tools connected over MCP · " + (a.mcp_config ? a.mcp_config : "none (add mcp.json to use them)")));
  }).catch(function () {});
  pane.appendChild(h("h3", "", "Health check"));
  var results = h("div");
  var checkBtn = button("Run checks", function () {
    checkBtn.textContent = "Checking";
    api("POST", "/api/doctor", {}).then(function (r) {
      checkBtn.textContent = "Run checks";
      results.innerHTML = "";
      if (r.error || !r.checks.length) { results.appendChild(h("div", "empty", "The checks could not run. " + (r.error || ""))); return; }
      r.checks.forEach(function (c) {
        results.appendChild(row("mrow", c.name.replace(/-/g, " "), c.detail, h("span", "tag " + (c.ok ? "ok" : "bad"), c.ok ? "Passed" : "Failed")));
      });
    }).catch(function (err) { checkBtn.textContent = "Run checks"; toastError(err); });
  });
  pane.appendChild(row("srow", "Check how the parts are wired", "Runs the built-in checks: approvals reach every layer, hooks can block, nothing is silently bypassed. No model is used.", checkBtn));
  pane.appendChild(results);
  pane.appendChild(h("h3", "dangerh", "Danger zone"));
  pane.appendChild(row("srow", "Close all sessions", "Ends every session and clears the list. Your files are untouched.",
    button("Close all", function () {
      askConfirm("Close all sessions?", "Every session is ended and removed from the list. Files on disk are not touched.", "Close all", function () {
        var ids = S.list.map(function (e) { return e.id; });
        (function next(i) {
          if (i >= ids.length) { S.status = {}; newSession(); refreshSessions(); return; }
          api("DELETE", "/api/sessions/" + ids[i]).catch(function () {}).then(function () { next(i + 1); });
        })(0);
        closeSettings();
      });
    }, "danger")));
  pane.appendChild(row("srow", "Reset settings", "Back to how CodeMonkey was first installed. Sessions and memory are kept.",
    button("Reset", function () {
      askConfirm("Reset all settings?", "Permissions, limits, rates and appearance go back to their defaults.", "Reset", function () {
        api("POST", "/api/settings/reset", {}).then(function (st) {
          S.settings = st; applyUi(); renderPickers(); showPage("advanced");
        }).catch(toastError);
      });
    }, "danger")));
};

function searchSettings() {
  var q = $("settings-search").value.trim().toLowerCase();
  var here = PAGE_WORDS[S.page] || "";
  if (q && here.indexOf(q) < 0) {
    var hit = Object.keys(PAGE_WORDS).filter(function (p) { return PAGE_WORDS[p].indexOf(q) >= 0; })[0];
    if (hit) showPage(hit);
  }
  Array.prototype.forEach.call($("settings-pane").querySelectorAll(".srow, .mrow, .urow"), function (r) {
    r.classList.toggle("hidden", !!q && r.textContent.toLowerCase().indexOf(q) < 0 &&
      (PAGE_WORDS[S.page] || "").indexOf(q) < 0);
  });
}

/* ---------------- the kernel, as its own tool ---------------- */

var kernelInfo = null;
function openKernel() {
  closeMenus();
  $("kernel-modal").classList.add("open");
  $("kernel-input").focus();
  var domain = $("kernel-domain").value;
  if (kernelInfo && kernelInfo.domain === domain) return;
  if (!$("kernel-domain").onchange) $("kernel-domain").onchange = function () {
    $("kernel-input").value = ""; $("kernel-out").innerHTML = ""; openKernel();
  };
  api("GET", "/api/kernel?domain=" + encodeURIComponent(domain)).then(function (info) {
    kernelInfo = info;
    var box = $("kernel-examples");
    box.innerHTML = "";
    info.examples.forEach(function (ex) {
      var b = h("button", "dn");
      b.type = "button";
      b.title = ex.input;
      b.appendChild(h("span", "", ex.id + " · " + ex.category.toLowerCase().replace(/_/g, " ")));
      b.onclick = function () { $("kernel-input").value = ex.input; runKernel(); };
      box.appendChild(b);
    });
    if (!$("kernel-input").value && info.examples.length) $("kernel-input").value = info.examples[0].input;
  }).catch(function (err) { $("kernel-note").textContent = "The kernel could not be loaded: " + err.message; });
}

function runKernel() {
  var text = $("kernel-input").value, out = $("kernel-out");
  if (!text.trim()) return;
  api("POST", "/api/kernel/run", { text: text, domain: $("kernel-domain").value }).then(function (r) {
    out.innerHTML = "";
    r.answers.forEach(function (a) {
      var card = h("div", "kans");
      card.appendChild(h("div", "g", a.goal));
      var v = h("div", "v");
      v.appendChild(h("span", "", a.value));
      var chip = h("span", "chip " + (a.supported ? "good" : /CONFABULATED|CONFLICTED/.test(a.state) ? "maybe" : "no"));
      chip.appendChild(h("i", "dot"));
      chip.appendChild(document.createTextNode(a.state));
      v.appendChild(chip);
      card.appendChild(v);
      var help = (kernelInfo && kernelInfo.states[a.state]) || "";
      card.appendChild(h("div", "c", (a.causes.length ? a.causes.join("; ") + ". " : "") + help));
      out.appendChild(card);
    });
    if (!r.answers.length) out.appendChild(h("div", "empty", "No question was asked. Add a line that starts with ask."));
    $("kernel-note").textContent = "Steps: " + r.steps.join(" → ") + ". No model is used.";
  }).catch(function (err) {
    out.innerHTML = "";
    out.appendChild(h("div", "empty", "The Logic lab could not run this: " + err.message));
  });
}

/* ---------------- wiring ---------------- */

function wire() {
  // menus open from their trigger and close on any click outside
  Array.prototype.forEach.call(document.querySelectorAll("[data-menu]"), function (b) {
    b.onclick = function (e) {
      var id = b.getAttribute("data-menu"), was = $(id).classList.contains("open");
      e.stopPropagation();
      if (was) closeMenus(); else openMenu(id);
    };
  });
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".menu")) closeMenus();
    var copy = e.target.closest(".code-copy");
    if (copy) {
      var code = copy.closest(".codeblock").querySelector("pre code");
      copyText(code ? code.textContent : "", function () {
        copy.textContent = "copied";
        setTimeout(function () { copy.textContent = "copy"; }, 1200);
      });
    }
  });
  // a click on the dark surround closes a dialog (never the permission one: that needs an answer)
  Array.prototype.forEach.call(document.querySelectorAll(".scrim"), function (s) {
    s.addEventListener("mousedown", function (e) {
      if (e.target !== s || s.id === "perm-modal") return;
      if (s.id === "settings-modal") closeSettings(); else closeDialog(s.id);
    });
  });

  $("new-session").onclick = newSession;
  $("search-btn").onclick = openCmdk;
  $("settings-btn").onclick = function () { openSettings(); };
  $("scheduled-btn").onclick = function () { openSettings("scheduled"); };
  $("customize-btn").onclick = function () { openSettings("skills"); };
  $("kernel-btn").onclick = openKernel;
  $("nav-btn").onclick = function () { document.body.classList.toggle("nav-open"); };
  $("veil").onclick = function () { document.body.classList.remove("nav-open"); };
  if (window.desktop && window.desktop.onUpdate) {
    // the desktop app found a newer build: one button, next to Activity
    window.desktop.onUpdate(function (version) {
      var b = $("update-btn");
      if (!b) {
        b = h("button", "ghost update", "Relaunch to update");
        b.id = "update-btn";
        b.onclick = function () { b.disabled = true; b.textContent = "Updating…"; window.desktop.installUpdate(); };
        $("pane-anchor").parentElement.insertBefore(b, $("pane-anchor"));
      }
      b.title = "CodeMonkey " + version + " is ready";
    });
  }
  $("inspector-close").onclick = function () { showActivity(false); };
  $("review-comments-send").onclick = sendComments;
  $("review").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && S.comments.length) { e.preventDefault(); sendComments(); }
  });
  $("dcard-chg").onclick = toggleChanges;
  $("dcard-ctx").onclick = function () {
    var open = $("dcard-ctx-body").classList.toggle("hidden") === false;
    $("dcard-ctx").setAttribute("aria-expanded", open ? "true" : "false");
    if (open) loadContext($("dcard-ctx-body"));
  };
  document.addEventListener("contextmenu", function (e) {
    var step = e.target.closest(".step");
    var el = e.target.closest("#chat code, .chgrow .p, .cf .it > span, .diff .h span:first-child");
    var path = step && step._args && step._args.path ? String(step._args.path) : el ? pathFrom(el) : "";
    if (!path) return;
    e.preventDefault();
    openPathMenu(path, e.clientX, e.clientY);
  });
  PANES.forEach(makeResizable);
  Array.prototype.forEach.call(document.querySelectorAll("[data-pop-pane]"), function (b) {
    b.onclick = function () { popPane(b); };
  });
  $("split-close").onclick = closeSplit;
  $("diff-unified").onclick = function () { setDiffView("unified"); };
  $("diff-split").onclick = function () { setDiffView("split"); };
  setDiffView(diffView());
  $("keys-tour").onclick = function () { $("keys-modal").classList.remove("open"); startTour(true); };
  wireBrowser();
  $("split-pop").onclick = function () { var id = S.splitId; closeSplit(); if (id) popOut(id); };
  Array.prototype.forEach.call(document.querySelectorAll("[data-close-pane]"), function (b) {
    b.onclick = function () { closePanes(null); };
  });
  $("file-text").addEventListener("input", function () {
    if (S.file && !S.file.viewOnly) { S.file.dirty = $("file-text").value !== S.file.text; renderFileState(); }
  });
  $("file-text").addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); e.stopPropagation(); saveFile(false); }
    else if (e.key === "Tab" && !e.shiftKey) {   // a tab is two spaces, not a jump to the next control
      e.preventDefault();
      var ta = e.target, a = ta.selectionStart;
      ta.setRangeText("  ", a, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input"));
    }
  });
  $("file-save").onclick = function () { saveFile(false); };
  $("file-back").onclick = backToTree;
  $("file-filter").oninput = function () { S.tree.filter = this.value; renderTree(); };
  $("file-filter").onkeydown = function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); var r = $("file-tree").querySelector(".trow"); if (r) r.focus(); }
  };
  $("file-tree").onkeydown = treeKeys;
  $("term-stop").onclick = function () {
    if (S.current) api("POST", "/api/sessions/" + S.current + "/terminal", { stop: true }).then(pollTerm).catch(toastError);
  };
  $("term-input").addEventListener("keydown", function (e) {
    var t = S.term;
    if (e.key === "Enter") { e.preventDefault(); var c = e.target.value; e.target.value = ""; runTerm(c); }
    else if (e.key === "ArrowUp" && t.hist.length) { e.preventDefault(); t.at = Math.max(0, t.at - 1); e.target.value = t.hist[t.at]; }
    else if (e.key === "ArrowDown" && t.hist.length) { e.preventDefault(); t.at = Math.min(t.hist.length, t.at + 1); e.target.value = t.hist[t.at] || ""; }
    else if (e.key === "c" && e.ctrlKey && !e.target.selectionEnd) { e.preventDefault(); $("term-stop").click(); }
  });
  $("chat").addEventListener("click", onReviewClick);
  $("chat").addEventListener("click", function (e) {
    if (e.target.closest(".rvf [data-rv]")) return;
    var c = e.target.closest("code");
    if (!c || c.closest("pre") || e.ctrlKey || e.metaKey) return;
    var path = pathFrom(c);
    if (path && path.indexOf("/") >= 0) openFile(path);
  });
  $("git-cancel").onclick = function () { closeDialog("git-modal"); };
  $("git-go").onclick = submitGit;
  ["git-branch", "git-subject", "git-body"].forEach(function (f) {
    $(f).addEventListener("input", function () { $(f).dataset.touched = "1"; });
  });
  $("git-subject").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); submitGit(); } });
  setInterval(function () { if (S.current && !document.hidden && !S.workStart) refreshGit(false); }, 30000);
  $("env-worktree").onclick = function () { S.pendingWorktree = !S.pendingWorktree; setWorktreeChip(); };
  $("side-close").onclick = closeSide;
  $("side-clear").onclick = function () {
    $("side-log").innerHTML = "";
    if (S.current) api("POST", "/api/sessions/" + S.current + "/side", { clear: true }).catch(function () {});
    openSide();
  };
  $("side-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); var q = e.target.value; e.target.value = ""; askSide(q); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSide(); $("input").focus(); }
  });
  var wasWide = window.innerWidth > 1000;
  window.addEventListener("resize", function () {
    var wide = window.innerWidth > 1000;
    // the panel covers the conversation on a narrow window: close it when the window narrows
    if (wasWide && !wide && !$("inspector").classList.contains("hidden")) showActivity(false, true);
    wasWide = wide;
  });
  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: light)");
    var follow = function () { if (S.settings && S.settings.ui && S.settings.ui.theme === "system") applyUi(); };
    if (scheme.addEventListener) scheme.addEventListener("change", follow); else if (scheme.addListener) scheme.addListener(follow);
  }
  $("checkpoint-btn").onclick = checkpoint;

  $("send-btn").onclick = sendMessage;
  $("cancel-btn").onclick = cancelTurn;
  $("attach-btn").onclick = function () { $("file-picker").click(); };
  $("file-picker").onchange = function (e) { handleFiles(e.target.files); e.target.value = ""; };
  var comp = $("composer");
  comp.addEventListener("dragover", function (e) { e.preventDefault(); comp.classList.add("drag"); });
  comp.addEventListener("dragleave", function () { comp.classList.remove("drag"); });
  comp.addEventListener("drop", function (e) {
    e.preventDefault();
    comp.classList.remove("drag");
    if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files);
  });

  var input = $("input");
  input.addEventListener("keydown", function (e) {
    var pal = S.palette.open && S.palette.items.length;
    // the grey suggestion: Tab or the right arrow takes it into the box (Enter alone never sends it)
    if (S.suggestion && !input.value && !S.attachments.length && !pal &&
        ((e.key === "Tab" && !e.shiftKey) || e.key === "ArrowRight")) {
      e.preventDefault();
      input.value = S.suggestion; setSuggestion(""); autosize();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (pal) pickPalette(); else sendMessage();
    } else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !pal && !e.shiftKey && historyStep(e.key === "ArrowUp" ? 1 : -1)) {
      e.preventDefault();
    } else if (e.key.toLowerCase() === "r" && e.ctrlKey && !e.altKey) {
      e.preventDefault(); openHistorySearch();
    } else if (e.key.toLowerCase() === "b" && e.ctrlKey && !e.altKey && S.workStart) {
      e.preventDefault(); toBackground();
    } else if (e.key === "Tab" && e.shiftKey && !pal) {
      e.preventDefault(); cycleMode();
    } else if (e.key === "Tab" && pal) {
      e.preventDefault(); pickPalette();
    } else if (e.key === "Escape" && S.palette.open) {
      closePalette(); e.preventDefault(); e.stopPropagation();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && pal) {
      e.preventDefault();
      var n = S.palette.items.length;
      S.palette.index = (S.palette.index + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      renderPalette();
    }
  });
  input.addEventListener("input", function () { autosize(); updatePalette(); });
  startKeeping();   // drafts and scroll kept as they change (keep.js)
  input.addEventListener("click", updatePalette);
  input.addEventListener("blur", function () { setTimeout(closePalette, 120); });

  $("new-msg-pill").onclick = function () { scrollToEnd(true); };
  $("chat-wrap").addEventListener("scroll", updateToBottom, { passive: true });
  // the input area growing (long text, the working line, a queue) shrinks the view without a scroll
  if (window.ResizeObserver) new ResizeObserver(updateToBottom).observe($("chat-wrap"));

  $("text-cancel").onclick = function () { closeDialog("text-modal"); };
  $("text-save").onclick = function () {
    var cb = S.textCb;
    S.textCb = null; closeDialog("text-modal");
    if (cb) cb($("text-body").value);
  };

  $("perm-allow").onclick = function () { resolvePermission("allow"); };
  $("perm-always").onclick = function () { resolvePermission("always"); };
  $("perm-deny").onclick = function () { resolvePermission("deny"); };
  $("elicit-send").onclick = function () { answerElicitation("accept"); };
  $("review-close").onclick = closeReview;
  $("rewind-cancel").onclick = function () { closeDialog("rewind-modal"); };
  $("remember-cancel").onclick = function () { closeDialog("remember-modal"); };
  $("keys-close").onclick = function () { closeDialog("keys-modal"); };
  $("setup-close").onclick = function () { closeDialog("setup-modal"); };
  $("report-cancel").onclick = function () { closeDialog("report-modal"); };
  $("memory-cancel").onclick = function () { closeDialog("memory-modal"); };
  $("review-keep-all").onclick = function () { if (S.reviewTurn) decideChange(S.reviewTurn, "", "keep_all"); };
  $("review-undo-all").onclick = function () { if (S.reviewTurn) decideChange(S.reviewTurn, "", "undo_all"); };
  $("elicit-decline").onclick = function () { answerElicitation("decline"); };
  $("perm-message").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); resolvePermission("deny", $("perm-message").value); }
  });

  $("confirm-ok").onclick = function () {
    var cb = S.confirmCb;
    S.confirmCb = null; closeDialog("confirm-modal");
    if (cb) cb();
  };
  $("confirm-cancel").onclick = function () { S.confirmCb = null; closeDialog("confirm-modal"); };
  function promptOk() {
    var cb = S.promptCb, v = $("prompt-input").value.trim();
    if (!v) return;
    S.promptCb = null; closeDialog("prompt-modal");
    if (cb) cb(v);
  }
  $("prompt-ok").onclick = promptOk;
  $("prompt-cancel").onclick = function () { S.promptCb = null; closeDialog("prompt-modal"); };
  $("prompt-input").addEventListener("keydown", function (e) { if (e.key === "Enter") promptOk(); });

  $("cmd-cancel").onclick = function () { closeDialog("cmd-modal"); };
  $("cmd-save").onclick = function () {
    api("POST", "/api/commands", { name: $("cmd-name").value, description: $("cmd-desc").value, body: $("cmd-body").value })
      .then(function (cmds) {
        S.commands = cmds;
        closeDialog("cmd-modal");
        if ($("settings-modal").classList.contains("open")) showPage("commands");
      }).catch(function (err) { $("cmd-title").textContent = "Not saved: " + err.message; });
  };

  ["bg", "panel", "text", "accent"].forEach(function (k) {
    $("theme-" + k).addEventListener("input", function () { applyTheme(themeFrom(themeDraft(), "draft")); });
  });
  $("theme-cancel").onclick = function () { closeDialog("theme-modal"); applyUi(); };
  $("theme-save").onclick = function () {
    var name = (themeEditing || $("theme-name").value.trim() || "My theme").replace(/[:"<>]/g, "").slice(0, 30);
    var custom = {};
    Object.keys(S.settings.ui.custom_themes || {}).forEach(function (n) { custom[n] = S.settings.ui.custom_themes[n]; });
    custom[name] = themeDraft();
    saveCustomThemes(custom, "custom:" + name);
  };
  $("theme-delete").onclick = function () {
    var custom = {};
    Object.keys(S.settings.ui.custom_themes || {}).forEach(function (n) { if (n !== themeEditing) custom[n] = S.settings.ui.custom_themes[n]; });
    saveCustomThemes(custom, "warm");
  };
  $("skill-cancel").onclick = function () { closeDialog("skill-modal"); };
  $("skill-save").onclick = function () {
    api("POST", "/api/skills", { name: $("skill-name").value, description: $("skill-desc").value,
      keywords: $("skill-keys").value, body: $("skill-body").value })
      .then(function () { closeDialog("skill-modal"); showPage("skills"); })
      .catch(function (err) { $("skill-head").textContent = "Not saved: " + err.message; });
  };
  $("job-when").onchange = function () {
    $("job-cron").classList.toggle("hidden", $("job-when").value !== "");
    if ($("job-when").value) $("job-cron").value = $("job-when").value;
  };
  $("job-cancel").onclick = function () { closeDialog("job-modal"); };
  $("job-save").onclick = saveJob;
  $("job-remove").onclick = function () {
    var job = jobEditing;
    askConfirm("Remove “" + job.title + "”?", "The job and its run history are deleted. The session with its reports stays.", "Remove", function () {
      api("DELETE", "/api/jobs/" + job.id).then(function () { closeDialog("job-modal"); showPage("scheduled"); }).catch(toastError);
    });
  };

  $("browse-up").onclick = function () { if (browse.parent) browseTo(browse.parent); };
  $("browse-path").addEventListener("keydown", function (e) {
    if (e.key === "Enter") browseTo($("browse-path").value);
    if (e.key === "Escape") { e.stopPropagation(); browseTo(browse.path); }
  });
  $("browse-filter").oninput = browseList;
  $("browse-new").onclick = browseNewFolder;
  $("browse-native").onclick = function () { closeDialog("folder-modal"); openBrowse(browse.path); };
  $("browse-cancel").onclick = function () { closeDialog("folder-modal"); };
  $("browse-choose").onclick = function () { chooseFolder(browse.path); };

  $("cmdk-input").addEventListener("input", cmdkInput);
  $("cmdk-input").addEventListener("keydown", function (e) {
    var n = S.cmdk.results.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Tab") {
      e.preventDefault();
      if (n) S.cmdk.index = (S.cmdk.index + (e.key === "ArrowUp" ? n - 1 : 1)) % n;
      paintCmdk();
    } else if (e.key === "Enter") { e.preventDefault(); runCmdk(); }
  });

  Array.prototype.forEach.call($("settings-nav").querySelectorAll(".dn"), function (b) {
    b.onclick = function () { $("settings-search").value = ""; showPage(b.getAttribute("data-page")); };
  });
  $("settings-more-btn").onclick = function () { showAllSettings(!$("settings-nav").classList.contains("all")); };
  var allSettings = false;
  try { allSettings = !!localStorage.getItem("cm:allsettings"); } catch (e) {}
  showAllSettings(allSettings);
  $("settings-close").onclick = closeSettings;
  $("settings-back").onclick = closeSettings;
  $("settings-search").addEventListener("input", searchSettings);

  $("kernel-close").onclick = function () { closeDialog("kernel-modal"); };
  $("remote-close").onclick = function () { closeDialog("remote-modal"); };
  $("kernel-run").onclick = runKernel;
  $("kernel-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runKernel(); }
  });

  document.addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (S.perm) {   // the agent is waiting: Enter allows, A always allows, Esc denies
      if (e.key === "Escape") { e.preventDefault(); resolvePermission("deny"); }
      else if (!typing && e.key === "Enter") { e.preventDefault(); resolvePermission("allow"); }
      else if (!typing && key === "a" && !mod && !$("perm-always").classList.contains("hidden")) { e.preventDefault(); resolvePermission("always"); }
      return;
    }
    // the shortcuts you can change (Settings > Keyboard): looked up in KEY_ACTIONS
    var act = keyAction(e);
    if (act && !(act[0] === "background" && e.defaultPrevented)) { e.preventDefault(); act[3](); return; }
    // 1-9 picks an item in an open menu
    var openMenuEl = document.querySelector("#flyout.open") || document.querySelector(".menu.open:not(#palette)");
    if (openMenuEl && !typing && !mod && /^[1-9]$/.test(e.key)) {
      var items = openMenuEl.querySelectorAll("button.mi");
      if (items[+e.key - 1]) { e.preventDefault(); items[+e.key - 1].click(); }
      return;
    }
    if (e.ctrlKey && e.key === "Tab" && S.list.length > 1) {
      e.preventDefault();
      var at = S.list.findIndex(function (x) { return x.id === S.current; });
      openSession(S.list[(at + (e.shiftKey ? S.list.length - 1 : 1)) % S.list.length].id);
      return;
    }
    if (e.key === "Escape") {
      var open = openDialogs();
      if (open.length) {
        var top = open[open.length - 1];
        if (top.id === "settings-modal") closeSettings();
        else if (top.id === "elicit-modal") answerElicitation("cancel");   // the server is told, not left waiting
        else closeDialog(top.id);
        return;
      }
      if (document.querySelector(".menu.open:not(#palette)")) { closeMenus(); return; }
      if (document.body.classList.contains("nav-open")) { document.body.classList.remove("nav-open"); return; }
      if (S.palette.open) return;   // the message box closes its own list
      if (S.workStart) { cancelTurn(); return; }
      // twice with an empty message box: rewind (Claude Code's Esc Esc)
      if (S.current && !$("input").value.trim()) {
        if (Date.now() - (S.lastEsc || 0) < 700) { S.lastEsc = 0; openRewind(); } else S.lastEsc = Date.now();
      }
      return;
    }
    if (!typing && e.key === "/" && !openDialogs().length) { e.preventDefault(); input.focus(); }
    if (!typing && e.key === "?" && !openDialogs().length) { e.preventDefault(); openShortcuts(); }
  });
}

function waitForSession(id, fn, tries) {
  tries = tries || 0;
  if (S.current === id && S.snap) { setTimeout(fn, 150); return; }
  if (tries < 60) setTimeout(function () { waitForSession(id, fn, tries + 1); }, 100);
}

function init() {
  wire();
  if (location.hash.indexOf("shot") >= 0) {   // screenshots: wait for the content before "load"
    var hold = new Image();
    hold.src = "/api/hold?ms=3500";
    hold.style.display = "none";
    document.body.appendChild(hold);
  }
  Promise.all([api("GET", "/api/config"), api("GET", "/api/settings"), api("GET", "/api/commands"), api("GET", "/api/sessions")])
    .then(function (r) {
      S.config = r[0]; S.settings = r[1]; S.commands = r[2]; S.list = r[3];
      loadMcp();   // its prompts join the / commands
      pollStatusCommand();
      applyUi();
      if (!store("cm:activity-v2")) { store("cm:activity", false); store("cm:activity-v2", true); }
      showActivity(activityWanted(), true);
      noteUpdate();   // as you left it
      setTimeout(noteNewVersion, 8000);   // the daily check runs as the server starts
      openAppEvents();
      watchConnection();
      $("face").onclick = stopSpeaking;
      setInterval(updateFace, 300);
      updateFace();
      // carry on with the most recent session that is still open on the server
      var live = S.list.filter(function (e) { return e.live; })[0];
      if (HASH.solo) document.body.classList.add("solo");
      if (HASH.session) {
        openSession(HASH.session);
        if (HASH.pane) waitForSession(HASH.session, function () { openPoppedPane(HASH.pane); });
      } else if (live) openSession(live.id);
      else if (lastSession()) openSession(lastSession());   // after a restart or a crash: where you were
      else if (location.hash.indexOf("latest") >= 0 && S.list.length) openSession(S.list[0].id);
      else newSession();
      renderSessions();
      api("GET", "/api/folders").then(function (f) { S.folders = f; }).catch(function () {});
      // deep links, used for screenshots: #settings=models, #kernel, #search
      var m = location.hash.match(/settings(?:=(\w+))?/);
      if (m) openSettings(m[1]);
      if (location.hash.indexOf("kernel") >= 0) openKernel();
      if (location.hash.indexOf("search") >= 0) openCmdk();
      if (location.hash.indexOf("activity") >= 0) showActivity(true);
      if (location.hash.indexOf("expand") >= 0) setTimeout(function () {
        Array.prototype.forEach.call(document.querySelectorAll("details.work, details.basis"), function (d) { d.open = true; });
      }, 1500);
      if (!HASH.session && !HASH.shot && (HASH.tour || !navigator.webdriver) && !/settings|kernel|search|menu=/.test(location.hash)) setTimeout(function () { startTour(!!HASH.tour); }, 900);
      m = location.hash.match(/menu=([\w-]+)/);
      if (m && MENU_BUILDERS[m[1]]) setTimeout(function () { openMenu(m[1]); }, 600);
    }).catch(function (err) {
      document.body.textContent = "CodeMonkey could not reach its server: " + err.message;
    });
}

// the list is kept current by the app's event stream; this only refreshes the "5m ago" times
setInterval(function () { if (S.config && !document.hidden) renderSessions(); }, 60000);

document.addEventListener("DOMContentLoaded", init);
