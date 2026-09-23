// 预加载脚本：渲染进程能拿到的**全部**能力，就这几个。
//
// 界面不直接碰 Node（contextIsolation: true + nodeIntegration: false）：
// 它只拿数据和两个窗口按钮，所有"有权限"的动作都走主进程。
// 这样即便 Live2D 那页或聊天内容里混进了脚本，也动不了本机文件。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("yachiyoShell", {
  /** 后端地址 + token（token 不进 URL，只能这么拿）。 */
  info: () => ipcRenderer.invoke("shell:info"),

  minimize: () => ipcRenderer.send("win:minimize"),
  close: () => ipcRenderer.send("win:close"),

  /** 往主进程的日志文件里写一行。
   *  桌面应用出问题时用户看不到 console，所以界面这边的关键状态要落盘。 */
  log: (...parts) => ipcRenderer.send("app:log", ...parts),

  /** 主进程每隔 ~33ms 推一次鼠标的屏幕坐标（角色视线用）。 */
  onCursor: (callback) => {
    ipcRenderer.on("cursor", (_event, point) => {
      try {
        callback(point);
      } catch {
        /* 视线出错不该影响别的 */
      }
    });
  },
});
