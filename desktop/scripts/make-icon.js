// Draws CodeMonkey's icon (the same mark as the web UI's favicon) into
// build/icon.png and build/icon.ico. Run with: npx electron scripts/make-icon.js
//
// Electron renders the SVG, so no image tools are needed. The .ico holds
// PNG images at the sizes Windows asks for.

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="256" height="256">
<rect width="32" height="32" rx="8" fill="#15130f"/>
<path d="M11 11l-5 5 5 5M21 11l5 5-5 5" fill="none" stroke="#d26a43" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;
const SIZES = [16, 24, 32, 48, 64, 128, 256];
const OUT = path.join(__dirname, "..", "build");

function ico(pngs) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);            // icon
  header.writeUInt16LE(pngs.length, 4);
  const entries = [];
  let offset = 6 + 16 * pngs.length;
  for (const { size, data } of pngs) {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0);
    e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt16LE(1, 4);               // colour planes
    e.writeUInt16LE(32, 6);              // bits per pixel
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += data.length;
    entries.push(e);
  }
  return Buffer.concat([header, ...entries, ...pngs.map((p) => p.data)]);
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 256, height: 256, show: false, frame: false, transparent: true,
    webPreferences: { offscreen: true },
  });
  const html = `<html><body style="margin:0;background:transparent">${SVG}</body></html>`;
  await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
  await new Promise((r) => setTimeout(r, 300));
  const image = (await win.webContents.capturePage({ x: 0, y: 0, width: 256, height: 256 })).resize({ width: 256, height: 256 });
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, "icon.png"), image.toPNG());
  const pngs = SIZES.map((size) => ({ size, data: image.resize({ width: size, height: size, quality: "best" }).toPNG() }));
  fs.writeFileSync(path.join(OUT, "icon.ico"), ico(pngs));
  console.log("Wrote build/icon.png and build/icon.ico");
  app.quit();
});
