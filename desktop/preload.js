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

  /* ── 角色浮窗（把 Live2D 从右侧面板里拿出来放到桌面上） ──
     主窗口和浮窗加载的是**同一份 preload**，所以下面这组 API 两边都能用：
     主窗口用它开/收/驱动浮窗，浮窗那页用 petMove / petMenu 拖动自己和弹菜单。 */

  /** 开浮窗。url 由渲染层拼（它才知道角色页地址和模型参数）。 */
  petDetach: (payload) => ipcRenderer.invoke("pet:detach", payload),
  /** 收回面板。 */
  petDock: () => ipcRenderer.invoke("pet:dock"),
  /** 浮窗在屏幕上的位置/大小（没开时是 null）。 */
  petBounds: () => ipcRenderer.invoke("pet:bounds"),
  /** 浮窗大小：当前值 + 可调范围（浮窗没开也能问，回落到存盘的那份）。 */
  petSize: () => ipcRenderer.invoke("pet:size"),
  /** 按比例改浮窗大小（长宽比锁死 380:680，中心不动，主进程钳制在屏幕内）。 */
  petResize: (scale) => ipcRenderer.invoke("pet:resize", scale),
  /** 浮窗里角色页的状态快照（没开时是 null）。 */
  petState: () => ipcRenderer.invoke("pet:state"),
  /** 把窗口搬到 (x, y)：浮窗拖动时每帧调一次，主进程会钳制在屏幕内。 */
  petMove: (x, y) => ipcRenderer.send("pet:move", { x, y }),
  /** 让主进程把一个 window.yachiyo 调用转发给浮窗里那页（脱离时角色只有那一份）。
   *  单向：浮窗的返回值不回主窗口。 */
  petCmd: (name, args) => ipcRenderer.send("pet:cmd", { name, args: args || [] }),
  petSetAlwaysOnTop: (flag) => ipcRenderer.send("pet:setAlwaysOnTop", flag),
  /** 弹浮窗的原生右键菜单（收回面板 / 置顶 / 关闭）。 */
  petMenu: () => ipcRenderer.send("pet:menu"),
  /** 浮窗那页的日志，写进主进程的日志文件。 */
  petLog: (...parts) => ipcRenderer.send("pet:log", ...parts),
  /** 浮窗页监听主进程转发来的 window.yachiyo 调用。 */
  onPetCmd: (callback) => {
    ipcRenderer.on("pet:cmd", (_event, cmd) => {
      try {
        callback(cmd);
      } catch {
        /* 指令出错不该拖垮渲染循环 */
      }
    });
  },
  /** 浮窗被关掉（右键菜单/主窗口关闭）时通知主窗口把开关拨回去。 */
  onPetClosed: (callback) => {
    ipcRenderer.on("pet:closed", (_event, info) => {
      try {
        callback(info || {});
      } catch {
        /* 忽略 */
      }
    });
  },
  onPetAlwaysOnTop: (callback) => {
    ipcRenderer.on("pet:alwaysOnTop", (_event, flag) => {
      try {
        callback(Boolean(flag));
      } catch {
        /* 忽略 */
      }
    });
  },
});
