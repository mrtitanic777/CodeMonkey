// CodeMonkey desktop shell.
//
// Starts the Python web UI (webui.py) on a free loopback port, waits for it
// to say it is listening, and shows it in an Electron window. Closing the
// window stops the server and everything it started.
//
// The Python interpreter is python\python.exe beside the exe when there is
// one (the standalone installer), else `python` on Windows and `python3`
// elsewhere; set CODEMONKEY_PYTHON to use another one.
//
// Packaged (CodeMonkey.exe), the Python code is not inside the app: the exe
// lives in desktop/dist/ and finds the CodeMonkey folder by looking upward
// for webui.py. Installed (installer.nsi), a root.txt beside the exe names
// the folder instead. The Linux, Raspberry Pi and Mac builds (scripts/make-unix.js)
// carry the code in resources/codemonkey and run it from a copy in the user's
// data folder, where sessions and settings stay across updates.
// CODEMONKEY_ROOT overrides all of these.
//
// Updates (installed copies only): a newer dist/CodeMonkey-Setup-<version>.exe in the CodeMonkey
// folder (npm run release) is found at start and every 10 minutes; the page then shows "Relaunch
// to update", which runs that installer silently and opens the new version.

const { app, BrowserWindow, shell, dialog, ipcMain } = require("electron");
const { spawn, execFile } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

// The code a Linux/Mac build carries, copied to <user data>/app when it is new or changed. Only
// the code is replaced: agent-workspace (sessions, settings, memory), mcp.json and .env stay.
function unpackBundledCode() {
  const bundled = path.join(process.resourcesPath || "", "codemonkey");
  if (process.platform === "win32" || !fs.existsSync(path.join(bundled, "webui.py"))) return null;
  const dest = path.join(app.getPath("userData"), "app");
  const stamp = (dir) => { try { return fs.readFileSync(path.join(dir, ".bundle-version"), "utf8"); } catch (_) { return ""; } };
  if (!fs.existsSync(path.join(dest, "webui.py")) || stamp(dest) !== stamp(bundled)) {
    const KEEP = new Set(["agent-workspace", "mcp.json", ".env"]);
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(dest)) {
      if (!KEEP.has(name)) fs.rmSync(path.join(dest, name), { recursive: true, force: true });
    }
    fs.cpSync(bundled, dest, { recursive: true });
  }
  return dest;
}

function findRoot() {
  if (process.env.CODEMONKEY_ROOT) return path.resolve(process.env.CODEMONKEY_ROOT);
  const unpacked = unpackBundledCode();
  if (unpacked) return unpacked;
  try {
    const named = fs.readFileSync(path.join(path.dirname(process.execPath), "root.txt"), "utf8").trim();
    if (named && fs.existsSync(path.join(named, "webui.py"))) return named;
  } catch (e) { /* not installed: look upward */ }
  for (const start of [path.dirname(process.execPath), __dirname]) {
    let dir = start;
    for (;;) {
      if (fs.existsSync(path.join(dir, "webui.py"))) return dir;
      const up = path.dirname(dir);
      if (up === dir) break;
      dir = up;
    }
  }
  return path.resolve(__dirname, "..");
}

const ROOT = findRoot();
const ICON = path.join(__dirname, "build", "icon.png");
// the standalone installer (scripts/make-standalone.js) puts its own Python beside the exe
const BUNDLED_PYTHON = path.join(path.dirname(process.execPath), "python", "python.exe");
const HAS_BUNDLED = fs.existsSync(BUNDLED_PYTHON);
const SHIPPED = HAS_BUNDLED || ROOT.startsWith(app.getPath("userData"));   // a standalone or Linux/Mac build
// Opened from the Finder or a desktop menu, the app gets a short PATH (/usr/bin:/bin on a Mac), so
// the usual homes of Python, git and node are added: Homebrew, python.org, ~/.local/bin.
const EXTRA_PATH = process.platform === "win32" ? [] : ["/opt/homebrew/bin", "/usr/local/bin",
  "/Library/Frameworks/Python.framework/Versions/Current/bin", path.join(require("os").homedir(), ".local", "bin")]
  .filter((dir) => fs.existsSync(dir) && !(process.env.PATH || "").split(":").includes(dir));
const UNIX_PATH = [...EXTRA_PATH, process.env.PATH || "/usr/bin:/bin"].join(":");
function unixPython() {   // the first python3 on that PATH; a Mac's /usr/bin/python3 is last (it may be the Xcode stub)
  for (const dir of UNIX_PATH.split(":")) {
    if (dir === "/usr/bin" && process.platform === "darwin") continue;
    if (fs.existsSync(path.join(dir, "python3"))) return path.join(dir, "python3");
  }
  return "python3";
}
const PYTHON = process.env.CODEMONKEY_PYTHON || (HAS_BUNDLED ? BUNDLED_PYTHON
  : (process.platform === "win32" ? "python" : unixPython()));
const START_TIMEOUT_MS = 60000;

let server = null;
let serverLog = "";
let win = null;
let quitting = false;
let serverUrl = null;   // set once the server answers; until then the window shows SPLASH
let pendingUpdate = null;   // { version, file } of a newer installer, once one is found
const INSTALLED = fs.existsSync(path.join(path.dirname(process.execPath), "root.txt"));
const UPDATE_EVERY_MS = 10 * 60 * 1000;

function newer(a, b) {   // is version a newer than b ("1.2.3")
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0);
  return false;
}

function findUpdate() {
  const dir = path.join(ROOT, "desktop", "dist");
  let best = null;
  try {
    for (const name of fs.readdirSync(dir)) {
      const m = /^CodeMonkey-Setup-(\d+\.\d+\.\d+)\.exe$/.exec(name);
      if (m && newer(m[1], best ? best.version : app.getVersion())) best = { version: m[1], file: path.join(dir, name) };
    }
  } catch (_) { /* no builds yet */ }
  return best;
}

function checkForUpdate() {
  if (!INSTALLED) return;   // a copy run from the folder itself is updated by building it
  const found = findUpdate();
  if (!found || (pendingUpdate && pendingUpdate.version === found.version)) return;
  pendingUpdate = found;
  if (win && serverUrl) win.webContents.send("update-ready", found.version);
}

// Choose a folder with the system's own folder picker (the page's chooser is the fallback in a browser).
ipcMain.handle("pick-folder", async (_event, start) => {
  const result = await dialog.showOpenDialog(win, {
    title: "Choose the folder to work in",
    buttonLabel: "Work in this folder",
    defaultPath: typeof start === "string" && start ? start : app.getPath("documents"),
    properties: ["openDirectory", "createDirectory"],
  });
  return result.canceled || !result.filePaths.length ? "" : result.filePaths[0];
});

ipcMain.on("install-update", () => {
  if (!pendingUpdate || !fs.existsSync(pendingUpdate.file)) return;
  // the installer waits for this app to close, replaces it, and opens the new one
  spawn(pendingUpdate.file, ["/S", "/RELAUNCH", `/D=${path.dirname(process.execPath)}`],
    { detached: true, stdio: "ignore", windowsHide: true }).unref();
  quitting = true;
  stopServer();
  app.quit();
});

// Shown the moment the app opens, while Python starts (1-5 seconds).
const SPLASH = "data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><html><head>
<meta charset="utf-8"><title>CodeMonkey</title><style>
html,body{height:100%;margin:0;background:#15130f;color:#9b9384;font:14px system-ui,sans-serif}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
svg{width:72px;height:72px}
p{margin:0;letter-spacing:.02em;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.45}}</style></head><body>
<svg viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#1f1c17"/><path d="M11 11l-5 5 5 5M21 11l5 5-5 5"
fill="none" stroke="#d26a43" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
<p>Starting CodeMonkey…</p></body></html>`);

// The same port every launch when it is free: the page's address stays the same, so its cache
// (fonts, scripts) and what it keeps in browser storage carry over from one launch to the next.
const PREFERRED_PORT = 47811;

function freePort(port = PREFERRED_PORT) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on("error", (err) => (port ? freePort(0).then(resolve, reject) : reject(err)));
    probe.listen(port, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function startServer(port) {
  return new Promise((resolve, reject) => {
    // a copy given to someone else starts new sessions in their home folder, not in CodeMonkey's own code
    const project = SHIPPED ? ["--project", require("os").homedir()] : [];
    server = spawn(PYTHON, ["-u", "webui.py", "--port", String(port), ...project], {
      cwd: ROOT,
      // with a bundled Python, `python` in the agent's shell commands is that one too
      env: { ...process.env, PYTHONIOENCODING: "utf-8",
        ...(HAS_BUNDLED && !process.env.CODEMONKEY_PYTHON
          ? { PATH: `${path.dirname(BUNDLED_PYTHON)};${process.env.PATH || ""}` } : {}),
        ...(process.platform === "win32" ? {} : { PATH: UNIX_PATH }) },
      windowsHide: true,
      detached: process.platform !== "win32",   // its own process group, so stopServer can end all of it
    });
    if (!fs.existsSync(path.join(ROOT, "webui.py"))) {
      reject(new Error(`CodeMonkey's files were not found (looked for webui.py above ${path.dirname(process.execPath)}).\n` +
        "Keep CodeMonkey.exe inside the CodeMonkey folder, or set CODEMONKEY_ROOT."));
      return;
    }
    const timer = setTimeout(() => reject(new Error("The server did not start within 60 seconds.")), START_TIMEOUT_MS);
    const onData = (chunk) => {
      const text = chunk.toString();
      serverLog = (serverLog + text).slice(-8000);
      process.stdout.write(text);
      if (/Web UI: http:\/\//.test(serverLog)) {
        clearTimeout(timer);
        resolve(`http://127.0.0.1:${port}/`);
      }
    };
    server.stdout.on("data", onData);
    server.stderr.on("data", onData);
    server.on("error", (err) => {
      clearTimeout(timer);
      reject(new Error(process.platform === "win32" || err.code !== "ENOENT"
        ? `Could not run "${PYTHON}": ${err.message}`
        : "CodeMonkey needs Python 3.10 or newer, and none was found.\n\n" + (process.platform === "darwin"
          ? "Install it from python.org (or: brew install python), then open CodeMonkey again."
          : "Install it (sudo apt install python3), then open CodeMonkey again.")));
    });
    server.on("exit", (code) => {
      clearTimeout(timer);
      server = null;
      if (!quitting) {
        reject(new Error(`The server stopped (exit code ${code}).`));
        if (win && serverUrl) {
          dialog.showErrorBox("CodeMonkey stopped", `The server stopped (exit code ${code}).\n\n${serverLog.slice(-2000)}`);
          app.quit();
        }
      }
    });
  });
}

function stopServer() {
  if (!server) return;
  const pid = server.pid;
  server = null;
  if (process.platform === "win32") {
    // Stop the whole tree: the server starts model runners, shells and jobs.
    execFile("taskkill", ["/pid", String(pid), "/T", "/F"], () => {});
  } else {
    try { process.kill(-pid, "SIGTERM"); } catch (_) {   // the group: model runners, shells, jobs
      try { process.kill(pid, "SIGTERM"); } catch (_) {}
    }
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 720,
    minHeight: 480,
    title: "CodeMonkey",
    backgroundColor: "#15130f",
    icon: fs.existsSync(ICON) ? ICON : undefined,
    autoHideMenuBar: true,
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true,
      preload: path.join(__dirname, "preload.js") },
  });
  win.removeMenu();
  win.once("ready-to-show", () => win.show());

  // Pages on the server stay in the window; anything else opens in the browser.
  const isLocal = (target) => serverUrl !== null && target.startsWith(serverUrl);
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    // a session or pane opened in a window of its own looks like this one
    if (isLocal(target)) return { action: "allow", overrideBrowserWindowOptions: {
      autoHideMenuBar: true, backgroundColor: "#15130f", icon: fs.existsSync(ICON) ? ICON : undefined,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } } };
    shell.openExternal(target);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, target) => {
    if (!isLocal(target)) {
      event.preventDefault();
      shell.openExternal(target);
    }
  });
  win.webContents.on("before-input-event", (_event, input) => {
    if (input.type === "keyDown" && input.key === "F12") win.webContents.toggleDevTools();
    if (input.type === "keyDown" && input.control && input.key.toLowerCase() === "r") win.webContents.reload();
  });
  win.webContents.on("did-finish-load", () => {   // a reloaded page is told again
    if (pendingUpdate && serverUrl) win.webContents.send("update-ready", pendingUpdate.version);
  });
  win.on("closed", () => { win = null; });
  win.loadURL(SPLASH);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    createWindow();
    try {
      const url = await startServer(await freePort());
      serverUrl = url;
      checkForUpdate();
      setInterval(checkForUpdate, UPDATE_EVERY_MS).unref();
      if (win) win.loadURL(url);
      else { quitting = true; stopServer(); app.quit(); }   // closed while it was starting
    } catch (err) {
      dialog.showErrorBox("CodeMonkey could not start", `${err.message}\n\n${serverLog.slice(-2000)}`);
      quitting = true;
      stopServer();
      app.quit();
    }
  });

  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => { quitting = true; stopServer(); });
  // Ended from outside (logging out, kill): the server is in its own process group, so it must be stopped too.
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
    process.on(signal, () => { quitting = true; stopServer(); app.quit(); });
  }
}
