const { contextBridge, ipcRenderer } = require("electron");

// P0：只暴露版本信息 + 两个窗口按钮，验证 contextBridge/ipcRenderer 通路。
contextBridge.exposeInMainWorld("yachiyo", {
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  minimize: () => ipcRenderer.send("win:minimize"),
  close: () => ipcRenderer.send("win:close"),
});
