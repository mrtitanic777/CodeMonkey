/* A voice call with the model: talk, it answers out loud, it listens again, until you hang up.

   Listening happens here: the microphone at 16 kHz, a simple voice detector (a level well above
   the room's own noise for a moment starts a sentence; most of a second of quiet ends it), and
   what was said goes to the server as a WAV, where whisper.cpp writes it down (webui/speech.py).
   That text is sent as an ordinary message marked as said aloud, so the model answers the way
   people talk, and the transcript keeps it like any other message.

   Speaking starts with the first finished sentence of the reply, not at its end. The voice is
   Kokoro (or Piper) when it is set up: the page plays it, so the microphone's echo cancelling hears
   it and you can talk over it to interrupt. Otherwise the system's voices, which play outside the page,
   so the microphone waits while it talks and a click (or Space) interrupts instead. */

var CALL = {
  on: false, state: "idle", muted: false, stream: null, ctx: null, node: null, src: null,
  noise: 0.006, inSpeech: false, hearing: 0, lastParts: null, loud: 0, quiet: 0, pre: [], chunks: [], level: 0,
  spoken: 0, replying: false, ended: false, playing: null, voice: "system", hearOk: true,
  out: null, endAt: 0, sources: [], making: 0, chain: Promise.resolve(), gen: 0, pending: [], saidAny: false, flushTimer: null,
  el: null
};
var CALL_RATE = 16000, CALL_FRAME = 1024;            // 64 ms a frame

function callStatus(state, text) {
  CALL.state = state;
  if (!CALL.el) return;
  CALL.el.setAttribute("data-state", state);
  var words = { listening: CALL.muted ? "Muted" : "Listening", hearing: "Writing down what you said…",
    thinking: "Thinking…", speaking: "Speaking · talk or click to interrupt", starting: "Connecting…" };
  CALL.el.querySelector(".cstate").textContent = text || words[state] || "";
}

function callHeard(text, who) {
  if (!CALL.el) return;
  var line = CALL.el.querySelector(".cheard");
  line.textContent = text ? (who === "you" ? "You: " : "") + text : "";
}

function startCall() {
  if (CALL.on) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { toastError(new Error("This window has no microphone access.")); return; }
  CALL.on = true; CALL.muted = false; CALL.spoken = 0; CALL.replying = false; CALL.pending = [];
  // the reply's voice plays on its own clock; made here, in the click, so the browser lets it play
  try { CALL.out = CALL.out || new (window.AudioContext || window.webkitAudioContext)(); CALL.out.resume(); } catch (e) {}
  callOverlay();
  callStatus("starting");
  fetch("/api/speech").then(function (r) { return r.json(); }).then(function (s) {
    CALL.voice = s.voice || "system"; CALL.hearOk = !!s.hear;
    if (!s.hear) throw new Error("A call needs hearing set up first: Settings > General > Voice calls and video sound.");
    return navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  }).then(function (stream) {
    if (!CALL.on) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
    CALL.stream = stream;
    var AC = window.AudioContext || window.webkitAudioContext;
    CALL.ctx = new AC({ sampleRate: CALL_RATE });
    CALL.src = CALL.ctx.createMediaStreamSource(stream);
    CALL.node = CALL.ctx.createScriptProcessor(CALL_FRAME, 1, 1);
    CALL.node.onaudioprocess = function (e) { callFrame(new Float32Array(e.inputBuffer.getChannelData(0))); };
    CALL.src.connect(CALL.node);
    CALL.node.connect(CALL.ctx.destination);       // a script node only runs while connected; it writes silence
    return ensureSession().then(function () { callStatus("listening"); });
  }).catch(function (err) {
    var msg = /Permission|NotAllowed/i.test(String(err && err.name) + String(err && err.message))
      ? "The microphone was not allowed." : (err.message || String(err));
    endCall(); toastError(new Error(msg));
  });
}

function endCall() {
  CALL.on = false;
  callStopSpeaking();
  try { if (CALL.node) CALL.node.disconnect(); if (CALL.src) CALL.src.disconnect(); } catch (e) {}
  try { if (CALL.ctx) CALL.ctx.close(); } catch (e) {}
  if (CALL.stream) CALL.stream.getTracks().forEach(function (t) { t.stop(); });
  CALL.stream = CALL.ctx = CALL.node = CALL.src = null;
  CALL.inSpeech = false; CALL.chunks = []; CALL.pre = [];
  if (CALL.el) { CALL.el.remove(); CALL.el = null; }
  var btn = $("call-btn"); if (btn) btn.classList.remove("on");
}

/* ---- hearing ---- */

function callFrame(buf) {
  if (!CALL.on) return;
  var sum = 0;
  for (var i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  var rms = Math.sqrt(sum / buf.length);
  CALL.level = rms;
  if (CALL.el) CALL.el.style.setProperty("--lvl", Math.min(1, rms * 18).toFixed(3));
  if (CALL.muted) return;
  var start = Math.max(0.012, CALL.noise * 3.2);
  if (CALL.state === "speaking") {
    // talking over the reply interrupts it, when the reply plays in the page (echo is cancelled);
    // the system's voices play outside it, so then only a click or Space interrupts
    if (CALL.voice !== "system" && rms > Math.max(start * 2.2, 0.03)) {
      if (++CALL.loud >= 5) { CALL.loud = 0; callInterrupt(); CALL.inSpeech = true; CALL.chunks = CALL.pre.slice(); CALL.quiet = 0; }
    } else CALL.loud = 0;
    CALL.pre.push(buf); if (CALL.pre.length > 6) CALL.pre.shift();
    return;
  }
  if (CALL.state === "thinking" && !CALL.inSpeech) {
    // talking again while it works on the answer: that is a new message, and this answer is dropped
    if (rms > start) { if (++CALL.loud >= 4) { CALL.loud = 0; callInterrupt(); CALL.inSpeech = true; CALL.chunks = CALL.pre.slice(); CALL.quiet = 0; } }
    else CALL.loud = 0;
    CALL.pre.push(buf); if (CALL.pre.length > 6) CALL.pre.shift();
    return;
  }
  if (CALL.state !== "listening" && CALL.state !== "hearing" && !CALL.inSpeech) return;
  if (!CALL.inSpeech) {
    CALL.pre.push(buf); if (CALL.pre.length > 6) CALL.pre.shift();      // ~0.4 s from before the first word
    if (rms > start) {
      if (++CALL.loud >= 2) {
        CALL.inSpeech = true; CALL.quiet = 0; CALL.loud = 0;
        // still being written down: the pause was inside one message, so it goes on from there
        if (CALL.state === "hearing" && CALL.lastParts) { CALL.hearing++; CALL.chunks = CALL.lastParts.concat(CALL.pre); callStatus("listening"); }
        else CALL.chunks = CALL.pre.slice();
      }
    }
    else { CALL.loud = 0; CALL.noise = CALL.noise * 0.97 + rms * 0.03; }   // the room's own level
    return;
  }
  CALL.chunks.push(buf);
  CALL.quiet = rms < start * 0.8 ? CALL.quiet + 1 : 0;
  var seconds = CALL.chunks.length * CALL_FRAME / CALL_RATE;
  if (CALL.quiet >= 17 || seconds > 45) {                                  // ~1.1 s of quiet ends it
    var spoken = CALL.chunks.length - CALL.quiet;
    var parts = CALL.chunks; CALL.chunks = []; CALL.inSpeech = false; CALL.quiet = 0; CALL.pre = [];
    if (spoken * CALL_FRAME / CALL_RATE < 0.35) return;                    // a knock or a cough, not a sentence
    callSend(parts);
  }
}

function callWav(parts) {
  var n = 0; parts.forEach(function (p) { n += p.length; });
  var buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf), o = 0;
  function str(s) { for (var i = 0; i < s.length; i++) v.setUint8(o++, s.charCodeAt(i)); }
  str("RIFF"); v.setUint32(o, 36 + n * 2, true); o += 4; str("WAVEfmt ");
  v.setUint32(o, 16, true); o += 4; v.setUint16(o, 1, true); o += 2; v.setUint16(o, 1, true); o += 2;
  v.setUint32(o, CALL_RATE, true); o += 4; v.setUint32(o, CALL_RATE * 2, true); o += 4;
  v.setUint16(o, 2, true); o += 2; v.setUint16(o, 16, true); o += 2; str("data"); v.setUint32(o, n * 2, true); o += 4;
  parts.forEach(function (p) {
    for (var i = 0; i < p.length; i++, o += 2) { var s = Math.max(-1, Math.min(1, p[i])); v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  });
  return new Blob([buf], { type: "audio/wav" });
}

function callSend(parts) {
  callStatus("hearing");
  CALL.lastParts = parts;
  var mine = ++CALL.hearing;               // a later one (the same message, continued) replaces this one
  ensureSession().then(function (id) {
    return fetch("/api/sessions/" + id + "/hear", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: callWav(parts) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || "HTTP " + r.status); return j; }); })
      .then(function (j) {
        var text = String(j.text || "").trim();
        if (!CALL.on || mine !== CALL.hearing) return;
        CALL.lastParts = null;
        if (!text || /^[\s.,!?-]*$/.test(text)) { callStatus("listening"); return; }
        callHeard(text, "you");
        callStatus("thinking");
        CALL.spoken = 0; CALL.replying = true; CALL.ended = false; CALL.pending = []; CALL.saidAny = false;
        addUserMsg(text);
        var cid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
        S.sentIds[cid] = 1;
        return api("POST", "/api/sessions/" + id + "/message", { text: text, attachments: [], client_id: cid, voice: true });
      });
  }).catch(function (err) { if (CALL.on) { callStatus("listening"); toastError(err); } });
}

/* ---- speaking ---- */

function callSentences(text, final) {
  // the reply's next whole sentences, from where speaking got to; at its end, whatever is left
  text = String(text || "");
  if ((text.match(/```/g) || []).length % 2) text = text.slice(0, text.lastIndexOf("```"));   // code still being written
  var plain = text
    .replace(/```[\s\S]*?```/g, " I put the code in the chat. ")
    .replace(/^\s*\|.*\|\s*$/gm, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "the link in the chat")
    .replace(/\[Removed:[^\]]*\]/g, " ")
    .replace(/[*_#>]+/g, " ")
    .replace(/[ \t]+/g, " ");
  if (plain.length < CALL.spoken) CALL.spoken = 0;     // the reply went on after a step: a new piece of text
  var rest = plain.slice(CALL.spoken), out = [];
  var re = /[^.!?\n]+[.!?]+["')\]]*(\s+|$)/g, m, used = 0;
  while ((m = re.exec(rest))) {
    if (m.index + m[0].length === rest.length && !final && !/\s$/.test(m[0])) break;   // maybe not finished yet
    out.push(m[0].trim()); used = m.index + m[0].length;
  }
  if (final && rest.slice(used).trim()) { out.push(rest.slice(used).trim()); used = rest.length; }
  CALL.spoken += used;
  return out.filter(function (s) { return /[A-Za-z0-9]/.test(s); });
}

/* The reply is said in stretches, not sentence by sentence: a voice made one sentence at a time
   starts each one afresh and sounds clipped. The first sentence goes alone, so speaking starts at
   once; after that, whatever sentences have been written by the time the voice is about to run out
   go together (the model writes much faster than anyone talks, so a stretch is usually two to four
   sentences). The stretches play back to back on one clock (Web Audio), with a short breath
   between them instead of the gap a new audio file would leave. */
var CALL_BREATH = 0.12, CALL_LONGEST = 420;

function callOnToken(d) {
  if (!CALL.on || !CALL.replying) return;
  CALL.pending = CALL.pending.concat(callSentences(d.text || "", false));
  callFlush(false);
}

function callOnTurnEnd() {
  if (!CALL.on || !CALL.replying) return;
  var text = (S.turn && S.turn.reply && S.turn.reply._text) || "";
  CALL.pending = CALL.pending.concat(callSentences(text, true));
  CALL.replying = false; CALL.ended = true;
  callFlush(true);
  callAfterSaying();
}

function callOnPermission() {
  if (CALL.on) { CALL.pending.push("I need your OK on the screen for that."); callFlush(true); }
}

function callAhead() {
  // seconds of speech already made and waiting to be heard, and whether more is being made
  if (CALL.voice === "system") return window.speechSynthesis && window.speechSynthesis.pending ? 9 : 0;
  var left = CALL.out ? Math.max(0, CALL.endAt - CALL.out.currentTime) : 0;
  return CALL.making ? 9 : left;
}

function callFlush(final) {
  if (!CALL.on || !CALL.pending.length) return;
  if (!CALL.saidAny) { CALL.saidAny = true; callSay(CALL.pending.shift()); }
  if (!CALL.pending.length) return;
  if (!final && callAhead() > 1.2) {           // plenty still to hear: let more sentences gather
    clearTimeout(CALL.flushTimer);
    CALL.flushTimer = setTimeout(function () { callFlush(!CALL.replying); }, 250);
    return;
  }
  var stretch = "";
  while (CALL.pending.length && (stretch.length + CALL.pending[0].length < CALL_LONGEST || !stretch)) {
    stretch += (stretch ? " " : "") + CALL.pending.shift();
  }
  callSay(stretch);
  if (CALL.pending.length) callFlush(final);
}

function callSay(text) {
  if (!CALL.on || !text) return;
  callHeard(text, "it");
  if (CALL.voice === "system") {
    if (!window.speechSynthesis) return;
    var u = new SpeechSynthesisUtterance(text);
    var ui = (S.settings && S.settings.ui) || {};
    var chosen = (window.speechSynthesis.getVoices() || []).filter(function (v) { return v.name === ui.voice; })[0];
    if (chosen) u.voice = chosen;
    u.onstart = function () { CALL.playing = u; callStatus("speaking"); VOICE.talking = true; updateFace(); };
    u.onend = u.onerror = function () { CALL.playing = null; callAfterSaying(); };
    window.speechSynthesis.speak(u);
    return;
  }
  // made on the server now; played in order, each starting where the last one ends
  var gen = CALL.gen;
  CALL.making++;
  var ready = fetch("/api/speak", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text }) })
    .then(function (r) { if (!r.ok) throw new Error("voice"); return r.arrayBuffer(); })
    .then(function (bytes) { return CALL.out.decodeAudioData(bytes); });
  CALL.chain = CALL.chain.then(function () { return ready; }).then(function (buf) {
    CALL.making--;
    if (!CALL.on || gen !== CALL.gen) return;
    var src = CALL.out.createBufferSource();
    src.buffer = buf;
    src.connect(CALL.out.destination);
    var now = CALL.out.currentTime;
    var at = CALL.endAt > now ? CALL.endAt + CALL_BREATH : now + 0.02;
    src.start(at);
    CALL.endAt = at + buf.duration;
    CALL.sources.push(src);
    callStatus("speaking"); VOICE.talking = true; updateFace();
    src.onended = function () {
      CALL.sources = CALL.sources.filter(function (s) { return s !== src; });
      callAfterSaying();
    };
  }).catch(function () {
    CALL.making = Math.max(0, CALL.making - 1);
    if (gen === CALL.gen) { CALL.voice = "system"; callSay(text); }   // the voice failed: the system's says it
  });
}

function callAfterSaying() {
  var busy = CALL.voice === "system" ? window.speechSynthesis && window.speechSynthesis.speaking
    : CALL.sources.length || CALL.making || CALL.pending.length;
  if (busy) return;
  VOICE.talking = false; $("face") && $("face").classList.remove("open"); updateFace();
  if (CALL.on) callStatus(CALL.replying ? "thinking" : "listening");
}

function callStopSpeaking() {
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) {}
  CALL.gen++;                                  // anything still being made is dropped when it arrives
  CALL.sources.forEach(function (s) { try { s.stop(); } catch (e) {} });
  CALL.sources = []; CALL.pending = []; CALL.making = 0; CALL.endAt = 0; CALL.playing = null;
  CALL.chain = Promise.resolve();
  clearTimeout(CALL.flushTimer);
  VOICE.talking = false; updateFace();
}

function callInterrupt() {
  // stop talking, and stop the turn if it is still going: what you say next is the new message
  callStopSpeaking();
  if (CALL.replying && S.current) api("POST", "/api/sessions/" + S.current + "/cancel", {}).catch(function () {});
  CALL.replying = false;
  if (CALL.on) callStatus("listening");
}

/* ---- the call's own window ---- */

function callOverlay() {
  if (CALL.el) return;
  var el = h("div", "callbox");
  el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "Voice call");
  el.innerHTML = '<button class="corb" title="Click to interrupt" aria-label="Interrupt"><span></span></button>' +
    '<div class="ctext"><div class="cstate"></div><div class="cheard"></div></div>' +
    '<button class="cbtn cmute" title="Mute the microphone" aria-label="Mute">' +
    '<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/></svg></button>' +
    '<button class="cbtn cend" title="End the call" aria-label="End the call">' +
    '<svg viewBox="0 0 24 24"><path d="M6.6 10.8a15 15 0 006.6 6.6l2.2-2.2a1 1 0 011-.25 11.4 11.4 0 003.6.57 1 1 0 011 1V20a1 1 0 01-1 1A17 17 0 013 4a1 1 0 011-1h3.5a1 1 0 011 1c0 1.25.2 2.45.57 3.57a1 1 0 01-.25 1z"/></svg></button>';
  document.body.appendChild(el);
  CALL.el = el;
  el.querySelector(".cend").onclick = endCall;
  el.querySelector(".corb").onclick = function () { if (CALL.state === "speaking" || CALL.replying) callInterrupt(); };
  el.querySelector(".cmute").onclick = function () {
    CALL.muted = !CALL.muted; CALL.inSpeech = false; CALL.chunks = [];
    this.classList.toggle("on", CALL.muted);
    if (CALL.state === "listening") callStatus("listening");
  };
  var btn = $("call-btn"); if (btn) btn.classList.add("on");
}

document.addEventListener("keydown", function (e) {
  if (!CALL.on) return;
  var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "") || (e.target && e.target.isContentEditable);
  if (e.key === "Escape" && !typing) { e.preventDefault(); endCall(); }
  else if (e.key === " " && !typing && (CALL.state === "speaking" || CALL.replying)) { e.preventDefault(); callInterrupt(); }
});

(function () {
  var btn = $("call-btn");
  if (btn) btn.addEventListener("click", function () { if (CALL.on) endCall(); else startCall(); });
})();
