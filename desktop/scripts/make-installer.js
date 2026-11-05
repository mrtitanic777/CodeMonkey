// Builds dist/CodeMonkey-Setup-<version>.exe from the packaged app (npm run installer).
// The installed app runs the Python code in this CodeMonkey folder (see installer.nsi).

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktop = path.resolve(__dirname, "..");
const root = path.resolve(desktop, "..");
const version = require(path.join(desktop, "package.json")).version;

if (!fs.existsSync(path.join(desktop, "dist", "CodeMonkey-win32-x64", "CodeMonkey.exe"))) {
  console.error("Package the app first: npm run package");
  process.exit(1);
}
const candidates = ["makensis",
  path.join(process.env.LOCALAPPDATA || "", "Programs", "NSIS", "Bin", "makensis.exe"),
  "C:\\Program Files (x86)\\NSIS\\makensis.exe", "C:\\Program Files\\NSIS\\makensis.exe"];
for (const exe of candidates) {
  try {
    execFileSync(exe, ["/V2", `/DROOT=${root}`, `/DVERSION=${version}`, "installer.nsi"], { cwd: desktop, stdio: "inherit" });
    console.log(`Built dist\\CodeMonkey-Setup-${version}.exe`);
    process.exit(0);
  } catch (e) {
    if (e.code !== "ENOENT") process.exit(e.status || 1);
  }
}
console.error("NSIS was not found. Install it (winget install NSIS.NSIS) and try again.");
process.exit(1);
