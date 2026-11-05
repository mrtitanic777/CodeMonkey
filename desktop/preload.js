// What the page may ask of the desktop shell: the update, and the system's folder picker.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  onUpdate: (cb) => ipcRenderer.on("update-ready", (_event, version) => cb(version)),
  installUpdate: () => ipcRenderer.send("install-update"),
  pickFolder: (start) => ipcRenderer.invoke("pick-folder", start || ""),
});
