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
const { app, BrowserWindow, Menu, ipcMain, screen, shell } = require("electron");
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

/* ── 角色浮窗：把 Live2D 从右侧面板里"拿出来"，变成桌面上一个透明小窗 ──
 *
 * 为什么是**第二个 BrowserWindow**，而不是把 iframe 搬走：
 *   iframe 只能在同一个文档里移动，跨窗口搬不了。浮窗直接加载同一个角色页
 *   （?window=1），渲染进程那侧只保留一层"角色目标"抽象。
 *
 * 三个参数是踩过的坑，别随手改：
 *   transparent + backgroundColor 全透明 + hasShadow:false —— 不然桌面上是个方块；
 *   skipTaskbar —— 桌面宠物不该在任务栏占一格；
 *   alwaysOnTop(screen-saver) —— 盖得住任务栏和别的置顶窗，不然"放在桌面上"没意义。
 */
const PET_W = 380;              // 和面板里的舞台同尺寸，脱离瞬间大小不跳
const PET_H = 680;
const PET_MIN_W = 180;
const PET_MIN_H = 260;
let petWin = null;
let petBoundsTimer = null;

function petBoundsFile() {
  // 位置/大小是**界面状态**，不进后端配置（那里面是应用数据）
  return path.join(app.getPath("userData"), "pet-window.json");
}

function readPetBounds() {
  try {
    const raw = JSON.parse(fs.readFileSync(petBoundsFile(), "utf8"));
    const n = (v) => (Number.isFinite(v) ? Math.round(v) : null);
    const width = n(raw.width);
    const height = n(raw.height);
    if (width >= PET_MIN_W && height >= PET_MIN_H) {
      return { x: n(raw.x), y: n(raw.y), width, height };
    }
  } catch {
    /* 没存过、或者文件坏了 —— 都走默认位置 */
  }
  return null;
}

function writePetBounds(bounds) {
  try {
    fs.writeFileSync(petBoundsFile(), JSON.stringify(bounds));
  } catch {
    /* 位置存不下不该影响功能 */
  }
}

/** 存浮窗的位置/大小（去抖 400ms，给拖动/缩放这类连续事件用）。
 *  bounds 省略时读浮窗当前的（move/resize 事件走这条）；显式传进来是为了
 *  **浮窗没开时也能存** —— 设置里调大小就是这种情况，存下来等下次脱离生效。 */
function savePetBoundsSoon(bounds) {
  if (petBoundsTimer) clearTimeout(petBoundsTimer);
  petBoundsTimer = setTimeout(() => {
    petBoundsTimer = null;
    const b = bounds || (petWin && !petWin.isDestroyed() ? petWin.getBounds() : null);
    if (b) writePetBounds(b);
  }, 400);
}

function defaultPetBounds() {
  const area = screen.getPrimaryDisplay().workArea;
  return {
    x: Math.round(area.x + area.width - PET_W - 24),
    y: Math.round(area.y + area.height - PET_H - 24),
    width: PET_W,
    height: PET_H,
  };
}

/** 拖动时别让窗口整块跑出屏幕：至少留 80px 在最近的显示器工作区里。 */
function clampPetPos(x, y, width, height) {
  const area = screen.getDisplayNearestPoint({
    x: Math.round(x + width / 2),
    y: Math.round(y + height / 2),
  }).workArea;
  const minX = area.x - width + 80;
  const maxX = area.x + area.width - 80;
  const minY = area.y;
  const maxY = area.y + area.height - 80;
  return {
    x: Math.round(Math.min(maxX, Math.max(minX, x))),
    y: Math.round(Math.min(maxY, Math.max(minY, y))),
  };
}

function notifyPetClosed(reason) {
  if (win && !win.isDestroyed()) win.webContents.send("pet:closed", { reason });
}

function createPetWindow(url, bounds) {
  if (petWin && !petWin.isDestroyed()) return petWin;
  const b = Object.assign(defaultPetBounds(), readPetBounds() || {}, bounds || {});
  petWin = new BrowserWindow({
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
    minWidth: PET_MIN_W,
    minHeight: PET_MIN_H,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: true,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    show: false,                  // 等 ready-to-show，免得先闪一个黑框
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,             // 要 WebGL
    },
  });
  // 构造参数里的 alwaysOnTop 只是普通置顶，盖不住任务栏；桌面宠物要的是最高一级
  petWin.setAlwaysOnTop(true, "screen-saver");

  petWin.once("ready-to-show", () => {
    if (!petWin || petWin.isDestroyed()) return;
    petWin.showInactive();        // 不抢主窗口的焦点
    log("角色浮窗已显示", petWin.getBounds());
  });

  petWin.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target)) shell.openExternal(target);
    return { action: "deny" };
  });

  // 事件对象会被当成 bounds 塞进来，所以包一层
  petWin.on("move", () => savePetBoundsSoon());
  petWin.on("resize", () => savePetBoundsSoon());
  const self = petWin;
  petWin.on("closed", () => {
    if (petWin !== self) return;  // 已经是另一个浮窗了，别把新的置空
    petWin = null;
    if (petBoundsTimer) clearTimeout(petBoundsTimer);
    petBoundsTimer = null;
    notifyPetClosed("window-closed");
  });

  petWin.loadURL(url);
  return petWin;
}

function closePetWindow(reason) {
  if (!petWin || petWin.isDestroyed()) {
    petWin = null;
    return false;
  }
  log("角色浮窗收回：", reason);
  petWin.close();                 // closed 回调里置空并通知渲染层
  return true;
}

/* 视线跟手范围比浮窗本身外扩的比例（0.25 = 四边各多 25%）。
 * 指针刚出窗口时视线贴在那一侧，而不是立刻回正前方。
 * 只作用于**脱离后的浮窗**：面板那边（renderer/app.js 的 watchGaze）不扩。 */
const GAZE_PAD = 0.25;

function cursorLoop() {
  if (cursorTimer) clearInterval(cursorTimer);
  cursorTimer = setInterval(() => {
    try {
      const point = screen.getCursorScreenPoint();
      /* 角色脱离到桌面时，视线由**主进程直接驱动浮窗**：算成浮窗内的画面坐标发过去。
         这样主窗口最小化、被挡住、甚至看不见都不影响跟随（渲染层那边不再插手）。 */
      if (petWin && !petWin.isDestroyed()) {
        const b = petWin.getBounds();
        const x = point.x - b.x;
        const y = point.y - b.y;
        const padX = b.width * GAZE_PAD;
        const padY = b.height * GAZE_PAD;
        const inside = x >= -padX && y >= -padY && x <= b.width + padX && y <= b.height + padY;
        petWin.webContents.send("pet:cmd", inside
          ? { name: "lookAt", args: [x, y] }
          : { name: "lookForward", args: [] });
        return;
      }
      if (!win || win.isDestroyed()) return;
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
    // 主窗口关了就一起收摊：不然浮窗还挂着，window-all-closed 不触发、应用退不掉
    closePetWindow("主窗口关闭");
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

/* ── 角色浮窗的 IPC ──
 * 渲染层（主窗口）负责决定"脱离/收回"和转发指令；
 * 浮窗那页拿到的 preload 是同一份，它用 pet:move / pet:menu 直接跟这里说话。 */
ipcMain.handle("pet:detach", (_event, payload) => {
  const url = payload && typeof payload.url === "string" ? payload.url : "";
  if (!url) return { ok: false, error: "缺少角色页 url" };
  // 已经开着了（界面刷新后重新恢复、或者连点两下开关）：换页就好，
  // 千万别再开第二个 —— 两份模型同时跑，GPU 直接吃满，而且旧窗口再也关不掉。
  if (petWin && !petWin.isDestroyed()) {
    if (petWin.webContents.getURL() !== url) petWin.loadURL(url);
    if (!petWin.isVisible()) petWin.showInactive();
    return { ok: true, bounds: petWin.getBounds(), reused: true };
  }
  try {
    createPetWindow(url, payload.bounds);
  } catch (err) {
    log("角色浮窗创建失败：", err);
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
  return { ok: true, bounds: petWin && !petWin.isDestroyed() ? petWin.getBounds() : null };
});

ipcMain.handle("pet:dock", () => ({ ok: closePetWindow("渲染层收回") }));

ipcMain.handle("pet:bounds", () => (
  petWin && !petWin.isDestroyed() ? petWin.getBounds() : null
));

/* ── 浮窗大小 ──
 * 单一来源就是 pet-window.json（savePetBoundsSoon 写的那份）。浮窗没开时也要能调：
 * 设置里改大小 → 存盘 → 下次脱离就按这个尺寸开出来。 */
const PET_MAX_SCALE = 2;                  // 相对默认 380×680 的最大倍数

function petSizeInfo() {
  const b = (petWin && !petWin.isDestroyed())
    ? petWin.getBounds()
    : (readPetBounds() || defaultPetBounds());
  const area = screen.getPrimaryDisplay().workArea;
  return {
    ok: true,
    open: Boolean(petWin && !petWin.isDestroyed()),
    bounds: { x: b.x, y: b.y, width: Math.round(b.width), height: Math.round(b.height) },
    base: { width: PET_W, height: PET_H },
    // 下界要同时满足宽和高的最小尺寸；上界别超出工作区，也别超过 2 倍
    min: Math.max(PET_MIN_W / PET_W, PET_MIN_H / PET_H),
    max: Math.min(area.width / PET_W, area.height / PET_H, PET_MAX_SCALE),
  };
}

ipcMain.handle("pet:size", () => petSizeInfo());

/** 按比例改浮窗大小：长宽比锁死 380:680，中心不动，位置照样钳制在屏幕内。 */
ipcMain.handle("pet:resize", (_event, scale) => {
  const k = Number(scale);
  if (!Number.isFinite(k) || k <= 0) return { ok: false, error: "比例不合法" };
  const info = petSizeInfo();
  const use = Math.min(info.max, Math.max(info.min, k));
  const width = Math.round(PET_W * use);
  const height = Math.round(PET_H * use);
  const cur = info.bounds;
  const pos = clampPetPos(
    cur.x + (cur.width - width) / 2,
    cur.y + (cur.height - height) / 2,
    width, height);
  const next = { x: pos.x, y: pos.y, width, height };
  if (petWin && !petWin.isDestroyed()) petWin.setBounds(next);
  /* 立刻落盘、不走 400ms 去抖：这是一次性动作，而且紧接着 pet:size 就要读回来
     （刚拖完滑块再打开设置，读到的必须是新尺寸）。 */
  writePetBounds(next);
  log("角色浮窗大小：", next);
  return { ok: true, scale: use, bounds: next };
});

/** 浮窗那页的状态快照（面板上的状态药丸和统计日志用）。 */
ipcMain.handle("pet:state", async () => {
  if (!petWin || petWin.isDestroyed()) return null;
  try {
    const raw = await petWin.webContents.executeJavaScript(
      "(window.yachiyo && window.yachiyo.state) ? JSON.stringify(window.yachiyo.state()) : null");
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
});

/** 拖动：浮窗那页按下指针后按屏幕增量算新位置，这里负责钳制并真的搬窗口。 */
ipcMain.on("pet:move", (_event, payload) => {
  if (!petWin || petWin.isDestroyed()) return;
  const x = Number(payload && payload.x);
  const y = Number(payload && payload.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  const b = petWin.getBounds();
  const next = clampPetPos(x, y, b.width, b.height);
  if (next.x === b.x && next.y === b.y) return;
  petWin.setPosition(next.x, next.y, false);
});

ipcMain.on("pet:setAlwaysOnTop", (_event, flag) => {
  if (!petWin || petWin.isDestroyed()) return;
  const on = Boolean(flag);
  petWin.setAlwaysOnTop(on, on ? "screen-saver" : "normal");
  if (win && !win.isDestroyed()) win.webContents.send("pet:alwaysOnTop", on);
  log("角色浮窗置顶：", on);
});

/** 渲染层 → 浮窗页里的 window.yachiyo 调用（口型、主题、帧率上限…）。 */
ipcMain.on("pet:cmd", (_event, payload) => {
  if (!petWin || petWin.isDestroyed()) return;
  const name = payload && payload.name;
  if (typeof name !== "string") return;
  petWin.webContents.send("pet:cmd", {
    name,
    args: Array.isArray(payload.args) ? payload.args : [],
  });
});

/** 浮窗页右键 → 原生菜单（网页自己画一个在透明窗口里太脆）。 */
ipcMain.on("pet:menu", () => {
  if (!petWin || petWin.isDestroyed()) return;
  const onTop = petWin.isAlwaysOnTop();
  Menu.buildFromTemplate([
    { label: "收回面板", click: () => closePetWindow("右键菜单收回") },
    {
      label: "始终置顶",
      type: "checkbox",
      checked: onTop,
      click: (item) => {
        if (!petWin || petWin.isDestroyed()) return;
        petWin.setAlwaysOnTop(item.checked, item.checked ? "screen-saver" : "normal");
        if (win && !win.isDestroyed()) win.webContents.send("pet:alwaysOnTop", item.checked);
      },
    },
    { type: "separator" },
    { label: "关闭角色浮窗", click: () => closePetWindow("右键菜单关闭") },
  ]).popup({ window: petWin });
});

ipcMain.on("pet:log", (_event, ...parts) => log("[pet]", ...parts));

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
