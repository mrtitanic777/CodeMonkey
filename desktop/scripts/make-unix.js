// Builds CodeMonkey for Linux, Raspberry Pi and Mac (npm run unix), to give to someone else:
//
//   dist/CodeMonkey-<version>-Linux-x64.tar.gz          PCs with Linux
//   dist/CodeMonkey-<version>-RaspberryPi-64bit.tar.gz  Pi 4/5 (and other ARM64 Linux), 64-bit OS
//   dist/CodeMonkey-<version>-RaspberryPi-32bit.tar.gz  Pi 2/3/4 with the 32-bit OS
//   dist/CodeMonkey-<version>-Mac-AppleSilicon.zip      only when built on a Mac (it must be signed there)
//   dist/CodeMonkey-<version>-Mac-Intel.zip
//
// Each carries the committed code (git archive HEAD, as the Windows standalone does) in
// resources/codemonkey; main.js copies it to the user's data folder and runs it with the
// system's python3 (3.10 or newer). Name targets to build only some: npm run unix -- linux-x64

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktop = path.resolve(__dirname, "..");
const root = path.resolve(desktop, "..");
const version = require(path.join(desktop, "package.json")).version;
const out = path.join(desktop, "dist", "unix");
const ON_MAC = process.platform === "darwin";

const TARGETS = {
  "linux-x64": { platform: "linux", arch: "x64", name: "Linux-x64" },
  "linux-arm64": { platform: "linux", arch: "arm64", name: "RaspberryPi-64bit" },
  "linux-armv7l": { platform: "linux", arch: "armv7l", name: "RaspberryPi-32bit" },
  "darwin-arm64": { platform: "darwin", arch: "arm64", name: "Mac-AppleSilicon" },
  "darwin-x64": { platform: "darwin", arch: "x64", name: "Mac-Intel" },
};
let wanted = process.argv.slice(2);
if (!wanted.length) wanted = Object.keys(TARGETS).filter((t) => ON_MAC || !t.startsWith("darwin"));
for (const t of wanted) {
  if (!TARGETS[t]) { console.error(`Unknown target ${t}; one of: ${Object.keys(TARGETS).join(", ")}`); process.exit(1); }
  if (t.startsWith("darwin") && !ON_MAC) { console.error("Mac builds must be made on a Mac (they are signed there)."); process.exit(1); }
}

const run = (cmd, args, opts = {}) => execFileSync(cmd, args, { stdio: "inherit", ...opts });
const dirty = execFileSync("git", ["status", "--porcelain", "--untracked-files=no"], { cwd: root, encoding: "utf8" }).trim();
if (dirty) console.warn("Note: uncommitted changes are not included (it ships what is committed):\n" + dirty);

// the code, once for every target
fs.rmSync(out, { recursive: true, force: true });
const code = path.join(out, "code");   // not "codemonkey": on Windows that is the same folder as "CodeMonkey" below
fs.mkdirSync(code, { recursive: true });
// autocrlf off: with it on, git archive writes CRLF line ends, which break shell scripts on Linux
run("git", ["-c", "core.autocrlf=false", "archive", "--format=tar", "-o", path.join(out, "code.tar"), "HEAD"], { cwd: root });
run("tar", ["-xf", path.join("..", "code.tar")], { cwd: code });   // relative: Git's tar reads "C:..." as a host
fs.rmSync(path.join(out, "code.tar"));
for (const extra of ["desktop", "tests", "kernel/tests", "docs", ".github"]) {
  fs.rmSync(path.join(code, extra), { recursive: true, force: true });
}
const commit = execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
fs.writeFileSync(path.join(code, ".bundle-version"), `${version} ${commit}`);

const packager = path.join(desktop, "node_modules", "@electron", "packager", "bin", "electron-packager.mjs");
const lf = (file) => fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");   // scripts must not carry CRLF

for (const t of wanted) {
  const { platform, arch, name } = TARGETS[t];
  run(process.execPath, [packager, ".", "CodeMonkey", `--platform=${platform}`, `--arch=${arch}`, `--out=${out}`,
    "--overwrite", "--asar", "--ignore=^/dist", "--ignore=^/scripts", "--ignore=^/unix", "--ignore=^/installer.nsi",
    ...(platform === "darwin" ? ["--app-bundle-id=dev.codemonkey.app", "--app-category-type=public.app-category.developer-tools"] : [])],
    { cwd: desktop });
  const built = path.join(out, `CodeMonkey-${platform}-${arch}`);
  const file = path.join(desktop, "dist", `CodeMonkey-${version}-${name}${platform === "darwin" ? ".zip" : ".tar.gz"}`);

  if (platform === "linux") {
    fs.cpSync(code, path.join(built, "resources", "codemonkey"), { recursive: true });
    for (const f of ["start.sh", "install.sh", "README.txt"]) fs.writeFileSync(path.join(built, f), lf(path.join(desktop, "unix", f)));
    fs.copyFileSync(path.join(desktop, "build", "icon.png"), path.join(built, "codemonkey.png"));
    const folder = path.join(out, "CodeMonkey");   // the folder name people see when they unpack it
    fs.rmSync(folder, { recursive: true, force: true });
    fs.renameSync(built, folder);
    run(process.env.CODEMONKEY_BUILD_PYTHON || (process.platform === "win32" ? "python" : "python3"),
      [path.join(__dirname, "tarball.py"), folder, file]);
    fs.renameSync(folder, built);
  } else {
    const bundle = path.join(built, "CodeMonkey.app");
    fs.cpSync(code, path.join(bundle, "Contents", "Resources", "codemonkey"), { recursive: true });
    run("codesign", ["--force", "--deep", "--sign", "-", bundle]);   // ad hoc: Apple Silicon runs nothing unsigned
    run("ditto", ["-c", "-k", "--keepParent", bundle, file]);
  }
  console.log(`Built dist/${path.basename(file)} (${(fs.statSync(file).size / 1048576).toFixed(0)} MB)`);
}
