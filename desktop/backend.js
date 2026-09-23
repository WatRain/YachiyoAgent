// 启动/收摊 Python 后端。
//
// 为什么用「重定向到文件 + 轮询文件」而不是 pipe 读 stdout：
//   沙箱/受限环境里 Node 的 child_process 默认 stdio:'pipe' 会 EPERM
//   （开不了命名管道）。写成文件两边都能读，主进程不会因为一个管道崩掉，
//   而且后端日志天然落盘，出问题能翻。
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const READY_PREFIX = "YACHIYO_BACKEND_READY ";

function pythonExe() {
  const exe = path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe");
  return fs.existsSync(exe) ? exe : "python";
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 从日志文件里找本次启动打出的 YACHIYO_BACKEND_READY，返回它后面的 JSON。
 *  只看最后一次 "--- backend start" 之后的内容 —— 否则会读到上一次启动的端口。 */
function readReady(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
  const start = text.lastIndexOf("--- backend start");
  const recent = start >= 0 ? text.slice(start) : text;
  const lines = recent.split(/\r?\n/).filter((l) => l.includes(READY_PREFIX));
  if (!lines.length) return null;
  const line = lines[lines.length - 1];
  const raw = line.slice(line.indexOf(READY_PREFIX) + READY_PREFIX.length);
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** 轮询 /api/health，确认真能连上（唯一免 token 的接口）。 */
async function waitForHealth(info, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(info.url + "api/health");
      if (resp.ok) return true;
    } catch {
      /* 还没监听上，继续等 */
    }
    await sleep(150);
  }
  return false;
}

/**
 * 起后端，等它就绪。
 * @param {{logDir?:string, dataDir?:string, logLevel?:string, timeoutMs?:number,
 *          exe?:string, modelsDir?:string}} options
 *   exe       —— 打包版后端的可执行文件（给了就用它，不再找 python）
 *   modelsDir —— 打包版内置模型的目录（转成 YACHIYO_MODEL_DIR 交给后端）
 * @returns {{child, info:{port,token,url,api}, logFile, stop():void}}
 */async function startBackend({ logDir, dataDir, logLevel = "INFO", timeoutMs = 90000,
                              exe = "", modelsDir = "" } = {}) {
  fs.mkdirSync(logDir, { recursive: true });
  const logFile = path.join(logDir, "backend.log");
  // 这次启动的分隔线：不然上次的 READY 行会被误读成这次的
  fs.appendFileSync(logFile, `\n--- backend start ${new Date().toISOString()} ---\n`);

  const out = fs.openSync(logFile, "a");
  const env = {
    ...process.env,
    YACHIYO_LOG_LEVEL: logLevel,
  };
  if (dataDir) env.YACHIYO_DATA_DIR = dataDir;
  if (modelsDir && fs.existsSync(modelsDir)) env.YACHIYO_MODEL_DIR = modelsDir;
  // PYTHONPATH 只有开发期需要（打包版的后端自己带了全套模块）
  if (!exe) env.PYTHONPATH = PROJECT_ROOT;

  const [command, args] = exe ? [exe, []] : [pythonExe(), ["-m", "backend"]];
  const child = spawn(command, args, {
    cwd: exe ? path.dirname(exe) : PROJECT_ROOT,
    env,
    stdio: ["ignore", out, out],
    windowsHide: true,
  });

  let info = null;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`后端进程退出了（code=${child.exitCode}），看 ${logFile}`);
    }
    info = readReady(logFile);
    if (info) break;
    await sleep(120);
  }
  if (!info) throw new Error(`后端 ${timeoutMs}ms 没就绪，看 ${logFile}`);

  // READY 行只是"进程活着"，再确认端口真的能连上（列表里那行也可能来自上一次启动）
  if (!(await waitForHealth(info))) {
    throw new Error(`后端宣告就绪但连不上 ${info.url}，看 ${logFile}`);
  }

  return {
    child,
    info,
    logFile,
    stop() {
      try {
        child.kill();
      } catch {
        /* 已经没了 */
      }
    },
  };
}

module.exports = { startBackend, PROJECT_ROOT };
