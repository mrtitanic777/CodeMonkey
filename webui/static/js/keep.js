"use strict";
/* CodeMonkey page, keep.js: the page comes back the way it was after CodeMonkey closes, crashes or
   is forced shut: the session that was open, what was typed but not sent (per session), the pane
   beside the chat, and where the conversation was scrolled to. All of it is kept in this window's
   own storage as it changes, so nothing has to be saved "on the way out". The conversation itself
   is the server's (a reply cut off mid-way is marked there, with Continue). */

var KEEP = { draftTimer: null, scrollTimer: null, pane: store("cm:pane") };   // read before anything closes it

function keepKey() { return S.current || "new"; }

/* the session that was open, so the next start opens it again (or the start screen, if that was it) */
function keepSession(id) { store("cm:last-session", id || ""); }
function lastSession() {
  var id = store("cm:last-session");
  return id && S.list.some(function (e) { return e.id === id; }) ? id : "";
}

/* what was typed but not sent, per session */
function keepDraft() {
  clearTimeout(KEEP.draftTimer);
  var key = keepKey(), text = $("input").value;
  KEEP.draftTimer = setTimeout(function () {
    var all = store("cm:drafts") || {};
    if (text.trim()) all[key] = text; else delete all[key];
    store("cm:drafts", all);
  }, 250);
}
function restoreDraft() {
  var input = $("input"), all = store("cm:drafts") || {};
  input.value = all[keepKey()] || "";
  autosize();
}
function clearDraft(key) {
  clearTimeout(KEEP.draftTimer);
  var all = store("cm:drafts") || {};
  delete all[key || keepKey()];
  delete all["new"];   // a first message makes a session; what was typed before it is sent
  store("cm:drafts", all);
}

/* the pane beside the chat that was open (the terminal or the files) */
function keepPane(id) { store("cm:pane", id === "term-pane" || id === "file-pane" ? id : ""); }
function restorePane() {   // once, for the session opened at start: later ones open as they always have
  var pane = KEEP.pane;
  KEEP.pane = null;
  if (pane === "term-pane" && !paneOpen("term-pane")) toggleTerm();
  else if (pane === "file-pane" && !paneOpen("file-pane")) toggleFiles();
}

/* where the conversation was scrolled to, per session ("end" for the latest) */
function keepScroll() {
  if (!S.current) return;
  clearTimeout(KEEP.scrollTimer);
  var id = S.current;
  KEEP.scrollTimer = setTimeout(function () {
    var all = store("cm:scroll") || {}, wrap = $("chat-wrap");
    all[id] = isAtBottom() ? "end" : Math.round(wrap.scrollTop);
    var ids = Object.keys(all);
    if (ids.length > 50) delete all[ids[0]];   // the 50 most recent are plenty
    store("cm:scroll", all);
  }, 300);
}
function restoreScroll(id) {
  var at = (store("cm:scroll") || {})[id];
  if (typeof at !== "number") return;   // "end", or never scrolled: the latest, as usual
  setTimeout(function () {   // after the conversation has settled at the end
    if (S.current === id) $("chat-wrap").scrollTop = at;
  }, 1700);
}

function startKeeping() {
  $("input").addEventListener("input", keepDraft);
  $("chat-wrap").addEventListener("scroll", keepScroll, { passive: true });
}
