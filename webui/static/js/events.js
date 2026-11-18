"use strict";
/* CodeMonkey page, events.js: The live event stream from the server.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- live events (SSE) ---------------- */

var EVENT_TYPES = ["state", "user_message", "prefill", "turn_start", "turn_effort", "token", "claims", "activity", "subagent_progress", "artifact", "tool_call", "tool_progress", "tool_result",
  "permission_request", "permission_resolved", "plan_ready", "plan_decision", "todo_update",
  "subagent_start", "subagent_end", "checkpoint_created", "rewind_done", "notice", "turn_end",
  "settings_changed", "session_end", "context_compacted", "elicitation_request", "elicitation_resolved",
  "turn_changes", "open_rewind", "rewound", "context_usage", "processes", "task_suggested", "browser_view", "thinking"];

/* ---- presence: a face that shows what the agent is doing, and can read its replies aloud ----
   The states come from the session itself (working, waiting for you, stopped on an error), so
   the face never claims more than the record does. Speech uses the voices already on this
   computer (the browser's speech synthesis); nothing is sent anywhere and no model is involved.
   The mouth moves with the words as they are spoken. Click the face to stop it talking. */

var FACE_WORDS = { idle: "The agent is idle", working: "The agent is working", asking: "The agent is waiting for your answer",
  speaking: "The agent is reading its reply aloud (click to stop)", error: "The last turn ended with an error", away: "Reconnecting" };
var VOICE = { talking: false, timer: null };

function faceState() {
  if (SYNC.down) return "away";
  if (VOICE.talking) return "speaking";
  if (S.perm) return "asking";
  var st = S.current ? S.status[S.current] : "idle";
  if (st === "working") return "working";
  if (st === "permission") return "asking";
  if (st === "error") return "error";
  return "idle";
}
function updateFace() {
  var face = $("face"), ui = (S.settings && S.settings.ui) || {};
  if (!face) return;
  face.classList.toggle("hidden", ui.presence === false);
  var state = faceState();
  document.documentElement.classList.toggle("busy", state === "working");   // the background breathes a little faster
  if (face.getAttribute("data-state") !== state) {
    face.setAttribute("data-state", state);
    face.title = FACE_WORDS[state];
    face.setAttribute("aria-label", FACE_WORDS[state]);
  }
}
function plainSpeech(text) {
  // what is worth hearing: prose, not code, tables, links or the system's footers
  return String(text || "")
    .replace(/```[\s\S]*?```/g, " (code omitted) ")
    .replace(/^\s*\|.*\|\s*$/gm, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\[Removed:[^\]]*\]/g, " ")
    .replace(/[*_#>]+/g, " ")
    .replace(/\s+/g, " ").trim().slice(0, 900);
}
function stopSpeaking() {
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) {}
  VOICE.talking = false;
  $("face").classList.remove("open");
  updateFace();
}
function speakReply(text) {
  var ui = (S.settings && S.settings.ui) || {};
  if (!ui.speak || !window.speechSynthesis || document.hidden) return;
  var words = plainSpeech(text);
  if (!words) return;
  try {
    window.speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(words);
    var voices = window.speechSynthesis.getVoices() || [];
    var chosen = voices.filter(function (v) { return v.name === ui.voice; })[0];
    if (chosen) u.voice = chosen;
    u.onstart = function () { VOICE.talking = true; updateFace(); };
    u.onboundary = function () {   // one word spoken: the mouth opens and closes with it
      var face = $("face");
      face.classList.add("open");
      clearTimeout(VOICE.timer);
      VOICE.timer = setTimeout(function () { face.classList.remove("open"); }, 110);
    };
    u.onend = u.onerror = function () { VOICE.talking = false; $("face").classList.remove("open"); updateFace(); };
    window.speechSynthesis.speak(u);
  } catch (e) { VOICE.talking = false; }
}

/* ---- staying in step with the server ----
   Every event has a number. The page remembers the last one it got; when the stream drops (a
   phone going to sleep, Wi-Fi blinking) it reconnects saying "I have up to N" and is sent what it
   missed. If it was away too long for that, the server says "resync" and the conversation is
   reloaded. A watchdog reconnects a stream that has gone silent, because a mobile browser can
   keep a dead connection looking alive for a long time. */

var SYNC = { seq: 0, at: 0, hiddenAt: 0, down: false, appEs: null, appSeq: 0 };

function setLink(down) {
  if (SYNC.down === down) return;
  SYNC.down = down;
  $("conn").classList.toggle("hidden", !down);
}

/* Resolves once the stream is attached (its first "state" event), so a message
   sent right after can never race ahead of the listener. */
function openEvents(id, after) {
  return new Promise(function (resolve) {
    if (S.es) S.es.close();
    var es = new EventSource("/api/sessions/" + id + "/events" + (after != null ? "?after=" + after : ""));
    S.es = es;
    SYNC.at = Date.now();
    var attached = false;
    function seen(e) {
      SYNC.at = Date.now();
      if (e && e.lastEventId) SYNC.seq = Math.max(SYNC.seq, +e.lastEventId || 0);
      setLink(false);
    }
    es.addEventListener("ping", function (e) { if (S.es === es) seen(null); });
    es.addEventListener("resync", function () { if (S.es === es) resyncSession(); });
    EVENT_TYPES.forEach(function (type) {
      es.addEventListener(type, function (e) {
        if (S.es !== es) return;
        seen(e);
        var data = {};
        try { data = JSON.parse(e.data); } catch (err) {}
        handleEvent(type, data);
        if (type === "state" && !attached) { attached = true; resolve(); }
      });
    });
    // NOTE: the DOM "error" event fires on connection drops (the browser
    // auto-reconnects); it is NOT a server-sent error payload, so it must not
    // share the SSE "error" handler. Only MessageEvents carry server errors.
    es.addEventListener("error", function (e) {
      if (S.es !== es) return;
      if (!(e instanceof MessageEvent)) { setLink(true); return; }   // connection drop: it retries
      seen(e);
      var data = {};
      try { data = JSON.parse(e.data); } catch (err) {}
      handleEvent("error", data);
    });
    setTimeout(resolve, 4000); // never leave a send waiting on a slow stream
  });
}

/* Reconnect now, picking up after the last event this page has. */
function reconnect() {
  if (S.current) openEvents(S.current, SYNC.seq);
  openAppEvents();
}

/* Rebuild the conversation from the server's record: the cure for any drift. */
function resyncSession() {
  var id = S.current;
  if (!id) return Promise.resolve();
  var wasAtBottom = isAtBottom();
  return api("GET", "/api/sessions/" + id + "/history").then(function (hist) {
    if (S.current !== id) return;
    return api("GET", "/api/sessions/" + id).then(function (snap) {
      if (S.current !== id) return;
      $("chat").innerHTML = "";
      S.turn = null; S.subagents = {};
      applySnapshot(snap);
      var entries = hist.transcript || [];
      showHello(!entries.length);
      entries.forEach(replayEntry);
      endReplay();
      SYNC.seq = Math.max(SYNC.seq, hist.seq || 0);
      if (wasAtBottom) scrollToEnd();
    });
  }).catch(function () {});
}

/* One stream for the whole app: sessions appearing, being renamed, starting and finishing work,
   on any device. */
var listTimer = null;
function openAppEvents() {
  if (SYNC.appEs) SYNC.appEs.close();
  var es = new EventSource("/api/events" + (SYNC.appSeq ? "?after=" + SYNC.appSeq : ""));
  SYNC.appEs = es;
  function note(e) { if (e.lastEventId) SYNC.appSeq = Math.max(SYNC.appSeq, +e.lastEventId || 0); }
  es.addEventListener("sessions", function (e) {
    note(e);
    clearTimeout(listTimer);
    listTimer = setTimeout(function () { refreshSessions().catch(function () {}); }, 250);
  });
  es.addEventListener("status", function (e) {
    note(e);
    try {
      var d = JSON.parse(e.data);
      if (d.id !== S.current) {
        // finished while you were elsewhere: ready for review, as Claude Code marks it
        if (S.status[d.id] === "working" && d.status === "idle") {
          markReady(d.id, true);
          var done = sessionEntry(d.id);
          notify("CodeMonkey finished", (done && done.title) || "A session finished", { anyway: true, open: d.id });
        }
        S.status[d.id] = d.status; renderSessions();
      }
    } catch (err) {}
  });
  es.addEventListener("devices", function (e) {
    note(e);
    // the code and the QR are used up when a device pairs, so the whole panel is drawn again
    [$("remote-body"), document.querySelector(".phonebox")].forEach(function (b) {
      if (b && b.isConnected && b._redraw) b._redraw();
    });
  });
  var settingsTimer = null;
  es.addEventListener("settings", function (e) {   // changed here or on another device
    note(e);
    clearTimeout(settingsTimer);
    settingsTimer = setTimeout(function () {
      api("GET", "/api/settings").then(function (st) { S.settings = st; applyUi(); renderPickers(); })
        .catch(function () {});
      reloadConfig();   // a model server that has started, a key added elsewhere
    }, 300);
  });
  es.addEventListener("plugins", function (e) {   // commands and agents may have changed
    note(e);
    api("GET", "/api/commands").then(function (cmds) { S.commands = cmds; }).catch(function () {});
  });
  es.addEventListener("mcp", function (e) {   // a server connected, failed, changed its tools
    note(e);
    loadMcp().then(function () {
      if (S.page === "mcp" && $("settings-modal").classList.contains("open") && !S.mcpEditing) showPage("mcp");
    });
  });
  es.addEventListener("models", function (e) {
    note(e);
    if (S.page === "models" && $("settings-modal").classList.contains("open") && PAGES.models.reload) PAGES.models.reload();
  });
  es.addEventListener("library", function (e) {
    note(e);
    if (S.page === "library" && $("settings-modal").classList.contains("open") && PAGES.library.reload) PAGES.library.reload();
  });
  es.addEventListener("resync", function () { refreshSessions().catch(function () {}); });
}

function watchConnection() {
  // a stream that has said nothing for well over two keep-alives is dead, whatever it claims
  setInterval(function () {
    if (document.hidden || !S.current || !S.es) return;
    if (S.es.readyState === 2 || Date.now() - SYNC.at > 27000) { setLink(true); reconnect(); }
  }, 5000);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { SYNC.hiddenAt = Date.now(); return; }
    // back in view: a phone may have frozen the page, so do not trust the old connection
    if (SYNC.hiddenAt && Date.now() - SYNC.hiddenAt > 3000) { reconnect(); refreshSessions().catch(function () {}); }
  });
  window.addEventListener("pageshow", function (e) { if (e.persisted) reconnect(); });
  window.addEventListener("online", reconnect);
  window.addEventListener("offline", function () { setLink(true); });
}

/* A partly written answer arrives as its newest words, not the whole of it again every time
   (webui/app.py _on_delta): a long reply used to cost the stream about twenty times its own
   length. The first event of a run, and any after a second of silence, carries the whole text,
   so a missed event cannot leave a hole. Everything below this still sees the whole answer. */
var STREAM = { text: "" };

function tokenData(d) {
  if (d.piece == null) { STREAM.text = d.text || ""; return d; }
  STREAM.text += d.piece;
  return { text: STREAM.text, final: !!d.final };
}

function handleEvent(type, d) {
  switch (type) {
    case "state": applySnapshot(d); break;
    case "user_message":
      // typed on another device (this page already shows its own)
      if (d.client_id && S.sentIds[d.client_id]) { delete S.sentIds[d.client_id]; break; }
      addUserMsg(d.text || "");
      break;
    case "turn_effort":   // Auto's choice for this message (webui/effort.py), said on the working line
      S.turnEffort = d; tickLive(); break;
    case "turn_start":
      setStatus("working");
      S.workStart = Date.now(); S.liveStep = "";
      if (d.note) { newTurn(); addNote(d.note); } else ensureTurn();
      break;
    case "prefill":
      // the model server is still reading what it was sent: say how far it is
      if (d.total > 0 && S.workStart) {
        var fresh = Math.max(1, d.total - d.reused);
        var pct = Math.min(100, Math.round(100 * Math.max(0, d.done - d.reused) / fresh));
        S.liveStep = "Reading the conversation · " + pct + "%" + (d.reused ? " (" + fmtNum(d.reused) + " tokens already read)" : "");
        tickLive();
      }
      break;
    case "token":
      if (S.liveStep && S.liveStep.indexOf("Reading the conversation") === 0) S.liveStep = "";
      d = tokenData(d);
      onToken(d);
      if (typeof CALL !== "undefined" && CALL.on) callOnToken(d);
      break;
    case "claims": onClaims(d); break;
    case "artifact": addArtifactCard(d); break;
    case "activity":
      if (S.snap) { S.snap.activity = [d].concat(S.snap.activity || []).slice(0, 24); }
      break;
    case "tool_call": stepStart(d, true); break;
    case "tool_progress": stepProgress(d); break;
    case "tool_result": stepEnd(d); try { noticeLocalUrl(JSON.stringify(d).slice(0, 20000)); } catch (e) {} break;
    case "permission_request": onPermissionRequest(d); if (typeof CALL !== "undefined" && CALL.on) callOnPermission(); break;
    case "elicitation_request": onElicitation(d); break;
    case "turn_changes": addChangesCard(d); if (S.reviewTurn === d.turn) renderReview(); break;
    case "open_rewind": openRewind(); break;
    case "processes": S.processes = d.items || []; renderBackground(); break;
    case "rewound": if (!S.rewinding) openSession(S.current); break;
    case "context_usage": addContextCard(d); break;
    case "elicitation_resolved": if (S.elicit && S.elicit.request_id === d.request_id) closeElicitation(); break;
    case "permission_resolved":
      closePerm();
      // another question (from a helper, say) may be waiting behind this one
      if (S.current) api("GET", "/api/sessions/" + S.current).then(function (snap) {
        if (snap.id === S.current) applySnapshot(snap);
      }).catch(function () {});
      break;
    case "subagent_progress":
      if (S.subagents[d.id]) { S.subagents[d.id].steps = d.steps; S.subagents[d.id].last = d.summary; renderSubagents(); }
      break;
    case "plan_ready": renderPlan(d.plan, true); break;
    case "plan_decision": resolvePlanCard(d.action); break;
    case "todo_update": S.todos = d.items || []; renderTodos(); break;
    case "subagent_start":
      S.subagents[d.id] = { brief: (d.agent ? d.agent + ": " : "") + d.brief, done: false, started: Date.now() };
      renderSubagents(); break;
    case "subagent_end":
      if (S.subagents[d.id]) { S.subagents[d.id].done = true; S.subagents[d.id].ok = d.ok; S.subagents[d.id].summary = d.summary; }
      renderSubagents(); break;
    case "checkpoint_created":
      if (S.snap) { S.snap.checkpoints = (S.snap.checkpoints || []).concat([d]); renderCheckpoints(S.snap.checkpoints); }
      addNote("Checkpoint saved. You can rewind to it from Activity.");
      break;
    case "context_compacted":
      addNote("Older messages were summarised to save space (" + fmtNum(d.before_chars / 4) + " → " + fmtNum(d.after_chars / 4) + " tokens).");
      break;
    case "rewind_done": addNote(d.report || "Rewound."); break;
    case "notice": addNote(d.text || ""); break;
    case "error": addError(d); break;
    case "browser_view": onBrowserView(d); break;
    case "thinking": onThinking(d); break;
    case "task_suggested":
      addTaskCard(d);
      break;
    case "turn_end":
      if (typeof CALL !== "undefined" && CALL.on) callOnTurnEnd();   // a call says the rest of the reply
      if (paneOpen("file-pane") && $("file-pane").classList.contains("treeview")) loadTree();   // what the turn changed
      S.lastSpeed = d.tokens_per_s || null;
      S.lastReuse = d.reused_pct == null ? null : d.reused_pct;
      S.lastRead = d.read_per_s || null;
      settleThought(S.turn);   // stopped mid-thought: the line stops shimmering
      finishWork(d.elapsed_ms);
      finishReply();
      if (S.turn && S.turn.reply && !(typeof CALL !== "undefined" && CALL.on)) speakReply(S.turn.reply._text || (S.turn.reply.querySelector(".md") || {}).innerText || "");
      if (S.snap) notify("CodeMonkey finished", S.snap.title);
      var ended = S.turn;
      S.turn = null;
      setStatus(S.status[S.current] === "error" ? "error" : "idle");
      var wasLive = !!S.workStart;
      setWorking(false);
      fixTurnEnded(ended);   // Fix it for me: check the fix in the terminal
      refreshSnapshot();
      if (wasLive) fetchSuggestion();
      refreshGit(false);
      break;
    case "settings_changed":
      if (S.snap) { S.snap.settings = d; renderPickers(); updateCtx(); }
      break;
    case "session_end": setStatus("idle"); setWorking(false); break;
  }
}

function setStatus(status) {
  if (!S.current) return;
  S.status[S.current] = status;
  if (S.snap) S.snap.status = status;
  setWorking(status === "working" || status === "permission");
  renderSessions();
}

function refreshSnapshot() {
  var id = S.current;
  if (!id) return Promise.resolve();
  return api("GET", "/api/sessions/" + id).then(function (snap) {
    if (S.current !== id) return;
    var keep = S.status[id];
    S.snap = snap;
    if (keep === "error" && snap.status === "idle") S.status[id] = "error";
    setSessionTitle(snap.title === "New session" ? "" : snap.title);
    var entry = sessionEntry(id);
    if (entry) { entry.title = snap.title; entry.last_active = snap.last_active; }
    renderSessions(); renderPickers(); updateCtx();
    renderCheckpoints(snap.checkpoints || []);
  }).catch(function () {});
}

/* Working state: the stop button replaces send, and one quiet line says what is happening. */
function setWorking(on) {
  $("livebar").classList.toggle("hidden", !on);
  $("cancel-btn").classList.toggle("hidden", !on);
  $("send-btn").classList.toggle("hidden", on);
  $("chat").setAttribute("aria-busy", on ? "true" : "false");
  if (on && !S.workStart) S.workStart = Date.now();
  if (!on) { S.workStart = 0; S.turnEffort = null; }
  pollModelLive(on);
  tickLive();
}
function tickLive() {
  if (S.snap && statusFields().indexOf("time") >= 0) renderStatusline();
  if (!S.workStart) return;
  var waiting = S.status[S.current] === "permission";
  $("live-text").innerHTML = waiting ? "Waiting for your answer" : (S.liveStep || "Thinking");
  // Claude Code's line: the spark, then "41s · 2.9k tokens"
  var toks = S.snap && (S.snap.tokens_out || 0), secs = Math.floor((Date.now() - S.workStart) / 1000);
  $("live-time").textContent = (secs < 60 ? secs + "s" : Math.floor(secs / 60) + "m " + (secs % 60) + "s") +
    (toks ? " · ↓ " + fmtNum(toks) + " tokens" : "") + liveModelText() +
    (S.turnEffort ? " · thinking: " + S.turnEffort.label : "") +
    (S.liveTool === "bash" && !waiting ? " · Ctrl+B to run in background" : "") + " · Esc to stop";
}
setInterval(tickLive, 1000);

/* What a model server on this computer is doing while it answers (webui/modellive.py): reading the
   request, how fast it writes. Asked only while working; a server that does not say gives nothing. */
S.modelLive = null;
function pollModelLive(on) {
  clearTimeout(S.modelLiveTimer);
  if (!on) { S.modelLive = null; return; }
  var provider = effective().provider;
  if (!provider) return;
  api("GET", "/api/models/live?provider=" + encodeURIComponent(provider)).then(function (m) {
    S.modelLive = m && m.state ? m : null;
    if (S.modelLive) S.lastModelLive = S.modelLive;
    tickLive();
  }).catch(function () {}).then(function () {
    if (S.workStart) S.modelLiveTimer = setTimeout(function () { pollModelLive(true); }, 1500);
  });
}
function liveModelText() {
  var m = S.modelLive;
  if (!m) return "";
  if (m.state === "reading" && m.total) {
    return " · reading your request" + (m.read ? " " + Math.round(100 * m.read / m.total) + "%" : "");
  }
  if (m.tok_s) return " · " + Math.round(m.tok_s) + " tokens/s";
  if (m.state === "queued") return " · waiting its turn";
  return "";
}
