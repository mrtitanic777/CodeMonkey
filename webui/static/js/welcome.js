"use strict";
/* CodeMonkey page, welcome.js: the first five minutes.
   On the start screen, until it is all done: connect an AI (a key pasted in, or a model on this
   computer), pick a folder, and a first task to try. And when a message is sent with no model
   connected, the same "connect an AI" card instead of an error. */

var SERVICES = [
  { id: "anthropic", name: "Claude", sub: "By Anthropic", keys: "https://console.anthropic.com/settings/keys", prefer: /sonnet/i },
  { id: "openai", name: "OpenAI", sub: "GPT models", keys: "https://platform.openai.com/api-keys", prefer: /^gpt-[0-9.]+$/i },
  { id: "openrouter", name: "OpenRouter", sub: "One key, many companies", keys: "https://openrouter.ai/keys", prefer: /claude-sonnet|gpt-[0-9.]+$/i }
];
var FIRST_TASKS = [
  ["Explain this project", "Look around this project and explain, in plain words, what it does and how it is put together."],
  ["Find a bug and fix it", "Find one real bug in this project, explain it, fix it, and show me how you checked the fix."],
  ["Make a small game", "Make a small game in Python I can play in the terminal: guess the number, with a score."]
];

function modelConnected() {
  return Array.isArray(S.available) && S.available.some(function (g) { return g.models && g.models.length; });
}

/* The model a service's list is best started with: the preferred kind, else the first chat model. */
function pickModel(service, models) {
  var chat = models.filter(function (m) { return !/embed|tts|whisper|audio|realtime|transcribe|image|dall-e|moderation|search|babbage|davinci/i.test(m); });
  return chat.filter(function (m) { return service.prefer.test(m); })[0] || chat[0] || models[0];
}

/* Paste a key, test it, and make the service's best model the default. Calls done() when connected. */
function connectService(service, key, note, done) {
  note.className = "wnote";
  note.textContent = "Checking the key…";
  var hadKey = false;   // a key that fails the check is taken away again, unless one was there before
  api("GET", "/api/setup").then(function (st) { hadKey = !!(st.keys || {})[service.id]; }).catch(function () { hadKey = true; })
    .then(function () { return api("POST", "/api/providers/" + service.id + "/key", { key: key.trim() }); })
    .then(function () { return api("POST", "/api/providers/" + service.id + "/test", {}); })
    .then(function (r) {
      if (!r.ok || !r.models || !r.models.length) throw new Error(r.error || "the key works, but no models came back");
      var model = pickModel(service, r.models);
      return saveSetting({ default_provider: service.id, default_model: model }).then(function () {
        if (S.current) sessionSetting({ provider: service.id, model: model });
        return api("GET", "/api/models/available?fresh=1");
      }).then(function (live) {
        S.available = live;
        toast("Connected to " + service.name + ": using " + model);
        done();
      });
    })
    .catch(function (err) {
      if (!hadKey) api("DELETE", "/api/providers/" + service.id + "/key").catch(function () {});
      note.className = "wnote bad";
      note.textContent = "That didn't work: " + String(err.message || err).replace(/^HTTP \d+:?\s*/, "") +
        ". Check the key and try again.";
    });
}

/* Step 1's body: four choices, and a key box under the one picked. */
function connectChoices(onDone) {
  var wrap = h("div", "wconnect");
  var row = wrap.appendChild(h("div", "wchoices"));
  var form = wrap.appendChild(h("div", "wkey hidden"));
  SERVICES.forEach(function (sv) {
    var b = row.appendChild(h("button", "wchoice"));
    b.type = "button";
    b.appendChild(h("b", "", sv.name));
    b.appendChild(h("span", "", sv.sub));
    b.onclick = function () {
      row.querySelectorAll(".wchoice").forEach(function (x) { x.classList.toggle("on", x === b); });
      form.classList.remove("hidden");
      form.innerHTML = "";
      var line = form.appendChild(h("div", "wkeyrow"));
      var input = line.appendChild(h("input", "field"));
      input.type = "password";
      input.placeholder = "Paste your " + sv.name + " key";
      input.autocomplete = "off";
      input.spellcheck = false;
      var go = line.appendChild(button("Connect", function () {
        if (input.value.trim()) connectService(sv, input.value, note, onDone);
      }, "primary"));
      input.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
      var help = form.appendChild(h("div", "wnote"));
      var link = h("a", "", "Get a key from " + sv.name + " ↗");
      link.href = sv.keys; link.target = "_blank"; link.rel = "noopener";
      help.appendChild(link);
      help.appendChild(document.createTextNode(". It stays on this computer; you pay " + sv.name + " for what you use."));
      var note = form.appendChild(h("div", "wnote"));
      input.focus();
    };
  });
  var local = row.appendChild(h("button", "wchoice"));
  local.type = "button";
  local.appendChild(h("b", "", "On this computer"));
  local.appendChild(h("span", "", "Free, private, needs a strong PC"));
  local.onclick = function () { openModelSetup(); };
  return wrap;
}

/* The start screen's "Get started" card: three steps, each ticked off as it is done. */
function welcomeCard(box) {
  var old = document.querySelector(".welcome");
  if (old) old.remove();
  if (store("cm:welcome-done")) return;
  var connected = modelConnected(), folder = currentFolder();
  var folderSet = !!folder && !(S.config && folder === S.config.chat_dir);
  if (connected && folderSet && S.list && S.list.length) { store("cm:welcome-done", true); return; }

  var card = h("section", "welcome");
  var hd = card.appendChild(h("div", "whd"));
  hd.appendChild(h("b", "", "Get started"));
  hd.appendChild(h("span", "", "Three steps, about a minute."));
  var skip = hd.appendChild(h("button", "iconbtn wskip", "×"));
  skip.type = "button"; skip.title = "Hide this"; skip.setAttribute("aria-label", "Hide Get started");
  skip.onclick = function () { store("cm:welcome-done", true); card.remove(); };

  function step(n, done, title, sub, body) {
    var el = card.appendChild(h("div", "wstep" + (done ? " done" : "")));
    el.appendChild(h("i", "wn", done ? "✓" : String(n)));
    var main = el.appendChild(h("div", "wmain"));
    main.appendChild(h("b", "", title));
    if (sub) main.appendChild(h("span", "wsub", sub));
    if (body && !done) main.appendChild(body);
    return el;
  }
  var model = connected ? ((S.settings && S.settings.default_model) || "a model") : "";
  step(1, connected, connected ? "An AI is connected" : "Connect an AI",
    connected ? "Using " + model + ". Change it any time from the model menu under the message box."
      : "CodeMonkey needs an AI model to talk to. Pick a service and paste its key, or run one on this computer.",
    connectChoices(function () { renderHome(); }));
  var pick = h("div", "btnrow");
  pick.appendChild(button("Choose a folder", function () { openBrowse(""); }, connected ? "primary" : ""));
  step(2, folderSet, folderSet ? "Working in " + folderName(folder) : "Pick a project folder",
    folderSet ? "" : "The folder with your code, or an empty one for something new. CodeMonkey only changes files inside it.",
    pick);
  var tasks = h("div", "wtasks");
  FIRST_TASKS.forEach(function (t) {
    var b = tasks.appendChild(h("button", "wtask", t[0]));
    b.type = "button";
    b.onclick = function () {
      var input = $("input");
      input.value = t[1]; autosize(); input.focus();
    };
  });
  step(3, false, "Try a first task", "Click one to put it in the message box, then press Enter.", tasks);
  box.parentNode.insertBefore(card, box);
}

/* Sending with no model connected: say so plainly, with the way to fix it. */
function noModelCard() {
  var old = document.querySelector(".nomodel");
  if (old) old.remove();
  var card = h("div", "nomodel");
  card.appendChild(h("b", "", "Connect an AI first"));
  card.appendChild(h("p", "", "No AI model is connected yet, so there is nobody to answer. Pick one below; your message is still in the box."));
  card.appendChild(connectChoices(function () { card.remove(); toast("Connected. Press Enter to send your message."); $("input").focus(); }));
  var close = card.appendChild(h("button", "iconbtn wskip", "×"));
  close.type = "button"; close.title = "Close"; close.setAttribute("aria-label", "Close");
  close.onclick = function () { card.remove(); };
  var composer = document.querySelector(".composer");
  composer.parentNode.insertBefore(card, composer);
}
