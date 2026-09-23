// Electron 主进程：起 Python 后端 → 开窗口 → 关窗口时收摊。
//
// 三条与 Flet 版不同的地方，写在最前面免得后人踩：
//
// 1. **窗口加载的是 http://127.0.0.1:<port>/ ，不是本地文件。**
//    界面、Live2D 渲染页、接口都由后端那一个源提供（backend/app.py 的
//    StaticFiles 挂在 / 上）。这样窗口里就没有 file:// 的跨源问题，
//    面板 iframe 也能直接 contentWindow.yachiyo.lookAt(...)。
//
// 2. **token 不进 URL。** 渲染进程通过 IPC 拿（shell:info），
//    URL 里带上 token 会留在地址栏/历史/devtools 里。
//
// 3. **日志写文件，不写 stdout。** 探针阶段踩过：把渲染进程的 console
//    转发到 stdout，stdout 被外部工具接管后关闭 → EPIPE → Electron 弹一个
//    原生 "A JavaScript error occurred in the main process" 对话框。
const { app, BrowserWindow, ipcMain, screen, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { startBackend } = require("./backend.js");

const PROJECT_ROOT = path.resolve(__dirname, "..");
// 打包后 __dirname 在 app.asar 里面（只读）—— 往那儿建目录会直接抛错、应用起不来。
// 所以打包版把日志写进 %APPDATA%\月见八千代\logs。
const LOG_DIR = app.isPackaged
  ? path.join(app.getPath("userData"), "logs")
  : path.join(__dirname, ".logs");
const LOG_FILE = path.join(LOG_DIR, "app.log");

// 打包版：后端是随应用一起分发的 exe，数据写进 %APPDATA%（安装目录不可写），
// 内置模型放在 resources/models。开发期这三个都不参与，直接跑 .venv 里的 python。
const PACKAGED_BACKEND = path.join(process.resourcesPath || "", "backend", "yachiyo-backend.exe");
const PACKAGED_MODELS = path.join(process.resourcesPath || "", "models");

const WINDOW_WIDTH = 960;
const WINDOW_HEIGHT = 800;
const BG = "#121215";

// 鼠标位置轮询的间隔（毫秒）。角色的视线跟着它走 ——
// 为什么不让渲染进程自己监听 mousemove：面板是 iframe，指针在 iframe 上时
// 事件不会冒泡到父页面；而主进程能拿到**屏幕坐标**，指到窗口外面也照样跟。
const CURSOR_INTERVAL_MS = 33;

fs.mkdirSync(LOG_DIR, { recursive: true });
const logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });

/* 设了 YACHIYO_DEBUG_PORT 就开一个远程调试端口：自动化测试和排查界面问题都靠它
   （连上去能在页面上下文里读 DOM、点按钮）。默认不开 —— 公开版不该留这个口子。 */
if (process.env.YACHIYO_DEBUG_PORT) {
  app.commandLine.appendSwitch("remote-debugging-port", process.env.YACHIYO_DEBUG_PORT);
}

function fmt(value) {
  if (value instanceof Error) return `${value.name}: ${value.message}`;
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function log(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.map(fmt).join(" ")}\n`;
  process.stdout.write(line);
  try {
    logStream.write(line);
  } catch {
    /* 盘写不进去也不能拖垮应用 */
  }
}

// stdout/stderr 被外部接管后可能已经被关掉（EPIPE），吞掉错误别让它变成崩溃
process.stdout.on("error", () => {});
process.stderr.on("error", () => {});
process.on("uncaughtException", (err) => log("未捕获异常：", err));
process.on("unhandledRejection", (err) => log("未处理的 Promise 拒绝：", err));

let backend = null;
let startupError = "";
let win = null;
let cursorTimer = null;

function cursorLoop() {
  if (cursorTimer) clearInterval(cursorTimer);
  cursorTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return;
    try {
      const point = screen.getCursorScreenPoint();
      win.webContents.send("cursor", point);
    } catch {
      /* 屏幕接口偶发失败无所谓 */
    }
  }, CURSOR_INTERVAL_MS);
}

function createWindow() {
  win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    minWidth: 720,
    minHeight: 560,
    frame: false,                 // 原生标题栏关掉，界面自己画一条（renderer/index.html 里的 .titlebar）
    backgroundColor: BG,
    show: false,                  // 等 ready-to-show，免得看到白屏闪一下
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // 面板 iframe 与我们同源，但仍旧把沙箱关掉：Live2D 页要用 WebGL
      sandbox: false,
    },
  });

  win.once("ready-to-show", () => {
    win.show();
    log("窗口已显示");
  });

  // 界面里的链接一律用系统浏览器打开，别把应用窗口导航走
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  if (startupError) {
    win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
      `<body style="background:#121215;color:#ECECF0;font:14px 'Microsoft YaHei UI';padding:40px">
       <h2>后端没起来</h2><pre style="color:#FF5A5F;white-space:pre-wrap">${startupError}</pre>
       <p style="color:#8E8E98">日志：${LOG_FILE}</p></body>`));
    return;
  }

  win.loadURL(`${backend.info.url}?token=1`);
  cursorLoop();

  win.on("closed", () => {
    win = null;
    if (cursorTimer) clearInterval(cursorTimer);
    cursorTimer = null;
  });
}

ipcMain.handle("shell:info", () => {
  if (startupError) return { error: startupError, logFile: LOG_FILE };
  return {
    apiBase: backend.info.url,
    token: backend.info.token,
    logFile: LOG_FILE,
    versions: {
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
    },
  };
});

ipcMain.on("app:log", (_event, ...parts) => log("[renderer]", ...parts));

ipcMain.on("win:minimize", () => {
  if (win && !win.isDestroyed()) win.minimize();
});

ipcMain.on("win:close", () => {
  // close() 会走正常的关闭流程（将来要在这里存一次对话），destroy() 是硬杀
  if (win && !win.isDestroyed()) win.close();
});

app.whenReady().then(async () => {
  log("=== YachiyoAgent 启动 ===", process.versions.electron, process.versions.chrome);
  try {
    const packaged = app.isPackaged && fs.existsSync(PACKAGED_BACKEND);
    backend = await startBackend({
      logDir: LOG_DIR,
      logLevel: process.env.YACHIYO_LOG_LEVEL || "INFO",
      exe: packaged ? PACKAGED_BACKEND : "",
      // 打包版的数据目录 = %APPDATA%\月见八千代\data（和 core/paths.py 的约定一致）
      dataDir: packaged ? path.join(app.getPath("userData"), "data") : process.env.YACHIYO_DATA_DIR || "",
      modelsDir: packaged ? PACKAGED_MODELS : "",
    });
    log("后端就绪：", backend.info.url, packaged ? "（打包版 exe）" : "（开发期 python）");
  } catch (err) {
    startupError = err instanceof Error ? err.message : String(err);
    log("后端启动失败：", startupError);
  }
  createWindow();
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", () => {
  if (cursorTimer) clearInterval(cursorTimer);
  if (backend) {
    log("收摊：关掉后端进程");
    backend.stop();
    backend = null;
  }
});
