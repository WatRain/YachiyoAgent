// P3 探针：Electron 直接渲染 Live2D，量真实帧率。
//
// 和 Flet 版的关键差别：渲染页由**后端自己**提供（同源），Electron 把它当普通网页打开，
// 中间没有 CDP、没有截屏、没有 base64 传输 —— 帧率上限就是渲染进程自己的上限。
//
// 跑法：desktop> npm run probe
// 结果写在 desktop/.logs/probe.log，最后一行是 PROBE_SUMMARY。
const { app, BrowserWindow } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { startBackend } = require("../backend.js");

const LOG_DIR = path.join(__dirname, "..", ".logs");
fs.mkdirSync(LOG_DIR, { recursive: true });
const LOG_STREAM = fs.createWriteStream(path.join(LOG_DIR, "probe.log"), { flags: "a" });

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(" ")}\n`;
  try {
    LOG_STREAM.write(line);
  } catch {
    /* 写不进去也不能崩 */
  }
}

process.stdout.on("error", () => {});
process.stderr.on("error", () => {});
process.on("uncaughtException", (err) => log("UNCAUGHT:", err?.stack || String(err)));
process.on("unhandledRejection", (err) => log("UNHANDLED:", err?.stack || String(err)));

const SAMPLE_MS = 1000;        // 每秒采一次
// 视线走一圈：四个角 + 中间 + 正中，顺便验证渲染进程能不能被外部驱动
const GAZE = [[60, 120], [320, 120], [60, 560], [320, 560], [190, 340], [190, 340]];

// 逐段量：先量现状，再逐项拆掉开销，看瓶颈到底在哪
const PHASES = [
  {
    name: "A-基线（页面自带 33ms 兜底循环 + 物理开启）",
    seconds: 10,
    setup: null,
  },
  {
    name: "B-关物理（internalModel.physics = null）",
    seconds: 8,
    setup: `(() => { const d = window.yachiyo._debug();`
      + ` window.__physSaved = d.model.internalModel.physics;`
      + ` d.model.internalModel.physics = null; return "physics=null"; })()`,
  },
  {
    name: "C-关物理 + rAF 驱动（绕过 33ms interval 的上限）",
    seconds: 10,
    setup: `(() => { window.yachiyo.setAutoTick(false); let stop = false;`
      + ` window.__probeStopRaf = () => { stop = true; };`
      + ` const loop = () => { if (stop) return; try { window.yachiyo.tick(16.7); } catch (e) {}`
      + ` requestAnimationFrame(loop); }; requestAnimationFrame(loop); return "raf"; })()`,
  },
  {
    name: "D-rAF 驱动 + 物理开启（能不能两样都要）",
    seconds: 12,
    setup: `(() => { const d = window.yachiyo._debug();`
      + ` d.model.internalModel.physics = window.__physSaved || d.model.internalModel.physics;`
      + ` return "physics=" + (d.model.internalModel.physics ? "on" : "null"); })()`,
  },
];

let backend = null;
let win = null;
let timer = null;

function createWindow() {
  const w = new BrowserWindow({
    width: 420,
    height: 700,
    frame: false,
    transparent: true,          // pet.html 的 canvas 是 backgroundAlpha: 0
    backgroundColor: "#00000000",
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false },
  });
  w.once("ready-to-show", () => w.show());
  w.webContents.on("console-message", (_e, _level, message) => log("[renderer]", message));
  w.webContents.on("render-process-gone", (_e, details) => log("RENDERER GONE:", JSON.stringify(details)));
  return w;
}

async function readState() {
  const raw = await win.webContents.executeJavaScript(
    "JSON.stringify(window.yachiyo && window.yachiyo.state ? window.yachiyo.state() : null)",
  );
  return raw ? JSON.parse(raw) : null;
}

async function drive(dx, dy, mouth) {
  await win.webContents.executeJavaScript(
    `(() => { const y = window.yachiyo; if (!y || !y.ready) return null;`
    + ` y.lookAt(${dx}, ${dy}); y.setMouth(${mouth});`
    + ` const e = y.eyeBall ? y.eyeBall() : null;`
    + ` return JSON.stringify(e); })()`,
  );
}

function summarize(samples) {
  if (!samples.length) return null;
  const sorted = [...samples].sort((a, b) => a - b);
  const sum = samples.reduce((a, b) => a + b, 0);
  return {
    samples: samples.length,
    min: +sorted[0].toFixed(1),
    max: +sorted[sorted.length - 1].toFixed(1),
    avg: +(sum / samples.length).toFixed(1),
    median: +sorted[Math.floor(sorted.length / 2)].toFixed(1),
    all: samples.map((v) => +v.toFixed(1)),
  };
}

async function measure() {
  const results = {};
  let last = null;
  let tick = 0;

  for (const phase of PHASES) {
    if (phase.setup) {
      try {
        const reply = await win.webContents.executeJavaScript(phase.setup);
        log(`阶段【${phase.name}】设置完成：${reply}`);
      } catch (err) {
        log(`阶段【${phase.name}】设置失败：`, err?.message || String(err));
        continue;
      }
    }
    log(`阶段【${phase.name}】开始，量 ${phase.seconds} 秒`);
    const samples = [];
    last = null;
    const t0 = Date.now();
    await new Promise((resolve) => {
      const timer = setInterval(async () => {
        if (!win || win.isDestroyed()) {
          clearInterval(timer);
          resolve();
          return;
        }
        const now = Date.now();
        const [gx, gy] = GAZE[tick % GAZE.length];
        const mouth = +(0.15 + 0.85 * Math.abs(Math.sin(tick / 2))).toFixed(2);
        tick += 1;

        let snap = null;
        try {
          await drive(gx, gy, mouth);
          snap = await readState();
        } catch (err) {
          log("采样失败:", err?.message || String(err));
          return;
        }

        if (last && snap && typeof snap.frames === "number") {
          const fps = (snap.frames - last.frames) / ((now - last.t) / 1000);
          samples.push(fps);
        }
        last = { frames: snap?.frames ?? 0, t: now };

        if (now - t0 >= phase.seconds * 1000) {
          clearInterval(timer);
          const summary = summarize(samples);
          results[phase.name] = summary;
          log(`阶段【${phase.name}】结果：` + JSON.stringify(summary));
          resolve();
        }
      }, SAMPLE_MS);
    });
  }

  log("PROBE_SUMMARY " + JSON.stringify(results));
  try {
    const snap = await readState();
    log("最后状态：" + JSON.stringify(snap));
  } catch (err) {
    log("读末态失败:", err?.message || String(err));
  }
  setTimeout(() => app.quit(), 500);
}

async function main() {
  log("=== P3 探针启动 ===");
  backend = await startBackend({ logDir: LOG_DIR, dataDir: path.join(LOG_DIR, "probe-data") });
  log("后端就绪:", JSON.stringify(backend.info));

  const boot = await fetch(backend.info.url + "api/bootstrap", {
    headers: { "X-Yachiyo-Token": backend.info.token },
  }).then((r) => r.json());
  const l2d = boot.live2d;
  log("live2d:", JSON.stringify(l2d));
  if (!l2d?.ready) {
    log("没有可渲染的模型，退出");
    app.quit();
    return;
  }

  const url = backend.info.url + "pet/?" + new URLSearchParams({ model: l2d.model }).toString();
  log("打开:", url);
  win = createWindow();
  await win.loadURL(url);
  log("页面加载完成，开始计时");
  await measure();
}

app.whenReady().then(() =>
  main().catch((err) => {
    log("探针失败:", err?.stack || String(err));
    app.quit();
  }),
);

app.on("window-all-closed", () => app.quit());
app.on("will-quit", () => {
  if (timer) clearInterval(timer);
  backend?.stop();
  log("=== P3 探针结束 ===");
});
