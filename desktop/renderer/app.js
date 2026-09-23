"use strict";

/* 渲染进程：界面 + 对话 + 角色面板。
 *
 * 分层规矩和 Flet 版一样：
 *   · 这里只管"画"和"收集输入"，对话逻辑全在后端（core/chat.py）
 *   · 这个页面永远不碰 API Key —— 密钥只在后端和系统凭据管理器之间走
 *   · 界面拿到的唯一特权来自 preload 暴露的 yachiyoShell（拿地址/token + 两个窗口按钮）
 */

// ───────────────────────── 运行时状态 ─────────────────────────

const state = {
  apiBase: "",
  token: "",
  ws: null,
  cfg: null,
  providers: [],
  presets: [],
  activeProvider: "",
  live2d: null,
  secrets: null,
  busy: false,
  toolTask: null,
};

const STAGE_WIDTH = 380;
const STAGE_HEIGHT = 680;
const THROTTLE_MS = 40;          // 流式刷新节流：模型逐字吐得比屏幕刷新快，40ms 够顺滑又不会抖

const $ = (id) => document.getElementById(id);
const ui = {
  messages: $("messages"),
  status: $("status"),
  input: $("input"),
  send: $("btn-send"),
  stop: $("btn-stop"),
  shell: $("input-shell"),
  dot: $("dot"),
  panelStatus: $("panel-status"),
  pet: $("pet"),
  overlay: $("overlay"),
};

// ───────────────────────── 小工具 ─────────────────────────

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 极简 Markdown：够用、无毒、零依赖（不引 marked 之类的包）。
 *  先整体转义，再按行处理，所以模型吐出来的 HTML 不会被当标签执行。 */
function mdToHtml(source) {
  const blocks = [];
  let text = escapeHtml(source || "");

  // 先摘出围栏代码块，免得里面的 * _ # 被后面的规则改坏
  text = text.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (_m, lang, code) => {
    const cls = lang ? ` class="language-${lang}"` : "";
    blocks.push(`<pre><code${cls}>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });

  const inline = (s) => s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>');

  const out = [];
  let list = null;          // "ul" / "ol" / null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trimEnd();

    if (!line.trim()) { closeList(); continue; }

    const fence = line.match(/^\u0000(\d+)\u0000$/);
    if (fence) { closeList(); out.push(blocks[Number(fence[1])]); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^(-{3,}|\*{3,})$/.test(line.trim())) { closeList(); out.push("<hr>"); continue; }

    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) { closeList(); out.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }

    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }

    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("");
}

/** 调后端接口。token 走请求头（后端 /api/* 认 X-Yachiyo-Token）。 */
async function api(path, { method = "GET", body = null } = {}) {
  const options = {
    method,
    headers: { "X-Yachiyo-Token": state.token },
  };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(state.apiBase.replace(/\/$/, "") + path, options);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!resp.ok) {
    throw new Error((data && data.detail) || `请求失败（${resp.status}）`);
  }
  return data;
}

// ───────────────────────── 聊天气泡 ─────────────────────────

function addBubble(text, { role = "bot", animate = true } = {}) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  if (!animate) bubble.style.animation = "none";
  const content = document.createElement("div");
  content.className = "content";
  content.innerHTML = mdToHtml(text || "");
  bubble.appendChild(content);
  ui.messages.appendChild(bubble);
  return { bubble, content };
}

function setBubbleText(node, text) {
  node.content.innerHTML = mdToHtml(text === "" ? " " : text);
}

function scrollDown() {
  requestAnimationFrame(() => { ui.messages.scrollTop = ui.messages.scrollHeight; });
}

function renderHistory(messages) {
  ui.messages.innerHTML = "";
  let shown = 0;
  for (const message of messages || []) {
    const role = message.role;
    const content = message.content;
    // 只画对话本身：system（人格设定）和 tool（工具调用）不是给用户看的
    if (!["user", "assistant"].includes(role) || !content) continue;
    addBubble(content, { role: role === "user" ? "user" : "bot", animate: false });
    shown += 1;
  }
  if (shown) scrollDown();
  return shown;
}

function setStatus(text, isError = false) {
  ui.status.textContent = text || "";
  ui.status.classList.toggle("error", Boolean(isError));
}

function setBusy(busy) {
  state.busy = busy;
  ui.send.classList.toggle("hidden", busy);
  ui.stop.classList.toggle("hidden", !busy);
  ui.dot.classList.toggle("busy", busy);
  ui.shell.style.borderColor = busy ? "var(--accent)" : "var(--outline)";
}

// ───────────────────────── 对话（WebSocket） ─────────────────────────

function connectWs() {
  const url = state.apiBase.replace(/^http/, "ws").replace(/\/$/, "") + `/ws/chat?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  ws.onopen = () => setStatus("");
  ws.onclose = () => {
    state.ws = null;
    setBusy(false);
    setStatus("和后端的连接断了，正在重连…", true);
    setTimeout(connectWs, 1500);
  };
  ws.onerror = () => { /* onclose 会跟着来，统一在那里处理 */ };
  ws.onmessage = (event) => {
    let msg = null;
    try { msg = JSON.parse(event.data); } catch { return; }
    onWsEvent(msg);
  };
  state.ws = ws;
}

let reply = null;            // 当前正在写的那个气泡
let replyText = "";
let lastFlush = 0;
let lastPulse = 0;

function onWsEvent(msg) {
  switch (msg.type) {
    case "start":
      setBusy(true);
      setStatus("正在思考…");
      break;
    case "delta": {
      replyText += msg.text || "";
      const now = performance.now();
      if (now - lastPulse >= 90) { lastPulse = now; pulseMouth(); }
      if (now - lastFlush >= THROTTLE_MS) {
        lastFlush = now;
        if (reply) setBubbleText(reply, replyText);
        scrollDown();
      }
      break;
    }
    case "tool_start":
      state.toolTask = msg.name;
      setStatus(`正在用「${msg.name}」…`);
      break;
    case "tool_end":
      state.toolTask = null;
      setStatus("");
      break;
    case "done":
      replyText = msg.text || replyText;
      if (reply) setBubbleText(reply, replyText || "（没有内容）");
      finishTurn();
      break;
    case "stopped":
      if (reply) setBubbleText(reply, (msg.text || replyText || "") + "\n\n_（已停止）_");
      finishTurn();
      break;
    case "error":
      if (reply) {
        reply.bubble.classList.add("error");
        setBubbleText(reply, msg.message || "出错了");
      } else {
        const fallback = addBubble(msg.message || "出错了", { role: "bot" });
        fallback.bubble.classList.add("error");
      }
      setStatus(msg.message || "出错了", true);
      finishTurn();
      break;
    case "pong":
      break;
    default:
      break;
  }
}

function finishTurn() {
  setBusy(false);
  scrollDown();
  setTimeout(() => { if (!state.busy) setStatus(""); }, 1200);
  refreshRecall();
}

/** 回合结束后刷新一次记忆数（后端在 reply 结束后会抽长期记忆）。 */
async function refreshRecall() {
  try {
    const data = await api("/api/memories");
    const count = (data.memories || []).length;
    if (count) setStatus(`记住了 ${count} 件事`);
  } catch { /* 记忆是锦上添花，失败就算了 */ }
}

function sendMessage() {
  const text = (ui.input.value || "").trim();
  if (!text || state.busy) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    setStatus("还没连上后端，稍等一下再试。", true);
    return;
  }

  ui.input.value = "";
  autoGrow();
  setBubbleText(addBubble(text, { role: "user" }), text);

  replyText = "";
  lastFlush = 0;
  lastPulse = 0;
  reply = addBubble("", { role: "bot" });
  setBusy(true);
  setStatus("正在思考…");
  scrollDown();

  state.ws.send(JSON.stringify({ type: "user", text }));
}

function stopMessage() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "cancel" }));
  }
}

function autoGrow() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 96)}px`;
}

// ───────────────────────── 角色面板 ─────────────────────────

function petWindow() {
  try { return ui.pet.contentWindow; } catch { return null; }
}

function pulseMouth() {
  const win = petWindow();
  if (!win || !win.yachiyo) return;
  const value = 0.25 + Math.random() * 0.65;   // 说话时的口型（没有真实音频，先按节奏开合）
  try { win.yachiyo.setMouth(value); } catch { /* 角色出问题不该影响聊天 */ }
}

/** 视线：主进程每 33ms 推一次鼠标的屏幕坐标，这里换算成画面坐标。
 *  为什么不用 mousemove：面板是 iframe，指针在 iframe 上时父页面收不到事件；
 *  而屏幕坐标还有个好处 —— 指到窗口外面也照样跟。 */
function watchGaze() {
  let last = null;
  let gazeLogged = false;
  let lastGazeLog = 0;
  let rect = { left: 0, top: 0, width: STAGE_WIDTH, height: STAGE_HEIGHT };

  const recompute = () => {
    const box = ui.pet.getBoundingClientRect();
    rect = { left: window.screenX + box.left, top: window.screenY + box.top, width: box.width, height: box.height };
  };
  recompute();
  window.addEventListener("resize", recompute);
  setInterval(recompute, 2000);       // 窗口被拖动时 screenX/Y 自己会变，定期校准

  if (!window.yachiyoShell || !window.yachiyoShell.onCursor) return;
  window.yachiyoShell.onCursor((point) => {
    const win = petWindow();
    if (!win || !win.yachiyo) return;
    // 画面坐标 = 指针相对 iframe 左上角的位置。iframe 的 CSS 尺寸就是页面里的
    // 像素尺寸（两者都是 DIP），所以不用换算比例；窗口被拖动过就重新校准。
    const x = Math.round(point.x - rect.left);
    const y = Math.round(point.y - rect.top);
    const inside = x >= 0 && y >= 0 && x <= rect.width && y <= rect.height;
    const key = inside ? `${x},${y}` : "out";
    if (key !== last) {
      last = key;
      if (inside && !gazeLogged) {
        gazeLogged = true;
        logToShell(`视线跟随已启用：指针 (${point.x},${point.y}) → 画面 (${x},${y})`);
      }
      try {
        if (inside) win.yachiyo.lookAt(x, y);
        else win.yachiyo.lookForward();      // 指针离开画面 → 视线回正前方
      } catch { /* 页面可能还没就绪 */ }
    }
    /* 指针停在角色身上时，每 2 秒留一行"目标 + 渲染时真正用到的眼球参数"。
       这一行是判断"视线到底进没进模型"最直接的证据：
       renderedEye 是在 coreModel.update 里抄下来的**渲染值**，不是我们写进去的值。 */
    if (inside) {
      const now = performance.now();
      if (now - lastGazeLog > 2000) {
        lastGazeLog = now;
        let snap = null;
        try { snap = win.yachiyo.state(); } catch { /* 忽略 */ }
        logToShell(`视线自检：画面 (${x},${y}) → 渲染中的眼球 ${JSON.stringify(snap && snap.renderedEye)}`);
      }
    }
  });
}

function bootPanel() {
  if (!state.live2d || !state.live2d.ready) {
    ui.panelStatus.textContent = (state.live2d && state.live2d.message) || "没有可用的 Live2D 模型";
    return;
  }
  const physics = state.cfg && state.cfg.live2d_physics === false ? "0" : "1";
  const url = state.apiBase.replace(/\/$/, "") + state.live2d.page
    + `?model=${encodeURIComponent(state.live2d.model)}&physics=${physics}`;
  ui.pet.src = url;
  watchGaze();
  pollPanel();
}

let lastFrameSample = null;
let panelSelfCheck = "";          // 面板状态变化时只报一次，免得日志淹掉
let lastStatsLog = 0;             // 上一次写"面板统计"的时间

/** 界面这边的关键状态写进主进程日志。
 *  桌面应用里用户看不到 console，角色挂了从外观看只是"一直没动"，必须落盘才查得动。 */
function logToShell(...parts) {
  try { window.yachiyoShell?.log?.(...parts); } catch { /* 日志失败无所谓 */ }
}

async function pollPanel() {
  const win = petWindow();
  const info = win && win.yachiyo ? (() => { try { return win.yachiyo.state(); } catch { return null; } })() : null;

  if (!info) {
    ui.panelStatus.textContent = "正在唤醒八千代…";
  } else if (info.error) {
    ui.panelStatus.textContent = `角色加载失败：${info.error}`;
    if (panelSelfCheck !== "error:" + info.error) {
      panelSelfCheck = "error:" + info.error;
      logToShell("Live2D 面板出错：" + info.error, "stage=" + info.stage, "diag=" + JSON.stringify(info.diag || []));
    }
  } else if (!info.ready) {
    ui.panelStatus.textContent = "正在唤醒八千代…";
  } else {
    const now = performance.now();
    let fps = 0;
    if (lastFrameSample) {
      const dt = (now - lastFrameSample.t) / 1000;
      if (dt > 0.3) {
        fps = Math.round((info.frames - lastFrameSample.frames) / dt);
        lastFrameSample = { t: now, frames: info.frames };
      }
    } else {
      lastFrameSample = { t: now, frames: info.frames };
    }
    const phys = info.physics === false ? "物理关" : "物理开";
    ui.panelStatus.textContent = fps ? `模型就绪 · ${fps} fps · ${phys}` : `模型就绪 · ${phys}`;
    if (panelSelfCheck !== "ready") {
      panelSelfCheck = "ready";
      logToShell("Live2D 面板就绪：" + JSON.stringify({
        canvas: info.canvas, model: info.model, physics: info.physics,
        autoTick: info.autoTick, mouthPath: info.mouthPath,
        diag: (info.diag || []).slice(-12),
      }));
    }
    // 每隔一阵留一行统计。用户说"角色卡/不动"时，这一行就能分辨是帧率问题
    // 还是别的问题（renderedEye 是渲染那一刻模型里真正的眼球参数）。
    if (now - lastStatsLog > 15000) {
      lastStatsLog = now;
      logToShell(`面板统计：${fps || "-"} fps 物理=${info.physics ? "开" : "关"}`
                 + ` 视线目标=${JSON.stringify(info.look)}`
                 + ` 渲染中的眼球=${JSON.stringify(info.renderedEye)}`
                 + ` 帧计数=${info.frames}`);
    }
  }
  setTimeout(pollPanel, 900);
}

// ───────────────────────── 浮层：引导 / 设置 ─────────────────────────

function closeOverlay() {
  ui.overlay.classList.add("hidden");
  ui.overlay.innerHTML = "";
}

function showSheet(build) {
  ui.overlay.innerHTML = "";
  const sheet = document.createElement("div");
  sheet.className = "sheet";
  ui.overlay.appendChild(sheet);
  ui.overlay.classList.remove("hidden");
  build(sheet);
}

const PROTOCOLS = [
  ["openai", "OpenAI 兼容"],
  ["anthropic", "Anthropic"],
  ["gemini", "Gemini"],
  ["custom", "自定义"],
];

/** provider 编辑表单（引导页和设置页共用）。 */
function providerForm(sheet, { provider = null, onSaved }) {
  const form = document.createElement("div");
  form.className = "field-group";
  form.innerHTML = `
    <div class="field">
      <label>服务商</label>
      <div class="chips" id="preset-chips"></div>
    </div>
    <div class="field">
      <label>显示名称</label>
      <input id="p-name" type="text" placeholder="DeepSeek" value="${escapeHtml(provider?.display_name || "")}" />
    </div>
    <div class="field">
      <label>协议</label>
      <select id="p-proto">
        ${PROTOCOLS.map(([v, label]) =>
          `<option value="${v}" ${provider?.protocol === v ? "selected" : ""}>${label}</option>`).join("")}
      </select>
    </div>
    <div class="field">
      <label>接口地址（Base URL）</label>
      <input id="p-base" type="text" placeholder="https://api.deepseek.com/v1" value="${escapeHtml(provider?.base_url || "")}" />
    </div>
    <div class="field">
      <label>模型 ID</label>
      <div class="row">
        <input id="p-model" class="grow" type="text" placeholder="deepseek-chat" value="${escapeHtml(provider?.model_id || "")}" />
        <button class="btn" id="p-models" type="button">拉取列表</button>
      </div>
      <div id="p-models-list" class="hint"></div>
    </div>
    <div class="field">
      <label>API Key${provider?.has_key ? "（已存，留空表示不改）" : ""}</label>
      <input id="p-key" type="password" placeholder="sk-..." autocomplete="off" />
    </div>
    <div class="row">
      <button class="btn" id="p-test" type="button">测试连接</button>
      <button class="btn primary" id="p-save" type="button">保存</button>
    </div>
    <div id="p-notice" class="notice"></div>
  `;
  sheet.appendChild(form);

  const notice = (text, kind = "") => {
    const node = form.querySelector("#p-notice");
    node.className = `notice ${kind}`;
    node.textContent = text;
  };
  const grab = () => ({
    display_name: form.querySelector("#p-name").value.trim(),
    protocol: form.querySelector("#p-proto").value,
    base_url: form.querySelector("#p-base").value.trim(),
    model_id: form.querySelector("#p-model").value.trim(),
    api_key: form.querySelector("#p-key").value.trim(),
  });

  // 预设：点一下就填好地址和模型。
  // 用后端给的 BUILTIN_PRESETS，不是用户已配置的 providers —— 引导页上还没有
  // 任何 provider，用后者会导致这一行是空的（用户只能自己背 base_url）。
  const chips = form.querySelector("#preset-chips");
  const presets = state.presets?.length ? state.presets : [];
  if (!presets.length) chips.textContent = "（内置预设没拿到，手填地址也行）";
  for (const item of presets) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.dataset.preset = item.id || "";
    chip.innerHTML = `${escapeHtml(item.display_name)}<span class="sub">${escapeHtml(item.model_id || item.protocol || "")}</span>`;
    // 已经配过并有密钥的，标出来，免得用户重复添加
    if (state.providers.some((p) => p.id === item.id && p.has_key)) {
      chip.querySelector(".sub").textContent = "已存密钥";
      chip.classList.add("on");
    }
    chip.onclick = () => {
      form.querySelector("#p-name").value = item.display_name;
      form.querySelector("#p-proto").value = item.protocol;
      form.querySelector("#p-base").value = item.base_url;
      form.querySelector("#p-model").value = item.model_id || "";
      chip.parentElement.querySelectorAll(".chip").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
    };
    chips.appendChild(chip);
  }

  form.querySelector("#p-models").onclick = async () => {
    const data = grab();
    try {
      const resp = await api("/api/providers/models", {
        method: "POST",
        body: { provider: { ...data, id: provider?.id || "" }, api_key: data.api_key },
      });
      const models = resp.models || [];
      const box = form.querySelector("#p-models-list");
      if (!models.length) { box.textContent = resp.message || "这个端点没有提供模型列表。"; return; }
      box.innerHTML = "";
      const select = document.createElement("select");
      select.innerHTML = models.slice(0, 200)
        .map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
      select.onchange = () => { form.querySelector("#p-model").value = select.value; };
      box.appendChild(select);
      form.querySelector("#p-model").value = models[0];
      notice(`拿到 ${models.length} 个模型。`, "ok");
    } catch (err) {
      notice(err.message, "bad");
    }
  };

  form.querySelector("#p-test").onclick = async () => {
    const data = grab();
    if (!data.base_url || !data.model_id) { notice("先把接口地址和模型 ID 填上。", "bad"); return; }
    notice("正在测试…");
    try {
      const resp = await api("/api/providers/test", {
        method: "POST",
        body: {
          provider: { ...data, id: provider?.id || "" },
          api_key: data.api_key,
          provider_id: provider?.id || "",
        },
      });
      notice(resp.message || (resp.ok ? "连上了。" : "没连上。"), resp.ok ? "ok" : "bad");
    } catch (err) {
      notice(err.message, "bad");
    }
  };

  form.querySelector("#p-save").onclick = async () => {
    const data = grab();
    if (!data.base_url || !data.model_id || !data.display_name) {
      notice("名称、接口地址、模型 ID 都得填。", "bad");
      return;
    }
    if (!data.api_key && !provider?.has_key) { notice("还得填一个 API Key。", "bad"); return; }
    notice("正在保存…");
    try {
      const payload = {
        display_name: data.display_name,
        protocol: data.protocol,
        base_url: data.base_url,
        model_id: data.model_id,
        id: provider?.id || "",
        set_active: true,      // 存完就用它（引导页和设置页都是这个意图）
      };
      const saved = await api("/api/providers", { method: "POST", body: payload });
      const id = saved.provider.id;
      let persisted = true;
      if (data.api_key) {
        // 密钥单独走 /api/secrets：它进的是系统凭据管理器，不是配置文件
        const secret = await api("/api/secrets", {
          method: "POST",
          body: { provider_id: id, api_key: data.api_key, persist: true },
        });
        persisted = secret.persisted !== false;
      }
      await onSaved?.(id, { persisted });
    } catch (err) {
      notice(err.message, "bad");
    }
  };

  return form;
}

async function reloadCore() {
  const data = await api("/api/bootstrap");
  state.cfg = data.config;
  state.providers = data.providers || [];
  state.presets = data.presets || [];
  state.activeProvider = data.active_provider || "";
  state.secrets = data.secrets || null;
  state.live2d = data.live2d || null;
  return data;
}

function openSettings() {
  showSheet(async (sheet) => {
    const data = await reloadCore();
    const active = state.providers.find((p) => p.id === state.activeProvider);

    sheet.innerHTML = `<h2>设置</h2>`;
    const intro = document.createElement("div");
    intro.className = "hint";
    intro.textContent = state.secrets
      ? `密钥存在：${state.secrets.backend || "未知"}${state.secrets.persistent ? "" : "（这次运行临时用）"}`
      : "";
    sheet.appendChild(intro);

    // 角色：显示的是模型目录名。模型是美术作品，版权与代码无关 ——
    // 用官方样例模型分发时，版权声明就该出现在用户看得到的地方。
    const modelName = (state.live2d && state.live2d.name) || "";
    const who = document.createElement("div");
    who.className = "field";
    who.innerHTML = `<label>角色</label><div class="hint">${
      modelName
        ? `${escapeHtml(modelName)}（Live2D 模型，版权归模型作者）`
        : "没有找到模型 —— 往 models/ 里放一个"
    }</div>`;
    sheet.appendChild(who);

    // 当前用哪个 provider
    const list = document.createElement("div");
    list.className = "field";
    list.innerHTML = `<label>正在使用的模型服务</label><div class="chips"></div>`;
    const chips = list.querySelector(".chips");
    for (const item of state.providers) {
      const chip = document.createElement("div");
      chip.className = `chip ${item.id === state.activeProvider ? "on" : ""}`;
      chip.innerHTML = `${escapeHtml(item.display_name)}<span class="sub">${item.has_key ? "已存密钥" : "没密钥"}</span>`;
      chip.onclick = async () => {
        await api("/api/config", { method: "POST", body: { active_provider: item.id } });
        await reloadCore();
        closeOverlay();
        openSettings();
        setStatus(`已切到 ${item.display_name}`);
      };
      chips.appendChild(chip);
    }
    sheet.appendChild(list);

    // 物理开关（帧率的取舍：开≈36fps，关≈60fps）
    const physics = state.cfg?.live2d_physics !== false;
    const row = document.createElement("div");
    row.className = "switch-row";
    row.innerHTML = `
      <div><div class="label">角色物理（头发、衣摆）</div>
      <div class="desc">关掉能到 60fps，开着约 36fps —— 物理约占一半的帧时间</div></div>
      <button class="switch ${physics ? "on" : ""}" data-act="physics" role="switch"
              aria-checked="${physics}" aria-label="角色物理"></button>`;
    const sw = row.querySelector(".switch");
    sw.onclick = async () => {
      const next = !sw.classList.contains("on");
      sw.classList.toggle("on", next);
      sw.setAttribute("aria-checked", String(next));
      await api("/api/config", { method: "POST", body: { live2d_physics: next } });
      state.cfg.live2d_physics = next;
      try { petWindow()?.yachiyo?.setPhysics(next); } catch { /* 页面没就绪就算了 */ }
      setStatus(`物理已${next ? "打开" : "关闭"}`);
    };
    sheet.appendChild(row);

    // 温度
    const temp = document.createElement("div");
    temp.className = "field";
    temp.innerHTML = `<label>温度（越高越活泼，0.8 比较自然）</label>
      <input type="range" min="0" max="1.5" step="0.1" value="${state.cfg?.temperature ?? 0.8}" />
      <div class="hint" id="temp-value">${state.cfg?.temperature ?? 0.8}</div>`;
    const range = temp.querySelector("input");
    range.oninput = () => { temp.querySelector("#temp-value").textContent = range.value; };
    range.onchange = async () => {
      await api("/api/config", { method: "POST", body: { temperature: Number(range.value) } });
      state.cfg.temperature = Number(range.value);
    };
    sheet.appendChild(temp);

    // 换密钥 / 改配置
    const editTitle = document.createElement("div");
    editTitle.className = "hint";
    editTitle.textContent = active ? `修改「${active.display_name}」` : "还没有可用的服务，先在下面配一个";
    sheet.appendChild(editTitle);
    providerForm(sheet, {
      provider: active || null,
      onSaved: async (id, extra) => {
        await reloadCore();
        closeOverlay();
        setStatus(extra?.persisted === false ? "已保存，但密钥只能用到本次退出（系统凭据库不可用）" : "设置已保存");
      },
    });

    // 危险动作
    const danger = document.createElement("div");
    danger.className = "row wrap";
    danger.innerHTML = `<button class="btn danger" id="clear-chat">清空对话记录</button>`;
    danger.querySelector("#clear-chat").onclick = async () => {
      await api("/api/conversation/clear", { method: "POST" });
      renderHistory([]);
      setStatus("对话记录已清空");
    };
    sheet.appendChild(danger);

    const close = document.createElement("div");
    close.className = "row";
    close.innerHTML = `<button class="btn grow">关闭</button>`;
    close.querySelector("button").onclick = closeOverlay;
    sheet.appendChild(close);
  });
}

function openSetup() {
  showSheet(async (sheet) => {
    await reloadCore();
    sheet.innerHTML = `
      <h2>欢迎，先把八千代叫醒</h2>
      <div class="hint">
        填一个你自己的模型服务（任何 OpenAI 兼容的都行）。<br />
        API Key 直接存进 ${state.secrets?.backend || "系统凭据管理器"}，程序里不留明文，也不经过我们。
      </div>`;
    providerForm(sheet, {
      onSaved: async (id, extra) => {
        const data = await reloadCore();
        const ready = state.providers.find((p) => p.id === id);
        if (extra?.persisted === false) {
          setStatus("密钥只能用到本次退出（系统凭据库不可用）", true);
        }
        closeOverlay();
        setStatus(`已经接上 ${ready?.display_name || id}，开始聊吧`);
      },
    });
  });
}

// ───────────────────────── 启动 ─────────────────────────

async function boot() {
  if (!window.yachiyoShell) {
    setStatus("预加载脚本没生效，界面拿不到后端地址。", true);
    return;
  }
  const info = await window.yachiyoShell.info();
  if (info.error) {
    setStatus(`后端没起来：${info.error}`, true);
    ui.panelStatus.textContent = "后端没起来";
    return;
  }
  state.apiBase = info.apiBase;
  state.token = info.token;

  const data = await reloadCore();
  renderHistory(data.conversation);
  bootPanel();
  connectWs();

  const active = state.providers.find((p) => p.id === data.active_provider);
  const ready = Boolean(active && data.active_has_key);
  if (!ready) {
    setStatus("还没有接上模型服务，点右上角 ⚙ 配一下");
    openSetup();
  } else {
    setStatus(`已接上 ${active.display_name}`);
    setTimeout(() => setStatus(""), 2500);
  }
}

// 事件绑定
$("btn-min").onclick = () => window.yachiyoShell.minimize();
$("btn-close").onclick = () => window.yachiyoShell.close();
$("btn-settings").onclick = openSettings;
ui.send.onclick = sendMessage;
ui.stop.onclick = stopMessage;
ui.input.addEventListener("input", autoGrow);
ui.input.addEventListener("keydown", (event) => {
  // 回车发送，Shift+回车换行（和 Flet 版的 shift_enter=True 一致）
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});
ui.overlay.addEventListener("click", (event) => {
  if (event.target === ui.overlay && !state.cfg?.active_provider) closeOverlay();
});
window.addEventListener("error", (event) => {
  setStatus(`界面出错：${event.message}`, true);
  // 落盘一份：桌面应用里用户看不到 console，"点了没反应"必须能从日志查
  logToShell(`界面错误：${event.message} @${event.filename}:${event.lineno}`);
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  logToShell("未处理的 Promise 拒绝：" + ((reason && (reason.stack || reason.message)) || reason));
});

boot().catch((err) => setStatus(`启动失败：${err.message}`, true));
