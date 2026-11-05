// The release file copies of CodeMonkey check for new versions (webui/updates.py):
// dist/latest.json, for this version. Upload it (and the new downloads) where update-link.txt points.
//
//   npm run release-info -- --notes "What changed" [--page <link to the downloads folder>]
//
// Links already in dist/latest.json (the page, each download) are kept, so they only need filling in once
// when they stay the same (a shared folder whose files are replaced keep their links on Google Drive).
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
function arg(name) {
  const i = args.indexOf("--" + name);
  return i >= 0 ? String(args[i + 1] || "") : null;
}

const version = require("../package.json").version;
const out = path.join(__dirname, "..", "dist", "latest.json");
let before = {};
try { before = JSON.parse(fs.readFileSync(out, "utf8")); } catch (e) {}

const downloads = Object.assign({ windows: "", "linux-x64": "", "pi-64": "", "pi-32": "", mac: "" }, before.downloads || {});
const info = {
  version,
  date: new Date().toISOString().slice(0, 10),
  notes: arg("notes") !== null ? arg("notes") : (before.version === version ? before.notes || "" : ""),
  page: arg("page") !== null ? arg("page") : before.page || "",
  downloads,
};
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify(info, null, 2) + "\n");

const files = fs.readdirSync(path.dirname(out)).filter((f) => f.startsWith(`CodeMonkey-${version}-`));
console.log(`Wrote ${out} for ${version}.`);
if (files.length) console.log("This version's downloads:\n  " + files.join("\n  "));
const empty = Object.keys(downloads).filter((k) => !downloads[k]);
if (!info.page && empty.length) console.log(`No link yet for: ${empty.join(", ")} (and no --page): those copies are told about the version but not where to get it.`);
console.log("Upload latest.json where update-link.txt points (replace the old one so its link stays the same).");
