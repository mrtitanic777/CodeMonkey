"use strict";
/* CodeMonkey page, core.js: Utilities, markdown, and the page's state.
   The page's scripts share one global scope and load in order (see index.html). */

/* CodeMonkey console client. Dependency-free. */

/* ---------------- utilities ---------------- */

function $(id) { return document.getElementById(id); }

/* Line icons (1.6px stroke). One drawing per meaning, used everywhere. */
var I = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4-4"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M7 10l5 5 5-5"/>',
  clock: '<path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="9"/>',
  folder: '<path d="M3 7a2 2 0 012-2h4l2 2.5h8a2 2 0 012 2V17a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-6 7"/>',
  folderPlus: '<path d="M3 7a2 2 0 012-2h4l2 2.5h8a2 2 0 012 2V17a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/><path d="M12 11v5M9.5 13.5h5"/>',
  file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  image: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 15l4-4 4 4 3-3 5 5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>',
  retry: '<path d="M3 12a9 9 0 109-9 9.7 9.7 0 00-6.7 2.8L3 8"/><path d="M3 3v5h5"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="5" cy="6" r="1"/><circle cx="5" cy="12" r="1"/><circle cx="5" cy="18" r="1"/>',
  warn: '<path d="M12 8v5M12 16.5v.5"/><circle cx="12" cy="12" r="9"/>',
  pencil: '<path d="M4 20h4l10-10-4-4L4 16v4z"/>',
  split: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M12 5v14"/>',
  popout: '<path d="M14 4h6v6M20 4l-8 8M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  star: '<path d="M12 3l2.5 5.5L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.5-.5z"/>',
  trash: '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
  more: '<circle cx="6" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="18" cy="12" r="1"/>',
  chat: '<path d="M5 6h14v10H9l-4 3z"/>',
  term: '<path d="M4 17l6-5-6-5M12 19h8"/>',
  sliders: '<path d="M4 8h9M19 8h1M4 16h3M13 16h7"/><circle cx="16" cy="8" r="2.2"/><circle cx="10" cy="16" r="2.2"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 010 11H11"/>'
};
function svg(name, cls) {
  return '<svg viewBox="0 0 24 24"' + (cls ? ' class="' + cls + '"' : "") + ' aria-hidden="true">' + I[name] + "</svg>";
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* Build an element: h("div", "cls", "text"). Text is always set as text, never as markup. */
function h(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function api(method, path, body) {
  var opts = { method: method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  return fetch(path, opts).then(function (r) {
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.error || ("HTTP " + r.status));
      });
    }
    return r.json();
  });
}

function fmtNum(n) {
  n = +n || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 10000) return Math.round(n / 1000) + "k";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(Math.round(n));
}
function fmtElapsed(ms) {
  if (ms < 1000) return Math.round(ms) + "ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + "s";
  return fmtDuration(ms / 1000);
}
function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + (s === 1 ? " second" : " seconds");
  var m = Math.floor(s / 60);
  if (m < 60) return m + "m " + (s % 60) + "s";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}
/* Short relative time for the session list: now, 5m, 2h, 3d. */
function shortAgo(iso) {
  var t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  if (s < 86400 * 30) return Math.floor(s / 86400) + "d";
  return new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
}
function baseName(path) {
  var parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || String(path || "");
}
function copyText(text, done) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, done);
  } else { done(); }
}
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {}
  return null;
}

/* ---------------- markdown-lite ---------------- */

var KEYWORDS = /\b(import|from|def|class|return|if|elif|else|for|while|try|except|finally|with|as|pass|raise|lambda|yield|await|async|function|const|let|var|new|typeof|instanceof|this|true|false|null|undefined|switch|case|break|continue|do|in|of|export|default|extends|super|static|get|set|None|True|False|and|or|not|is|print)\b/g;

function highlight(code) {
  var h2 = esc(code);
  // strings first (protect contents), then comments, keywords, numbers.
  h2 = h2.replace(/(&quot;.*?&quot;|'[^'\n]*'|`[^`]*`)/g, "\u0001$1\u0002");
  h2 = h2.replace(/(^|\s)(#[^\n]*|\/\/[^\n]*)/gm, "$1\u0003$2\u0004");
  h2 = h2.replace(KEYWORDS, "\u0005$1\u0006");
  h2 = h2.replace(/\b(\d+(?:\.\d+)?)\b/g, "\u0007$1\u0008");
  return h2
    .replace(/\u0001/g, '<span class="tok-s">').replace(/\u0002/g, "</span>")
    .replace(/\u0003/g, '<span class="tok-c">').replace(/\u0004/g, "</span>")
    .replace(/\u0005/g, '<span class="tok-k">').replace(/\u0006/g, "</span>")
    .replace(/\u0007/g, '<span class="tok-n">').replace(/\u0008/g, "</span>");
}

/* ![description](path): a picture saved in the project, shown in the reply (the server checks it is a
   picture inside the project). A web address is never loaded: a picture's address can carry text out
   to any server, so it stays a link, and the model saves the picture first (picture_save). */
function pictureHtml(m, alt, src) {
  var raw = src.replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  if (/^https?:\/\//i.test(raw)) {
    return '<a href="' + src + '" target="_blank" rel="noopener noreferrer">picture: ' + (alt || "link") + "</a>";
  }
  if (!S.current || /^[a-z]+:/i.test(raw)) return m;
  var url = "/api/sessions/" + S.current + "/picture?path=" + encodeURIComponent(raw.replace(/^\.\//, ""));
  return '<img class="replypic" loading="lazy" src="' + url + '" alt="' + alt + '" title="' + (alt || src) + '">';
}

document.addEventListener("click", function (e) {   // a picture in a reply: click for full size, again to shrink
  var pic = e.target && e.target.closest ? e.target.closest("img.replypic") : null;
  if (pic) pic.classList.toggle("big");
});

function renderInline(s) {
  // inline code is protected first, then links, bold and italics
  var parts = [];
  var out = esc(s).replace(/`([^`]+)`/g, function (m, c) {
    parts.push(c);
    return "@@CODE" + (parts.length - 1) + "@@";
  });
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, pictureHtml);
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  out = out.replace(/\[\[([^\]|]{1,40})\|([^\]]{1,240})\]\]/g, function (m, w, d) { return termHtml(w, d); });   // Beginner mode's words
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  return out.replace(/@@CODE(\d+)@@/g, function (m, n) { return "<code>" + parts[+n] + "</code>"; });
}

function splitRow(line) {
  var t = line.trim();
  if (t.charAt(0) === "|") t = t.slice(1);
  if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
  return t.split("|").map(function (c) { return c.trim(); });
}

function renderMarkdown(src) {
  var lines = String(src || "").split("\n"), out = [], i = 0, para = [];
  function flushPara() {
    if (para.length) { out.push("<p>" + renderInline(para.join(" ")) + "</p>"); para = []; }
  }
  function listOf(re, tag) {
    var items = [];
    while (i < lines.length) {
      var m = lines[i].match(re);
      if (!m) break;
      items.push("<li>" + renderInline(m[1]) + "</li>"); i++;
    }
    out.push("<" + tag + ">" + items.join("") + "</" + tag + ">");
  }
  while (i < lines.length) {
    var line = lines[i];
    var fence = line.match(/^\s*```([\w+-]*)\s*$/);
    if (fence) {
      flushPara();
      var lang = fence[1], buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      if (lang === "mermaid") {   // drawn once the reply is complete (drawDiagrams); the code until then
        out.push('<div class="codeblock mermaid-src" data-src="' + esc(buf.join("\n")) + '"><div class="lang"><span>diagram</span></div>' +
          "<pre><code>" + esc(buf.join("\n")) + "</code></pre></div>");
        continue;
      }
      if (lang === "choices") {   // Beginner mode: answers as buttons
        var choices = choicesHtml(buf);
        if (choices) { out.push(choices); continue; }
      }
      if (lang === "findings") {
        try { out.push(reviewCardHtml(JSON.parse(buf.join("\n") || "[]"))); continue; } catch (e) {}
      }
      out.push('<div class="codeblock"><div class="lang"><span>' + esc(lang || "") + "</span>" +
        '<button class="code-copy" type="button" title="Copy code">copy</button></div>' +
        "<pre><code>" + highlight(buf.join("\n")) + "</code></pre></div>");
      continue;
    }
    // a table: a row of cells followed by a |---|---| line
    if (line.indexOf("|") >= 0 && i + 1 < lines.length &&
        /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      flushPara();
      var head = splitRow(line), rows = [];
      i += 2;
      while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim()) { rows.push(splitRow(lines[i])); i++; }
      out.push('<div class="tablewrap"><table><thead><tr>' +
        head.map(function (c) { return "<th>" + renderInline(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
        rows.map(function (r) {
          return "<tr>" + head.map(function (_, k) { return "<td>" + renderInline(r[k] || "") + "</td>"; }).join("") + "</tr>";
        }).join("") + "</tbody></table></div>");
      continue;
    }
    var hd = line.match(/^(#{1,4})\s+(.*)$/);
    if (hd) { flushPara(); out.push("<h3>" + renderInline(hd[2]) + "</h3>"); i++; continue; }
    if (/^\s*[-*]\s+/.test(line)) { flushPara(); listOf(/^\s*[-*]\s+(.*)$/, "ul"); continue; }
    if (/^\s*\d+[.)]\s+/.test(line)) { flushPara(); listOf(/^\s*\d+[.)]\s+(.*)$/, "ol"); continue; }
    if (/^\s*$/.test(line)) { flushPara(); i++; continue; }
    para.push(line.trim()); i++;
  }
  flushPara();
  return out.join("\n") || "<p></p>";
}

/* Diff: file name, +/- counts, line numbers, tinted rows. */
function renderDiff(text, filename) {
  var wrap = h("div", "diff");
  var adds = 0, dels = 0, newNo = 0, oldNo = 0, inHunk = false, firstLine = 0;
  var rows = document.createDocumentFragment();
  var all = String(text).replace(/\n+$/, "").split("\n");
  var hasHunks = all.some(function (l) { return /^@@ /.test(l); });
  all.forEach(function (line) {
    if (/^(---|\+\+\+) /.test(line)) return;
    if (hasHunks && !inHunk && !/^@@ /.test(line)) return;   // a heading before the first hunk
    var m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    var row = h("div", "l"), ln = h("span"), dg = h("span"), lc = h("span");
    if (m) {
      oldNo = +m[1]; newNo = +m[2]; inHunk = true;
      if (!firstLine) { firstLine = newNo; return; }
      row.classList.add("hunk"); lc.textContent = "⋯";
    } else if (line.charAt(0) === "+") {
      row.classList.add("add"); adds++; dg.textContent = "+";
      ln.textContent = inHunk ? newNo++ : ""; lc.textContent = line.slice(1);
    } else if (line.charAt(0) === "-") {
      row.classList.add("del"); dels++; dg.textContent = "−";
      ln.textContent = inHunk ? oldNo++ : ""; lc.textContent = line.slice(1);
    } else {
      if (inHunk) { ln.textContent = newNo++; oldNo++; }
      lc.textContent = line.charAt(0) === " " ? line.slice(1) : line;
    }
    row.appendChild(ln); row.appendChild(dg); row.appendChild(lc);
    rows.appendChild(row);
  });
  var head = h("div", "h");
  head.appendChild(h("span", "", filename || "changes"));
  head.appendChild(h("span", "", "+" + adds + " −" + dels + (firstLine ? " · line " + firstLine : "")));
  wrap.appendChild(head); wrap.appendChild(rows);
  wrap._adds = adds; wrap._dels = dels;
  return wrap;
}

/* The same diff side by side: the file before on the left, after on the right. Removed lines
   are paired with the added lines that replace them; unchanged lines show on both sides. */
function renderSplitDiff(text, filename) {
  var wrap = h("div", "diff split");
  var adds = 0, dels = 0, oldNo = 0, newNo = 0, inHunk = false;
  var rows = document.createDocumentFragment(), dq = [], aq = [];
  var all = String(text).replace(/\n+$/, "").split("\n");
  var hasHunks = all.some(function (l) { return /^@@ /.test(l); });
  function cell(row, cls, no, txt) {
    var c = row.appendChild(h("div", "sc " + cls));
    c.appendChild(h("span", "", no == null ? "" : String(no)));
    c.appendChild(h("span", "", txt == null ? "" : txt));
  }
  function flush() {   // removed and added lines, paired row by row
    for (var i = 0; i < Math.max(dq.length, aq.length); i++) {
      var row = h("div", "sl"), d = dq[i], a = aq[i];
      cell(row, d ? "del" : "none", d ? d[0] : null, d ? d[1] : null);
      cell(row, a ? "add" : "none", a ? a[0] : null, a ? a[1] : null);
      rows.appendChild(row);
    }
    dq = []; aq = [];
  }
  all.forEach(function (line) {
    if (/^(---|\+\+\+) /.test(line)) return;
    if (hasHunks && !inHunk && !/^@@ /.test(line)) return;
    var m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (m) {
      flush();
      if (inHunk) { var gap = h("div", "sl hunk"); cell(gap, "same", null, "\u22EF"); cell(gap, "same", null, "\u22EF"); rows.appendChild(gap); }
      oldNo = +m[1]; newNo = +m[2]; inHunk = true;
      return;
    }
    var c = line.charAt(0), body = c === "+" || c === "-" || c === " " ? line.slice(1) : line;
    if (c === "-") { dels++; dq.push([oldNo++, body]); return; }
    if (c === "+") { adds++; aq.push([newNo++, body]); return; }
    flush();
    var row = h("div", "sl");
    cell(row, "same", oldNo++, body);
    cell(row, "same", newNo++, body);
    rows.appendChild(row);
  });
  flush();
  var head = h("div", "h");
  head.appendChild(h("span", "", filename || "changes"));
  head.appendChild(h("span", "", "+" + adds + " \u2212" + dels));
  wrap.appendChild(head); wrap.appendChild(rows);
  wrap._adds = adds; wrap._dels = dels;
  return wrap;
}

/* Diagrams: Mermaid blocks in a reply are drawn as pictures. The library (webui/static/vendor,
   MIT) is loaded the first time a reply has one; a block it cannot draw stays as code. */
var mermaidReady = null, diagramSeq = 0;
function loadMermaid() {
  if (!mermaidReady) mermaidReady = new Promise(function (ok, fail) {
    var s = document.createElement("script");
    s.src = "/vendor/mermaid.min.js";
    s.onload = function () { themeMermaid(); ok(window.mermaid); };
    s.onerror = function () { mermaidReady = null; fail(new Error("the diagram library did not load")); };
    document.head.appendChild(s);
  });
  return mermaidReady;
}
function themeMermaid() {
  var dark = !document.documentElement.hasAttribute("data-light");
  window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "neutral",
    fontFamily: getComputedStyle(document.body).fontFamily });
  return dark;
}
/* The theme changed: diagrams already drawn are drawn again in its colours. */
function redrawDiagrams() {
  if (!mermaidReady || !window.mermaid) return;
  var dark = !document.documentElement.hasAttribute("data-light");
  if (S.diagramDark === dark) return;
  S.diagramDark = themeMermaid();
  document.querySelectorAll(".mermaid-src").forEach(function (b) {
    if (!b._drawn) return;
    var fig = b.previousElementSibling;
    if (fig && fig.classList.contains("diagram")) fig.remove();
    b._drawn = false;
    b.classList.remove("hidden");
  });
  drawDiagrams(document);
}
function drawDiagrams(root) {
  var blocks = Array.prototype.filter.call((root || document).querySelectorAll(".mermaid-src"), function (b) { return !b._drawn; });
  if (!blocks.length) return;
  loadMermaid().then(function (mm) {
    S.diagramDark = !document.documentElement.hasAttribute("data-light");
    blocks.forEach(function (b) {
      b._drawn = true;
      var src = b.getAttribute("data-src") || "";
      mm.render("cm-diagram-" + (++diagramSeq), src).then(function (r) {
        var fig = h("div", "diagram");
        fig.innerHTML = r.svg;
        var bar = fig.appendChild(h("div", "dgbar"));
        var show = bar.appendChild(h("button", "btn link", "Show code"));
        show.type = "button";
        show.onclick = function () {
          var code = !b.classList.contains("hidden");
          b.classList.toggle("hidden", code);
          show.textContent = code ? "Show code" : "Hide code";
        };
        b.parentNode.insertBefore(fig, b);
        b.classList.add("hidden");
      }).catch(function () {
        b.querySelector(".lang span").textContent = "diagram (could not be drawn)";
        var stray = document.getElementById("dcm-diagram-" + diagramSeq);   // mermaid leaves its error box behind
        if (stray) stray.remove();
      });
    });
  }).catch(function () {});
}

/* ---------------- state ---------------- */

var S = {
  list: [],            // every session on record: {id, title, project_dir, last_active, live}
  status: {},          // session id -> idle | working | permission | error
  current: null,       // open session id; null = a new session not created yet
  snap: null,          // latest snapshot of the open session
  pendingFolder: null, // folder picked before the session exists ("" = no folder)
  pendingSettings: {}, // model, effort and permissions picked before the session exists
  es: null,
  config: null,
  settings: null,
  commands: [],
  folders: [],
  todos: [],
  subagents: {},       // id -> {brief, summary, ok, done, started}
  attachments: [],
  turn: null,          // {el, work, steps, failed, started, lastUser}
  sentIds: {},         // messages this page sent, so its own echo is not shown twice
  palette: { open: false, items: [], index: 0 },
  perm: null,
  confirmCb: null, promptCb: null,
  cmdk: { results: [], index: 0, files: [] },
  page: "general",
  lastRetry: null,
  workStart: 0, liveStep: ""
};

var PERM_LABEL = {   // Claude Code's names and lines
  auto: ["Auto", "CodeMonkey handles permission decisions"],
  ask: ["Manual", "Always ask before making changes"],
  edits: ["Accept edits", "Automatically accept all file edits"],
  plan: ["Plan", "Create a plan before making changes"],
  bypass: ["Bypass permissions", "Accepts all permissions"]
};
var PERM_PLAIN = {   // what the chip under the message box says: the mode in plain words
  auto: "Decides for itself", ask: "Asks first", edits: "Edits without asking", plan: "Plans first", bypass: "Never asks"
};
var MODE_ORDER = ["auto", "ask", "edits", "plan", "bypass"];
var MODE_CYCLE = ["ask", "edits", "plan"];   // Shift+Tab, as in Claude Code (Auto only from the menu)

function cycleMode() {
  var now = effective().permission_mode, i = MODE_CYCLE.indexOf(now);
  var next = MODE_CYCLE[(i + 1) % MODE_CYCLE.length];
  sessionSetting({ permission_mode: next });
}

function sessionEntry(id) {
  for (var i = 0; i < S.list.length; i++) if (S.list[i].id === id) return S.list[i];
  return null;
}
function currentFolder() {
  if (S.snap) return S.snap.no_project ? "" : S.snap.project_dir;
  if (S.pendingFolder != null) return S.pendingFolder;
  return S.config ? S.config.default_project : "";
}
function folderName(path) {
  if (!path || (S.config && path === S.config.chat_dir)) return "No folder";
  return baseName(path);
}
