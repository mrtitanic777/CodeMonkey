// Builds dist/CodeMonkey-<version>-Windows.exe (npm run standalone): an installer to give to someone
// else. Unlike CodeMonkey-Setup (which runs the Python code in this folder), it carries everything:
//
//   app\      the committed CodeMonkey code (git archive HEAD), so nothing local goes with it:
//             no agent-workspace (sessions, memory, models, library), no mcp.json, no .env. Saved
//             keys are in Windows Credential Manager, never in files.
//   python\   a copy of this machine's Python (stdlib only: the app needs no other packages),
//             without its docs, headers, Tcl/Tk, pip and site-packages.
//
// Its name does not match CodeMonkey-Setup-<version>.exe, so installed copies never take it as an update.
// CODEMONKEY_BUNDLE_PYTHON names the Python to copy (default: the `python` on PATH).

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktop = path.resolve(__dirname, "..");
const root = path.resolve(desktop, "..");
const version = require(path.join(desktop, "package.json")).version;
const stage = path.join(desktop, "dist", "standalone");

if (!fs.existsSync(path.join(desktop, "dist", "CodeMonkey-win32-x64", "CodeMonkey.exe"))) {
  console.error("Package the app first: npm run package");
  process.exit(1);
}
const dirty = execFileSync("git", ["status", "--porcelain", "--untracked-files=no"], { cwd: root, encoding: "utf8" }).trim();
if (dirty) console.warn("Note: uncommitted changes are not included (it ships what is committed):\n" + dirty);

fs.rmSync(stage, { recursive: true, force: true });
fs.mkdirSync(path.join(stage, "app"), { recursive: true });

// the code
const tarball = path.join(stage, "app.tar");
execFileSync("git", ["archive", "--format=tar", "-o", tarball, "HEAD"], { cwd: root });
// relative paths: Git's tar reads "C:..." as a remote host
execFileSync("tar", ["-xf", path.join("..", "app.tar")], { cwd: path.join(stage, "app") });
fs.rmSync(tarball);
for (const extra of ["desktop", "tests", "kernel/tests", "docs", ".github"]) {   // not needed to run it
  fs.rmSync(path.join(stage, "app", extra), { recursive: true, force: true });
}
const commit = execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
fs.writeFileSync(path.join(stage, "app", ".bundle-version"), `${version} ${commit}`);   // app_version() reads it

// Python
const python = process.env.CODEMONKEY_BUNDLE_PYTHON || "python";
const home = execFileSync(python, ["-c", "import sys; print(sys.base_prefix)"], { encoding: "utf8" }).trim();
const pyVersion = execFileSync(python, ["--version"], { encoding: "utf8" }).trim();
const SKIP = new Set(["doc", "include", "libs", "tcl", "tools", "scripts", "site-packages", "__pycache__",
  "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "news.txt"]);
fs.cpSync(home, path.join(stage, "python"), {
  recursive: true,
  filter: (src) => !SKIP.has(path.basename(src).toLowerCase()) && !/^_?tk|^tcl/i.test(path.basename(src)),
});
fs.mkdirSync(path.join(stage, "python", "Lib", "site-packages"));
console.log(`Bundled ${pyVersion} from ${home}`);

const candidates = ["makensis",
  path.join(process.env.LOCALAPPDATA || "", "Programs", "NSIS", "Bin", "makensis.exe"),
  "C:\\Program Files (x86)\\NSIS\\makensis.exe", "C:\\Program Files\\NSIS\\makensis.exe"];
for (const exe of candidates) {
  try {
    execFileSync(exe, ["/V2", "/DSTANDALONE", `/DVERSION=${version}`, "installer.nsi"], { cwd: desktop, stdio: "inherit" });
    console.log(`Built dist\\CodeMonkey-${version}-Windows.exe`);
    process.exit(0);
  } catch (e) {
    if (e.code !== "ENOENT") process.exit(e.status || 1);
  }
}
console.error("NSIS was not found. Install it (winget install NSIS.NSIS) and try again.");
process.exit(1);
