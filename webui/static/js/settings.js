"use strict";
/* CodeMonkey page, settings.js: Settings.
   The page's scripts share one global scope and load in order (see index.html). */

/* ---------------- settings ---------------- */

/* ---- remote control: opening the app on a phone ----
   One set of controls, shown in Settings and in the /remote-control window. */
function agoWords(seconds) {
  var a = shortAgo(seconds * 1000);
  return a === "now" ? "just now" : a + " ago";
}
function deviceList(box, redraw) {
  api("GET", "/api/devices").then(function (d) {
    box.innerHTML = "";
    if (d.remote) {
      var me = d.devices[0];
      box.appendChild(row("srow", "This device", (me ? me.name + ", paired " + agoWords(me.paired) + ". " : "") +
        "Signing out removes it from the computer's list; pair again to come back.",
        button("Sign out", function () {
          askConfirm("Sign this device out?", "It will need the code from the computer to connect again.", "Sign out", function () {
            api("POST", "/api/devices/signout", {}).then(function () { location.reload(); }).catch(toastError);
          });
        }, "danger")));
      return;
    }
    box.appendChild(h("div", "sechead", "Paired devices"));
    if (!d.devices.length) {
      box.appendChild(h("div", "empty", "No device is paired. This computer's own browser never needs pairing."));
      return;
    }
    d.devices.forEach(function (dev) {
      var active = Date.now() / 1000 - dev.seen < 90 ? "active now" : "last active " + agoWords(dev.seen);
      var line = row("srow devrow", dev.name, active + " · paired " + agoWords(dev.paired) + " · " + dev.address);
      var acts = h("div", "btnrow");
      acts.appendChild(button("Rename", function () {
        askText("Name this device", dev.name, "Save", function (name) {
          api("POST", "/api/devices/" + dev.id + "/rename", { name: name }).then(redraw).catch(toastError);
        });
      }));
      acts.appendChild(button("Disconnect", function () {
        askConfirm("Disconnect " + dev.name + "?", "It is signed out at once and has to pair again. Other devices stay connected.", "Disconnect", function () {
          api("POST", "/api/devices/" + dev.id + "/remove", {}).then(redraw).catch(toastError);
        });
      }, "danger"));
      line.appendChild(acts);
      box.appendChild(line);
    });
  }).catch(function () {});
}

function phoneOptions(box) {
  var devBox = h("div");
  function devices() { if (box.isConnected) deviceList(devBox, devices); }
  // the app's event stream calls this when a device pairs or leaves
  box._redraw = function () { api("GET", "/api/phone").then(draw).catch(function () {}); };
  function draw(st) {
    box.innerHTML = "";
    if (st.remote) { box.appendChild(devBox); devices(); return; }
    var toggle = toggleControl(st.enabled, function (on) {
      api("POST", "/api/phone", { enabled: on }).then(draw).catch(function (err) {
        draw(st);
        box.appendChild(h("div", "empty", "It could not be switched on: " + err.message + "."));
      });
    });
    if (!st.can_switch) toggle.disabled = true;
    box.appendChild(row("srow", "Use from your phone",
      st.enabled ? "On. Your phone must be on the same Wi-Fi as this computer."
        : "Open CodeMonkey on your phone while it is on the same Wi-Fi. Nothing is reachable until the phone is paired.", toggle));
    if (!st.enabled) { box.appendChild(devBox); devices(); return; }
    var card = h("div", "paircard");
    var img = h("img");
    img.src = "/api/phone/qr.svg?" + Date.now();
    img.alt = "QR code that opens CodeMonkey on your phone";
    card.appendChild(img);
    var text = h("div");
    text.appendChild(h("b", "", "Scan this with your phone's camera"));
    text.appendChild(h("i", "", "or open this address in the phone's browser and enter the code:"));
    text.appendChild(h("div", "pairaddr", st.address));
    text.appendChild(h("div", "paircode", st.code.slice(0, 3) + " " + st.code.slice(3)));
    text.appendChild(h("i", "", "The code works once and runs out in " + Math.max(1, Math.round(st.expires_in / 60)) + " minutes. If Windows asks, allow Python on private networks."));
    card.appendChild(text);
    box.appendChild(card);
    box.appendChild(row("srow", "Pair another phone", "Makes a fresh 6-digit code; the one above stops working",
      button("New code", function () { api("POST", "/api/phone/code", {}).then(draw).catch(toastError); })));
    box.appendChild(row("srow", "Copy the pairing link", "The same link the QR code holds, to send to yourself. Anyone who has it can connect, so keep it private.",
      button("Copy link", function (ev) {
        var b = ev.currentTarget;
        copyText(st.link, function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy link"; }, 1200); });
      })));
    box.appendChild(devBox);
    devices();
    box.appendChild(row("srow", "Disconnect every device", "Every paired device is signed out at once and must pair again. The QR code and link change.",
      button("Disconnect all", function () {
        askConfirm("Disconnect every device?", "Each paired device is signed out and has to pair again with a new code.", "Disconnect", function () {
          api("POST", "/api/phone/forget", {}).then(draw).catch(toastError);
        });
      }, "danger")));
  }
  api("GET", "/api/phone").then(draw).catch(function (err) {
    box.innerHTML = "";
    box.appendChild(h("div", "empty", "Remote control can only be set up on the computer itself."));
  });
}
function openRemote() {
  closeMenus();
  $("remote-modal").classList.add("open");
  phoneOptions($("remote-body"));
}

/* ---- themes ----
   A theme is a set of colour tokens and nothing else: the stylesheet takes every colour from
   them. The built-in ones are written out; one of your own is four colours (background, panels,
   text, accent) from which the rest is worked out. */
var THEMES = {
  // the neutral dark grey of Claude Code's desktop app (September 2026)
  warm: { name: "Dark", bg: "#1a1a1a", panel: "#171717", raised: "#262626", hover: "#2c2c2c", text: "#ececec",
    muted: "#a3a3a3", faint: "#787878", accent: "#d97757", menu: "#262626", dialog: "#1f1f1f", nav: "#171717",
    code: "#141414", codeText: "#e6e6e6", primaryText: "#eba58c", scrim: "rgba(0, 0, 0, .6)" },
  midnight: { name: "Midnight", bg: "#000000", panel: "#060607", raised: "#101013", hover: "#19191e", text: "#f2f3f6",
    muted: "#a7aab5", faint: "#74778a", accent: "#7c9cff", menu: "#131317", dialog: "#0a0a0c", nav: "#040405",
    code: "#07080b", codeText: "#e3e7f1", primaryText: "#d5dfff", scrim: "rgba(0, 0, 0, .72)" },
  slate: { name: "Slate", bg: "#12161c", panel: "#171c24", raised: "#1f2630", hover: "#27303c", text: "#e8edf4",
    muted: "#a3aebd", faint: "#76808f", accent: "#4fb3a5", menu: "#232b36", dialog: "#1a2029", nav: "#141920",
    code: "#0c1016", codeText: "#e3e9f2", primaryText: "#c4ece5", scrim: "rgba(5, 8, 12, .66)" },
  paper: { name: "Light", bg: "#faf9f5", panel: "#f0eee6", raised: "#ffffff", hover: "#e8e6dc", text: "#141413",
    muted: "#5e5d59", faint: "#8a8984", accent: "#d97757", menu: "#ffffff", dialog: "#faf9f5", nav: "#f0eee6",
    code: "#f5f4ef", codeText: "#141413", primaryText: "#b0532f", scrim: "rgba(20, 20, 19, .3)" }
};

function hexRgb(hex) {
  var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || "").trim());
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [0, 0, 0];
}
function mix(a, b, t) {   // t = 0 gives a, t = 1 gives b
  var x = hexRgb(a), y = hexRgb(b);
  return "#" + [0, 1, 2].map(function (i) {
    return ("0" + Math.round(x[i] + (y[i] - x[i]) * t).toString(16)).slice(-2);
  }).join("");
}
function luminance(hex) { var c = hexRgb(hex); return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255; }

/* Work a whole theme out of four colours. */
function themeFrom(spec, name) {
  var dark = luminance(spec.bg) < 0.5;
  return {
    name: name, bg: spec.bg, panel: spec.panel, text: spec.text, accent: spec.accent,
    raised: dark ? mix(spec.panel, spec.text, 0.06) : mix(spec.panel, "#ffffff", 0.7),
    hover: mix(spec.panel, spec.text, 0.11),
    muted: mix(spec.text, spec.bg, 0.3), faint: mix(spec.text, spec.bg, 0.5),
    menu: mix(spec.panel, spec.text, 0.09), dialog: mix(spec.bg, spec.panel, 0.7), nav: mix(spec.bg, spec.panel, 0.3),
    code: dark ? mix(spec.bg, "#000814", 0.5) : "#1b1f27", codeText: "#e6e9f0",
    primaryText: mix(spec.accent, spec.text, 0.6),
    scrim: dark ? "rgba(0, 0, 0, .68)" : "rgba(30, 25, 15, .38)"
  };
}

function themeById(id) {
  if (id === "system") {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? THEMES.paper : THEMES.warm;
  }
  var custom = (S.settings && S.settings.ui && S.settings.ui.custom_themes) || {};
  if (id && id.indexOf("custom:") === 0 && custom[id.slice(7)]) return themeFrom(custom[id.slice(7)], id.slice(7));
  return THEMES[id] || THEMES.warm;
}

function themeVars(t) {
  var tint = hexRgb(t.text), acc = hexRgb(t.accent);
  return {
    "--bg": t.bg, "--panel": t.panel, "--raised": t.raised, "--hover": t.hover, "--text": t.text,
    "--muted": t.muted, "--faint": t.faint, "--accent": t.accent, "--accent-rgb": acc.join(", "),
    "--tint": tint.join(", "), "--menu": t.menu, "--dialog": t.dialog, "--dialog-nav": t.nav,
    "--code-bg": t.code, "--code-text": t.codeText, "--primary-text": t.primaryText, "--scrim": t.scrim,
    "--on-accent": luminance(t.accent) > 0.55 ? "#14110d" : "#ffffff",
    "color-scheme": luminance(t.bg) < 0.5 ? "dark" : "light"
  };
}

function applyTheme(t) {
  var vars = themeVars(t), el = document.documentElement;
  Object.keys(vars).forEach(function (k) { el.style.setProperty(k, vars[k]); });
  el.toggleAttribute("data-light", luminance(t.bg) >= 0.5);   // a few colours differ on a light page
  store("cm:theme-vars", vars);   // read back before the first paint next time
  redrawDiagrams();   // diagrams take the new colours too
}

/* Fonts: the app's text, the replies, and code, each chosen in Settings > General > Appearance.
   All are bundled (webui/static/fonts), so nothing is fetched from the internet. */
var FONTS = {
  ui: { system: ["System (Segoe UI)", ""], inter: ["Inter", '"Inter Var"'], geist: ["Geist", '"Geist Var"'],
    plex: ["IBM Plex Sans", '"IBM Plex Sans Var"'], manrope: ["Manrope", '"Manrope Var"'] },
  reply: { same: ["Same as the app", ""], serif: ["Serif (Source Serif)", '"Source Serif Var"'] },
  code: { system: ["System (Cascadia Mono)", ""], jetbrains: ["JetBrains Mono", '"JetBrains Mono Var"'],
    "geist-mono": ["Geist Mono", '"Geist Mono Var"'], "plex-mono": ["IBM Plex Mono", '"IBM Plex Mono"'] }
};
var SYSTEM_UI = '"Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, sans-serif';
var SYSTEM_MONO = '"Cascadia Mono", "Cascadia Code", Consolas, ui-monospace, "SF Mono", Menlo, monospace';
function applyFonts(ui) {
  var el = document.documentElement, vars = {};
  var pick = function (kind, id, fallback) { var f = FONTS[kind][id] || FONTS[kind][fallback]; return f[1]; };
  var uiFam = pick("ui", ui.font_ui, "system"), code = pick("code", ui.font_code, "system"), reply = pick("reply", ui.font_reply, "same");
  vars["--ui"] = uiFam ? uiFam + ", " + SYSTEM_UI : "";
  vars["--display"] = uiFam ? uiFam + ", " + SYSTEM_UI : "";
  vars["--mono"] = code ? code + ", " + SYSTEM_MONO : "";
  vars["--reply-font"] = reply ? reply + ", Georgia, serif" : "";
  Object.keys(vars).forEach(function (k) { if (vars[k]) el.style.setProperty(k, vars[k]); else el.style.removeProperty(k); });
  store("cm:font-vars", vars);   // read back before the first paint next time, like the theme
}
function fontSelect(kind, current, key) {
  return selectControl(Object.keys(FONTS[kind]).map(function (id) { return [id, FONTS[kind][id][0]]; }), current,
    function (v) { saveUi(key, v).then(function () { applyUi(); }); });
}

/* Glass (blurred, see-through panels): on, off, or auto, which leaves it off on a small computer
   (a Raspberry Pi, an old laptop), where the blur would make scrolling stutter. */
function glassWanted(ui) {
  var mode = ui.glass || "auto";
  if (mode !== "auto") return mode === "on";
  var cores = navigator.hardwareConcurrency || 8, mem = navigator.deviceMemory || 8;
  return cores > 4 && mem > 4 && !/arm|aarch/i.test(navigator.userAgent || "");
}

/* A highlight that glides to the chosen item (Settings' sections, the session list). The list may be
   drawn again from scratch, so where it last stood is kept on the list and it glides on from there. */
function glide(box, item) {
  if (!box) return;
  var g = box.querySelector(":scope > .glide");
  box.classList.add("glidebox");
  if (!item || !item.offsetParent) { if (g) g.classList.add("gone"); return; }
  var b = box.getBoundingClientRect(), r = item.getBoundingClientRect();
  var to = { x: r.left - b.left + box.scrollLeft, y: r.top - b.top + box.scrollTop, w: r.width, h: r.height };
  function place(el, p) { el.style.transform = "translate(" + p.x + "px," + p.y + "px)"; el.style.width = p.w + "px"; el.style.height = p.h + "px"; }
  if (!g) {
    g = h("div", "glide");
    box.insertBefore(g, box.firstChild);
    if (box._glide) {   // drawn again: start where it was, then move
      g.style.transition = "none";
      place(g, box._glide);
      void g.offsetWidth;
      g.style.transition = "";
    }
  }
  g.classList.remove("gone");
  place(g, to);
  box._glide = to;
}

function applyUi() {
  var ui = (S.settings && S.settings.ui) || {};
  document.documentElement.setAttribute("data-size", ui.text_size || "comfortable");
  document.documentElement.classList.toggle("calm", !!ui.reduce_motion);
  document.documentElement.classList.toggle("noglass", !glassWanted(ui));
  applyTheme(themeById(ui.theme || "warm"));
  applyFonts(ui);
}

var themeEditing = null;
function editTheme(name) {
  var custom = (S.settings.ui.custom_themes || {}), base = themeById(S.settings.ui.theme || "warm");
  var spec = name && custom[name] ? custom[name] : { bg: base.bg, panel: base.panel, text: base.text, accent: base.accent };
  themeEditing = name || null;
  $("theme-head").textContent = name ? "Change “" + name + "”" : "Your theme";
  $("theme-name").value = name || "";
  $("theme-name").disabled = !!name;
  ["bg", "panel", "text", "accent"].forEach(function (k) { $("theme-" + k).value = mix(spec[k], spec[k], 0); });
  $("theme-delete").classList.toggle("hidden", !name);
  $("theme-modal").classList.add("open");
}
function themeDraft() {
  return { bg: $("theme-bg").value, panel: $("theme-panel").value, text: $("theme-text").value, accent: $("theme-accent").value };
}
function saveCustomThemes(custom, themeId) {
  var ui = {};
  Object.keys(S.settings.ui).forEach(function (k) { ui[k] = S.settings.ui[k]; });
  ui.custom_themes = custom;
  ui.theme = themeId;
  return saveSetting({ ui: ui }).then(function () {
    closeDialog("theme-modal");
    if ($("settings-modal").classList.contains("open")) showPage("general");
  });
}
function saveSetting(patch) {
  return api("POST", "/api/settings", patch).then(function (st) {
    S.settings = st;
    applyUi(); renderPickers(); updateCtx();
    return st;
  }).catch(function (err) { toastError(err); });
}
function saveUi(key, value) {
  var ui = {};
  Object.keys(S.settings.ui || {}).forEach(function (k) { ui[k] = S.settings.ui[k]; });
  ui[key] = value;
  return saveSetting({ ui: ui });
}

var hwTimer = null;
function openSettings(page) {
  closeMenus();
  $("settings-modal").classList.add("open");
  $("settings-search").value = "";
  api("GET", "/api/settings").then(function (st) { S.settings = st; showPage(page || S.page); })
    .catch(function () { showPage(page || S.page); });
}
function closeSettings() {
  closeDialog("settings-modal");
  clearInterval(hwTimer);
}

// a beginner sees four sections; the rest wait behind "More settings" (remembered per browser)
function showAllSettings(on) {
  $("settings-nav").classList.toggle("all", on);
  $("settings-more-btn").setAttribute("aria-expanded", on ? "true" : "false");
  $("settings-more-btn").textContent = on ? "Fewer settings" : "More settings";
  try { localStorage.setItem("cm:allsettings", on ? "1" : ""); } catch (e) {}
}

function showPage(name) {
  S.page = PAGES[name] ? name : "general";
  clearInterval(hwTimer);
  if ($("settings-more").querySelector('[data-page="' + S.page + '"]')) showAllSettings(true);
  Array.prototype.forEach.call($("settings-nav").querySelectorAll(".dn"), function (b) {
    b.classList.toggle("on", b.getAttribute("data-page") === S.page);
    // on a narrow screen the sections are a row of tabs: bring the open one into view
    if (b.classList.contains("on") && b.scrollIntoView) b.scrollIntoView({ block: "nearest", inline: "center" });
  });
  var pane = $("settings-pane");
  pane.innerHTML = "";
  PAGES[S.page](pane);
  pane.parentNode.scrollTop = 0;
  glide($("settings-nav"), $("settings-nav").querySelector(".dn.on"));
}
function pageHead(pane, title, sub) {
  pane.appendChild(h("h2", "", title));
  if (sub) pane.appendChild(h("p", "pgsub", sub));
}

var PAGES = {};
var PAGE_WORDS = {
  general: "general default model reasoning effort activity panel appearance text size motion",
  models: "models servers provider test connection default hosted local",
  permissions: "permissions ask allow never edit run read helpers guard guess kernel labels commands always risky delete dangerous",
  usage: "usage tokens tasks cost rates spend time",
  context: "context budget memory remember forget summarise",
  commands: "commands slash keyboard shortcuts",
  library: "library artifacts pages documents reports made files",
  people: "people person colleague client contact notes about someone names",
  ideas: "ideas suggestions next todo fixme failing tests",
  goals: "goals progress milestones working toward",
  skills: "skills playbooks proposals learned workflows",
  review: "review lessons failures mistakes what went wrong daily reflection",
  scheduled: "scheduled jobs cron recurring timetable run history watch remind",
  plugins: "plugins install marketplace bundles extensions",
  worktrees: "worktrees branches parallel merge discard isolated copies",
  agents: "agents subagents helpers roles custom tools",
  hooks: "hooks events before after tool turn prompt session script matcher",
  mcp: "mcp servers tool connectors sign in oauth",
  hardware: "hardware memory graphics processor ram gpu limit stop",
  advanced: "advanced settings file workspace code intelligence language close all reset danger"
};

PAGES.general = function (pane) {
  pageHead(pane, "General");
  beginnerSettings(pane);
  var st = S.settings;
  var modelSel = h("select", "sel");
  pane.appendChild(row("srow", "Default model", "Used for every new session. Only models that are running are listed",
    modelSel));
  function fillModels() {
    var choices = liveChoices({ provider: st.default_provider, model: st.default_model });
    modelSel.innerHTML = "";
    choices.forEach(function (c, i) {
      var o = h("option", "", c.model + (c.live ? "" : " (not reachable)"));
      o.value = String(i);
      if (c.provider === st.default_provider && c.model === st.default_model) o.selected = true;
      modelSel.appendChild(o);
    });
    modelSel.onchange = function () {
      var c = choices[+modelSel.value];
      saveSetting({ default_provider: c.provider, default_model: c.model });
    };
  }
  fillModels();
  api("GET", "/api/models/available").then(function (live) { S.available = live; fillModels(); }).catch(function () {});
  var efforts = S.config.efforts || {};
  pane.appendChild(row("srow", "Reasoning effort", "How long the model may think before it answers",
    selectControl(Object.keys(efforts).map(function (k) { return [k, efforts[k].label]; }), st.effort,
      function (v) { saveSetting({ effort: v }); })));
  var phoneBox = h("div", "phonebox");
  pane.appendChild(phoneBox);
  phoneOptions(phoneBox);
  pane.appendChild(row("srow", "Tell me when it needs me", "A desktop notification when it asks a question or finishes while this window is in the background",
    toggleControl(st.ui.notify, function (on) {
      if (on && window.Notification && Notification.permission === "default") Notification.requestPermission();
      saveUi("notify", on);
    })));
  pane.appendChild(row("srow", "Show the answer as it is written", "Words appear as the model produces them, and Stop works mid-answer",
    toggleControl(st.stream, function (on) { saveSetting({ stream: on }); })));
  var styleSel = h("select", "sel");
  pane.appendChild(row("srow", "Reply style", "How replies are written in new sessions. Change one session's from the menu beside its title", styleSel));
  api("GET", "/api/styles?project_dir=" + encodeURIComponent(currentFolder() || "")).then(function (list) {
    list.forEach(function (one) {
      var o = h("option", "", one.name.charAt(0).toUpperCase() + one.name.slice(1));
      o.value = one.name; styleSel.appendChild(o);
    });
    styleSel.value = st.output_style || "default";
  }).catch(function () {});
  styleSel.onchange = function () { saveSetting({ output_style: styleSel.value }); };
  pane.appendChild(row("srow", "Prompt suggestions", "After a reply, a likely next message waits in grey in the empty box: Tab or → takes it. Each one is a small extra request to the model",
    toggleControl(!st.ui || st.ui.prompt_suggestions !== false, function (on) { saveUi("prompt_suggestions", on); if (!on) setSuggestion(""); })));
  pane.appendChild(h("h3", "", "Persona"));
  var personaBtn = button("Edit", function () {
    api("GET", "/api/persona").then(function (p) {
      askLongText("How the agent works with you", "Plain instructions it follows in every session, before anything else.", p.persona, function (text) {
        api("POST", "/api/persona", { persona: text }).catch(toastError);
      });
    }).catch(toastError);
  });
  var userBtn = button("Edit", function () {
    api("GET", "/api/persona").then(function (p) {
      askLongText("About you", "Anything you want it to know from the start: what to call you, what you work on, what to avoid. It is yours to write; the agent does not edit this.", p.user, function (text) {
        api("POST", "/api/persona", { user: text }).catch(toastError);
      });
    }).catch(toastError);
  });
  pane.appendChild(row("srow", "How the agent works with you", "Its manner and rules, in your words", personaBtn));
  pane.appendChild(row("srow", "About you", "What it should know about you from the first message", userBtn));
  pane.appendChild(h("h3", "", "Appearance"));
  var themeOptions = [["system", "Follow Windows (light or dark)"]].concat(Object.keys(THEMES).map(function (id) { return [id, THEMES[id].name]; }))
    .concat(Object.keys(st.ui.custom_themes || {}).map(function (n) { return ["custom:" + n, n]; }));
  pane.appendChild(row("srow", "Theme", "The colours of the whole app",
    selectControl(themeOptions, st.ui.theme || "warm", function (v) { saveUi("theme", v).then(function () { showPage("general"); }); })));
  var mine = (st.ui.theme || "").indexOf("custom:") === 0 ? st.ui.theme.slice(7) : null;
  pane.appendChild(row("srow", "Your own themes", "Four colours make a theme; the rest is worked out. Saved with your settings.",
    buttonRow(button("New theme", function () { editTheme(null); }), button("Change", function () { editTheme(mine); }))));
  pane.lastChild.querySelectorAll(".btn")[1].disabled = !mine;
  pane.appendChild(row("srow", "Font", "The app's text: menus, settings and the conversation",
    fontSelect("ui", st.ui.font_ui || "system", "font_ui")));
  pane.appendChild(row("srow", "Reply font", "The agent's replies. A serif reads like a page",
    fontSelect("reply", st.ui.font_reply || "same", "font_reply")));
  pane.appendChild(row("srow", "Code font", "Code, diffs, paths and the terminal",
    fontSelect("code", st.ui.font_code || "system", "font_code")));
  var sample = pane.appendChild(h("div", "fontsample"));
  sample.appendChild(h("p", "fs-ui", "Menus and settings read like this."));
  sample.appendChild(h("p", "fs-reply", "Replies read like this: plain sentences about your code."));
  sample.appendChild(h("code", "fs-code", "const retry = (fn, n = 3) => fn().catch(() => retry(fn, n - 1));"));
  pane.appendChild(row("srow", "Text size", "Conversation and code",
    selectControl([["compact", "Compact"], ["comfortable", "Comfortable"], ["large", "Large"]], st.ui.text_size,
      function (v) { saveUi("text_size", v); })));
  pane.appendChild(row("srow", "Reduce motion", "Turn off the working pulse and transitions",
    toggleControl(st.ui.reduce_motion, function (on) { saveUi("reduce_motion", on); })));
  pane.appendChild(row("srow", "Glass", "See-through, blurred panels. Automatic leaves it off on small computers like a Raspberry Pi",
    selectControl([["auto", "Automatic"], ["on", "On"], ["off", "Off"]], st.ui.glass || "auto",
      function (v) { saveUi("glass", v); })));
  pane.appendChild(h("h3", "", "Presence and voice"));
  pane.appendChild(row("srow", "Show the face", "A small face beside the conversation's title shows whether the agent is idle, working, waiting for your answer or stopped on an error",
    toggleControl(st.ui.presence !== false, function (on) { saveUi("presence", on); })));
  var canSpeak = !!window.speechSynthesis;
  var speakToggle = toggleControl(!!st.ui.speak && canSpeak, function (on) { saveUi("speak", on); if (!on) stopSpeaking(); });
  speakToggle.disabled = !canSpeak;
  pane.appendChild(row("srow", "Read replies aloud", canSpeak
    ? "Uses the voices already on this computer. Code and tables are skipped. Click the face to stop it."
    : "This browser has no speech voices.", speakToggle));
  if (canSpeak) {
    var voices = (window.speechSynthesis.getVoices() || []).map(function (v) { return [v.name, v.name + " (" + v.lang + ")"]; });
    if (voices.length) {
      pane.appendChild(row("srow", "Voice", "From this computer's installed voices",
        selectControl([["", "The system's default"]].concat(voices), st.ui.voice || "", function (v) { saveUi("voice", v); }),
        button("Try it", function () {
          var was = S.settings.ui.speak;
          S.settings.ui.speak = true;
          speakReply("This is how I will read my replies to you.");
          S.settings.ui.speak = was;
        })));
    }
  }
  pane.appendChild(h("h3", "", "Status line"));
  pane.appendChild(h("p", "pgsub", "A quiet row under the message box. Pick what it shows; your own command can add one more " +
    "piece (it gets the session as JSON on its input and the first line it prints is shown)."));
  var chosen = statusFields().slice();
  STATUS_FIELDS.forEach(function (f) {
    pane.appendChild(row("srow", f[1], "", toggleControl(chosen.indexOf(f[0]) >= 0, function (on) {
      var next = statusFields().filter(function (x) { return x !== f[0]; });
      if (on) next.push(f[0]);
      next.sort(function (a, b) {
        return STATUS_FIELDS.map(function (x) { return x[0]; }).indexOf(a) - STATUS_FIELDS.map(function (x) { return x[0]; }).indexOf(b);
      });
      saveUi("statusline", next).then(renderStatusline);
    })));
  });
  var cmd = textField("e.g. git log -1 --format=%s   (empty: none)");
  cmd.value = st.statusline_command || "";
  pane.appendChild(row("srow", "Your own command", "Runs in the session's folder at most every few seconds, for 3 seconds at most",
    cmd, button("Save", function () { saveSetting({ statusline_command: cmd.value.trim() }); })));
  pane.appendChild(row("srow", "Run it every", "",
    selectControl([[5, "5 seconds"], [10, "10 seconds"], [30, "30 seconds"], [60, "minute"]], (st.ui && st.ui.statusline_seconds) || 10,
      function (v) { saveUi("statusline_seconds", +v); })));
  pane.appendChild(h("h3", "", "Right now"));
  var c = contextParts();
  pane.appendChild(usageRow("Context used", fmtNum(c.used) + " of " + fmtNum(c.budget) + " tokens in this session", c.pct));
  var slot = h("div");
  pane.appendChild(slot);
  liveMeters(slot);
};

function reloadConfig(page) {
  return api("GET", "/api/config").then(function (c) { S.config = c; renderPickers(); if (page) showPage(page); });
}
function textField(placeholder, type) {
  var f = h("input", "field");
  f.type = type || "text";
  f.placeholder = placeholder;
  f.autocomplete = "off";
  f.spellcheck = false;
  return f;
}
/* Where saved keys go on this system: Windows Credential Manager, the macOS Keychain, the system keyring. */
function keyStore() {
  return (S.config && S.config.key_store) || "the system's key store";
}
function keyText(p) {
  var k = p.key || {};
  if (k.source === "env") return "key from the " + k.env + " environment variable " + k.hint;
  if (k.source === "stored") return "key saved in " + keyStore() + " " + k.hint;
  return p.needs_key || p.custom ? "needs an API key" : "no key needed";
}
function keyForm(name, p) {
  var box = h("div", "addrow hidden");
  var input = textField("Paste the API key", "password");
  var save = button("Save key", function () {
    save.disabled = true;
    api("POST", "/api/providers/" + encodeURIComponent(name) + "/key", { key: input.value }).then(function () {
      input.value = "";
      return reloadConfig("models");
    }).catch(function (err) { save.disabled = false; toastError(err); });
  });
  input.onkeydown = function (e) { if (e.key === "Enter") save.click(); };
  box.appendChild(input);
  box.appendChild(save);
  if (p.key && p.key.source === "stored") {
    box.appendChild(button("Remove key", function () {
      api("DELETE", "/api/providers/" + encodeURIComponent(name) + "/key").then(function () {
        return reloadConfig("models");
      }).catch(toastError);
    }));
  }
  return box;
}

/* What a provider is called on screen; its own id stays in config.yaml. */
var PROVIDER_NAMES = { anthropic: "Claude (Anthropic)", openai: "OpenAI", openrouter: "OpenRouter", ollama: "Ollama",
  lmstudio: "LM Studio", kessie: "KeSSie", vllm: "vLLM" };
function providerName(id) { return PROVIDER_NAMES[id] || id; }

PAGES.models = function (pane) {
  pageHead(pane, "Models", "The model servers CodeMonkey can talk to: models on this computer, and AI services with an API key.");
  var guide = h("button", "btn", "Set up a model");
  guide.type = "button";
  guide.onclick = function () { closeSettings(); openModelSetup(); };
  pane.appendChild(row("srow", "New to this?", "A step-by-step guide: what is on this computer, which model fits it, and how to start it", guide));
  var st = S.settings, provs = S.config.providers || {}, states = [];
  Object.keys(provs).forEach(function (name) {
    var p = provs[name];
    var keys = keyForm(name, p);
    var keyBtn = button(p.key && p.key.source ? "Key" : "Add key", function () {
      keys.classList.toggle("hidden");
      if (!keys.classList.contains("hidden")) keys.querySelector("input").focus();
    });
    if (!(p.needs_key || p.custom)) keyBtn = null;
    var removeBtn = p.custom ? button("Remove", function () {
      if (!confirm("Remove " + name + " and its saved key?")) return;
      api("DELETE", "/api/providers/" + encodeURIComponent(name)).then(function () {
        return reloadConfig("models");
      }).catch(toastError);
    }) : null;
    // its state, found out when the page opens (no Test button): running with its models, off, or
    // waiting for a key. Clicking a running server's state lists its models.
    var isDefault = name === st.default_provider;
    var tag = h("button", "tag", needsKey(p) ? "Needs a key" : "Checking\u2026");
    tag.type = "button";
    var sub = h("div", "sub-models hidden");
    states.push(function (live) {
      if (needsKey(p)) return;
      var found = live.filter(function (g) { return g.provider === name; })[0];
      if (!found && p.starting === "starting") { tag.className = "tag off"; tag.textContent = "Starting…"; tag.title = "Started when CodeMonkey opened; it answers once its model has loaded"; return; }
      if (!found) { tag.className = "tag off"; tag.textContent = "Off"; tag.title = "Not running: nothing answered at " + (p.base_url || ""); return; }
      tag.className = "tag ok"; tag.textContent = "Running \u00b7 " + found.models.length + (found.models.length === 1 ? " model" : " models");
      tag.title = "Show its models";
      found.models.forEach(function (model) {
        var on = isDefault && model === st.default_model;
        var use = h("button", "mini", on ? "Default" : "Use");
        use.disabled = on;
        use.onclick = function () {
          saveSetting({ default_provider: name, default_model: model }).then(function () {
            return sessionSetting({ provider: name, model: model });
          }).then(function () { showPage("models"); });
        };
        sub.appendChild(row("mrow", model, "", use));
      });
      tag.onclick = function () { sub.classList.toggle("hidden"); };
    });
    // a server added here can start with CodeMonkey: the command that starts it (webui/autostart.py)
    var startBtn = p.custom ? button(p.start_command ? "Starts with CodeMonkey" : "Start with CodeMonkey", function () {
      askLongText("Start " + providerName(name) + " with CodeMonkey",
        "The command that starts it, as you would type it in a terminal. When CodeMonkey opens and " + providerName(name) +
        " isn't running yet, CodeMonkey runs this in the background; it keeps running after CodeMonkey closes. Leave it empty to stop.",
        p.start_command || "", function (command) {
          api("POST", "/api/providers/" + encodeURIComponent(name) + "/start", { command: command }).then(function () {
            toast(command.trim() ? providerName(name) + " will start with CodeMonkey" : providerName(name) + " won't start with CodeMonkey");
            return reloadConfig("models");
          }).catch(toastError);
        });
    }) : null;
    if (startBtn && p.start_command) startBtn.title = p.start_command;
    pane.appendChild(row("mrow", providerName(name),
      (isDefault ? "Default \u00b7 " : "") + (p.base_url || "address from the top of config.yaml") + " \u00b7 " + keyText(p) + (p.vision ? " \u00b7 sees images" : "") +
        (p.start_command ? " \u00b7 starts with CodeMonkey" : ""),
      needsKey(p) ? null : tag, buttonRow(startBtn, keyBtn, removeBtn)));   // "Add key" already says it
    pane.appendChild(keys);
    pane.appendChild(sub);
  });
  var knownBefore = JSON.stringify(S.available || null);
  api("GET", "/api/models/available?fresh=1").then(function (live) {
    S.available = live;
    // the pickers below were drawn from what was known before: draw the page again if that changed
    if (JSON.stringify(live) !== knownBefore && S.page === "models" && pane.isConnected) { showPage("models"); return; }
    states.forEach(function (f) { f(live); });
  }).catch(function () { states.forEach(function (f) { f([]); }); });
  pane.appendChild(h("p", "fine", "Servers come from config.yaml, plus any you add below. Keys are kept in " + keyStore() + ", never in a file; an environment variable, when set, is used instead."));

  // ---- adding a server: any OpenAI-compatible address, or a Claude API proxy
  pane.appendChild(h("h3", "", "Add a model server"));
  pane.appendChild(h("p", "pgsub", "Any service that speaks the OpenAI chat API (OpenRouter, Groq, Together, Mistral, a vLLM box...), with the model names you want to use."));
  var nName = textField("Name, such as openrouter"), nUrl = textField("Address, such as https://openrouter.ai/api/v1");
  var nModels = textField("Model names, separated by commas"), nKey = textField("API key (optional)", "password");
  var nKind = selectControl([["openai", "OpenAI-compatible"], ["anthropic", "Claude API (or a proxy for it)"]], "openai", function () {});
  var nVision = selectControl([["", "Text only"], ["1", "Can view images"]], "", function () {});
  var add = button("Add server", function () {
    add.disabled = true;
    api("POST", "/api/providers", { name: nName.value, base_url: nUrl.value, models: nModels.value, client: nKind.value,
                                    key: nKey.value, vision: !!nVision.value })
      .then(function () { return reloadConfig("models"); })
      .catch(function (err) { add.disabled = false; toastError(err); });
  });
  var form = h("div", "addform");
  [nName, nUrl, nModels, nKey].forEach(function (f) { form.appendChild(f); });
  form.appendChild(buttonRow(nKind, nVision, add));
  pane.appendChild(form);

  // ---- when the model in use does not answer
  pane.appendChild(h("h3", "", "If the model does not answer"));
  pane.appendChild(h("p", "pgsub", "A request is tried again twice. If the model still does not answer, the next one in this list takes over in the same turn, and you are told."));
  var chain = (st.fallback_models || []).slice();
  if (!chain.length) pane.appendChild(h("div", "empty", "No fallback set: the turn stops with an error when the model stays silent."));
  chain.forEach(function (ref, i) {
    pane.appendChild(row("mrow", (i + 1) + ". " + ref, "", button("Remove", function () {
      saveSetting({ fallback_models: chain.filter(function (r) { return r !== ref; }) }).then(function () { showPage("models"); });
    })));
  });
  var choices = liveRefs([]).filter(function (ref) {
    return chain.indexOf(ref) < 0 && ref !== st.default_provider + "/" + st.default_model;
  }).map(function (ref) { return [ref, ref]; });
  if (choices.length && chain.length < 4) {
    pane.appendChild(row("srow", "Add a fallback", "A model from the servers above",
      selectControl([["", "Choose a model"]].concat(choices), "", function (v) {
        if (v) saveSetting({ fallback_models: chain.concat([v]) }).then(function () { showPage("models"); });
      })));
  }

  // ---- models that run on this computer
  pane.appendChild(h("h3", "", "Models for this computer"));
  var local = h("div", "localmodels");
  pane.appendChild(local);
  var VERDICT = { gpu: ["ok", "Fits the graphics card"], split: ["ok", "Card plus memory"], cpu: ["", "Runs from memory"],
                  slow: ["warn", "Loads, but slow"], no: ["bad", "Does not run here"], unknown: ["", "Unknown"] };
  function gb(bytes) { return (bytes / 1e9).toFixed(bytes < 1e9 ? 2 : 1) + " GB"; }
  function act(path, body) {
    return api("POST", path, body || {}).then(draw).catch(function (err) { toastError(err); loadLocal(); });
  }
  function draw(v) {
    if (!local.isConnected) return;
    local.innerHTML = "";
    var hw = v.hardware;
    local.appendChild(h("p", "pgsub",
      (hw.gpu ? hw.gpu + " with " + hw.vram_gb + " GB" : "No graphics card found") + " · " + (hw.ram_gb || "?") + " GB of memory · " +
      (hw.disk_free_gb == null ? "?" : hw.disk_free_gb) + " GB free where models are kept. What follows is worked out from sizes: an estimate until it is run."));
    var run = v.running || {};
    if (run.state && run.state !== "stopped") {
      var word = { loading: "Loading", ready: "Running", asleep: "Asleep", exited: "Stopped by itself" }[run.state] || run.state;
      var runRow = row("mrow", word + ": " + (run.name || ""),
        run.state === "exited" ? "It exited with code " + run.exit + ". The last lines of its log are below."
          : run.state === "asleep" ? "It unloaded itself after sitting idle, and the graphics card and memory are free. It loads again on the next message."
          : "Answering at 127.0.0.1:" + v.port + " as the server named “local”.",
        h("span", "tag " + (run.state === "ready" ? "ok" : run.state === "exited" ? "bad" : ""), word),
        button("Stop", function () { act("/api/models/stop"); }));
      local.appendChild(runRow);
      if (run.state === "exited" && run.log) local.appendChild(h("pre", "logtail", run.log));
    }
    if (run.router && run.state !== "exited") {
      var EACH = { loaded: ["ok", "Loaded"], loading: ["", "Loading"], sleeping: ["", "Asleep"], unloaded: ["", "Not loaded"], failed: ["bad", "Failed to load"] };
      (run.models || []).forEach(function (id) {
        var st2 = (run.each || {})[id] || "unloaded";
        var known = v.models.filter(function (m) { return m.id === id; })[0];
        var b = st2 === "loaded" || st2 === "sleeping"
          ? button("Unload", function () { act("/api/models/unload", { id: id }); })
          : button("Load", function () { act("/api/models/load", { id: id }); });
        if (st2 === "loading") b.disabled = true;
        local.appendChild(row("mrow", known ? known.name + " · " + known.quant : id,
          "Asked for as local/" + id + ". It loads by itself when a message is sent to it.", h("span", "tag " + EACH[st2][0], EACH[st2][1]), b));
      });
      (run.left_out || []).forEach(function (text) { local.appendChild(h("div", "empty", "Left out: " + text + ".")); });
    }
    var stockInstalled = v.models.filter(function (m) { return m.installed && m.runner === "stock" && m.verdict !== "no"; }).length;
    if ((!run.state || run.state === "stopped") && stockInstalled >= 2) {
      var all = button("Start all", function () { act("/api/models/start-all"); });
      if (!v.runner_found) all.disabled = true;
      local.appendChild(row("srow", "Switch between installed models",
        "One server for the " + stockInstalled + " installed models the ordinary llama.cpp can run. It keeps one loaded and swaps when a message names another. Only one fits this graphics card at a time, so a swap takes as long as loading a model." +
        (v.runner_found ? "" : " Needs llama-server, which is not installed."), all));
    }
    if (run.state === "stopped" && run.why) local.appendChild(h("div", "empty", run.why));
    var dl = v.download || {};
    v.models.forEach(function (m) {
      var tag = VERDICT[m.verdict] || VERDICT.unknown;
      var lines = [m.how].concat(m.why || []);
      if (m.runner === "fork") lines.push("Note: " + m.runner_note + ".");
      else if (m.runner_note) lines.push("Runner: " + m.runner_note + ".");
      if (m.measured) lines.push("Measured here: " + m.measured + ".");
      if (m.reported) lines.push("Reported, not measured here: " + m.reported + ".");
      (m.results || []).slice(0, 1).forEach(function (r) { lines.push("Checked on this computer " + r.day + ": " + r.summary + "."); });
      if (m.advice) lines.push(m.advice);
      if (m.issues && m.issues.length) lines.push("Known issues (from its publisher, as of " + m.issues_as_of + "; CodeMonkey works around the ones it can):\n· " + m.issues.join("\n· "));
      var title = m.name + " · " + m.quant + " · " + m.size_gb + " GB";
      var buttons = [];
      var mine = dl.id === m.id ? dl : null;
      if (mine && mine.state === "downloading") {
        lines.push("Downloading: " + gb(mine.done) + " of " + gb(mine.total) + (mine.speed ? " at " + (mine.speed / 1e6).toFixed(1) + " MB a second" : "") + ".");
        buttons.push(button("Pause", function () { act("/api/models/download/cancel"); }));
      } else if (mine && mine.state === "checking") {
        lines.push("Checking the file against the publisher's checksum.");
      } else {
        if (mine && mine.state === "failed") lines.push("The download stopped: " + mine.error + " Press Download to carry on.");
        if (m.installed) {
          lines.push("On this computer: " + m.path);
          if (m.verdict !== "no" && m.verdict !== "unknown" && !(run.state === "loading" || run.state === "ready")) {
            var start = button("Start", function () { act("/api/models/start", { id: m.id }); });
            if (!m.runner_ready) { start.disabled = true; lines.push((m.runner === "fork" ? "PrismML's build of llama.cpp" : "llama-server") + " is not installed, so it cannot be started from here."); }
            buttons.push(start);
          }
        } else if (m.can_download && m.verdict !== "no") {
          var word2 = m.partial_gb ? "Carry on (" + m.partial_gb + " GB so far)" : "Download";
          var get = button(word2, function () {
            askConfirm("Download " + m.file + "?", m.size_gb + " GB from " + m.source + ". It is saved in " + v.folder + " and checked against the publisher's checksum. You can pause it and carry on later.",
              "Download", function () { act("/api/models/download", { id: m.id }); });
          });
          if (!m.enough_disk) { get.disabled = true; lines.push("Not enough free disk for this file."); }
          if (dl.state === "downloading") get.disabled = true;
          buttons.push(get);
        }
      }
      if (m.command) buttons.push(button("Copy command", function (ev) {
        var b = ev.currentTarget;
        copyText(m.command, function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy command"; }, 1200); });
      }));
      var card = row("mrow modelrow", title, lines.join("\n"), h("span", "tag " + tag[0], tag[1]));
      if (buttons.length) card.appendChild(buttonRow.apply(null, buttons));
      local.appendChild(card);
    });
    local.appendChild(h("p", "fine", "Nothing is downloaded or started until you press the button, and a download shows the file, where it comes from and its size first. Figures marked as reported are other people's; only “measured here” was measured on this computer."));
  }
  function loadLocal() { api("GET", "/api/models").then(draw).catch(function () {}); }

  // ---- which model does what
  pane.appendChild(h("h3", "", "Which model does what"));
  pane.appendChild(h("p", "pgsub", "Optional. Left alone, one model does everything. A second model only helps if it is different in a way that matters; and if both are local, switching between them costs a reload each time, because only one fits the graphics card."));
  var every = [["", "The main model"]].concat(liveRefs([st.helper_model, st.escalate_model]).map(function (r) { return [r, r]; }));
  pane.appendChild(row("srow", "Helpers and background work", "Helpers, scheduled jobs, loops and the look back over a conversation. A smaller, faster model suits these.",
    selectControl(every, st.helper_model || "", function (v) { saveSetting({ helper_model: v }); })));
  pane.appendChild(row("srow", "When the main model goes round in circles", "After three refused changes to one file, or the same call made three times with the same result, this model takes over the rest of the turn. You are told when it happens.",
    selectControl([["", "Nothing: keep the same model"]].concat(every.slice(1)), st.escalate_model || "", function (v) { saveSetting({ escalate_model: v }); })));

  pane.appendChild(row("srow", "Unload a model that sits idle", "A model started from here frees the graphics card and memory after this long with nothing to do, and loads again on the next message. Applies the next time one is started.",
    selectControl([[0, "Never"], [5, "After 5 minutes"], [15, "After 15 minutes"], [60, "After an hour"]],
      st.model_sleep_minutes == null ? 15 : st.model_sleep_minutes, function (v) { saveSetting({ model_sleep_minutes: +v }); })));

  // ---- the check: what a model does on this computer
  pane.appendChild(h("h3", "", "Check a model on this computer"));
  pane.appendChild(h("p", "pgsub", "A fixed set of probes: speed, six tool calls, six small functions run against tests the model never sees, and two questions about things that are not there. It takes a few minutes, uses the model for all of it, and never runs while the agent is working. The functions a model writes are run in an empty temporary folder."));
  var benchBox = h("div");
  pane.appendChild(benchBox);
  var picked = st.default_provider + "/" + st.default_model;
  function drawBench(b) {
    if (!benchBox.isConnected) return;
    benchBox.innerHTML = "";
    var all = liveRefs([picked]).map(function (r) { return [r, r]; });
    var tr = b.trial || {};
    if (tr.running) {
      benchBox.appendChild(row("srow", "Agent trial of " + tr.model, tr.step + "…",
        button("Stop", function () { api("POST", "/api/trial/stop", {}).then(drawBench).catch(toastError); })));
    } else if (b.state.running) {
      benchBox.appendChild(row("srow", "Checking " + b.state.model, b.state.step + "…",
        button("Stop", function () { api("POST", "/api/bench/stop", {}).then(drawBench).catch(toastError); })));
    } else {
      var sel = selectControl(all, picked, function (v) { picked = v; });
      var go = button("Run the check", function () {
        var at = picked.indexOf("/");
        api("POST", "/api/bench", { provider: picked.slice(0, at), model: picked.slice(at + 1) }).then(drawBench).catch(toastError);
      });
      benchBox.appendChild(row("srow", "Model to check", "It must be loaded and answering", sel, go));
      function trial(compare) {
        var at = picked.indexOf("/");
        api("POST", "/api/trial", { provider: picked.slice(0, at), model: picked.slice(at + 1), compare: compare }).then(drawBench).catch(toastError);
      }
      benchBox.appendChild(row("srow", "Agent trial", "Five real tasks through the whole of CodeMonkey, each in its own temporary folder and scored by tests the model never sees: fix a bug, add a function, change two files, answer a question, report a test run. It may only read, edit and run the tests there. Compare does every task twice, with the checks on and off, to measure what they catch.",
        buttonRow(button("Run the trial", function () { trial(false); }), button("Compare", function () { trial(true); }))));
    }
    if (!b.results.length) benchBox.appendChild(h("div", "empty", "Nothing has been checked on this computer yet."));
    b.results.slice(0, 8).forEach(function (r) {
      var failed = [];
      ["tools", "code", "candour"].forEach(function (k) {
        (r.details[k] || []).forEach(function (t) { if (!t.ok) failed.push(t.task + ": " + t.why); });
      });
      Object.keys(r.details.trial || {}).forEach(function (label) {
        (r.details.trial[label] || []).forEach(function (t) {
          if (!t.solved) failed.push(t.task + " (" + label + "): " + t.why + (t.false_done ? ", and it said it was done" : ""));
        });
      });
      var sub = r.day + " · " + r.machine + " · took " + Math.round(r.seconds) + " s" + (failed.length ? "\nMissed: " + failed.slice(0, 6).join("; ") : "");
      benchBox.appendChild(row("mrow modelrow", r.model + ": " + r.summary, sub));
    });
  }
  function loadBench() { api("GET", "/api/bench").then(drawBench).catch(function () {}); }
  PAGES.models.reload = function () { if (local.isConnected) { loadLocal(); loadBench(); } };
  loadLocal();
  loadBench();
};

PAGES.permissions = function (pane) {
  pageHead(pane, "Permissions", "What the agent may do without asking you first.");
  var st = S.settings;
  pane.appendChild(row("srow", "Default mode", "Applies to new sessions. Change the open session from the composer.",
    segControl(MODE_ORDER.map(function (k) { return [k, k === "bypass" ? "Bypass" : PERM_LABEL[k][0]]; }), st.permission_mode,
      function (v) { saveSetting({ permission_mode: v }); }, "w")));
  pane.appendChild(h("h3", "", "By kind of action"));
  var subs = {
    read: "Looking never changes anything",
    edit: "Changes are logged, and Rewind undoes them back to the last checkpoint",
    run: "Tests, builds, anything in a terminal",
    helpers: "A second copy of the agent, working in the background under these same rules",
    network: "Searching, reading pages and driving a browser"
  };
  var names = { read: "Read and search files", edit: "Edit files", run: "Run commands", helpers: "Start helpers", network: "Use the web" };
  Object.keys(names).forEach(function (kind) {
    pane.appendChild(row("srow", names[kind], subs[kind],
      segControl([["ask", "Ask"], ["allow", "Allow"], ["never", "Never"]], st.action_permissions[kind], function (v) {
        var perms = {};
        Object.keys(S.settings.action_permissions).forEach(function (k) { perms[k] = S.settings.action_permissions[k]; });
        perms[kind] = v;
        saveSetting({ action_permissions: perms });
      }, "w")));
  });
  // ---- rules: patterns for particular commands, files and sites
  pane.appendChild(h("h3", "", "Rules"));
  pane.appendChild(h("p", "pgsub", "For particular commands, files and sites. Deny refuses without asking, in every mode; " +
    "Ask always asks, even in Auto; Allow runs without asking. Deny wins over Ask, Ask over Allow. " +
    "Examples: bash(npm test:*) · bash(rm -rf*) · read(**/.env) · edit(src/**) · web_fetch(domain:github.com) · mcp__github__*"));
  var pr = st.permission_rules || { allow: [], ask: [], deny: [] };
  var KIND_NAME = { deny: "Deny", ask: "Ask", allow: "Allow" };
  var any = false;
  ["deny", "ask", "allow"].forEach(function (kind) {
    (pr[kind] || []).forEach(function (rule) {
      any = true;
      var rm = h("button", "mini", "Remove");
      rm.onclick = function () {
        var next = { allow: (pr.allow || []).slice(), ask: (pr.ask || []).slice(), deny: (pr.deny || []).slice() };
        next[kind] = next[kind].filter(function (r) { return r !== rule; });
        saveSetting({ permission_rules: next }).then(function () { showPage("permissions"); });
      };
      pane.appendChild(row("mrow", codeTitle(rule), "", h("span", "tag " + (kind === "deny" ? "bad" : kind === "allow" ? "ok" : "warn"), KIND_NAME[kind]), rm));
    });
  });
  if (!any) pane.appendChild(h("div", "empty", "No rules yet."));
  var ruleKind = selectControl([["deny", "Deny"], ["ask", "Ask"], ["allow", "Allow"]], "deny", function () {});
  var ruleText = textField("Tool(pattern), e.g. bash(git push:*)");
  var addRule = button("Add rule", function () {
    var text = ruleText.value.trim();
    if (!/^[A-Za-z0-9_*?.\-]+(\(.+\))?$/.test(text)) { toastError(new Error("Write Tool or Tool(pattern), e.g. bash(npm test:*) or read(**/.env)")); return; }
    var next = { allow: (pr.allow || []).slice(), ask: (pr.ask || []).slice(), deny: (pr.deny || []).slice() };
    if (next[ruleKind.value].indexOf(text) < 0) next[ruleKind.value].push(text);
    saveSetting({ permission_rules: next }).then(function () { showPage("permissions"); });
  });
  ruleText.onkeydown = function (e) { if (e.key === "Enter") addRule.click(); };
  var addRow = h("div", "addrow");
  [ruleKind, ruleText, addRule].forEach(function (x) { addRow.appendChild(x); });
  pane.appendChild(addRow);
  // try one: what would happen to this call here
  var tTool = selectControl([["bash", "Command"], ["read", "Read a file"], ["edit", "Edit a file"], ["web_fetch", "Open a page"]], "bash", function () {});
  var tValue = textField("e.g. git push origin main, or src/app.py");
  var tOut = h("p", "fine", "");
  var tryIt = button("Check", function () {
    api("POST", "/api/permissions/check", { tool: tTool.value, value: tValue.value, project_dir: currentFolder() || "" }).then(function (r) {
      tOut.textContent = r.decision
        ? KIND_NAME[r.decision] + ", by the rule " + r.rule + (r.from_project ? " (from the project's .codemonkey/settings.json)" : "")
        : "No rule matches: the mode and the settings above decide.";
    }).catch(toastError);
  });
  var tryRow = h("div", "addrow");
  [tTool, tValue, tryIt].forEach(function (x) { tryRow.appendChild(x); });
  pane.appendChild(tryRow);
  pane.appendChild(tOut);
  pane.appendChild(h("p", "fine", "A project can bring its own rules in .codemonkey/settings.json (shared) or settings.local.json (yours), " +
    "as {\"permissions\": {\"allow\": [...], \"ask\": [...], \"deny\": [...]}}. They apply on top of these."));

  pane.appendChild(h("h3", "", "Command sandbox"));
  pane.appendChild(h("p", "pgsub", "Guard rails around the commands the agent runs. Guarded refuses commands that would write outside " +
    "the project or do lasting damage (wiping a disk, sudo, download-and-run, force-push). Strict adds: no network commands, " +
    "no secrets such as API keys in the command's environment, web traffic sent nowhere, and on Windows a limit of 64 processes " +
    "and 4 GB between them. It is not a full operating-system sandbox: for code you do not trust at all, use a virtual machine."));
  pane.appendChild(row("srow", "Level", "Applies to every session, right away",
    segControl([["off", "Off"], ["guarded", "Guarded"], ["strict", "Strict"]], st.sandbox || "off",
      function (v) { saveSetting({ sandbox: v }); }, "w")));

  pane.appendChild(row("srow", "Stop before risky commands",
    "Deleting a folder, throwing away uncommitted work, force-pushing, wiping a disk: CodeMonkey asks first, in every mode, Never asks too",
    toggleControl(st.risky_check !== "off", function (on) { saveSetting({ risky_check: on ? "on" : "off" }); })));
  pane.appendChild(h("h3", "", "Guard"));
  pane.appendChild(h("p", "pgsub",
    "CodeMonkey checks every action against what the agent has actually seen in the session. Changing a file it never read, or running a command that names a file that does not exist, is a guess."));
  pane.appendChild(row("srow", "When the agent acts on a guess", "Applies in every mode, including Auto",
    segControl([["ask", "Ask me"], ["block", "Refuse it"], ["off", "Off"]], st.guard, function (v) { saveSetting({ guard: v }); }, "w")));
  pane.appendChild(row("srow", "Show what each reply is based on", "Marks the files, results and names in a reply as checked, seen, guessed or not found",
    toggleControl(st.claim_labels, function (on) { saveSetting({ claim_labels: on }); })));
  pane.appendChild(h("h3", "", "Checks on the agent's work"));
  pane.appendChild(h("p", "pgsub",
    "None of these use the model. They stop a small model passing off things that are not there: code that calls what does not exist, results nobody produced, work that was not done."));
  pane.appendChild(row("srow", "Code gate", "A change that does not parse, or uses a name that does not exist in your project, is refused before it is written. Strict also undoes a change that breaks a test that passed before.",
    segControl([["check", "Check"], ["strict", "Strict"], ["off", "Off"]], st.code_gate, function (v) { saveSetting({ code_gate: v }); }, "w")));
  pane.appendChild(row("srow", "Run the tests again after each change", "Only a test command you already allowed in the session, and only when it takes under half a minute",
    toggleControl(st.gate_tests, function (on) { saveSetting({ gate_tests: on }); })));
  pane.appendChild(row("srow", "Check C and C++ against your SDKs", "Every include and call is looked up in the headers installed on this computer (Xbox 360 XDK, Windows SDK, MSVC, DirectX, GCC), for the language version the project builds with, and reported.",
    toggleControl(st.code_knowledge, function (on) { saveSetting({ code_knowledge: on }); })));
  pane.appendChild(row("srow", "Only names that exist", "A C, C++ or assembly change that uses a function, method, type, constant, header or instruction that neither your SDKs nor your project has is refused before it is written. Checked by the target's own compiler where it is installed.",
    toggleControl(st.code_grounded, function (on) { saveSetting({ code_grounded: on }); })));
  pane.appendChild(row("srow", "Compile C and C++ after each change", "With the project's own compiler where it is installed (the Xbox 360 compiler for a 360 project). Syntax only: nothing is built.",
    toggleControl(st.code_compile_check, function (on) { saveSetting({ code_compile_check: on }); })));
  pane.appendChild(row("srow", "Reply gate", "A reply that reports a result no command produced, or work that was not done, is sent back once. What is still unbacked is taken out, and the reply says so.",
    toggleControl(st.reply_gate === "enforce", function (on) { saveSetting({ reply_gate: on ? "enforce" : "off" }); })));
  pane.appendChild(row("srow", "Working set", "Each round the model is shown the task in your words, what it has changed, whether the tests are out of date, and the real signatures of the functions it is working on. Old output and stale readings are kept out.",
    toggleControl(st.working_set, function (on) { saveSetting({ working_set: on }); })));
  pane.appendChild(row("srow", "Task brief", "When your message names a function or class that exists in the project, its source, its callers and the tests that use it are found and sent with the message, so the model does not spend its first rounds looking for them.",
    toggleControl(st.task_brief !== false, function (on) { saveSetting({ task_brief: on }); })));
  pane.appendChild(row("srow", "Think more only when stuck", "The model starts each round thinking briefly. It is given more room after a failing test or a refused change, up to the effort you chose, and goes back to brief once the tests pass.",
    toggleControl(st.adaptive_thinking !== false, function (on) { saveSetting({ adaptive_thinking: on }); })));
  pane.appendChild(row("srow", "Model's context window", "How much the model can take in at once. As it fills, older tool output is replaced by one line, then the oldest steps are dropped. Automatic asks a local model server; if it does not say, 32K is assumed.",
    selectControl([[0, "Automatic"], [4096, "4K tokens"], [8192, "8K"], [16384, "16K"], [32768, "32K"], [65536, "64K"], [131072, "128K"], [262144, "256K"], [524288, "512K"], [1048576, "1M"]],
      st.context_window || 0, function (v) { saveSetting({ context_window: +v }); })));
  pane.appendChild(h("h3", "", "Commands you always allow"));
  var rules = st.always_commands || [];
  if (!rules.length) pane.appendChild(h("div", "empty", "None yet. Choose “Always allow” when the agent asks to run a command."));
  rules.forEach(function (rule) {
    var rm = h("button", "mini", "Remove");
    rm.onclick = function () {
      saveSetting({ always_commands: S.settings.always_commands.filter(function (r) {
        return !(r.cmd === rule.cmd && r.project_dir === rule.project_dir);
      }) }).then(function () { showPage("permissions"); });
    };
    pane.appendChild(row("mrow", codeTitle(rule.cmd), "Added " + rule.added + " in " + baseName(rule.project_dir), rm));
  });
};

PAGES.hardware = function (pane) {
  pageHead(pane, "Hardware", "What this computer is using right now, and when CodeMonkey should stop.");
  var live = h("div");
  pane.appendChild(live);
  pane.appendChild(row("srow", "Update the meters", "How often the bars above are refreshed while this page is open",
    selectControl([[0.5, "Twice a second"], [1, "Every second"], [2, "Every 2 seconds"], [5, "Every 5 seconds"]],
      (S.settings.ui && S.settings.ui.meter_seconds) || 1,
      function (v) { saveUi("meter_seconds", +v).then(function () { showPage("hardware"); }); })));
  pane.appendChild(h("h3", "", "Limits"));
  pane.appendChild(row("srow", "Stop the agent when free memory falls below",
    "It stops at its next step and tells you. A command that is already running is not killed.",
    selectControl([[0, "Off"], [1, "1 GB"], [2, "2 GB"], [4, "4 GB"], [8, "8 GB"]], S.settings.min_free_ram_gb,
      function (v) { saveSetting({ min_free_ram_gb: +v }); })));
  var stops = h("p", "fine");
  pane.appendChild(stops);
  liveMeters(live, function (hw) {
    stops.textContent = hw.stops ? "It has stopped the agent " + hw.stops + (hw.stops === 1 ? " time" : " times") + " since CodeMonkey started." : "It has not had to stop anything since CodeMonkey started.";
  });
};

/* Tokens a day: one bar a day, read and written together; hover a day for the split. A quiet day
   is a zero on the baseline, not a gap. The same numbers are there as a table. */
function drawUsageChart(box, days, table) {
  box.innerHTML = "";
  box.classList.toggle("astable", table);
  var fmtDay = function (iso, long) {
    var d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString(undefined, long ? { weekday: "long", month: "short", day: "numeric" } : { month: "short", day: "numeric" });
  };
  if (table) {
    var t = box.appendChild(h("table", "utable"));
    t.innerHTML = "<thead><tr><th>Day</th><th>Read</th><th>Written</th><th>Tasks</th></tr></thead>";
    var tb = t.appendChild(h("tbody"));
    days.slice().reverse().forEach(function (d) {
      var tr = tb.appendChild(h("tr"));
      [fmtDay(d.day, true), fmtNum(d["in"]), fmtNum(d.out), String(d.tasks)].forEach(function (c) { tr.appendChild(h("td", "", c)); });
    });
    return;
  }
  var max = Math.max.apply(null, days.map(function (d) { return d["in"] + d.out; }).concat([1]));
  // a round top for the scale: 1, 2 or 5 times a power of ten
  var mag = Math.pow(10, Math.floor(Math.log10(max))), top = [1, 2, 5, 10].map(function (m) { return m * mag; }).filter(function (v) { return v >= max; })[0];
  var plot = box.appendChild(h("div", "ucplot"));
  [1, 0.5].forEach(function (f) {
    var g = plot.appendChild(h("div", "ucgrid"));
    g.style.bottom = (f * 100) + "%";
    g.appendChild(h("span", "", fmtNum(top * f)));
  });
  var bars = plot.appendChild(h("div", "ucbars")), tip = box.appendChild(h("div", "uctip hidden"));
  days.forEach(function (d, i) {
    var total = d["in"] + d.out;
    var col = bars.appendChild(h("div", "ucol"));
    var bar = col.appendChild(h("i", total ? "" : "zero"));
    bar.style.height = total ? Math.max(1.5, total / top * 100) + "%" : "0";
    col.setAttribute("aria-label", fmtDay(d.day, true) + ": " + fmtNum(total) + " tokens");
    col.onmouseenter = function () {
      tip.innerHTML = "";
      tip.appendChild(h("b", "", fmtDay(d.day, true)));
      [["Total", fmtNum(total)], ["Read", fmtNum(d["in"])], ["Written", fmtNum(d.out)], ["Tasks", String(d.tasks)]].forEach(function (r) {
        var row = tip.appendChild(h("div", "uctr"));
        row.appendChild(h("span", "", r[0]));
        row.appendChild(h("em", "", r[1]));
      });
      tip.classList.remove("hidden");
      var bx = box.getBoundingClientRect(), cx = col.getBoundingClientRect();
      var left = cx.left - bx.left + cx.width / 2 - tip.offsetWidth / 2;
      tip.style.left = Math.max(0, Math.min(bx.width - tip.offsetWidth, left)) + "px";
      bars.classList.add("hovering"); col.classList.add("on");
    };
    col.onmouseleave = function () { tip.classList.add("hidden"); bars.classList.remove("hovering"); col.classList.remove("on"); };
  });
  var axis = box.appendChild(h("div", "ucaxis"));
  days.forEach(function (d, i) {
    axis.appendChild(h("span", "", i === 0 || i === 7 || i === days.length - 1 ? (i === days.length - 1 ? "Today" : fmtDay(d.day)) : ""));
  });
}

/* What's new: each version's changes in plain words, newest first. */
var WHATS_NEW = [
  ["0.0.3", "November 2026", [
    ["Leaner", "Parts of CodeMonkey nothing used any more are gone, and the README has new pictures."],
    ["CODEMONKEY_API_KEY", "The environment variable for a default key has CodeMonkey's own name now (the old name still works)."]
  ]],
  ["0.0.2", "November 2026", [
    ["Get started", "A card on the start screen: connect an AI by pasting a Claude, OpenAI or OpenRouter key (it is checked before it is kept), or run one on this computer; pick a folder; try a first task."],
    ["Claude works out of the box", "The installed app talks to Claude without any extra package."],
    ["Undo, in reach", "Every reply that changed files has an Undo button, and Go back to here under your own messages rewinds the conversation, and the files if you like."],
    ["Careful with risky commands", "Deleting a folder, throwing away uncommitted work, force-pushing or running a downloaded script always asks first, even in Never asks, and says why. Settings > Permissions can turn it off."],
    ["Smaller things", "Model servers have the names you know, the terminal says what it is for, and sending with no AI connected shows how to connect one instead of an error."],
    ["Fixed", "After a change, re-running the tests could report the old code's result when the file kept its size."]
  ]],
  ["0.0.1", "November 2026", [
    ["The first public release", "CodeMonkey is now open source, under the Unlicense: anyone can use it, change it and share it."],
    ["Fix it for me", "When something you run in the terminal fails, a card says what went wrong in plain words and points at the line. Press Fix it: CodeMonkey fixes the code, runs it again to check, and tells you whether it works now."],
    ["Honest replies", "Each answer says how much of it was checked against your files, and what was a guess."],
    ["Beginner mode", "For someone who has never programmed: plain words, one small step at a time, programming words underlined (tap one to see what it means), and easy questions answered with buttons."],
    ["Terminal, files and git beside the chat", "Run commands, browse the project, see what changed, commit, push and open pull requests."],
    ["Set up a model", "A step-by-step guide finds what is on this computer and picks a model that fits it, or connect any OpenAI-compatible service or Anthropic's API. Keys stay on your computer."],
    ["A calm, glassy look", "White buttons, a softly moving background behind see-through panels, light and dark themes, and your own keyboard shortcuts."],
    ["Windows, Linux and Raspberry Pi", "An installer for Windows that brings everything it needs, and builds for Linux (x64) and the Raspberry Pi (64-bit and 32-bit)."]
  ]]
];
PAGES.whatsnew = function (pane) {
  pageHead(pane, "What's new", "CodeMonkey " + ((S.config && S.config.version) || "") + ". What changed, version by version.");
  updatesBox(pane);
  WHATS_NEW.forEach(function (v) {
    var head = pane.appendChild(h("div", "wnhd"));
    head.appendChild(h("h3", "", "Version " + v[0]));
    head.appendChild(h("span", "", v[1]));
    var list = pane.appendChild(h("div", "wnlist"));
    v[2].forEach(function (item) {
      var row = list.appendChild(h("div", "wnitem"));
      row.appendChild(h("b", "", item[0]));
      row.appendChild(h("p", "", item[1]));
    });
  });
};
/* Once after an update (not on a first install, where the walkthrough runs): a note that says so. */
function noteUpdate() {
  var v = S.config && S.config.version, seen = store("cm:seen-version");
  if (!v) return;
  store("cm:seen-version", v);
  if (seen === v || HASH.session) return;
  if (!seen && !store("cm:toured")) return;   // a first install: the walkthrough is the welcome
  var pill = h("div", "wnpill");
  pill.setAttribute("role", "status");
  pill.appendChild(h("span", "", "Updated to " + v));
  var see = pill.appendChild(h("button", "btn link", "See what's new"));
  see.type = "button";
  see.onclick = function () { pill.remove(); openSettings("whatsnew"); };
  var x = pill.appendChild(h("button", "iconbtn", ""));
  x.type = "button"; x.title = "Dismiss"; x.setAttribute("aria-label", "Dismiss");
  x.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  x.onclick = function () { pill.remove(); };
  document.body.appendChild(pill);
}

/* ---- a newer CodeMonkey: asked of the link whoever built this copy set (webui/updates.py) ---- */
function updateText(u) {
  if (u.error) return "Could not check: " + u.error + ".";
  if (!u.checked_at) return u.url ? "Not checked yet." : "No update link is set, so CodeMonkey cannot look for new versions.";
  var when = " Checked " + new Date(u.checked_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) + ".";
  if (u.newer) return "Version " + u.latest + " is available." + when;
  return "You have the latest version." + when;
}
function getUpdate(u) {
  if (u.download) window.open(u.download, "_blank", "noopener");
}
/* Once a version is out: a pill saying so (until it is dismissed for that version). */
function noteNewVersion() {
  api("GET", "/api/updates").then(function (u) {
    if (!u.newer || store("cm:skip-update") === u.latest || document.querySelector(".wnpill")) return;
    var pill = h("div", "wnpill");
    pill.setAttribute("role", "status");
    pill.appendChild(h("span", "", "CodeMonkey " + u.latest + " is available"));
    var get = pill.appendChild(h("button", "btn link", u.download ? "Download" : "See what's new"));
    get.type = "button";
    get.onclick = function () { pill.remove(); if (u.download) getUpdate(u); else openSettings("whatsnew"); };
    var x = pill.appendChild(h("button", "iconbtn", ""));
    x.type = "button"; x.title = "Not now"; x.setAttribute("aria-label", "Not now");
    x.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    x.onclick = function () { store("cm:skip-update", u.latest); pill.remove(); };
    document.body.appendChild(pill);
  }).catch(function () {});
}
function updatesBox(pane) {
  var box = pane.appendChild(h("div", "updbox"));
  var status = h("i", "", "…"), actions = h("div", "btnrow");
  var head = row("srow", "CodeMonkey " + ((S.config && S.config.version) || ""), null, actions);
  head.querySelector("div").appendChild(status);
  box.appendChild(head);
  var check = button("Check now", function () {
    check.disabled = true; check.textContent = "Checking…";
    api("POST", "/api/updates/check").then(draw).catch(toastError)
      .then(function () { check.disabled = false; check.textContent = "Check now"; });
  });
  var get = button("Download", function () { getUpdate(S.update || {}); }, "primary");
  function draw(u) {
    S.update = u;
    status.textContent = updateText(u);
    actions.innerHTML = "";
    if (u.newer && u.download) actions.appendChild(get);
    actions.appendChild(check);
    if (u.notes && u.newer) {
      var old = box.querySelector(".updnotes");
      if (old) old.remove();
      box.insertBefore(h("p", "pgsub updnotes", u.notes), head.nextSibling);
    }
  }
  api("GET", "/api/updates").then(draw).catch(function () {});
  box.appendChild(row("srow", "Look for new versions", "Once a day, from the update link. Nothing is downloaded until you choose to",
    toggleControl(S.settings.check_updates !== false, function (on) { saveSetting({ check_updates: on }); })));
  var link = textField("https://…");
  link.value = S.settings.update_url || "";
  link.onchange = function () { saveSetting({ update_url: link.value.trim() }); };
  box.appendChild(row("srow", "Update link", "The release file new versions are announced in. A Google Drive or Dropbox share link works. " +
    "Empty: the link this copy was built with", link));
  box.appendChild(row("srow", "Report a problem", "Saves one zip with what happened, to send to whoever gave you CodeMonkey",
    button("Report a problem…", function () { closeSettings(); openReport(); })));
}

/* ---- Report a problem: one zip to send (webui/report.py) ---- */
var PAGE_ERRORS = [];
function pageError(text) {
  PAGE_ERRORS.push(new Date().toTimeString().slice(0, 8) + " " + String(text).slice(0, 2000));
  if (PAGE_ERRORS.length > 100) PAGE_ERRORS.shift();
}
window.addEventListener("error", function (e) {
  pageError(e.message + (e.filename ? " (" + e.filename.split("/").pop() + ":" + e.lineno + ")" : ""));
});
window.addEventListener("unhandledrejection", function (e) {
  var r = e.reason;
  pageError("Unhandled: " + (r && r.stack ? r.stack : r));
});
function openReport(about) {
  closeMenus();
  var body = $("report-body");
  body.innerHTML = "";
  body.appendChild(h("p", "pgsub", "Say what you were doing and what went wrong. CodeMonkey puts that in a zip with its version, " +
    "this computer, the settings and what the server printed lately. Keys and passwords are blanked out."));
  var note = body.appendChild(h("textarea", "field"));
  note.rows = 5;
  note.placeholder = "What happened? What did you expect?";
  if (about) note.value = "The error said: " + about + "\n\n";
  var conv = null;
  if (S.current) {
    var label = body.appendChild(h("label", "chk"));
    conv = label.appendChild(h("input"));
    conv.type = "checkbox";
    label.appendChild(document.createTextNode(" Include this conversation (everything said in it)"));
  }
  var said = body.appendChild(h("p", "pgsub", ""));
  $("report-modal").classList.add("open");
  setTimeout(function () { note.focus(); note.setSelectionRange(note.value.length, note.value.length); }, 30);
  var save = $("report-save");
  save.disabled = false;
  save.textContent = "Save report";
  save.onclick = function () {
    save.disabled = true; save.textContent = "Saving…";
    fetch("/api/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note.value, conversation: !!(conv && conv.checked), session_id: S.current || "",
        page: { agent: navigator.userAgent, screen: innerWidth + "x" + innerHeight, errors: PAGE_ERRORS } })
    }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || "HTTP " + r.status); });
      var name = ((r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/) || [])[1] || "CodeMonkey-report.zip";
      return r.blob().then(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 60000);
        said.textContent = "Saved as " + name + ". Send it to whoever gave you CodeMonkey; you can open it first to see what is in it.";
        save.textContent = "Save again";
      });
    }).catch(function (err) {
      said.textContent = "The report could not be made: " + err.message;
      save.textContent = "Save report";
    }).then(function () { save.disabled = false; });
  };
}

PAGES.usage = function (pane) {
  pageHead(pane, "Usage", "What the agent has used on this computer. Nothing here is sent anywhere.");
  var stats = h("div", "statrow"), models = h("div"), chart = h("div", "uchart");
  pane.appendChild(stats);
  var chartHead = pane.appendChild(h("div", "uchd"));
  chartHead.appendChild(h("h3", "", "Tokens a day, last 14 days"));
  var asTable = chartHead.appendChild(h("button", "btn link", "Show as a table"));
  pane.appendChild(chart);
  pane.appendChild(h("h3", "", "By model, last 7 days"));
  pane.appendChild(models);
  api("GET", "/api/usage").then(function (u) {
    [[u.today.tasks, "tasks today"], [fmtNum(u.today["in"]), "tokens read"], [fmtNum(u.today.out), "tokens written"],
     [u.today.ms ? fmtDuration(u.today.ms / 1000).replace(" seconds", "s").replace(" second", "s") : "0s", "time working"]]
      .forEach(function (s) {
        var card = h("div", "stat");
        card.appendChild(h("b", "", String(s[0])));
        card.appendChild(h("i", "", s[1]));
        stats.appendChild(card);
      });
    drawUsageChart(chart, u.days || [], false);
    asTable.onclick = function () {
      var table = !chart.classList.contains("astable");
      drawUsageChart(chart, u.days || [], table);
      asTable.textContent = table ? "Show as a chart" : "Show as a table";
    };
    if (!u.by_model.length) models.appendChild(h("div", "empty", "Nothing yet."));
    u.by_model.forEach(function (m) {
      models.appendChild(usageRow(m.model, fmtNum(m.tokens) + " tokens · " + m.tasks + (m.tasks === 1 ? " task" : " tasks") +
        (m.cost > 0 ? " · $" + m.cost.toFixed(2) : ""), m.share));
    });
  }).catch(function () {});
  pane.appendChild(h("h3", "", "Rates for hosted models"));
  var rates = S.settings.cost_rates || {};
  function rateRow(title, key) {
    var lab = h("label", "inp");
    lab.appendChild(document.createTextNode("$"));
    var inp = h("input");
    inp.value = (+rates[key] || 0).toFixed(2);
    inp.inputMode = "decimal";
    inp.onchange = function () {
      var next = { input: +(S.settings.cost_rates || {}).input || 0, output: +(S.settings.cost_rates || {}).output || 0 };
      next[key] = Math.max(0, parseFloat(inp.value) || 0);
      inp.value = next[key].toFixed(2);
      saveSetting({ cost_rates: next });
    };
    lab.appendChild(inp);
    pane.appendChild(row("srow", title, "Used only to estimate spend. Leave at 0 for local models.", lab));
  }
  rateRow("Cost per million tokens read", "input");
  rateRow("Cost per million tokens written", "output");
  pane.appendChild(h("p", "fine", "Token counts are estimates (about four characters per token)."));
};

PAGES.context = function (pane) {
  pageHead(pane, "Context", "How much the model is given to read, and what it always remembers.");
  pane.appendChild(row("srow", "Context budget", "Older messages are summarised once a session passes this. Choose 512k or 1M only for a model that takes that much",
    segControl([[32000, "32k"], [64000, "64k"], [128000, "128k"], [256000, "256k"], [512000, "512k"], [1000000, "1M"]], S.settings.context_budget, function (v) {
      saveSetting({ context_budget: +v });
      if (S.current) sessionSetting({ context_budget: +v });
    }, "w")));
  var fixed = S.snap && S.snap.context_breakdown ? S.snap.context_breakdown.fixed : 0;
  pane.appendChild(row("srow", "Sent with every message",
    "Instructions, tool descriptions, memory and matching skills" + (fixed ? " · about " + fmtNum(fixed / 4) + " tokens in this session" : "")));
  pane.appendChild(row("srow", "Offer extra tools only when needed",
    "The agent always has its core tools. Web, browser, helpers, scheduling, goals and the library are loaded when a task calls for them, which keeps every request smaller.",
    toggleControl(S.settings.lazy_tools, function (on) { saveSetting({ lazy_tools: on }); })));
  pane.appendChild(h("h3", "", "Memory"));
  var learnRow = row("srow", "Learn from conversations when they go quiet",
    "After about ten quiet minutes it looks back once and saves what is worth keeping. It can only save to memory, and what it merely concluded is marked unverified.",
    toggleControl(S.settings.learn_from_conversations, function (on) { saveSetting({ learn_from_conversations: on }); }));
  pane.appendChild(learnRow);
  api("GET", "/api/learning").then(function (st) {
    if (!st.last) return;
    var words = st.last.status === "error" ? "the last look back failed: " + st.last.detail
      : st.last.status === "skipped" ? "the last look back was skipped: " + st.last.detail
      : "last look back " + shortAgo(st.last.at) + " ago saved " + st.last.saved + (st.last.saved === 1 ? " thing" : " things");
    learnRow.querySelector("i").textContent += " " + words.charAt(0).toUpperCase() + words.slice(1) + ".";
  }).catch(function () {});
  pane.appendChild(h("p", "pgsub", "What the agent keeps between sessions. Each item shows how it knows it. What it only concluded by itself stays marked until you confirm it."));
  var list = h("div");
  pane.appendChild(list);
  function draw(claims) {
    list.innerHTML = "";
    if (!claims.length) list.appendChild(h("div", "empty", "Nothing remembered yet."));
    claims.forEach(function (c) {
      var tag = h("span", "tag " + (c.state === "KNOWN" ? "ok" : c.state === "DERIVED" ? "" : "warn"),
        c.kind === "lesson" ? "Lesson" : c.state === "KNOWN" ? "Confirmed" : c.state === "DERIVED" ? "From files" : "Unverified");
      tag.title = c.standing;
      var forget = button("Forget", function () { api("POST", "/api/memory/forget", { id: c.id }).then(draw).catch(toastError); });
      var confirm = button("Confirm", function () { api("POST", "/api/memory/confirm", { id: c.id }).then(draw).catch(toastError); });
      confirm.disabled = c.state === "KNOWN";
      var sub = c.basis.charAt(0).toUpperCase() + c.basis.slice(1) + " · " + c.first_seen +
        (c.project ? " · only in " + c.project : "") + (c.replaced ? " · replaced an older note" : "");
      list.appendChild(row("mrow", c.text, sub, tag, buttonRow(confirm, forget)));
    });
  }
  api("GET", "/api/memory").then(draw).catch(function () {});
  var add = h("div", "addrow"), inp = h("input", "field");
  inp.placeholder = "Something to remember, for example: tests run with pytest -q";
  function remember() {
    if (!inp.value.trim()) return;
    api("POST", "/api/memory", { fact: inp.value }).then(function (claims) { inp.value = ""; draw(claims); }).catch(toastError);
  }
  inp.onkeydown = function (e) { if (e.key === "Enter") remember(); };
  add.appendChild(inp);
  add.appendChild(button("Remember", remember));
  pane.appendChild(add);
};

/* ---- people ---- */

PAGES.people = function (pane) {
  pageHead(pane, "People", "What you have told the agent about the people you deal with, one page each. A person's notes are brought in only when you mention them. Nothing here leaves this computer, and the agent may only file what you said or a file states, never its impressions.");
  var body = h("div");
  pane.appendChild(body);
  function send(path, data) { return api("POST", path, data).then(draw).catch(toastError); }
  function draw(people) {
    body.innerHTML = "";
    if (!people.length) body.appendChild(h("div", "empty", "Nobody yet. Tell the agent about someone (\u201cDana reviews every database change\u201d) or add a note below."));
    people.forEach(function (p) {
      var card = h("div", "person");
      var head = h("div", "personhead");
      head.appendChild(h("h3", "", p.name));
      head.appendChild(buttonRow(
        button("Rename", function () {
          askText("Rename " + p.name, p.name, "Save", function (to) { send("/api/people/rename", { name: p.name, to: to }); });
        }),
        button("Forget", function () {
          askConfirm("Forget " + p.name + "?", "Every note about them is erased for good.", "Forget", function () {
            send("/api/people/forget", { name: p.name });
          });
        }, "danger")));
      card.appendChild(head);
      p.claims.forEach(function (c) {
        var tag = h("span", "tag " + (c.state === "KNOWN" ? "ok" : c.state === "DERIVED" ? "" : "warn"),
          c.state === "KNOWN" ? "Confirmed" : c.state === "DERIVED" ? "From files" : "Unverified");
        tag.title = c.standing;
        var confirm = button("Confirm", function () { send("/api/people/claim", { id: c.id, confirm: true }); });
        confirm.disabled = c.state === "KNOWN";
        var forget = button("Forget", function () { send("/api/people/claim", { id: c.id }); });
        card.appendChild(row("mrow", c.text, c.basis.charAt(0).toUpperCase() + c.basis.slice(1) + " \u00b7 " + c.first_seen, tag, buttonRow(confirm, forget)));
      });
      var add = h("div", "addrow"), inp = h("input", "field");
      inp.placeholder = "Add a note about " + p.name;
      function note() { if (inp.value.trim()) send("/api/people", { name: p.name, fact: inp.value }); }
      inp.onkeydown = function (e) { if (e.key === "Enter") note(); };
      add.appendChild(inp);
      add.appendChild(button("Add", note));
      card.appendChild(add);
      body.appendChild(card);
    });
  }
  api("GET", "/api/people").then(draw).catch(function () {});
  var fresh = h("div", "addrow"), who = h("input", "field"), what = h("input", "field");
  who.placeholder = "Name"; who.style.maxWidth = "180px";
  what.placeholder = "A note about them, for example: reviews every database change";
  function create() {
    if (!who.value.trim() || !what.value.trim()) return;
    send("/api/people", { name: who.value, fact: what.value }).then(function () { who.value = ""; what.value = ""; });
  }
  what.onkeydown = function (e) { if (e.key === "Enter") create(); };
  fresh.appendChild(who); fresh.appendChild(what);
  fresh.appendChild(button("Add person", create));
  pane.appendChild(fresh);
};

/* ---- daily review ---- */

PAGES.review = function (pane) {
  pageHead(pane, "Review", "Once a day the record of the last day is read back: what failed, what you declined, where a reply did not match a result. Problems that repeat become lessons the agent sees at the start of every session. No model is used.");
  var body = h("div");
  pane.appendChild(body);
  function draw(reviews) {
    body.innerHTML = "";
    var r = reviews[0];
    if (!r) { body.appendChild(h("div", "empty", "No review yet. One is made each day, or press Review now.")); return; }
    var stats = h("div", "statrow");
    [[r.tasks.done, "tasks finished"], [r.tasks.failed + r.tasks.stopped, "failed or stopped"], [r.steps, "steps taken"],
     [r.failures.reduce(function (n, f) { return n + f.count; }, 0), "steps that failed"]].forEach(function (s) {
      var card = h("div", "stat");
      card.appendChild(h("b", "", String(s[0])));
      card.appendChild(h("i", "", s[1]));
      stats.appendChild(card);
    });
    body.appendChild(h("h3", "", "The last day, reviewed " + r.day));
    body.appendChild(stats);
    body.appendChild(h("h3", "", "What went wrong"));
    var any = false;
    r.failures.forEach(function (f) {
      any = true;
      body.appendChild(row("mrow", (STEP_NAME[f.tool] || f.tool) + ": " + f.cause, f.example, h("span", "tag " + (f.count > 1 ? "warn" : ""), f.count + (f.count === 1 ? " time" : " times"))));
    });
    r.denied.forEach(function (d) {
      any = true;
      body.appendChild(row("mrow", "You declined: " + d.what, "", h("span", "tag", d.count + (d.count === 1 ? " time" : " times"))));
    });
    if (r.guard) { any = true; body.appendChild(row("mrow", "The guard questioned an action", "It was about to act on something it had not checked", h("span", "tag", r.guard + (r.guard === 1 ? " time" : " times")))); }
    var bad = (r.claims.CONFLICTED || 0) + (r.claims.UNKNOWN_REFERENCE || 0);
    if (bad) { any = true; body.appendChild(row("mrow", "Replies that did not match the evidence", (r.claims.CONFLICTED || 0) + " contradicted a result, " + (r.claims.UNKNOWN_REFERENCE || 0) + " named a file that does not exist", h("span", "tag warn", bad + (bad === 1 ? " claim" : " claims")))); }
    r.corrections.forEach(function (c) { any = true; body.appendChild(row("mrow", "You corrected it", c)); });
    if (!any) body.appendChild(h("div", "empty", "Nothing stood out."));
    body.appendChild(h("h3", "", "Lessons written from this review"));
    if (!r.lessons.length) body.appendChild(h("div", "empty", "None. A problem has to repeat before it becomes a lesson."));
    r.lessons.forEach(function (l) { body.appendChild(row("mrow", l.text, "Because " + l.why)); });
    (r.retired || []).forEach(function (t) { body.appendChild(row("mrow", t, "Retired: not needed again for two weeks")); });
  }
  api("GET", "/api/reviews").then(draw).catch(function () {});
  var foot = h("div", "pgfoot");
  var btn = button("Review now", function () {
    btn.textContent = "Reviewing";
    api("POST", "/api/reviews", {}).then(function (rv) { btn.textContent = "Review now"; draw(rv); })
      .catch(function (err) { btn.textContent = "Review now"; toastError(err); });
  }, "primary");
  foot.appendChild(buttonRow(btn));
  pane.appendChild(foot);
  pane.appendChild(h("p", "fine", "Lessons in force are listed with the rest of memory in Settings, Context, where you can forget one. A lesson that is not reinforced for two weeks is retired."));
};

/* ---- library ---- */

PAGES.library = function (pane) {
  pageHead(pane, "Library", "Pages and documents the agent made for you. They are kept here, apart from your project folders, and never leave this computer.");
  var list = h("div");
  pane.appendChild(list);
  function load() {
    api("GET", "/api/artifacts").then(function (items) {
      list.innerHTML = "";
      if (!items.length) list.appendChild(h("div", "empty", "Nothing yet. Ask for a report, a comparison or a small page and it will be kept here."));
      items.forEach(function (a) {
        var open = h("a", "btn", "Open");
        open.href = a.url; open.target = "_blank"; open.rel = "noopener";
        list.appendChild(row("mrow", a.title, (a.kind === "page" ? "Page" : "Document") + " · " + a.filename + " · " +
          fmtNum(a.size_bytes) + " bytes · " + a.updated_at.slice(0, 10),
          buttonRow(open, button("Delete", function () {
            askConfirm("Delete “" + a.title + "”?", "It is removed from the Library for good.", "Delete", function () {
              api("DELETE", "/api/artifacts/" + a.artifact_id).then(load).catch(toastError);
            });
          }))));
      });
    }).catch(function () {});
  }
  PAGES.library.reload = function () { if (list.isConnected) load(); };
  load();
};

/* ---- ideas ---- */

PAGES.ideas = function (pane) {
  var folder = currentFolder() || S.config.default_project;
  pageHead(pane, "Ideas", "Things worth doing next in " + folderName(folder) + ". Each one comes from something real: a note left in the code, a command that failed, a goal gone quiet. No model is used to make them.");
  var list = h("div");
  pane.appendChild(list);
  function draw(ideas) {
    list.innerHTML = "";
    if (!ideas.length) list.appendChild(h("div", "empty", "Nothing stands out in this folder right now."));
    ideas.forEach(function (idea) {
      var where = idea.source_path ? " · " + idea.source_path + (idea.source_line ? " line " + idea.source_line : "") : "";
      list.appendChild(row("mrow", idea.title.replace(/`/g, ""), idea.rationale + where,
        buttonRow(
          button("Do it", function () { closeSettings(); if (S.current && S.snap && S.snap.project_dir !== idea.project_dir) newSession(); startIdea(idea); }),
          button("Dismiss", function () {
            api("POST", "/api/ideas/" + idea.idea_id, { status: "dismissed" }).then(load).catch(toastError);
          }))));
    });
  }
  function load() { api("GET", "/api/ideas?project_dir=" + encodeURIComponent(folder)).then(draw).catch(function () {}); }
  load();
  pane.appendChild(h("p", "fine", "An idea you dismiss stays dismissed. One whose cause is gone (the note was removed, the command passes again) drops off by itself."));
};

/* ---- goals and skills ---- */

PAGES.goals = function (pane) {
  pageHead(pane, "Goals", "What you are working toward in each folder. The agent sees the active ones in every session there and logs real progress against them.");
  var list = h("div");
  pane.appendChild(list);
  function draw(goals) {
    list.innerHTML = "";
    if (!goals.length) list.appendChild(h("div", "empty", "No goals yet."));
    goals.forEach(function (g) {
      var done = g.status === "completed";
      var notes = h("div", "sub-models hidden");
      g.updates.forEach(function (u) {
        notes.appendChild(row("mrow", u.note, u.created_at.slice(0, 10) + " · logged by " + (u.author === "you" ? "you" : "the agent")));
      });
      if (!g.updates.length) notes.appendChild(h("div", "empty", "No progress logged yet."));
      var sub = baseName(g.project_dir) + " · " + g.updates.length + (g.updates.length === 1 ? " progress note" : " progress notes") +
        (g.updates.length ? " · latest: " + g.updates[0].note.slice(0, 90) : "") + (g.description ? " · " + g.description.slice(0, 120) : "");
      list.appendChild(row("mrow", g.title, sub, h("span", "tag " + (done ? "" : "ok"), done ? "Completed" : "Active"),
        buttonRow(
          button("Progress", function () { notes.classList.toggle("hidden"); }),
          button("Add note", function () {
            askText("Progress on “" + g.title + "”", "", "Log", function (note) {
              api("POST", "/api/goals/" + g.goal_id, { note: note }).then(draw).catch(toastError);
            });
          }),
          button(done ? "Reopen" : "Complete", function () {
            api("POST", "/api/goals/" + g.goal_id, { status: done ? "active" : "completed" }).then(draw).catch(toastError);
          }))));
      list.appendChild(notes);
    });
  }
  api("GET", "/api/goals").then(draw).catch(function () {});
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("New goal", function () {
    askText("New goal in " + folderName(currentFolder() || S.config.default_project), "", "Add", function (title) {
      api("POST", "/api/goals", { title: title, project_dir: currentFolder() || S.config.default_project }).then(draw).catch(toastError);
    });
  }, "primary")));
  pane.appendChild(foot);
};

function editSkill(folder) {
  function open(sk) {
    $("skill-head").textContent = sk ? "Edit " + sk.name : "New skill";
    $("skill-name").value = sk ? sk.folder : "";
    $("skill-name").disabled = !!sk;
    $("skill-desc").value = sk ? sk.description : "";
    $("skill-keys").value = sk ? sk.keywords.join(", ") : "";
    $("skill-body").value = sk ? sk.body : "";
    $("skill-modal").classList.add("open");
    (sk ? $("skill-desc") : $("skill-name")).focus();
  }
  if (!folder) return open(null);
  api("GET", "/api/skills/" + folder).then(open).catch(toastError);
}

PAGES.skills = function (pane) {
  pageHead(pane, "Skills", "Playbooks for particular kinds of work. The agent sees each skill's one-line description and reads the full playbook only when a task matches it.");
  var stats = h("div", "statrow"), proposals = h("div"), installed = h("div"), note = h("p", "fine");
  function stat(n, label) {
    var card = h("div", "stat");
    card.appendChild(h("b", "", String(n)));
    card.appendChild(h("i", "", label));
    stats.appendChild(card);
  }
  function draw(o) {
    stats.innerHTML = ""; proposals.innerHTML = ""; installed.innerHTML = "";
    stat(o.installed.length, "skills installed");
    stat(o.proposals.length, o.proposals.length === 1 ? "proposal waiting" : "proposals waiting");
    stat(o.cases, "finished tasks on record");
    stat(3, "repeats before a proposal");
    if (!o.proposals.length) proposals.appendChild(h("div", "empty", "No proposals waiting."));
    o.proposals.forEach(function (p) {
      var body = h("div", "sub-models hidden");
      body.appendChild(h("pre", "", p.body));
      proposals.appendChild(row("mrow", p.name, p.description || "A workflow that worked several times",
        buttonRow(
          button("Read", function () { body.classList.toggle("hidden"); }),
          button("Keep", function () { api("POST", "/api/skills/proposals/" + p.folder + "/install", {}).then(draw).catch(toastError); }),
          button("Dismiss", function () { api("POST", "/api/skills/proposals/" + p.folder + "/dismiss", {}).then(draw).catch(toastError); }))));
      proposals.appendChild(body);
    });
    if (!o.installed.length) installed.appendChild(h("div", "empty", "No skills installed."));
    o.installed.forEach(function (sk) {
      var body = h("div", "sub-models hidden");
      var sub = sk.description.length > 96 ? sk.description.slice(0, 96).replace(/\s+\S*$/, "") + "…" : sk.description;
      var line = row("mrow", sk.name, sub,
        buttonRow(
          button("Read", function () {
            if (!body.classList.contains("hidden")) { body.classList.add("hidden"); return; }
            api("GET", "/api/skills/" + sk.folder).then(function (d) {
              body.innerHTML = "";
              body.appendChild(h("pre", "", d.body));
              body.classList.remove("hidden");
            }).catch(toastError);
          }),
          button("Edit", function () { editSkill(sk.folder); }),
          button("Remove", function () {
            askConfirm("Remove the skill “" + sk.name + "”?", "Its folder is deleted from your skills folder. The agent will no longer follow it.", "Remove", function () {
              api("DELETE", "/api/skills/" + sk.folder).then(draw).catch(toastError);
            });
          })));
      line.title = sk.description + (sk.keywords.length ? "\n\nComes up for: " + sk.keywords.join(", ") : "");
      installed.appendChild(line);
      installed.appendChild(body);
    });
    note.textContent = "A proposal is written when the same steps have worked at least three times. Nothing becomes a skill until you keep it.";
  }
  pane.appendChild(stats);
  pane.appendChild(h("h3", "", "Proposed from your own work"));
  pane.appendChild(proposals);
  var mine = button("Look for patterns", function () {
    mine.textContent = "Looking";
    api("POST", "/api/skills/mine", {}).then(function (o) { mine.textContent = "Look for patterns"; draw(o); })
      .catch(function (err) { mine.textContent = "Look for patterns"; toastError(err); });
  });
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("New skill", function () { editSkill(null); }, "primary"), mine));
  pane.appendChild(note);
  pane.appendChild(h("h3", "", "Installed"));
  pane.appendChild(installed);
  pane.appendChild(foot);
  api("GET", "/api/skills").then(draw).catch(function () {});
};

/* ---- scheduled jobs ---- */

var JOB_PRESETS = [["*/30 * * * *", "Every 30 minutes"], ["0 * * * *", "Every hour"], ["0 9 * * *", "Every day at 09:00"],
  ["0 18 * * *", "Every day at 18:00"], ["0 9 * * 1-5", "Weekdays at 09:00"], ["0 9 * * 1", "Mondays at 09:00"], ["", "Custom"]];
var jobEditing = null;

function untilText(iso) {
  var s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (isNaN(s)) return "";
  if (s <= 60) return "any minute now";
  if (s < 3600) return "in " + Math.round(s / 60) + " minutes";
  if (s < 86400) return "in " + Math.round(s / 3600) + " hours";
  return "in " + Math.round(s / 86400) + " days";
}

function editJob(job) {
  jobEditing = job;
  $("job-title-head").textContent = job ? "Edit scheduled job" : "New scheduled job";
  $("job-title").value = job ? job.title : "";
  $("job-task").value = job ? job.task : "";
  var sel = $("job-when");
  sel.innerHTML = "";
  var known = job && JOB_PRESETS.some(function (p) { return p[0] === job.schedule; });
  JOB_PRESETS.forEach(function (p) {
    var o = h("option", "", p[1]);
    o.value = p[0];
    if (job ? (known ? p[0] === job.schedule : p[0] === "") : p[0] === "0 9 * * *") o.selected = true;
    sel.appendChild(o);
  });
  $("job-cron").value = job ? job.schedule : "0 9 * * *";
  $("job-watch").value = job ? job.watch : "";
  $("job-cron").classList.toggle("hidden", sel.value !== "");
  $("job-folder").textContent = "Works in " + (job ? job.project_dir : (currentFolder() || S.config.default_project)) +
    ". Runs happen around the set time, only while CodeMonkey is running.";
  $("job-remove").classList.toggle("hidden", !job);
  $("job-modal").classList.add("open");
  $("job-title").focus();
}

function saveJob() {
  var body = { title: $("job-title").value, task: $("job-task").value,
    schedule: $("job-when").value || $("job-cron").value, watch: $("job-watch").value };
  var call = jobEditing
    ? api("POST", "/api/jobs/" + jobEditing.id, body)
    : api("POST", "/api/jobs", Object.assign(body, { project_dir: currentFolder() || S.config.default_project }));
  call.then(function () { closeDialog("job-modal"); showPage("scheduled"); })
    .catch(function (err) { $("job-title-head").textContent = "Not saved: " + err.message; });
}

var RUN_WORD = { reported: "reported", silent: "nothing to report", error: "failed", skipped: "skipped", running: "running" };

PAGES.scheduled = function (pane) {
  pageHead(pane, "Scheduled", "Work the agent does by itself on a timetable. A run can only look (read, search, and commands you always allow); it reports into its own session, or stays quiet when there is nothing to say.");
  var list = h("div");
  pane.appendChild(list);
  api("GET", "/api/jobs").then(function (jobs) {
    if (!jobs.length) list.appendChild(h("div", "empty", "Nothing scheduled yet."));
    jobs.forEach(function (job) {
      var state = job.blocked_reason ? ["warn", "Paused itself"] : job.running ? ["ok", "Running"]
        : job.enabled ? ["ok", "On"] : ["", "Paused"];
      var sub = job.schedule_text + " · in " + job.folder + (job.watch ? " · only when `" + job.watch + "` changes" : "");
      if (job.blocked_reason) sub += " · " + job.blocked_reason + ". It tries again " + untilText(job.blocked_until) + ".";
      else if (job.enabled && job.next_run_at) sub += " · next run " + untilText(job.next_run_at);
      sub += job.last_run ? " · last run " + (shortAgo(job.last_run.started_at) === "now" ? "just now" : shortAgo(job.last_run.started_at) + " ago") + ": " + (RUN_WORD[job.last_run.status] || job.last_run.status)
        : " · has not run yet";
      var history = h("div", "sub-models hidden");
      var runBtn = button("Run now", function () {
        api("POST", "/api/jobs/" + job.id + "/run", {}).then(function () {
          runBtn.textContent = "Started";
          setTimeout(function () { if (S.page === "scheduled") showPage("scheduled"); }, 4000);
        }).catch(toastError);
      });
      var histBtn = button("History", function () {
        if (!history.classList.contains("hidden")) { history.classList.add("hidden"); return; }
        api("GET", "/api/jobs/" + job.id + "/runs").then(function (runs) {
          history.innerHTML = "";
          history.classList.remove("hidden");
          if (!runs.length) history.appendChild(h("div", "empty", "It has not run yet. A schedule is not a run."));
          runs.forEach(function (r) {
            var tag = h("span", "tag " + (r.status === "error" ? "bad" : r.status === "reported" ? "ok" : ""), RUN_WORD[r.status] || r.status);
            history.appendChild(row("mrow", new Date(r.started_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) +
              (r.trigger === "manual" ? " (run by hand)" : ""),
              (r.error_text || r.result_summary || (r.status === "silent" ? "Looked and found nothing worth telling you." : "")).slice(0, 220) +
              " · " + r.steps + (r.steps === 1 ? " step" : " steps"), tag));
          });
          if (job.session_id) {
            var open = h("button", "mini", "Open");
            open.title = "Open the session its reports go to";
            open.onclick = function () { closeSettings(); refreshSessions().then(function () { openSession(job.session_id); }); };
            history.appendChild(row("mrow", "Reports", "Delivered to their own session, where you can reply", open));
          }
        }).catch(toastError);
      });
      list.appendChild(row("mrow", job.title, sub, h("span", "tag " + state[0], state[1]),
        buttonRow(runBtn, histBtn, button("Edit", function () { editJob(job); }))));
      list.appendChild(history);
    });
  }).catch(function () {});
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("New scheduled job", function () { editJob(null); }, "primary")));
  pane.appendChild(foot);
  pane.appendChild(h("p", "fine", "It checks at intervals, so it notices things at the next run, not the moment they happen. The history is the proof of what ran."));
};

function editCommand(cmd) {
  $("cmd-title").textContent = cmd ? "Edit /" + cmd.name : "New command";
  $("cmd-name").value = cmd ? cmd.name : "";
  $("cmd-name").disabled = !!cmd;
  $("cmd-desc").value = cmd ? cmd.description : "";
  $("cmd-body").value = cmd ? cmd.body : "";
  $("cmd-modal").classList.add("open");
  (cmd ? $("cmd-desc") : $("cmd-name")).focus();
}

/* ---- MCP servers: what they give the agent, whether they are up, and their settings ---- */
function loadMcp() {
  return api("GET", "/api/mcp").then(function (r) { S.mcp = r; return r; }).catch(function () { return S.mcp; });
}
var MCP_TAG = { connected: ["ok", "Connected"], starting: ["", "Starting"], failed: ["bad", "Failed"], off: ["", "Off"],
  signin: ["warn", "Sign in"] };

PAGES.mcp = function (pane) {
  pageHead(pane, "MCP servers", "Programs on this computer and services on the web that give the agent more tools, " +
    "things to read, and prompts you can run as /server:prompt.");
  var data = S.mcp || { servers: [] };
  function act(method, path, body) {
    return api(method, path, body).then(function () { return loadMcp(); }).then(function () { showPage("mcp"); }).catch(toastError);
  }
  if (!data.servers.length) pane.appendChild(h("div", "empty", "No MCP servers yet. Add one below."));
  data.servers.forEach(function (s) {
    var t = MCP_TAG[s.status] || ["", s.status];
    var name = encodeURIComponent(s.name);
    var detail = h("div", "mcpdetail hidden");
    var counts = s.status === "connected"
      ? s.tools.length + (s.tools.length === 1 ? " tool" : " tools") + " · " + s.resources + " to read · " +
        s.prompts.length + (s.prompts.length === 1 ? " prompt" : " prompts")
      : (s.error || (s.status === "off" ? "Switched off" : "Connecting…"));
    var more = button("Details", function () { detail.classList.toggle("hidden"); });
    var power = button(s.disabled ? "Turn on" : "Turn off", function () { act("POST", "/api/mcp/" + name + "/enabled", { enabled: s.disabled }); });
    var restart = s.disabled ? null : button("Restart", function () { act("POST", "/api/mcp/" + name + "/restart", {}); });
    var signin = s.status === "signin" ? button("Sign in", function () {
      // the sign-in page opens in your browser and comes back to CodeMonkey by itself
      api("POST", "/api/mcp/" + name + "/signin", {}).then(function (r) {
        window.open(r.url, "_blank", "noopener");
        addNote("Finish signing in to " + s.name + " in your browser; it connects by itself after.");
      }).catch(toastError);
    }, "primary") : null;
    if (signin) restart = null;
    pane.appendChild(row("mrow", s.name, s.target + " · " + counts, h("span", "tag " + t[0], t[1]), buttonRow(signin, restart, power, more)));
    if (s.server && s.server.name) detail.appendChild(h("p", "fine", "Reports itself as " + s.server.name + (s.server.version ? " " + s.server.version : "") + "."));
    if (s.tools.length) {
      detail.appendChild(h("p", "fine", "Tools: " + s.tools.join(", ")));
      detail.appendChild(h("p", "fine", s.read_only_tools.length
        ? "Run without asking (you marked them read-only): " + s.read_only_tools.join(", ")
        : "Every tool asks before it runs, unless you list it as read-only (Edit)."));
    }
    if (s.prompts.length) detail.appendChild(h("p", "fine", "Prompts: " + s.prompts.map(function (p) { return "/" + s.name + ":" + p; }).join(", ")));
    if (s.kind === "remote") detail.appendChild(h("p", "fine", s.signed_in ? "Signed in; the token is kept in " + keyStore() + " and renewed by itself."
      : s.has_key ? "Key saved in " + keyStore() + "." : "No key saved."));
    if (s.offers && s.offers.length) detail.appendChild(h("p", "fine", "It may ask " + s.offers.map(function (o) {
      return o === "sampling" ? "for answers from your model (you are asked first)" : "you questions in a form";
    }).join(" and ") + "."));
    if (s.signed_in) detail.appendChild(buttonRow(button("Sign out", function () { act("POST", "/api/mcp/" + name + "/signout", {}); })));
    if (s.log.length) {
      var pre = h("pre", "logtail");
      pre.textContent = s.log.join("\n");
      detail.appendChild(pre);
    }
    detail.appendChild(buttonRow(
      button("Edit", function () { fillForm(s); }),
      button("Remove", function () {
        if (confirm("Remove the MCP server " + s.name + (s.kind === "remote" ? " and its saved key" : "") + "?")) act("DELETE", "/api/mcp/" + name);
      })));
    pane.appendChild(detail);
  });

  // ---- add or change one
  pane.appendChild(h("h3", "", "Add an MCP server"));
  var kind = selectControl([["local", "A program on this computer"], ["remote", "A service on the web"]], "local", function () { show(); });
  var fName = textField("Name, such as github");
  var fCmd = textField("Command, such as npx -y @modelcontextprotocol/server-filesystem .");
  var fEnv = h("textarea", "field mono");
  fEnv.rows = 2; fEnv.placeholder = "Environment, one NAME=value per line (optional)";
  var fUrl = textField("Address, such as https://example.com/mcp");
  var fHeader = textField("Header for the key (default Authorization)");
  var fKey = textField("API key or token (optional; kept in " + keyStore() + ")", "password");
  var fRo = textField("Tools that may run without asking, comma-separated (optional)");
  var fTimeout = textField("Seconds a call may take (default 120)");
  var save = button("Save server", function () {
    save.disabled = true;
    S.mcpEditing = false;
    var remote = kind.value === "remote";
    act("POST", "/api/mcp", {
      name: fName.value, command: remote ? "" : fCmd.value, env: remote ? "" : fEnv.value,
      url: remote ? fUrl.value : "", auth_header: remote ? fHeader.value : "", key: remote ? fKey.value : "",
      read_only_tools: fRo.value, timeout: fTimeout.value
    }).then(function () { save.disabled = false; });
  });
  var form = h("div", "addform");
  [kind, fName, fCmd, fEnv, fUrl, fHeader, fKey, fRo, fTimeout].forEach(function (f) {
    f.addEventListener("focus", function () { S.mcpEditing = true; });
    form.appendChild(f);
  });
  form.appendChild(buttonRow(save));
  pane.appendChild(form);
  function show() {
    var remote = kind.value === "remote";
    [fCmd, fEnv].forEach(function (f) { f.classList.toggle("hidden", remote); });
    [fUrl, fHeader, fKey].forEach(function (f) { f.classList.toggle("hidden", !remote); });
  }
  function fillForm(s) {
    S.mcpEditing = true;
    kind.value = s.kind; fName.value = s.name;
    if (s.kind === "remote") { fUrl.value = s.target; fHeader.value = s.auth_header; }
    else { fCmd.value = s.target; fEnv.value = s.env_keys.map(function (k) { return k + "="; }).join("\n"); }
    fRo.value = s.read_only_tools.join(", "); fTimeout.value = s.timeout;
    show();
    fName.scrollIntoView({ behavior: "smooth", block: "center" });
    if (s.env_keys.length) addNote("Environment values are not shown again: a NAME= line left empty keeps its value.");
  }
  show();
  pane.appendChild(h("p", "fine", "Kept in " + (data.path || "mcp.json") + ". Keys are never written there."));
};

/* ---- Plugins: skills, commands, agents, styles, hooks and MCP servers as one package ---- */
PAGES.plugins = function (pane) {
  pageHead(pane, "Plugins", "Skills, commands, agents, reply styles, hooks and MCP servers, packaged to share. " +
    "A new plugin arrives switched off: its hooks and MCP servers run programs, so look at what it brings first.");
  var box = h("div");
  pane.appendChild(box);
  function act(method, path, body, after) {
    return api(method, path, body).then(function (r) { if (after) after(r); showPage("plugins"); }).catch(toastError);
  }
  api("GET", "/api/plugins").then(function (list) {
    if (!list.length) box.appendChild(h("div", "empty", "No plugins installed."));
    list.forEach(function (p) {
      var name = encodeURIComponent(p.name);
      var bits = [];
      ["skills", "commands", "agents", "styles"].forEach(function (k) { if (p[k].length) bits.push(p[k].length + " " + k); });
      if (p.hooks.length) bits.push(p.hooks.length + " hook" + (p.hooks.length === 1 ? "" : "s"));
      if (p.mcp.length) bits.push(p.mcp.length + " MCP server" + (p.mcp.length === 1 ? "" : "s"));
      var detail = h("div", "mcpdetail hidden");
      ["skills", "commands", "agents", "styles"].forEach(function (k) {
        if (p[k].length) detail.appendChild(h("p", "fine", k.charAt(0).toUpperCase() + k.slice(1) + ": " +
          p[k].map(function (x) { return k === "commands" ? "/" + x : x; }).join(", ")));
      });
      p.hooks.forEach(function (hk) { detail.appendChild(h("p", "fine", "Hook on " + hk.event + " runs: " + hk.command)); });
      p.mcp.forEach(function (s) { detail.appendChild(h("p", "fine", "MCP server " + s.name + " runs: " + s.runs)); });
      detail.appendChild(h("p", "fine", "From " + (p.source || "unknown") + " · " + p.path));
      var power = button(p.enabled ? "Turn off" : "Turn on", function () {
        if (p.enabled) return act("POST", "/api/plugins/" + name + "/enabled", { enabled: false });
        var runs = p.hooks.map(function (hk) { return hk.command; }).concat(p.mcp.map(function (s) { return s.runs; }));
        askConfirm("Turn on " + p.name + "?", runs.length
          ? "It will run these on this computer: " + runs.join(" · ") + ". Only turn it on if you trust where it came from."
          : "It adds " + (bits.join(", ") || "nothing yet") + ". It runs no programs of its own.", "Turn on", function () {
          act("POST", "/api/plugins/" + name + "/enabled", { enabled: true });
        });
      });
      box.appendChild(row("mrow", p.name + (p.version ? " " + p.version : ""), (p.description || "No description") +
        (bits.length ? " · " + bits.join(", ") : ""), h("span", "tag " + (p.enabled ? "ok" : ""), p.enabled ? "On" : "Off"),
        buttonRow(power, button("Details", function () { detail.classList.toggle("hidden"); }))));
      detail.appendChild(buttonRow(
        button("Update", function () { act("POST", "/api/plugins/" + name + "/update", {}, function () { addNote("Updated " + p.name + "."); }); }),
        button("Remove", function () {
          askConfirm("Remove " + p.name + "?", "Its folder is deleted: " + p.path, "Remove", function () { act("DELETE", "/api/plugins/" + name); });
        })));
      box.appendChild(detail);
    });
  }).catch(toastError);
  pane.appendChild(h("h3", "", "Install a plugin"));
  var src = textField("A folder, a .zip file, or a git URL (https://github.com/...)");
  var install = button("Install", function () {
    install.disabled = true;
    act("POST", "/api/plugins/install", { source: src.value }, function (p) {
      addNote("Installed " + p.name + ", switched off. Look at what it brings, then turn it on.");
    }).then(function () { install.disabled = false; });
  }, "primary");
  var addRow = h("div", "addrow");
  addRow.appendChild(src); addRow.appendChild(install);
  pane.appendChild(addRow);
  pane.appendChild(h("p", "fine", "A plugin is a folder with plugin.json and any of: skills/, commands/, agents/, styles/, hooks.yaml, mcp.json. " +
    "Its commands are named /plugin:command."));
};

/* ---- Worktrees: sessions and agents working on branches of their own ---- */
function refreshSnap() {
  if (!S.current) return Promise.resolve();
  return api("GET", "/api/sessions/" + S.current).then(function (snap) { applySnapshot(snap); }).catch(function () {});
}
function worktreeMerge(wt) {
  askConfirm("Merge " + wt.branch + " into " + wt.base + "?",
    "What is not committed in the worktree is committed first. The merge happens in " + wt.repo +
    ", which must be on " + wt.base + ". Afterwards the worktree folder and branch are removed and sessions in it move back.",
    "Merge", function () {
      api("POST", "/api/worktrees/merge", { path: wt.path }).then(function (r) {
        addNote(r.note || "Merged."); refreshSnap(); if (S.page === "worktrees") showPage("worktrees");
      }).catch(toastError);
    });
}
function worktreeDiscard(wt) {
  askConfirm("Discard " + wt.branch + "?", "The worktree folder and its branch are deleted, with every change in them. " +
    "Sessions working there move back to " + wt.repo + ".", "Discard", function () {
      api("POST", "/api/worktrees/discard", { path: wt.path }).then(function () {
        addNote("Discarded " + wt.branch + "."); refreshSnap(); if (S.page === "worktrees") showPage("worktrees");
      }).catch(toastError);
    });
}
PAGES.worktrees = function (pane) {
  pageHead(pane, "Worktrees", "Sessions and agents working on a branch of their own, in their own folder. " +
    "Nothing changes in your main checkout until you merge.");
  var box = h("div");
  pane.appendChild(box);
  api("GET", "/api/worktrees").then(function (list) {
    if (!list.length) {
      box.appendChild(h("div", "empty", "No worktrees. Start one from the folder menu: Work on a new branch."));
      return;
    }
    list.forEach(function (w) {
      var state = !w.exists ? "folder missing" :
        (w.ahead ? w.ahead + (w.ahead === 1 ? " commit" : " commits") : "no commits yet") +
        (w.uncommitted ? " · " + w.uncommitted + " uncommitted" : "") + (w.behind ? " · " + w.base + " moved on by " + w.behind : "") +
        (w.stat ? " · " + w.stat : "");
      var who = w.sessions.length ? " · in use by " + w.sessions.map(function (s) { return s.title; }).join(", ") : "";
      box.appendChild(row("mrow", codeTitle(w.branch), "from " + w.base + " in " + baseName(w.repo) + " · " + state + who + " · " + w.path,
        buttonRow(button("Merge", function () { worktreeMerge(w); }), button("Discard", function () { worktreeDiscard(w); }))));
    });
  }).catch(toastError);
};

/* ---- Agents: specialists the main agent (or you, with /agent) hands work to ---- */
PAGES.agents = function (pane) {
  pageHead(pane, "Agents", "Specialists the agent can hand work to, each with its own instructions, tools and model. " +
    "You can also start one yourself: /agent reviewer check my last change.");
  var folder = currentFolder() || "";
  var list = h("div");
  pane.appendChild(list);
  var formBox = h("div");
  pane.appendChild(formBox);
  function draw(agents) {
    list.innerHTML = "";
    if (!agents.length) list.appendChild(h("div", "empty", "No agents yet."));
    agents.forEach(function (a) {
      var tools = a.tools === null ? "every tool a helper has" : a.tools.join(", ");
      var sub = (a.description || "No description") + " · " + a.origin + " · tools: " + tools +
        (a.model ? " · model: " + a.model : "") + (a.max_steps ? " · up to " + a.max_steps + " steps" : "") +
        (a.isolation === "worktree" ? " · own branch" : "");
      list.appendChild(row("mrow", codeTitle(a.name), sub, buttonRow(
        button("Edit", function () { form(a); }),
        a.origin.indexOf("plugin") === 0 ? null : button("Remove", function () {
          askConfirm("Remove the " + a.name + " agent?", "Its file is deleted: " + a.path, "Remove", function () {
            api("DELETE", "/api/agents/" + encodeURIComponent(a.name) + "?project_dir=" + encodeURIComponent(folder))
              .then(load).catch(toastError);
          });
        }))));
    });
  }
  function load() { return api("GET", "/api/agents?project_dir=" + encodeURIComponent(folder)).then(draw).catch(toastError); }
  function form(a) {
    formBox.innerHTML = "";
    formBox.appendChild(h("h3", "", a ? "Change " + a.name : "New agent"));
    var fName = textField("Name, such as security-reviewer"), fDesc = textField("When to use it (the agent reads this to decide)");
    var fTools = textField("Tools, comma-separated: read-only, or e.g. read, grep, bash, test_focus (empty: all)");
    var models = [["", "The helper model (Settings > Models)"]];
    Object.keys((S.config && S.config.providers) || {}).forEach(function (p) {
      (S.config.providers[p].models || []).forEach(function (m) { models.push([p + "/" + m, p + "/" + m]); });
    });
    var fModel = selectControl(models, a ? a.model : "", function () {});
    var fSteps = textField("Most steps (empty: the session's limit)");
    var fIso = selectControl([["", "Works in the session's folder"], ["worktree", "Works on a branch of its own (worktree)"]],
      a ? a.isolation || "" : "", function () {});
    var fScope = selectControl([["yours", "For every project (agents folder)"], ["project", "For this project only (.codemonkey/agents)"]],
      a && a.origin === "project" ? "project" : "yours", function () {});
    var fPrompt = h("textarea", "field mono");
    fPrompt.rows = 10; fPrompt.placeholder = "How it works: what it does step by step, what it must not do, and what its report must contain.";
    if (a) {
      fName.value = a.name; fDesc.value = a.description; fTools.value = a.tools === null ? "" : a.tools.join(", ");
      fSteps.value = a.max_steps || ""; fPrompt.value = a.prompt;
    }
    var save = button("Save agent", function () {
      api("POST", "/api/agents", { name: fName.value, description: fDesc.value, tools: fTools.value, model: fModel.value,
        max_steps: fSteps.value, isolation: fIso.value, prompt: fPrompt.value, scope: fScope.value, project_dir: folder })
        .then(function () { formBox.innerHTML = ""; load(); }).catch(toastError);
    }, "primary");
    var box = h("div", "addform");
    [fName, fDesc, fTools, fModel, fSteps, fIso, fScope, fPrompt].forEach(function (f) { box.appendChild(f); });
    box.appendChild(buttonRow(button("Cancel", function () { formBox.innerHTML = ""; }), save));
    formBox.appendChild(box);
    fName.focus();
  }
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("New agent", function () { form(null); }, "primary")));
  pane.appendChild(foot);
  load();
};

/* ---- Hooks: commands run at points in the agent's work (Claude Code's hook events) ---- */
var HOOK_EVENTS = {
  session_start: ["Session start", "When a session opens"],
  user_prompt: ["Message sent", "Before your message reaches the model; it can block it or add a note"],
  pre_tool: ["Before a tool", "It can block the call or change its arguments"],
  post_tool: ["After a tool", "It can add a note to the result"],
  turn_end: ["Agent stops", "When the agent finishes its reply"],
  session_end: ["Session end", "When a session closes"]
};

PAGES.hooks = function (pane) {
  pageHead(pane, "Hooks", "Commands this computer runs at points in the agent's work. Each gets JSON on stdin and may " +
    "answer JSON: {\"allow\": false, \"message\": \"why\"} to block, or {\"note\": \"...\"} to add a note.");
  var list = h("div"), formBox = h("div");
  pane.appendChild(list);
  pane.appendChild(formBox);
  var mine = [];
  function save(next) {
    return api("POST", "/api/hooks", { hooks: next }).then(draw).catch(toastError);
  }
  function draw(r) {
    mine = r.hooks.filter(function (x) { return x.origin === "yours"; }).map(function (x) {
      return { event: x.event, matcher: x.matcher, command: x.command, timeout: x.timeout };
    });
    list.innerHTML = "";
    if (!r.hooks.length) list.appendChild(h("div", "empty", "No hooks yet."));
    r.hooks.forEach(function (x) {
      var label = (HOOK_EVENTS[x.event] || [x.event])[0] + (x.matcher ? " \u00b7 " + x.matcher : "");
      var sub = x.command + " \u00b7 " + x.timeout + "s" + (x.origin === "yours" ? "" : " \u00b7 " + x.origin);
      var mineIndex = mine.findIndex(function (m) { return m.event === x.event && m.command === x.command && m.matcher === x.matcher; });
      list.appendChild(row("mrow", label, sub, x.origin === "yours" ? buttonRow(button("Remove", function () {
        save(mine.filter(function (_m, i) { return i !== mineIndex; }));
      })) : null));
    });
    list.appendChild(h("p", "fine", "Saved in " + r.path + ". Open sessions use the change at once."));
  }
  function form() {
    formBox.innerHTML = "";
    formBox.appendChild(h("h3", "", "Add a hook"));
    foot.classList.add("hidden");
    var hint = h("p", "fine", HOOK_EVENTS.pre_tool[1] + ".");
    var fEvent = selectControl(Object.keys(HOOK_EVENTS).map(function (k) { return [k, HOOK_EVENTS[k][0]]; }),
      "pre_tool", function (v) {
        hint.textContent = HOOK_EVENTS[v][1] + ".";
        fMatch.classList.toggle("hidden", v !== "pre_tool" && v !== "post_tool");
      });
    var fMatch = textField("Tools it applies to: a name or pattern, such as bash|edit (empty: every tool)");
    var fCmd = textField("Command, such as python hooks/examples/block-dangerous.py");
    var fTime = textField("Seconds it may take (default 10)");
    var box = h("div", "addform");
    [fEvent, hint, fMatch, fCmd, fTime].forEach(function (f) { box.appendChild(f); });
    function done() { formBox.innerHTML = ""; foot.classList.remove("hidden"); }
    box.appendChild(buttonRow(button("Cancel", done), button("Add hook", function () {
      save(mine.concat([{ event: fEvent.value, matcher: fMatch.value, command: fCmd.value, timeout: fTime.value || 10 }]))
        .then(done);
    }, "primary")));
    formBox.appendChild(box);
    fCmd.focus();
  }
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("Add hook", form, "primary")));
  pane.appendChild(foot);
  api("GET", "/api/hooks").then(draw).catch(toastError);
};

PAGES.commands = function (pane) {
  pageHead(pane, "Commands", "Shortcuts you type with a slash. Each one is a saved instruction you can change.");
  if (!S.commands.length) pane.appendChild(h("div", "empty", "No commands of your own yet."));
  S.commands.forEach(function (c) {
    pane.appendChild(row("mrow", codeTitle("/" + c.name), c.description || "No description",
      buttonRow(button("Edit", function () { editCommand(c); }), button("Remove", function () {
        askConfirm("Remove /" + c.name + "?", "The command file is deleted from your commands folder.", "Remove", function () {
          api("DELETE", "/api/commands/" + encodeURIComponent(c.name)).then(function (cmds) {
            S.commands = cmds; showPage("commands");
          }).catch(toastError);
        });
      }))));
  });
  var foot = h("div", "pgfoot");
  foot.appendChild(buttonRow(button("New command", function () { editCommand(null); }, "primary")));
  pane.appendChild(foot);
  pane.appendChild(h("h3", "", "Built in"));
  BUILTIN_COMMANDS.forEach(function (c) { pane.appendChild(row("mrow", codeTitle("/" + c[0]), c[1])); });
  pane.appendChild(h("h3", "", "Keyboard"));
  pane.appendChild(shortcutGrid());
};

/* Every shortcut, in one place: the ? sheet and Settings > Commands both show this list. */
/* Shortcuts you can change: each action once, with its default keys. The key handler (boot.js)
   looks a press up here; Settings > Keyboard changes them. Keys in the message box (Enter,
   Shift Tab, arrows, Esc) are not here: they stay as they are. */
var KEY_ACTIONS = [
  ["search", "Search everything", "Ctrl+K", function () { if ($("cmdk-modal").classList.contains("open")) closeDialog("cmdk-modal"); else openCmdk(); }],
  ["new-session", "New session", "Alt+N", function () { newSession(); }],
  ["close-session", "Close the session", "Ctrl+W", function () { newSession(); }, function () { return !!S.current; }],
  ["prev-session", "Previous session", "Ctrl+Shift+[", function () { stepSession(-1); }, function () { return S.list.length > 1; }],
  ["next-session", "Next session", "Ctrl+Shift+]", function () { stepSession(1); }, function () { return S.list.length > 1; }],
  ["mode-menu", "Mode menu", "Ctrl+Shift+M", function () { $("perm-btn").click(); }],
  ["model-menu", "Model menu", "Ctrl+Shift+I", function () { $("model-btn").click(); }],
  ["effort-menu", "Effort", "Ctrl+Shift+E", function () {
    openMenu("model-menu");
    setTimeout(function () { var m = $("model-menu"); if (m._effort) m._effort._open(); }, 0);
  }],
  ["changes", "Changes", "Ctrl+Shift+D", function () { toggleChanges(); }],
  ["activity", "Show or hide Activity", "Ctrl+.", function () { showActivity($("inspector").classList.contains("hidden")); }],
  ["terminal", "Terminal", "Ctrl+`", function () { toggleTerm(); }],
  ["side-chat", "Side chat", "Ctrl+;", function () { if ($("side").classList.contains("hidden")) openSide(); else closeSide(); }],
  ["close-pane", "Close the pane", "Ctrl+\\", function () { closePanes(null); }],
  ["view", "Normal or verbose transcript", "Ctrl+O", function () { cycleView(); }],
  ["folder", "Change folder", "Ctrl+Shift+O", function () { openBrowse(currentFolder()); }],
  ["checkpoint", "New checkpoint", "Ctrl+S", function () { checkpoint(); }],
  ["background", "Move the running command to the background", "Ctrl+B", function () { toBackground(); }, function () { return !!S.workStart; }],
  ["settings", "Settings", "Ctrl+,", function () { openSettings(); }],
  ["shortcuts", "Keyboard shortcuts", "Ctrl+/", function () { openShortcuts(); }]
];
function stepSession(dir) {
  var i = S.list.findIndex(function (x) { return x.id === S.current; });
  openSession(S.list[(i + (dir < 0 ? S.list.length - 1 : 1)) % S.list.length].id);
}
function keysFor(id) {
  var mine = (S.settings && S.settings.ui && S.settings.ui.keys) || {};
  var a = KEY_ACTIONS.filter(function (x) { return x[0] === id; })[0];
  return mine[id] != null ? mine[id] : (a ? a[2] : "");
}
// a key press as text: "Ctrl+Shift+K" (Cmd counts as Ctrl); "" for a bare modifier
var KEY_CODES = { Backslash: "\\", Backquote: "`", Comma: ",", Period: ".", Semicolon: ";", Slash: "/", BracketLeft: "[",
  BracketRight: "]", Minus: "-", Equal: "=", Quote: "'", Space: "Space", Tab: "Tab", Enter: "Enter" };
function comboOf(e) {
  var k = e.code || "";
  var key = /^Key[A-Z]$/.test(k) ? k.slice(3) : /^Digit\d$/.test(k) ? k.slice(5) : /^F\d{1,2}$/.test(k) ? k : KEY_CODES[k];
  if (!key) return "";
  return (e.ctrlKey || e.metaKey ? "Ctrl+" : "") + (e.altKey ? "Alt+" : "") + (e.shiftKey ? "Shift+" : "") + key;
}
function keyAction(e) {
  var combo = comboOf(e);
  if (!combo) return null;
  return KEY_ACTIONS.filter(function (a) { return keysFor(a[0]) === combo && (!a[4] || a[4]()); })[0] || null;
}
function keyLabel(combo) { return combo ? combo.replace(/\+/g, " ") : "None"; }

PAGES.keyboard = function (pane) {
  pageHead(pane, "Keyboard", "Click a shortcut, then press the keys you want. Esc cancels, Backspace turns it off.");
  var list = h("div", "keylist");
  pane.appendChild(list);
  function draw() {
    list.innerHTML = "";
    KEY_ACTIONS.forEach(function (a) {
      var now = keysFor(a[0]), changed = now !== a[2];
      var row = list.appendChild(h("div", "keyrow"));
      row.appendChild(h("span", "", a[1]));
      var b = row.appendChild(h("button", "keybtn" + (changed ? " changed" : ""), keyLabel(now)));
      b.type = "button";
      b.title = changed ? "Default: " + keyLabel(a[2]) : "Click and press new keys";
      b.onclick = function () { listen(a, b); };
      var reset = row.appendChild(h("button", "btn link", changed ? "Reset" : ""));
      reset.type = "button";
      reset.disabled = !changed;
      reset.onclick = function () { setKeys(a[0], null); };
    });
  }
  function setKeys(id, combo) {
    var keys = {};
    Object.keys(S.settings.ui.keys || {}).forEach(function (k) { keys[k] = S.settings.ui.keys[k]; });
    var a = KEY_ACTIONS.filter(function (x) { return x[0] === id; })[0];
    if (combo === null || combo === a[2]) delete keys[id]; else keys[id] = combo;
    return saveUi("keys", keys).then(draw);
  }
  function listen(a, b) {
    if (S.keyGrab) S.keyGrab();   // another shortcut was waiting for keys: it stops
    b.textContent = "Press keys\u2026";
    b.classList.add("listening");
    function done() { document.removeEventListener("keydown", grab, true); b.classList.remove("listening"); S.keyGrab = null; }
    S.keyGrab = function () { done(); b.textContent = keyLabel(keysFor(a[0])); };   // only this button goes back
    function grab(e) {
      if (!document.body.contains(b)) { done(); return; }   // the page was closed: the key is not ours
      e.preventDefault(); e.stopPropagation();
      if (e.key === "Escape") { done(); draw(); return; }
      if (e.key === "Backspace" || e.key === "Delete") { done(); setKeys(a[0], ""); return; }
      var combo = comboOf(e);
      if (!combo) return;   // only a modifier so far: wait for the key
      if (!/^(Ctrl|Alt)\+/.test(combo) && !/^F\d/.test(combo)) { b.textContent = "Add Ctrl or Alt"; return; }
      var taken = KEY_ACTIONS.filter(function (x) { return x[0] !== a[0] && keysFor(x[0]) === combo; })[0];
      if (taken) { b.textContent = keyLabel(combo) + " is " + taken[1]; return; }
      done();
      setKeys(a[0], combo);
    }
    document.addEventListener("keydown", grab, true);
  }
  draw();
  var foot = pane.appendChild(h("div", "btnrow"));
  var all = foot.appendChild(h("button", "btn", "Reset all to the defaults"));
  all.type = "button";
  all.onclick = function () { saveUi("keys", {}).then(draw); };
};

var SHORTCUTS = [
  ["In the message box", [["Send", "Enter"], ["New line", "Shift Enter"], ["Switch mode: Manual, Accept edits, Plan", "Shift Tab"],
    ["Earlier messages you sent", "\u2191 \u2193"], ["Search messages you sent", "Ctrl R"], ["Commands", "/"], ["Mention a file", "@"],
    ["Remember a note", "# note"]]],
  ["While the agent works", [["Stop", "Esc"], ["Move the running command to the background", "Ctrl B"]]],
  ["Anywhere", [["Rewind or fork from a message", "Esc Esc"], ["Search everything", "Ctrl K"], ["New session", "Alt N"],
    ["Next or previous session", "Ctrl Tab"], ["Next or previous session", "Ctrl Shift ] ["], ["Close the session", "Ctrl W"], ["Normal or verbose transcript", "Ctrl O"],
    ["Mode menu", "Ctrl Shift M"], ["Model menu", "Ctrl Shift I"], ["Pick an item in an open menu", "1–9"],
    ["Changes", "Ctrl Shift D"], ["Show or hide Activity", "Ctrl ."], ["Side chat", "Ctrl ;"], ["Close the pane", "Ctrl \\"],
    ["Change folder", "Ctrl Shift O"], ["New checkpoint", "Ctrl S"], ["Settings", "Ctrl ,"],
    ["Focus the message box", "/"], ["This list", "Ctrl /"]]]
];

function shortcutGrid() {
  var box = h("div");
  var mine = [["Anywhere", KEY_ACTIONS.filter(function (a) { return keysFor(a[0]); }).map(function (a) { return [a[1], keyLabel(keysFor(a[0]))]; })
    .concat([["Rewind or fork from a message", "Esc Esc"], ["Next or previous session", "Ctrl Tab"], ["Pick an item in an open menu", "1\u20139"],
             ["Focus the message box", "/"]])]];
  SHORTCUTS.filter(function (g) { return g[0] !== "Anywhere"; }).concat(mine).forEach(function (group) {
    box.appendChild(h("div", "kgroup", group[0]));
    var keys = h("div", "keys");
    group[1].forEach(function (k) {
      keys.appendChild(h("span", "", k[0]));
      keys.appendChild(h("kbd", "", k[1]));
    });
    box.appendChild(keys);
  });
  return box;
}
