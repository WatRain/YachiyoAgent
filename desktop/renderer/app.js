"use strict";

/* 渲染进程：界面 + 对话 + 角色面板。
 *
 * 分层规矩和 Flet 版一样：
 *   · 这里只管"画"和"收集输入"，对话逻辑全在后端（core/chat.py）
 *   · 这个页面永远不碰 API Key —— 密钥只在后端和系统凭据管理器之间走
 *   · 界面拿到的唯一特权来自 preload 暴露的 yachiyoShell（拿地址/token + 两个窗口按钮）
 */

// 最早的一笔：先按系统偏好把 <html data-theme> 定下来，别让浅色用户启动时闪一下深色。
// 到底用哪套（含"跟随系统"三态）要等从后端读到 config.theme，见 applyTheme()。
document.documentElement.dataset.theme =
  window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

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
  themeMode: "system",      // system / light / dark，与后端 config.theme 同步
  petDetached: false,       // 角色是不是已经脱离到桌面浮窗（与 config.live2d_detached 同步）
  panelBooted: false,       // 角色面板是不是已经放过出来了（引导期间先扣着，见 ensurePanel）
  settingsOpen: false,      // 设置浮层打开时暂停 Live2D 的鼠标视线跟随
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

// ───────────────────────── 外观（深色 / 浅色） ─────────────────────────

/* 三态：system 跟随系统、light、dark。值存在后端 config.theme（/api/config 白名单里有
 * theme），所以换主题是持久的；界面只负责把 <html data-theme> 和开关状态对齐。
 * 所有颜色都写在 style.css 的令牌里，这里不碰具体色值。 */
const THEME_MODES = ["system", "light", "dark"];

function systemPrefersDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** "跟随系统"要落到具体的一套配色上，其余原样返回。 */
function resolveTheme(mode) {
  if (mode === "light" || mode === "dark") return mode;
  return systemPrefersDark() ? "dark" : "light";
}

function applyTheme(mode, { persist = false } = {}) {
  const want = THEME_MODES.includes(mode) ? mode : "system";
  const resolved = resolveTheme(want);
  state.themeMode = want;
  document.documentElement.dataset.theme = resolved;
  // 角色页也要跟着换（浮窗里那份走 IPC，见 petCall）。它靠 color-scheme 决定底色透明还是近白
  petCall("setTheme", [resolved]);
  throttlePetDuringTheme();

  // 标题栏的快捷开关 + 设置里的三选一，都是 data-theme-set，一起对齐
  for (const btn of document.querySelectorAll("[data-theme-set]")) {
    btn.setAttribute("aria-checked", String(btn.dataset.themeSet === resolved));
  }
  const desc = document.getElementById("theme-desc");
  if (desc) {
    desc.textContent = want === "system"
      ? `跟随系统，现在是${resolved === "dark" ? "深色" : "浅色"}`
      : `固定${want === "dark" ? "深色" : "浅色"}`;
  }

  if (persist) {
    api("/api/config", { method: "POST", body: { theme: want } }).catch((err) => {
      setStatus(`外观没能存下来：${err.message}`, true);
    });
  }
  logToShell(`外观：${want === "system" ? "跟随系统" : want === "dark" ? "深色" : "浅色"}（实际 ${resolved}）`);
}

// 系统跟着切换时，"跟随系统"要立刻跟上（用户没选固定主题的情况）
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.themeMode === "system") applyTheme("system");
});

// ───────────────────────── 小工具 ─────────────────────────

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 零依赖的 Markdown 渲染（不引 marked 之类的包：离线可用、没有供应链、也没有体积）。
 *  支持：围栏代码块（``` / ~~~，反引号个数不限，带语言标签和复制按钮）、标题、分隔线、
 *  引用（多行合成一块）、有序 / 无序 / 任务列表（按缩进嵌套、续行并进上一项）、表格、
 *  行内代码、粗体、斜体、删除线、链接、裸链接。段落里的单个换行按 <br> 走 ——
 *  模型吐出来的换行通常就是想换行，合成一坨反而难读。
 *
 *  安全约定：**只有本文件拼出来的标签会进 innerHTML**。模型内容一律先过 escapeHtml；
 *  链接只放行 http/https（javascript: 之类退化成纯文本）；图片不渲染成 <img>，
 *  只当链接 —— CSP 的 img-src 只有 self/data/blob/127.0.0.1，外域图本来也加载不出来。
 */

/* 行内元素：一趟扫完，按优先级排（代码 > 链接 > 粗斜 > 粗 > 删 > 斜 > 裸链接），
   没被匹配到的片段才转义成文本。_ 的强调要卡词边界，不然 snake_case 会被拆成斜体。 */
const MD_INLINE = /(`+)([\s\S]*?)\1|!?\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)(?:\s+["'][^"']*["'])?\)|\*\*\*([^*]+)\*\*\*|___([^_]+)___|\*\*([^*]+)\*\*|(?<![A-Za-z0-9_])__([^_]+)__(?![A-Za-z0-9_])|~~([^~]+)~~|\*([^*\n]+)\*|(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])|(https?:\/\/[^\s<>()\[\]]+)/g;

function mdInline(text) {
  // 每次调用现建一个正则：同一个对象被递归调用会把 lastIndex 搅乱
  const re = new RegExp(MD_INLINE.source, "g");
  const s = String(text);
  let out = "";
  let last = 0;
  let m;
  while ((m = re.exec(s)) !== null) {
    // 没被规则吃掉的片段才是纯文本 —— 必须转义（模型吐的 HTML 就死在这一步）
    out += escapeHtml(s.slice(last, m.index));
    last = m.index + m[0].length;
    const g = m.slice(1, 13);
    if (g[0] !== undefined) out += `<code>${escapeHtml(g[1])}</code>`;
    else if (g[2] !== undefined) out += mdLink(g[2], g[3]);
    else if (g[4] !== undefined) out += `<strong><em>${mdInline(g[4])}</em></strong>`;
    else if (g[5] !== undefined) out += `<strong><em>${mdInline(g[5])}</em></strong>`;
    else if (g[6] !== undefined) out += `<strong>${mdInline(g[6])}</strong>`;
    else if (g[7] !== undefined) out += `<strong>${mdInline(g[7])}</strong>`;
    else if (g[8] !== undefined) out += `<del>${mdInline(g[8])}</del>`;
    else if (g[9] !== undefined) out += `<em>${mdInline(g[9])}</em>`;
    else if (g[10] !== undefined) out += `<em>${mdInline(g[10])}</em>`;
    else out += mdAutolink(g[11]);
    if (re.lastIndex <= m.index) re.lastIndex = m.index + 1;   // 防零宽死循环
  }
  return out + escapeHtml(s.slice(last));
}

/** 链接只放行 http/https；别的协议（javascript:、data:…）原样当文本，不给它变成可点的东西。 */
function mdLink(label, href) {
  const url = String(href || "").trim();
  if (!/^https?:\/\//i.test(url)) return escapeHtml(String(label || "") + "（" + url + "）");
  const text = String(label || "").trim() || url;
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${mdInline(text)}</a>`;
}

/** 裸链接：句末的标点不属于 URL，得留在链接外面（中文句号也一样）。 */
function mdAutolink(url) {
  const trimmed = String(url).replace(/[.,;:!?，。；：！？、）】》」』]+$/, "");
  const tail = String(url).slice(trimmed.length);
  return `<a href="${escapeHtml(trimmed)}" target="_blank" rel="noreferrer">${escapeHtml(trimmed)}</a>`
    + escapeHtml(tail);
}

/** 代码块：语言标签 + 复制按钮。复制按钮是事件代理（气泡每次重渲染都换 innerHTML）。 */
function mdCode(code, lang) {
  const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
  return `<div class="code-block"><div class="code-head">`
    + `<span class="code-lang">${escapeHtml(lang || "")}</span>`
    + `<button class="code-copy" type="button">复制</button></div>`
    + `<pre><code${cls}>${escapeHtml(code)}</code></pre></div>`;
}

const MD_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`]*)$/;
const MD_FENCE_END = /^ {0,3}(`{3,}|~{3,})\s*$/;
const MD_HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const MD_HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MD_QUOTE = /^ {0,3}>\s?(.*)$/;
const MD_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;

function mdToHtml(source) {
  return mdBlocks(String(source == null ? "" : source).split(/\r?\n/)).join("");
}

/** 返回块级标签的数组（不是拼好的字符串）：列表项要单独剥掉顶层的 <p> 壳。 */
function mdBlocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // 围栏代码块：没闭合也照收（流式输出时经常先出现 ``` 才慢慢吐内容）
    const fence = line.match(MD_FENCE);
    if (fence) {
      const mark = fence[1][0];
      const len = fence[1].length;
      const lang = (fence[2] || "").trim().split(/\s+/)[0] || "";
      const body = [];
      i++;
      while (i < lines.length) {
        const close = lines[i].match(MD_FENCE_END);
        if (close && close[1][0] === mark && close[1].length >= len) { i++; break; }
        body.push(lines[i]);
        i++;
      }
      out.push(mdCode(body.join("\n"), lang));
      continue;
    }

    const heading = line.match(MD_HEADING);
    if (heading) {
      // 气泡里 h1 太大，整体降两级：## → h4
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${mdInline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (MD_HR.test(line)) { out.push("<hr>"); i++; continue; }

    // 引用：连续的 > 行合成一块，里面再当 Markdown 递归渲染
    if (MD_QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length) {
        const q = lines[i].match(MD_QUOTE);
        if (q) { inner.push(q[1]); i++; continue; }
        if (lines[i].trim() && !MD_FENCE.test(lines[i]) && !MD_ITEM.test(lines[i])
            && !MD_HEADING.test(lines[i]) && !MD_HR.test(lines[i])) { inner.push(lines[i]); i++; continue; }
        break;
      }
      out.push(`<blockquote>${mdBlocks(inner).join("")}</blockquote>`);
      continue;
    }

    // 表格：这一行有 | 且下一行是分隔行
    if (line.includes("|") && i + 1 < lines.length && mdTableAlign(lines[i + 1])) {
      const table = mdTable(lines, i);
      out.push(table.html);
      i = table.next;
      continue;
    }

    if (MD_ITEM.test(line)) {
      const list = mdList(lines, i);
      out.push(list.html);
      i = list.next;
      continue;
    }

    // 段落：一直吃到空行或下一个块级元素为止
    const para = [];
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) break;
      if (MD_FENCE.test(l) || MD_HEADING.test(l) || MD_HR.test(l) || MD_QUOTE.test(l) || MD_ITEM.test(l)) break;
      if (l.includes("|") && i + 1 < lines.length && mdTableAlign(lines[i + 1])) break;
      para.push(mdInline(l.replace(/[ \t]+$/, "")));
      i++;
    }
    out.push(`<p>${para.join("<br>")}</p>`);
  }
  return out;
}

function mdLeadingWidth(line) {
  let n = 0;
  for (const ch of line) {
    if (ch === " ") n++;
    else if (ch === "\t") n += 4;
    else break;
  }
  return n;
}

function mdSplitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

/** 表格分隔行 → 每列的对齐；不是分隔行就返回 null。 */
function mdTableAlign(line) {
  const cells = mdSplitRow(line);
  if (!cells.length) return null;
  const align = [];
  for (const cell of cells) {
    if (!/^:?-+:?$/.test(cell)) return null;
    align.push(cell.startsWith(":") && cell.endsWith(":") ? "center"
      : cell.endsWith(":") ? "right"
        : cell.startsWith(":") ? "left" : "");
  }
  return align;
}

function mdTable(lines, start) {
  const align = mdTableAlign(lines[start + 1]);
  const head = mdSplitRow(lines[start]);
  let i = start + 2;
  const rows = [];
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    rows.push(mdSplitRow(lines[i]));
    i++;
  }
  const attr = (k) => (align[k] ? ` class="ta-${align[k]}"` : "");
  const thead = head.map((c, k) => `<th${attr(k)}>${mdInline(c)}</th>`).join("");
  const tbody = rows.map((r) =>
    `<tr>${r.map((c, k) => `<td${attr(k)}>${mdInline(c)}</td>`).join("")}</tr>`).join("");
  return {
    html: `<div class="md-table"><table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table></div>`,
    next: i,
  };
}

/** 列表：先圈出属于这个列表的连续行，再按缩进把项分出来，每项的内容递归当 Markdown 渲染。 */
function mdList(lines, start) {
  const first = lines[start].match(MD_ITEM);
  const baseIndent = first[1].length;
  const firstOrdered = /^\d/.test(first[2]);
  let end = start;
  while (end < lines.length) {
    const l = lines[end];
    if (!l.trim()) {
      const next = lines[end + 1];
      if (next === undefined) break;
      const nm = next.match(MD_ITEM);
      if (!((nm && nm[1].length >= baseIndent) || mdLeadingWidth(next) > baseIndent)) break;
      end++;
      continue;
    }
    const m = l.match(MD_ITEM);
    if (m && m[1].length === baseIndent && /^\d/.test(m[2]) !== firstOrdered) break;  // 换了一种列表，交给外层
    if (m && m[1].length >= baseIndent) { end++; continue; }
    if (!m && mdLeadingWidth(l) > baseIndent) { end++; continue; }
    break;
  }
  return { html: mdListItems(lines.slice(start, end), baseIndent, firstOrdered), next: end };
}

function mdListItems(block, baseIndent, ordered) {
  const items = [];
  let cur = null;
  for (const line of block) {
    const m = line.match(MD_ITEM);
    if (m && m[1].length === baseIndent && /^\d/.test(m[2]) === ordered) {
      // markerWidth：把续行按标记宽度回缩，"  - 子项" 就落到子列表该有的缩进上
      cur = { body: [m[3]], markerWidth: m[0].length - m[3].length };
      items.push(cur);
      continue;
    }
    if (!cur) continue;
    if (!line.trim()) { cur.body.push(""); continue; }
    cur.body.push(line.slice(Math.min(mdLeadingWidth(line), cur.markerWidth)));
  }

  const startNum = ordered ? Number(block[0].match(MD_ITEM)[2].replace(/[.)]$/, "")) : 1;
  const startAttr = ordered && startNum > 1 ? ` start="${startNum}"` : "";
  // 列表里出现过空行就是「松散列表」：段落各自留着 <p>；否则是紧凑列表，段落外壳要剥掉
  const loose = block.some((l) => !l.trim());
  const html = items.map((it) => {
    let box = "";
    const task = it.body[0].match(/^\[([ xX])\]\s+(.*)$/);
    if (task) {
      it.body[0] = task[2];
      box = `<input type="checkbox" disabled${task[1].toLowerCase() === "x" ? " checked" : ""} /> `;
    }
    return `<li>${box}${mdItemBody(it.body, loose)}</li>`;
  }).join("");
  return `<${ordered ? "ol" : "ul"}${startAttr}>${html}</${ordered ? "ol" : "ul"}>`;
}

/** 列表项的内容：紧凑列表把顶层段落的 <p> 剥掉（只剥顶层，嵌套列表/引用里的不动）。 */
function mdItemBody(lines, loose) {
  const body = lines.slice();
  while (body.length && !body[body.length - 1].trim()) body.pop();
  const blocks = mdBlocks(body);
  if (loose) return blocks.join("");
  return blocks.map((b) => {
    const only = b.match(/^<p>([\s\S]*)<\/p>$/);
    return only ? only[1] : b;
  }).join("");
}

/* 代码块的复制按钮：代理挂在消息容器上 —— 气泡每帧都在重渲染 innerHTML，
   直接往按钮上绑 onclick 一刷新就没了。 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 退路：clipboard 拿不到权限时用老办法
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch { return false; }
  }
}

ui.messages.addEventListener("click", (event) => {
  const btn = event.target.closest(".code-copy");
  if (!btn) return;
  const code = btn.closest(".code-block") && btn.closest(".code-block").querySelector("code");
  copyText(code ? code.textContent : "").then((ok) => {
    btn.textContent = ok ? "已复制" : "复制失败";
    btn.classList.toggle("bad", !ok);
    setTimeout(() => { btn.textContent = "复制"; btn.classList.remove("bad"); }, 1200);
  });
});

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
  toolCards = [];               // 补画的历史卡片都是"已完成"，不留给 tool_end 认领
  let shown = 0;
  for (const message of messages || []) {
    const role = message.role;
    const content = message.content;
    // 工具卡片：重启后也要把上次调过什么补回来。
    // 后端已经把 label / command / result 翻好了（跟实时那次是同一个函数），
    // 这里只负责画，所以补出来的卡片和当时看到的长得一模一样。
    if (role === "tool") {
      const card = buildToolCard(message);
      finishCard(card, message.result || "", message.failed);
      ui.messages.appendChild(card);
      shown += 1;
      continue;
    }
    // 只画对话本身：system（人格设定）不是给用户看的
    if (!["user", "assistant"].includes(role) || !content) continue;
    addBubble(content, { role: role === "user" ? "user" : "bot", animate: false });
    shown += 1;
  }
  if (shown) scrollDown();
  return shown;
}

/* 工具卡片：模型动手的时候，对话流里插一张这样的小卡片 ——
   工具的中文名 + 它到底要执行什么。

   为什么非要有：模型调工具时会安静好几秒，用户只看到状态行在转，
   完全不知道它在干嘛、更不知道要不要担心。把「执行了什么」摆出来，
   这几秒就从"卡住了"变成"它在干活"。

   command 是后端翻好的（core/tools.py 的 tool_command），界面只管显示 ——
   翻译只有一处，前端不自己解释参数。 */
let toolCards = [];          // 本轮还没收尾的卡片，tool_end 按顺序认领

/* 卡片的骨架（圆点 + 中文名 + 要执行什么）。实时插入和重启后补画共用这一份 ——
   两处各写一遍，迟早会长得不一样。 */
function buildToolCard(msg) {
  const card = document.createElement("div");
  card.className = "tool-call";

  const head = document.createElement("div");
  head.className = "tool-call-head";

  const dot = document.createElement("span");
  dot.className = "tool-call-dot";

  const label = document.createElement("span");
  label.className = "tool-call-label";
  label.textContent = msg.label || msg.name || "工具";
  head.append(dot, label);
  card.appendChild(head);

  // 不用参数的工具（看时间、截屏）就没有这一行，卡片自动矮一截
  if (msg.command) {
    const pre = document.createElement("pre");
    pre.className = "tool-call-cmd";
    const code = document.createElement("code");
    code.textContent = msg.command;
    pre.appendChild(code);
    card.appendChild(pre);
  }
  return card;
}

/* 收尾：写上「拿回来了什么」。收尾两次无妨（classList.add 是幂等的）。 */
function finishCard(card, text, failed) {
  card.classList.add("is-done");
  if (failed) card.classList.add("is-failed");
  if (!text) return;              // 没结果就不加这一行，卡片自动矮一截
  const line = document.createElement("p");
  line.className = "tool-call-result";
  line.textContent = text;        // textContent：结果里可能有尖括号，一律当字面量
  card.appendChild(line);
}

function addToolCall(msg) {
  const card = buildToolCard(msg);

  ui.messages.appendChild(card);
  scrollDown();
  toolCards.push(card);

  // 卡片之前那段文字就留在上面 —— 按时间顺序它确实发生在调用之前
  // （模型爱先说一句「我查一下」，日志里每个用了工具的回合都有这么一句）。
  // 但它不能继续当答案的容器：这里收笔，让后面真正的答案另起一个气泡落在卡片下面，
  // 否则答案会写进上面那个气泡里，看着像是「回复在工具调用的上方」。
  reply = null;
  replyText = "";
}

/* 一次调用做完了：把最早那张还在转的卡片收尾，顺手写上「拿回来了什么」。
   按顺序认领（FIFO）—— 后端的 tool_start / tool_end 本来就是成对同序的。

   result 是后端压过的一句话（core/tools.py 的 tool_result）：完整结果可能有两万字，
   全塞进渲染进程只为显示一行字，不值当。 */
function finishToolCall(msg) {
  const card = toolCards.shift();
  if (!card) return;
  finishCard(card, (msg && msg.result) || "", msg && msg.failed);
  scrollDown();
}

/* 新一轮开始前把上一轮的卡片都收掉：万一有哪张没等到 tool_end
   （模型出错、用户中途取消），别让它一直转下去，看着像卡死了。 */
function resetToolCalls() {
  for (const card of toolCards) card.classList.add("is-done");
  toolCards = [];
}

let statusFadeTimer = 0;

/* 状态行的动效（约定见 style.css 末尾）：进场直接淡入，退场必须先把文字留着淡完再清空 ——
   一置空就什么都看不见了，那文字就是"啪"地没的。整行高度固定，淡出不会让输入框跳。
   同一个函数被连着调用（比如 onopen 清空、紧接着 onclose 报错）也安全：
   新的文字会把正在淡出的那一层原地"接住"重新淡入。 */
function setStatus(text, isError = false) {
  clearTimeout(statusFadeTimer);
  ui.status.classList.toggle("error", Boolean(isError));
  const next = text || "";
  if (!next) {
    ui.status.classList.add("is-empty");
    statusFadeTimer = setTimeout(() => {
      if (ui.status.classList.contains("is-empty")) ui.status.textContent = "";
    }, 200);
    return;
  }
  ui.status.textContent = next;
  ui.status.classList.remove("is-empty");
}

function setBusy(busy) {
  state.busy = busy;
  ui.send.classList.toggle("hidden", busy);
  ui.stop.classList.toggle("hidden", !busy);
  ui.dot.classList.toggle("busy", busy);
  ui.shell.style.borderColor = busy ? "var(--accent)" : "var(--glass-border)";
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
const PET_EXPRESSIONS = Object.freeze({
  happy: "smile",
  sad: "tears",
  awkward: "tear_drop",
  skeptical: "narrow_eyes",
});
const PET_MOTIONS = Object.freeze({
  greet: "Greet",
  comfort: "ReactError",
});
let pendingPetControl = null;
let petResponseExpressionActive = false;
let petExpressionResetTimer = 0;

function normalizePetControl(msg) {
  const expression = typeof msg.expression === "string" ? msg.expression : "";
  const motion = typeof msg.motion === "string" ? msg.motion : "";
  if (expression && expression !== "neutral" && !Object.hasOwn(PET_EXPRESSIONS, expression)) return null;
  if (motion && !Object.hasOwn(PET_MOTIONS, motion)) return null;
  if (!expression && !motion) return null;
  return { expression, motion };
}

function applyPetControl(control) {
  pendingPetControl = null;
  if (control.motion) {
    petCall("playMotion", [PET_MOTIONS[control.motion], 0, 3, {
      loop: false,
      resetExpression: false,
    }]);
  }
  if (control.expression === "neutral") {
    petCall("resetExpression");
    petResponseExpressionActive = false;
  } else if (control.expression) {
    petCall("setExpression", [PET_EXPRESSIONS[control.expression]]);
    petResponseExpressionActive = true;
  }
  if (petResponseExpressionActive && !state.busy) schedulePetExpressionReset();
}

async function handlePetControl(msg) {
  const control = normalizePetControl(msg);
  if (!control) return;
  let ready = false;
  if (state.petDetached) {
    const snapshot = await petSnapshot();
    ready = Boolean(snapshot?.ready);
  } else {
    ready = Boolean(petWindow()?.yachiyo?.ready);
  }
  if (!ready) {
    pendingPetControl = control;
    return;
  }
  applyPetControl(control);
}

function schedulePetExpressionReset() {
  clearTimeout(petExpressionResetTimer);
  petExpressionResetTimer = setTimeout(() => {
    petExpressionResetTimer = 0;
    if (!petResponseExpressionActive) return;
    petCall("resetExpression");
    petResponseExpressionActive = false;
  }, 3500);
}

function beginPetResponse() {
  clearTimeout(petExpressionResetTimer);
  petExpressionResetTimer = 0;
  pendingPetControl = null;
  if (petResponseExpressionActive) petCall("resetExpression");
  petResponseExpressionActive = false;
}

function endPetResponse() {
  if (petResponseExpressionActive) schedulePetExpressionReset();
}

/* 助手气泡等到真有内容才建。
   原来是在 sendMessage 里就先建一个空气泡占位，可模型要是先调工具再回话，
   工具卡片就只能排在那个气泡下面 —— 最后答案反而显示在"我调了什么"的上面，
   顺序整个反了。error / stopped / done 三条分支本来就有兜底，懒建是安全的。 */
function ensureReply() {
  if (!reply) reply = addBubble("", { role: "bot" });
  return reply;
}

function onWsEvent(msg) {
  switch (msg.type) {
    case "start":
      beginPetResponse();
      resetToolCalls();
      setBusy(true);
      setStatus("正在思考…");
      break;
    case "delta": {
      replyText += msg.text || "";
      const now = performance.now();
      if (now - lastPulse >= 90) { lastPulse = now; pulseMouth(); }
      if (now - lastFlush >= THROTTLE_MS) {
        lastFlush = now;
        setBubbleText(ensureReply(), replyText);
        scrollDown();
      }
      break;
    }
    case "tool_start":
      state.toolTask = msg.name;
      setStatus(`正在用「${msg.label || msg.name}」…`);
      addToolCall(msg);
      break;
    case "tool_end":
      state.toolTask = null;
      setStatus("");
      finishToolCall(msg);
      break;
    case "live2d_control":
      handlePetControl(msg);
      break;
    case "tool_ask":
      // 模型想动真格了，等用户点头（后端那头的 approve 回调正卡在这儿）
      askToolPermission(msg);
      break;
    case "done":
      replyText = msg.text || replyText;
      setBubbleText(ensureReply(), replyText || "（没有内容）");
      finishTurn();
      endPetResponse();
      break;
    case "stopped":
      setBubbleText(ensureReply(), (msg.text || replyText || "") + "\n\n_（已停止）_");
      finishTurn();
      endPetResponse();
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
      endPetResponse();
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
  reply = null;              // 懒建，见 ensureReply()
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

/* ── 角色目标：面板里的 iframe，还是桌面上的浮窗 ──
 *
 * 角色只有一份，但可能住在两个地方：
 *   · 默认 —— 右侧面板里的 iframe，可以直接 contentWindow.yachiyo 调；
 *   · 脱离后 —— 桌面上的浮窗（另一个 BrowserWindow），只能让主进程转发。
 * 上层（口型 / 主题 / 帧率 / 统计）一律只认下面这几个函数，别到处判断。
 */
function petCall(name, args = []) {
  if (state.petDetached) {
    try { window.yachiyoShell?.petCmd?.(name, args); } catch { /* 主进程没了也无所谓 */ }
    return null;                  // 单向：浮窗那边的返回值不往回带
  }
  const win = petWindow();
  const fn = win && win.yachiyo && win.yachiyo[name];
  if (typeof fn !== "function") return null;
  try { return fn.apply(win.yachiyo, args); } catch { return null; }
}

/** 角色页的状态快照。脱离之后得问浮窗（异步）。 */
async function petSnapshot() {
  if (state.petDetached) {
    try { return await window.yachiyoShell.petState(); } catch { return null; }
  }
  const win = petWindow();
  if (!win || !win.yachiyo) return null;
  try { return win.yachiyo.state(); } catch { return null; }
}

/** 角色页地址。standalone=true 时带 ?window=1 —— 那一页会按"独立窗口"工作
 *  （自己拖动、右键弹原生菜单、接主进程转发的指令），见 pet.html 末尾那段脚本。 */
function petPageUrl(standalone) {
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const base = state.apiBase.replace(/\/$/, "") + state.live2d.page;
  const query = `?model=${encodeURIComponent(state.live2d.model)}&theme=${theme}`;
  return standalone ? `${base}${query}&window=1` : `${base}${query}`;
}

/* 这一行的说明文字有两份（模板里一份、切换时一份），写成常量免得两边说不一样的话。 */
const PET_DETACH_DESC_ON = "现在在桌面上：按住她可以拖动，右键可以收回或改置顶";
const PET_DETACH_DESC_OFF = "让八千代从右侧面板里出来，变成桌面上一个可以拖动的透明窗口";

/* 切换"角色住哪儿"。三件事必须一起做：界面状态、面板里的 iframe、后端配置。
 *
 * 面板里的 iframe 在脱离时要卸掉（src=about:blank）：两个窗口各跑一份模型
 * 是白烧 GPU（一份就已经把主线程吃到 99%）。收回来时按当前主题重新装一次。 */
function setPetDetached(flag, why) {
  if (state.petDetached === flag) return;
  state.petDetached = flag;
  document.body.classList.toggle("pet-detached", flag);
  if (flag) {
    try { ui.pet.src = "about:blank"; } catch { /* 忽略 */ }
  } else if (state.live2d && state.live2d.ready) {
    ui.pet.src = petPageUrl(false);
  }
  const toggle = $("pet-detach");
  if (toggle) {
    toggle.classList.toggle("on", flag);
    toggle.setAttribute("aria-checked", String(flag));
  }
  // 设置浮层正开着的时候也要跟着改（浮窗可能被右键菜单关掉，那时开关还亮着）
  const desc = $("pet-detach-desc");
  if (desc) desc.textContent = flag ? PET_DETACH_DESC_ON : PET_DETACH_DESC_OFF;
  logToShell(`角色位置：${flag ? "桌面浮窗" : "右侧面板"}（${why}）`);
  // 只有真的和配置不一样才写一次，免得每次启动都回写。
  // 本地这份 cfg 也要跟着改：不然"脱离→收回"里收回那次判断出"没变化"，后端就一直留着 true。
  if (Boolean(state.cfg?.live2d_detached) !== flag) {
    if (state.cfg) state.cfg.live2d_detached = flag;
    api("/api/config", { method: "POST", body: { live2d_detached: flag } }).catch(() => {
      /* 存不下来只是下次启动回到面板，不值得打扰用户；把本地缓存拨回去免得一直骗自己 */
      if (state.cfg) state.cfg.live2d_detached = !flag;
    });
  }
}

async function detachPet(opts = {}) {
  if (state.petDetached) return true;
  if (!state.live2d || !state.live2d.ready) {
    if (!opts.quiet) setStatus("还没有可用的角色，先修好 Live2D 再说", true);
    return false;
  }
  if (!window.yachiyoShell?.petDetach) {
    if (!opts.quiet) setStatus("这个版本的主进程不支持脱离窗口", true);
    return false;
  }
  let res = null;
  try {
    res = await window.yachiyoShell.petDetach({ url: petPageUrl(true) });
  } catch (err) {
    res = { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
  if (!res || !res.ok) {
    if (!opts.quiet) setStatus(`脱离失败：${(res && res.error) || "未知原因"}`, true);
    // 启动恢复失败：面板里那份刚才被 bootPanel 跳过了，补装回去，
    // 别把界面留在"面板空着、浮窗也没有"的状态。
    else if (state.live2d && state.live2d.ready) ui.pet.src = petPageUrl(false);
    return false;
  }
  setPetDetached(true, "脱离到桌面");
  if (!opts.quiet) setStatus("八千代到桌面上了，按住她可以拖，右键她可以收回");
  return true;
}

async function dockPet(opts = {}) {
  if (!state.petDetached) return true;
  try { await window.yachiyoShell?.petDock?.(); } catch { /* 已经关了也一样 */ }
  setPetDetached(false, opts.why || "收回面板");
  if (!opts.quiet) setStatus("八千代回到面板里了");
  return true;
}

/* 切主题时给角色页降载。
 *
 * 实测（CDP Performance 域）：静置 1.5 秒里 TaskDuration=1.511s、ScriptDuration=
 * 1.486s —— 主线程约 99% 的时间都被 Live2D 的每帧脚本吃掉（update ~7-12ms +
 * render ~5ms，75Hz 屏上正好贴满）。换主题自己只花 ~43ms 样式重算（19 次 recalc），
 * 但它落在一个没有余量的线程上，于是过渡期间必然掉帧（最坏帧 66~93ms、
 * 1.6 秒里有 6 帧 >50ms）。做法：过渡这 800ms 里把模型降到 20fps 让出主线程，
 * 之后再恢复 —— 实测最坏帧 66.7 → 40.0ms、>50ms 的卡顿帧 6 → 0。
 *
 * 为什么是降载而不是暂停：暂停（setPaused）同样能清零卡顿，但角色会硬停在
 * 半空一下，看着比掉几帧更怪；20fps 只是变慢，几乎看不出来。 */
const PET_FPS_CAP = 60;
const PET_FPS_DURING_THEME = 20;
const PET_THROTTLE_MS = 800;
let petThrottleTimer = 0;

function throttlePetDuringTheme() {
  clearTimeout(petThrottleTimer);
  // 脱离状态下 petCall 是单向的（返回 null 但指令确实发出去了），不能拿返回值当"没就绪"
  const throttled = petCall("setFrameCap", [PET_FPS_DURING_THEME]);
  if (throttled === null && !state.petDetached) return;   // 面板里的角色页还没就绪：这次降载就算了
  petThrottleTimer = setTimeout(() => {
    petCall("setFrameCap", [PET_FPS_CAP]);
  }, PET_THROTTLE_MS);
}

function pulseMouth() {
  const value = 0.25 + Math.random() * 0.65;   // 说话时的口型（没有真实音频，先按节奏开合）
  petCall("setMouth", [value]);
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
    if (state.settingsOpen) {
      if (last !== "paused") {
        last = "paused";
        try { petCall("lookForward"); } catch { /* 页面可能还没就绪 */ }
      }
      return;
    }
    // 脱离之后主进程不再往这边发 cursor（它直接驱动浮窗），这条只是保险
    if (state.petDetached) return;
    const win = petWindow();
    if (!win || !win.yachiyo) return;
    // 画面坐标 = 指针相对 iframe 左上角的位置。iframe 的 CSS 尺寸就是页面里的
    // 像素尺寸（两者都是 DIP），所以不用换算比例；窗口被拖动过就重新校准。
    const x = Math.round(point.x - rect.left);
    const y = Math.round(point.y - rect.top);
    // 面板里不扩范围：指针一离开画面就回正前方。
    // （外扩 GAZE_PAD 只给脱离后的浮窗，见 desktop/main.js 的 cursorLoop）
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
    // 这种是"一直修不好"的状态：不闪，留在那儿给用户看见
    setPanelStatus((state.live2d && state.live2d.message) || "没有可用的 Live2D 模型");
    return;
  }
  // 界面不再提供物理开关（关掉物理角色会僵在那儿，用户反馈太怪），所以这里也不传 physics：
  // pet.html 仍然认 ?physics=0，那是留给调试/自动化的口子，不在界面上出现。
  // 主题必须传：角色页靠 color-scheme 决定 iframe 的底色是透明还是近白（见 pet.html 里的注释），
  // 不传的话深色主题下面板里就是一块白。
  // 脱离状态下这一格是空的（模型在浮窗里），boot() 末尾会把浮窗开起来。
  // 配置里写着"上次是脱离的"也跳过：boot() 是先 bootPanel 再恢复浮窗的，
  // 不跳过就会先把模型在面板里加载一遍、再扔掉（多烧 2 秒 GPU，还可能闪一下）。
  if (!state.petDetached && !state.cfg?.live2d_detached) ui.pet.src = petPageUrl(false);
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

/* 状态药丸：平时是"睡着"的，只在状态变化时现一下身。
   flash = true 走一段渐入 → 停一会儿 → 渐出的动画（就是"模型就绪"那一下），
   动画放完挂上 .is-off 把整颗药丸藏起来 —— 文字留在 DOM 里（无障碍能读到），
   版面也不动，所以不会因为一句提示就把角色面板顶下去。 */
let panelStatusTimer = 0;

function setPanelStatus(text, { flash = false } = {}) {
  const el = ui.panelStatus;
  clearTimeout(panelStatusTimer);
  el.classList.remove("is-flash", "is-off");
  el.textContent = text;
  if (!flash) return;
  void el.offsetWidth;                 // 强制一次重排，让动画能重新播
  el.classList.add("is-flash");
  const done = () => {
    clearTimeout(panelStatusTimer);
    panelStatusTimer = 0;
    el.classList.remove("is-flash");
    el.classList.add("is-off");
  };
  el.addEventListener("animationend", done, { once: true });
  panelStatusTimer = setTimeout(done, 3400);   // 兜底：动画被跳过（reduced-motion / 被样式禁用）时也能收尾
}

async function pollPanel() {
  const info = await petSnapshot();

  if (info?.ready && pendingPetControl) {
    applyPetControl(pendingPetControl);
  }

  if (!info) {
    // 掉回"没就绪"要把那一次的印记清掉：收回面板时 iframe 会重载，这一下必然
    // 读到 null；不清的话"模型就绪"只报过一次，之后状态药丸就永远停在"正在唤醒"了。
    panelSelfCheck = "";
    setPanelStatus("正在唤醒八千代…");
  } else if (info.error) {
    setPanelStatus(`角色加载失败：${info.error}`);
    if (panelSelfCheck !== "error:" + info.error) {
      panelSelfCheck = "error:" + info.error;
      logToShell("Live2D 面板出错：" + info.error, "stage=" + info.stage, "diag=" + JSON.stringify(info.diag || []));
    }
  } else if (!info.ready) {
    panelSelfCheck = "";          // 同上：还没就绪不算报过
    setPanelStatus("正在唤醒八千代…");
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
    // 状态行只说"能不能用"：帧率、物理这些在这里都是噪音（真要查看下面每 15 秒那行日志）
    if (panelSelfCheck !== "ready") {
      panelSelfCheck = "ready";
      setPanelStatus("模型就绪", { flash: true });
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

/* 危险工具的审批弹窗。
   后端的 approve 回调正 await 在这一句回答上，用户不点，这一轮就卡着
   （最多等 5 分钟，超时按拒绝算）。所以这个弹窗必须能"只靠键盘/只靠鼠标"
   关掉，而且点拒绝和点允许走同一条路。 */
let toolAskId = null;

function askToolPermission(msg) {
  const id = String(msg.id || "");
  if (!id || toolAskId === id) return;
  toolAskId = id;

  const answer = (ok) => {
    if (toolAskId !== id) return;        // 已经答过了（比如连点两下）
    toolAskId = null;
    try {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "tool_decision", id, ok }));
      }
    } catch { /* 连接断了就算了，后端那边也会按拒绝收尾 */ }
    closeOverlay();
  };

  showSheet((sheet) => {
    sheet.classList.add("tool-ask");

    const title = document.createElement("h2");
    title.textContent = `可以${msg.label || msg.name}吗？`;
    sheet.appendChild(title);

    const note = document.createElement("div");
    note.className = "hint";
    note.textContent = "八千代想在你电脑上做这件事，需要你点一下头。";
    sheet.appendChild(note);

    const box = document.createElement("div");
    box.className = "tool-ask-preview";
    const code = document.createElement("code");
    code.textContent = String(msg.preview || msg.name || "");
    box.appendChild(code);
    sheet.appendChild(box);

    const actions = document.createElement("div");
    actions.className = "tool-ask-actions";

    const no = document.createElement("button");
    no.className = "btn ghost grow";
    no.type = "button";
    no.textContent = "拒绝";
    no.onclick = () => answer(false);

    const yes = document.createElement("button");
    yes.className = "btn primary grow";
    yes.type = "button";
    yes.textContent = "允许";
    yes.onclick = () => answer(true);

    actions.append(no, yes);
    sheet.appendChild(actions);

    // 默认焦点给「拒绝」：手滑敲回车不该等于放行
    setTimeout(() => no.focus(), 30);
  });
}

/* 关浮层：先挂 .is-closing 把退出动画播一遍（进入是 sheet-in/scrim-in，出去反着来），
   动画放完再真的收起来 —— 退出用 sheet 的 animationend 当信号，另配兜底定时器
   （reduced-motion 或被探针禁掉动画时也得能收尾）。
   探针要"立刻没了"就 closeOverlay({ immediate: true })。 */
let overlayCloseTimer = 0;

function closeOverlay(opts) {
  if (ui.overlay.classList.contains("hidden")) return;
  const immediate = Boolean(opts && opts.immediate === true);
  const sheet = ui.overlay.querySelector(".sheet");
  const finish = () => {
    clearTimeout(overlayCloseTimer);
    overlayCloseTimer = 0;
    ui.overlay.classList.remove("is-closing");
    ui.overlay.classList.add("hidden");
    ui.overlay.innerHTML = "";
    if (state.settingsOpen) setSettingsGazePaused(false);
  };
  if (immediate || !sheet) { finish(); return; }
  const onEnd = (ev) => {
    if (ev.target !== sheet) return;      // animationend 会冒泡，别被子元素的动画骗了
    sheet.removeEventListener("animationend", onEnd);
    finish();
  };
  sheet.addEventListener("animationend", onEnd);
  ui.overlay.classList.add("is-closing");
  overlayCloseTimer = setTimeout(finish, 900);
}

/* 滚动条按需显示：平静的时候右边不挂那一条，真的滑过之后留 700ms 再收回去（方便去抓滑块）。
   配合的样式在 style.css 的 .messages / .sheet 里，类名统一叫 .is-scrolling。 */
const scrollIdleTimers = new WeakMap();
function markScrolling(el) {
  el.addEventListener("scroll", () => {
    el.classList.add("is-scrolling");
    clearTimeout(scrollIdleTimers.get(el));
    scrollIdleTimers.set(el, setTimeout(() => el.classList.remove("is-scrolling"), 700));
  }, { passive: true });
}

function showSheet(build) {
  clearTimeout(overlayCloseTimer);          // 上一个浮层还在播退场就被重新打开：取消收尾
  overlayCloseTimer = 0;
  ui.overlay.classList.remove("is-closing");
  ui.overlay.innerHTML = "";
  const sheet = document.createElement("div");
  sheet.className = "sheet";
  ui.overlay.appendChild(sheet);
  ui.overlay.classList.remove("hidden");
  markScrolling(sheet);
  build(sheet);
}

function setSettingsGazePaused(paused) {
  state.settingsOpen = Boolean(paused);
  try { window.yachiyoShell?.setGazePaused?.(state.settingsOpen); } catch { /* 主进程可能已关闭 */ }
  // 主面板由渲染层驱动，浮窗由主进程驱动；打开设置时两边都回正。
  if (state.settingsOpen && !state.petDetached) petCall("lookForward");
}

const PROTOCOLS = [
  ["openai", "OpenAI 兼容"],
  ["anthropic", "Anthropic"],
  ["gemini", "Gemini"],
  ["custom", "自定义"],
];

/** provider 编辑表单（引导页和设置页共用）。
 *  actionsHost：动作行（测试连接 / 保存）挂到哪儿。默认留在表单里（设置页就这样）；
 *  引导页传底部动作条进来，好让主按钮跟系统引导一样一直贴在卡片底部。 */
function providerForm(sheet, { provider = null, onSaved, actionsHost = null }) {
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
    <div id="p-notice" class="notice"></div>
  `;
  sheet.appendChild(form);

  // 动作行单独建，方便整行搬去别处（引导页的底部动作条）
  const actions = document.createElement("div");
  actions.className = "row form-actions";
  actions.innerHTML = `
    <button class="btn" id="p-test" type="button">测试连接</button>
    <button class="btn primary" id="p-save" type="button">保存</button>
  `;
  if (actionsHost) actionsHost.appendChild(actions);
  else form.insertBefore(actions, form.querySelector("#p-notice"));

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

  actions.querySelector("#p-test").onclick = async () => {
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

  actions.querySelector("#p-save").onclick = async () => {
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
  setSettingsGazePaused(true);
  showSheet(async (sheet) => {
    // 后端要是暂时连不上，设置页也必须画出来：否则用户点"设置"只看到一片空白，
    // 连"外观"这种纯前端的开关都摸不到，也没法判断到底哪儿坏了。
    let data = null;
    try {
      data = await reloadCore();
    } catch (err) {
      setStatus(`设置没能读到后端数据：${err.message}`, true);
    }
    const active = state.providers.find((p) => p.id === state.activeProvider);

    sheet.innerHTML = `<h2>设置</h2>`;
    const intro = document.createElement("div");
    intro.className = "hint";
    intro.textContent = state.secrets
      ? `密钥存在：${state.secrets.backend || "未知"}${state.secrets.persistent ? "" : "（这次运行临时用）"}`
      : "";
    sheet.appendChild(intro);

    // 设置内容按使用场景分组，避免把所有选项堆成一条长页面。
    // 标签只是换视图，不会销毁面板里的控件，所以已有的事件和异步状态都能保留。
    const settingsTabs = document.createElement("div");
    settingsTabs.className = "settings-tabs";
    settingsTabs.setAttribute("role", "tablist");
    settingsTabs.setAttribute("aria-label", "设置分类");
    const settingsBody = document.createElement("div");
    settingsBody.className = "settings-body";
    const settingsGroups = [
      ["appearance", "外观与角色"],
      ["provider", "模型服务"],
      ["conversation", "对话与工具"],
      ["data", "数据管理"],
    ];
    const settingsPanels = new Map();
    const settingsTabButtons = new Map();
    for (const [id, label] of settingsGroups) {
      const tab = document.createElement("button");
      tab.className = "settings-tab";
      tab.type = "button";
      tab.id = `settings-tab-${id}`;
      tab.dataset.settingsTab = id;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", `settings-panel-${id}`);
      tab.setAttribute("aria-selected", "false");
      tab.tabIndex = -1;
      tab.textContent = label;
      settingsTabs.appendChild(tab);

      const panel = document.createElement("section");
      panel.className = "settings-panel";
      panel.id = `settings-panel-${id}`;
      panel.dataset.settingsPanel = id;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
      panel.hidden = true;
      settingsBody.appendChild(panel);
      settingsPanels.set(id, panel);
      settingsTabButtons.set(id, tab);
    }
    sheet.appendChild(settingsTabs);
    sheet.appendChild(settingsBody);

    const activateSettingsTab = (id, focus = false) => {
      for (const [tabId, tab] of settingsTabButtons) {
        const activeTab = tabId === id;
        tab.classList.toggle("on", activeTab);
        tab.setAttribute("aria-selected", activeTab ? "true" : "false");
        tab.tabIndex = activeTab ? 0 : -1;
        settingsPanels.get(tabId).hidden = !activeTab;
      }
      if (focus) settingsTabButtons.get(id)?.focus();
    };
    for (const [index, [id]] of settingsGroups.entries()) {
      const tab = settingsTabButtons.get(id);
      tab.onclick = () => activateSettingsTab(id);
      tab.onkeydown = (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0
          : event.key === "End" ? settingsGroups.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + settingsGroups.length) % settingsGroups.length;
        activateSettingsTab(settingsGroups[next][0], true);
      };
    }
    activateSettingsTab("appearance");

    const appearancePanel = settingsPanels.get("appearance");
    const providerPanel = settingsPanels.get("provider");
    const conversationPanel = settingsPanels.get("conversation");
    const dataPanel = settingsPanels.get("data");

    // 外观：跟随系统 / 浅色 / 深色，选完立刻写回 config.theme
    const look = document.createElement("div");
    look.className = "sect";
    look.innerHTML = `
      <div class="sect-title">外观</div>
      <div class="pref">
        <div class="pref-main">
          <div class="label">主题</div>
          <div class="desc" id="theme-desc"></div>
        </div>
        <div class="seg" role="radiogroup" aria-label="主题">
          <button class="seg-btn" data-theme-set="system" role="radio" aria-checked="false">跟随系统</button>
          <button class="seg-btn" data-theme-set="light" role="radio" aria-checked="false">浅色</button>
          <button class="seg-btn" data-theme-set="dark" role="radio" aria-checked="false">深色</button>
        </div>
      </div>`;
    for (const btn of look.querySelectorAll("[data-theme-set]")) {
      btn.onclick = () => applyTheme(btn.dataset.themeSet, { persist: true });
    }
    appearancePanel.appendChild(look);
    applyTheme(state.themeMode || state.cfg?.theme || "system");   // 把当前选中态刷到刚建好的开关上

    // 角色：显示的是模型目录名。模型是美术作品，版权与代码无关 ——
    // 用官方样例模型分发时，版权声明就该出现在用户看得到的地方。
    const modelName = (state.live2d && state.live2d.name) || "";
    const who = document.createElement("div");
    who.className = "sect";
    who.innerHTML = `
      <div class="sect-title">角色</div>
      <div class="pref">
        <div class="pref-main">
          <div class="label">${modelName ? escapeHtml(modelName) : "还没有模型"}</div>
          <div class="desc">${modelName
            ? "Live2D 模型，版权归模型作者"
            : "没有找到模型 —— 往 models/ 里放一个"}</div>
        </div>
      </div>
      <div class="pref">
        <div class="pref-main">
          <div class="label">脱离到桌面</div>
          <div class="desc" id="pet-detach-desc">${state.petDetached
            ? PET_DETACH_DESC_ON : PET_DETACH_DESC_OFF}</div>
        </div>
        <div class="switch ${state.petDetached ? "on" : ""}" id="pet-detach" role="switch"
             aria-checked="${state.petDetached}" aria-label="脱离到桌面" tabindex="0"></div>
      </div>
      <div class="field">
        <label>浮窗大小</label>
        <input type="range" id="pet-size" min="50" max="200" step="5" value="100" />
        <div class="hint" id="pet-size-value">—</div>
      </div>`;
    const detachToggle = who.querySelector("#pet-detach");
    // 开关的视觉同步（.on / aria-checked / 说明文字）全在 setPetDetached 里做 ——
    // 浮窗被右键菜单关掉时走的也是那条路，放这儿会漏。
    const flipDetach = async () => {
      await (state.petDetached ? dockPet() : detachPet());
    };
    detachToggle.onclick = flipDetach;
    detachToggle.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); flipDetach(); }
    };

    /* 浮窗大小：滑块给的是"相对默认尺寸的百分比"，真正的像素尺寸由主进程算
     * （长宽比锁死、中心不动、钳制在屏幕内），它返回的才算数 —— 按返回的刷回滑块，
     * 这样拖到超出工作区时会诚实地弹回实际值。浮窗没开也能调：存盘，下次脱离生效。 */
    const sizeInput = who.querySelector("#pet-size");
    const sizeHint = who.querySelector("#pet-size-value");
    const sizeBase = { width: 380, height: 680 };
    const sizeText = (k) => `${Math.round(sizeBase.width * k)} × ${Math.round(sizeBase.height * k)}`;
    sizeInput.oninput = () => { sizeHint.textContent = sizeText(Number(sizeInput.value) / 100); };
    sizeInput.onchange = async () => {
      let res = null;
      try { res = await window.yachiyoShell?.petResize?.(Number(sizeInput.value) / 100); } catch { res = null; }
      if (!res || !res.ok) { sizeHint.textContent = "改不了：主进程没响应"; return; }
      // 滑块停在用户选的那一档；提示按主进程**实际**给的尺寸写（真被钳制时这里就是真话）
      sizeHint.textContent = sizeText(res.bounds.width / sizeBase.width);
    };
    if (!(state.live2d && state.live2d.ready)) {
      // 没有模型就没有浮窗可调，别摆一个拖不动的滑块
      sizeInput.closest(".field").style.display = "none";
    } else {
      (async () => {
        let info = null;
        try { info = await window.yachiyoShell?.petSize?.(); } catch { info = null; }
        if (!info || !info.ok) {
          sizeInput.disabled = true;
          sizeHint.textContent = "这个版本的主进程不支持调大小";
          return;
        }
        sizeBase.width = info.base.width;
        sizeBase.height = info.base.height;
        /* 范围要**朝里取整到 step 的整数倍**：min=47 + step=5 这种组合下 100 不是合法档位，
           浏览器会把 value 悄悄吸到 102，提示里的像素尺寸就跟着错
           （实测 380×680 显示成 388×694）。取整之后每一档都合法，滑块和提示才对得上。 */
        const step = Number(sizeInput.step) || 5;
        const lo = Math.ceil((info.min * 100) / step) * step;
        const hi = Math.floor((info.max * 100) / step) * step;
        sizeInput.min = String(lo);
        sizeInput.max = String(hi);
        const raw = (info.bounds.width / info.base.width) * 100;
        const pct = Math.min(hi, Math.max(lo, Math.round(raw / step) * step));
        sizeInput.value = String(pct);
        sizeHint.textContent = sizeText(pct / 100) + (info.open ? "" : "（脱离后按这个开）");
      })();
    }
    appearancePanel.appendChild(who);

    // 当前用哪个 provider
    const list = document.createElement("div");
    list.className = "sect";
    list.innerHTML = `<div class="sect-title">模型服务</div><div class="chips"></div>`;
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
    providerPanel.appendChild(list);

    // 温度
    const temp = document.createElement("div");
    temp.className = "sect";
    temp.innerHTML = `<div class="sect-title">对话</div>
      <div class="field"><label>温度（越高越活泼，0.8 比较自然）</label>
      <input type="range" min="0" max="1.5" step="0.1" value="${state.cfg?.temperature ?? 0.8}" />
      <div class="hint" id="temp-value">${state.cfg?.temperature ?? 0.8}</div></div>`;
    const range = temp.querySelector("input");
    range.oninput = () => { temp.querySelector("#temp-value").textContent = range.value; };
    range.onchange = async () => {
      await api("/api/config", { method: "POST", body: { temperature: Number(range.value) } });
      state.cfg.temperature = Number(range.value);
    };
    conversationPanel.appendChild(temp);

    // 工具：档位 + 动手前确认 + 当前挂着哪些工具（目录从后端读，别在前端抄一份）
    const toolsSect = document.createElement("div");
    toolsSect.className = "sect";
    const toolCfg = state.cfg?.tools || {};
    toolsSect.innerHTML = `
      <div class="sect-title">工具</div>
      <div class="pref">
        <div class="pref-main">
          <div class="label">能力范围</div>
          <div class="desc" id="tool-profile-desc">—</div>
        </div>
      </div>
      <div class="seg" id="tool-profile" role="radiogroup" aria-label="工具档位">
        <button class="seg-btn" data-tool-profile="off" role="radio" aria-checked="false">关闭</button>
        <button class="seg-btn" data-tool-profile="safe" role="radio" aria-checked="false">安全</button>
        <button class="seg-btn" data-tool-profile="full" role="radio" aria-checked="false">完全</button>
      </div>
      <div class="pref">
        <div class="pref-main">
          <div class="label">动手前先问我</div>
          <div class="desc">写文件、改文件、执行命令这类操作，弹窗让你点头之后才做</div>
        </div>
        <div class="switch ${toolCfg.confirm === false ? "" : "on"}" id="tool-confirm" role="switch"
             aria-checked="${toolCfg.confirm === false ? "false" : "true"}"
             aria-label="动手前先问我" tabindex="0"></div>
      </div>
      <div class="tool-list" id="tool-list"><div class="hint">正在读工具列表…</div></div>`;
    conversationPanel.appendChild(toolsSect);

    const TOOL_PROFILE_DESC = {
      off: "八千代只会聊天，不碰你的电脑。",
      safe: "能联网搜索、看时间、翻记忆、截图、读剪贴板、读文件，也可按回复语气调整角色表情和动作；不会写入或修改文件。",
      full: "在上面那些之外，还能写文件、改文件、打开路径、执行命令（默认每次都要你点头）。",
    };
    let toolCatalog = [];
    const profileDesc = toolsSect.querySelector("#tool-profile-desc");
    const toolList = toolsSect.querySelector("#tool-list");

    /* 每次 POST 都从 state.cfg.tools 现读，只挑配置字段 ——
       /api/tools 的返回里还带着 catalog，整包塞回配置会被 pydantic 拒掉。 */
    const toolPayload = (patch) => {
      const cur = state.cfg?.tools || {};
      return {
        profile: cur.profile ?? "safe",
        allow: cur.allow ?? [],
        deny: cur.deny ?? [],
        confirm: cur.confirm !== false,
        ...patch,
      };
    };

    const paintTools = () => {
      const now = (state.cfg?.tools?.profile) || "safe";
      profileDesc.textContent = TOOL_PROFILE_DESC[now] || TOOL_PROFILE_DESC.safe;
      for (const btn of toolsSect.querySelectorAll("[data-tool-profile]")) {
        const on = btn.dataset.toolProfile === now;
        btn.classList.toggle("on", on);
        btn.setAttribute("aria-checked", on ? "true" : "false");
      }

      if (!toolCatalog.length) {
        toolList.innerHTML = `<div class="hint">读不到工具列表。</div>`;
        return;
      }
      // 分两栏列：安全的 / 要确认的。关闭档位时整列都标成灰的。
      const bucket = (safe) => toolCatalog.filter((t) => t.safe === safe);
      const row = (t) => {
        const need = t.needs_confirm ? `<span class="tag warn">要确认</span>` : "";
        return `<li class="${now === "off" ? "off" : ""}">
          <span class="n">${escapeHtml(t.label)}</span>
          <span class="d">${escapeHtml(t.description.split("。")[0])}</span>${need}</li>`;
      };
      const safeRows = bucket(true).map(row).join("");
      const riskyRows = bucket(false).map(row).join("");
      toolList.innerHTML = `
        <div class="tool-group">安全工具<span class="sub">${now === "off" ? "当前关闭" : "默认开启"}</span></div>
        <ul class="tool-items">${safeRows}</ul>
        <div class="tool-group">需要你同意的工具<span class="sub">${now === "full" ? "已开启" : "当前关闭"}</span></div>
        <ul class="tool-items">${riskyRows}</ul>`;
    };

    for (const btn of toolsSect.querySelectorAll("[data-tool-profile]")) {
      btn.onclick = async () => {
        const next = btn.dataset.toolProfile;
        try {
          const res = await api("/api/config", { method: "POST", body: { tools: toolPayload({ profile: next }) } });
          state.cfg.tools = res.tools;
        } catch { setStatus("改不了工具档位", true); return; }
        paintTools();
        setStatus(`工具档位：${next === "off" ? "关闭" : next === "safe" ? "安全" : "完全"}`);
      };
    }

    const confirmToggle = toolsSect.querySelector("#tool-confirm");
    const flipConfirm = async () => {
      const on = !confirmToggle.classList.contains("on");
      const flip = (state) => {
        confirmToggle.classList.toggle("on", state);
        confirmToggle.setAttribute("aria-checked", state ? "true" : "false");
      };
      flip(on);
      try {
        const res = await api("/api/config", { method: "POST", body: { tools: toolPayload({ confirm: on }) } });
        state.cfg.tools = res.tools;
      } catch {
        flip(!on);                      // 存不上就把开关拨回去，别骗用户
        setStatus("改不了这个开关", true);
      }
    };
    confirmToggle.onclick = flipConfirm;
    confirmToggle.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); flipConfirm(); }
    };

    (async () => {
      try {
        const data = await api("/api/tools");
        toolCatalog = data.catalog || [];
      } catch { toolCatalog = []; }
      paintTools();
    })();
    paintTools();

    // 换密钥 / 改配置
    const editTitle = document.createElement("div");
    editTitle.className = "sect-title";
    editTitle.textContent = active ? `修改「${active.display_name}」` : "还没有可用的服务，先在下面配一个";
    providerPanel.appendChild(editTitle);
    providerForm(providerPanel, {
      provider: active || null,
      onSaved: async (id, extra) => {
        await reloadCore();
        closeOverlay();
        ensurePanel();     // 引导被跳过 / 直接在这里配好的话，角色到这一步才放出来
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
    dataPanel.appendChild(danger);

    const dataNote = document.createElement("div");
    dataNote.className = "hint settings-data-note";
    dataNote.textContent = "对话记录保存在本机数据目录，删除后无法恢复。";
    dataPanel.insertBefore(dataNote, danger);

    const close = document.createElement("div");
    close.className = "row";
    close.innerHTML = `<button class="btn grow">关闭</button>`;
    close.querySelector("button").onclick = closeOverlay;
    sheet.appendChild(close);
  });
}

/* ── 第一次运行引导（OOBE） ─────────────────────────────────────────────
   照手机 / 电脑系统的开机引导搭：全屏玻璃卡、顶部品牌 + 步骤点、底部动作条、
   左右滑动切页、正文分层错峰入场。四步 —— 欢迎 / 隐私与许可 / 外观 / 连接。
   各步的 DOM 一次全建好，靠 .is-active 交叉滑动（不用 display:none），
   这样 #p-name 这些字段在哪一步都在，回上一步不用重建、探针也照样找得到。

   为什么要插一步「隐私与许可」：这个程序要发给别人用，得让人在配服务之前
   先看清密钥存哪、数据存哪、什么会发出去、包里带了谁的代码 —— 而且**必须勾同意
   才能往下走**（见 setup-next 的 disabled 逻辑）。它不是弹个好看的通知，
   是引导里唯一一条不能跳过的路。 */
const SETUP_STEPS = ["欢迎", "隐私与许可", "外观", "连接"];
// 「不勾同意就别想往下走」的那一步。文案和置灰逻辑都看这个下标，别在别处写死数字。
const SETUP_AGREE_STEP = 1;
// 版权署名。改这一处就够，许可页里那行是拼出来的。
const APP_COPYRIGHT = "© 2026 WatRain";
// 许可条款的版本号。条款有实质改动就 +1 —— 后端把它和同意时间一起写进
// config.json 的 consent，将来才能判断「他同意的是不是现在这一版」。
const LEGAL_VERSION = 1;
const SETUP_THEMES = [
  ["system", "跟随系统", "跟 Windows 的深色 / 浅色设置走"],
  ["light", "浅色", "白天亮堂一点"],
  ["dark", "深色", "夜里不刺眼"],
];

/** 把角色面板放出来。引导期间面板整个藏着（body.oobe），
 *  用户配好服务、或者从引导里退出去再配好，都得走这里把它放出来。 */
function ensurePanel() {
  if (state.panelBooted) return;
  if (!state.live2d || !state.live2d.ready) return;   // 模型不可用就别硬上
  state.panelBooted = true;
  document.body.classList.remove("oobe");
  bootPanel();
  if (state.cfg?.live2d_detached) detachPet({ quiet: true });
}

function openSetup() {
  showSheet(async (sheet) => {
    await reloadCore();
    sheet.classList.add("setup");
    sheet.innerHTML = `
      <div class="setup-top">
        <div class="setup-brand"><span class="setup-brand-text">YachiyoAgent</span></div>
        <div class="setup-dots" id="setup-dots" aria-hidden="true">
          ${SETUP_STEPS.map(() => `<span class="setup-dot"></span>`).join("")}
        </div>
      </div>
      <div class="setup-body">
        <section class="setup-pane" data-step="0">
          <h2 class="setup-hero">
            <span class="setup-hero-eyebrow">欢迎来到</span>
            <span class="setup-hero-word">YachiyoAgent</span>
          </h2>
          <div class="hint setup-hero-note">
            先花一分钟接上你自己的模型服务。<br />
            密钥直接存进 ${state.secrets?.backend || "系统凭据管理器"}，程序里不留明文，也不经过我们。
          </div>
          <div class="setup-points">
            <div class="setup-point"><span class="ic">🔒</span><div>
              <div class="t">密钥不进配置文件</div>
              <div class="d">存进系统凭据管理器，程序里不留明文。</div></div></div>
            <div class="setup-point"><span class="ic">🧩</span><div>
              <div class="t">用你自己的模型服务</div>
              <div class="d">OpenAI 兼容的端点都行，随时能在设置里换。</div></div></div>
            <div class="setup-point"><span class="ic">🎭</span><div>
              <div class="t">角色随包分发</div>
              <div class="d">Live2D 模型已经装好了，配完就能看到她动起来。</div></div></div>
          </div>
          <button class="setup-go" type="button" aria-label="开始设置">
            <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
              <path d="M5 12h13M12.5 5.5 19 12l-6.5 6.5" fill="none" stroke="currentColor"
                    stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </section>
        <!-- 隐私与许可。必须勾了下面那个框才能继续（见 paint() 里对 setup-next 的置灰）。
             这份文案是照着程序的实际行为写的，改代码时记得回头改这里：
             · 密钥去处   → core/secrets.py（Windows 凭据管理器，不进配置文件、不进日志）
             · 数据目录   → core/paths.py 的 data_dir()
             · 两条出境路 → 用户自己填的 base_url；app/assets/live2d/pet.html 里那个 Live2D CDN
             · 组件清单   → 仓库根的 THIRD_PARTY_NOTICES.md
             · 卸载行为   → desktop/package.json 的 nsis.deleteAppDataOnUninstall = false -->
        <section class="setup-pane" data-step="1">
          <h2 class="setup-title">隐私与许可</h2>
          <div class="hint">用之前，先花一分钟看完这三件事。</div>
          <div class="legal" id="setup-legal" tabindex="0">
            <section class="legal-sec">
              <h3>隐私政策</h3>
              <ul>
                <li><b>密钥存在哪</b>：API Key 只写进这台电脑的系统凭据管理器，不写进配置文件、不写进日志，程序里不留明文。在设置里删掉 provider 时，密钥一并删除。</li>
                <li><b>数据存在哪</b>：配置、对话记录、记忆都留在这台电脑的数据目录里（装好后是 <code>%APPDATA%\\YachiyoAgent\\data</code>）。没有账号，也没有我们的服务器 —— 这个程序不会连任何由我们控制的地址。</li>
                <li><b>什么会发出去</b>：一是你输入的内容，会直接发给你自己填的那个模型服务端点（设置里的「接口地址」），发给谁、发什么由你决定，请一并看那家的隐私政策；二是角色渲染引擎 Live2D Cubism Core，启动时从 Live2D 官方 CDN（<code>cubism.live2d.com</code>）取一次。</li>
                <li><b>不采集什么</b>：没有遥测、没有使用统计、没有崩溃上报、没有广告标识符。</li>
                <li><b>卸载</b>：卸载程序不会替你删数据目录和凭据管理器里的密钥。想彻底清干净，就自己删掉上面那个目录，再到「凭据管理器 → Windows 凭据」里删掉 <code>YachiyoAgent/</code> 开头的条目。</li>
              </ul>
            </section>
            <section class="legal-sec">
              <h3>第三方组件</h3>
              <ul class="legal-libs">
                <li><span class="n">Electron</span><span class="l">MIT</span></li>
                <li><span class="n">Inter</span><span class="l">OFL-1.1</span></li>
                <li><span class="n">PixiJS 8.13.1</span><span class="l">MIT</span></li>
                <li><span class="n">untitled-pixi-live2d-engine 1.4.0</span><span class="l">MIT</span></li>
                <li><span class="n">LiteLLM</span><span class="l">MIT</span></li>
                <li><span class="n">FastAPI</span><span class="l">MIT</span></li>
                <li><span class="n">uvicorn</span><span class="l">BSD-3</span></li>
                <li><span class="n">Starlette</span><span class="l">BSD-3</span></li>
                <li><span class="n">pydantic</span><span class="l">MIT</span></li>
                <li><span class="n">anyio</span><span class="l">MIT</span></li>
                <li><span class="n">websockets</span><span class="l">BSD-3</span></li>
                <li><span class="n">httpx</span><span class="l">BSD-3</span></li>
                <li><span class="n">aiosqlite</span><span class="l">MIT</span></li>
                <li><span class="n">Pillow</span><span class="l">MIT-CMU</span></li>
                <li><span class="n">certifi</span><span class="l">MPL-2.0</span></li>
                <li><span class="n">tiktoken</span><span class="l">MIT</span></li>
                <li><span class="n">python-dotenv</span><span class="l">BSD-3</span></li>
              </ul>
              <p>Live2D Cubism Core © Live2D Inc.，按 Live2D Proprietary Software License Agreement 使用（专有许可，<b>不是</b>开源）。它不在安装包里，页面运行时从 Live2D 官方 CDN 加载。把 Live2D 用作 AI / 聊天机器人的界面，还需要遵守 SDK Release License。</p>
              <p>完整的组件清单、版本号和许可全文见安装目录下的 <code>THIRD_PARTY_NOTICES.md</code>。</p>
            </section>
            <section class="legal-sec">
              <h3>版权声明</h3>
              <ul>
                <li>本程序的代码与界面 ${APP_COPYRIGHT}。</li>
                <li>角色模型「八千代辉夜姬」版权归 <b>雪熊企划</b> 所有。</li>
              </ul>
            </section>
          </div>
          <label class="setup-consent">
            <input type="checkbox" id="setup-agree" />
            <span>我已阅读并同意上面的隐私政策与许可条款</span>
          </label>
        </section>
        <section class="setup-pane" data-step="2">
          <h2 class="setup-title">挑一个外观</h2>
          <div class="hint">随时能在设置里改，这里先挑个顺眼的。</div>
          <div class="setup-themes" id="setup-themes"></div>
        </section>
        <section class="setup-pane" data-step="3">
          <h2 class="setup-title">连接模型服务</h2>
          <div class="hint">有预设就点一下芯片，地址和模型 ID 会自动填好。</div>
        </section>
      </div>
      <div class="setup-foot">
        <button class="btn ghost is-off" id="setup-back" type="button">上一步</button>
        <div class="fill"></div>
        <div class="setup-actions" id="setup-actions">
          <button class="btn primary" id="setup-next" type="button">开始</button>
        </div>
      </div>
    `;

    const lastStep = SETUP_STEPS.length - 1;
    const actionsHost = sheet.querySelector("#setup-actions");

    // 最后一步（连接）的正文就用设置页那份表单，动作行（测试 / 保存）挂到底部动作条上。
    // 下标走 lastStep，别写死 —— 以后再加步骤就不用回来改这里。
    providerForm(sheet.querySelector(`[data-step="${lastStep}"]`), {
      actionsHost,
      onSaved: async (id, extra) => {
        const data = await reloadCore();
        const ready = state.providers.find((p) => p.id === id);
        if (extra?.persisted === false) {
          setStatus("密钥只能用到本次退出（系统凭据库不可用）", true);
        }
        closeOverlay();
        ensurePanel();                       // 配好了，角色这才登场
        setStatus(`已经接上 ${ready?.display_name || id}，开始聊吧`);
      },
    });

    // 外观那一步：三张卡，点了立刻换（跟设置里那三选一同一个 applyTheme）
    const themeBox = sheet.querySelector("#setup-themes");
    const paintThemes = () => {
      for (const card of themeBox.querySelectorAll(".setup-theme")) {
        card.classList.toggle("on", card.dataset.theme === state.themeMode);
      }
    };
    for (const [mode, title, desc] of SETUP_THEMES) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "setup-theme";
      card.dataset.theme = mode;
      card.innerHTML = `
        <span class="swatch swatch-${mode}"><i></i><i></i><i></i></span>
        <span class="txt"><span class="t">${title}</span><span class="d">${desc}</span></span>
        <span class="tick">✓</span>`;
      card.onclick = () => {
        applyTheme(mode, { persist: true });
        paintThemes();
      };
      themeBox.appendChild(card);
    }
    paintThemes();

    // 翻页：只切 .is-active / .is-before，滑动和入场交给 CSS
    const panes = [...sheet.querySelectorAll(".setup-pane")];
    const dots = [...sheet.querySelectorAll(".setup-dot")];
    const backBtn = sheet.querySelector("#setup-back");
    const nextBtn = sheet.querySelector("#setup-next");
    const goBtn = sheet.querySelector(".setup-go");
    const agreeBox = sheet.querySelector("#setup-agree");
    const formActions = actionsHost.querySelector(".form-actions");
    let step = 0;

    // 许可页是引导里唯一一条"不同意就走不了"的路：没勾就把「同意并继续」置灰。
    // 用 disabled 而不是藏起来 —— 得让人看见这里要同意，否则只会觉得按钮坏了。
    const blocked = () => step === SETUP_AGREE_STEP && !agreeBox.checked;

    const paint = () => {
      // 第一步是"光板"欢迎页：CSS 靠这个属性把顶栏和底栏整条收掉
      sheet.dataset.step = String(step);
      panes.forEach((pane, i) => {
        pane.classList.toggle("is-active", i === step);
        pane.classList.toggle("is-before", i < step);
      });
      dots.forEach((dot, i) => dot.classList.toggle("on", i <= step));
      backBtn.classList.toggle("is-off", step === 0);
      nextBtn.classList.toggle("is-off", step === lastStep);
      if (formActions) formActions.classList.toggle("is-off", step !== lastStep);
      nextBtn.textContent = step === 0 ? "开始"
        : step === SETUP_AGREE_STEP ? "同意并继续" : "继续";
      nextBtn.disabled = blocked();
      panes[step].scrollTop = 0;
    };
    // 过许可页的那一刻往 config.json 记一笔「何时同意了哪一版」。
    // 只记一次；写失败不拦人 —— 同意是当场就生效的，这只是在留证据。
    let consentLogged = false;
    const logConsent = () => {
      if (consentLogged) return;
      consentLogged = true;
      api("/api/config", {
        method: "POST",
        body: { consent: { version: LEGAL_VERSION, at: new Date().toISOString() } },
      }).catch(() => { consentLogged = false; });
    };
    // 离开欢迎页那一下，从箭头的中心把下一页「揭开」（动画在 style.css 的 setup-reveal）。
    // 原点要分两次量：箭头的位置在切步骤**之前**量（切完它就跟旧页一起藏了），
    // 正文区的盒子在**之后**量 —— 顶栏底栏在步 0 是 display:none，一切到步 1
    // 正文区就矮一截、左上角也跟着下移，只量一次会整体错位。
    const revealFrom = (originX, originY) => {
      const sheetBox = sheet.getBoundingClientRect();
      const bodyBox = sheet.querySelector(".setup-body").getBoundingClientRect();
      const pane = panes[step];
      // 裁切坐标是相对被裁元素自己的边框盒，而正文区没有内边距、竖切面板是 inset:0，
      // 所以正文区的盒子就是面板的盒子，可以直接拿它换算。
      const x = originX - (bodyBox.left - sheetBox.left);
      const y = originY - (bodyBox.top - sheetBox.top);
      // 半径取正文区四角里离原点最远那个；多留 2px，免得动画演完撤掉裁切时四个角跳一下
      const r = Math.ceil(Math.max(
        Math.hypot(x, y), Math.hypot(bodyBox.width - x, y),
        Math.hypot(x, bodyBox.height - y), Math.hypot(bodyBox.width - x, bodyBox.height - y),
      )) + 2;
      sheet.style.setProperty("--rv-x", `${x}px`);
      sheet.style.setProperty("--rv-y", `${y}px`);
      sheet.style.setProperty("--rv-r", `${r}px`);
      sheet.classList.add("is-revealing");
      // 这一页的分层入场（setup-rise）要按住：两个动效叠在一起互相抵消 ——
      // 圆还没铺到，文字自己先淡进来了，看上去就不像「从箭头里长出来」的。
      // 按住之后圆内是已经完整的一页，只有裁切在动。
      // 这个类**不撤** —— 撤掉时动画名从 none 变回 setup-rise，会重放一次入场。
      pane.classList.add("no-rise");
      // 演完就撤：这层裁切只在这一次转场里有意义，留着会让后面几步的翻页也走这条路。
      // 再挂个定时器兜底 —— 万一 animationend 因为别的原因没来，卡着 .is-revealing
      // 会让引导一直停在裁切状态。
      const done = (ev) => {
        if (ev.target !== pane || ev.animationName !== "setup-reveal") return;
        pane.removeEventListener("animationend", done);
        sheet.classList.remove("is-revealing");
      };
      pane.addEventListener("animationend", done);
      setTimeout(() => sheet.classList.remove("is-revealing"), 1000);
    };

    const go = (next) => {
      // 往前走要过许可页；往回走永远放行，不然一勾错就卡死在那一页了
      if (blocked() && next > step) return;
      if (step === SETUP_AGREE_STEP && next > step) logConsent();
      // 只有「从欢迎页往前走」这一下扩散；后面几步是普通翻页，滑动更安静
      let origin = null;
      if (step === 0 && next > step) {
        const s = sheet.getBoundingClientRect();
        const g = goBtn.getBoundingClientRect();
        origin = [g.left + g.width / 2 - s.left, g.top + g.height / 2 - s.top];
      }
      step = Math.max(0, Math.min(lastStep, next));
      paint();
      if (origin) revealFrom(origin[0], origin[1]);
    };

    agreeBox.onchange = paint;
    goBtn.onclick = () => go(step + 1);          // 欢迎页那颗圆按钮
    backBtn.onclick = () => go(step - 1);
    nextBtn.onclick = () => go(step + 1);
    paint();
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
    setPanelStatus("后端没起来");
    return;
  }
  state.apiBase = info.apiBase;
  state.token = info.token;

  const data = await reloadCore();
  applyTheme(state.cfg?.theme || "system");   // 用户上次选的外观：system / light / dark
  renderHistory(data.conversation);
  connectWs();

  const active = state.providers.find((p) => p.id === data.active_provider);
  const ready = Boolean(active && data.active_has_key);
  if (!ready) {
    // 还没配好服务：这会儿不渲染 Live2D（引导页上也没地方放它，白拉一遍模型纯属浪费），
    // 面板整个藏起来，让引导卡独占窗口
    document.body.classList.add("oobe");
    setStatus("还没有接上模型服务，先完成设置");
    openSetup();
  } else {
    ensurePanel();                            // 里面会按 live2d_detached 恢复桌面浮窗
    setStatus(`已接上 ${active.display_name}`);
    setTimeout(() => setStatus(""), 2500);
  }
}

// 事件绑定
// 标题栏上的外观快捷开关（完整三选一在设置里）
for (const btn of document.querySelectorAll("#theme-quick [data-theme-set]")) {
  btn.onclick = () => applyTheme(btn.dataset.themeSet, { persist: true });
}
$("btn-min").onclick = () => window.yachiyoShell.minimize();
$("btn-close").onclick = () => window.yachiyoShell.close();
$("btn-settings").onclick = openSettings;
// 浮窗被关掉（右键菜单 / 主窗口关闭）→ 把开关和面板拨回来，状态栏也别再留着"她在桌面上"
window.yachiyoShell?.onPetClosed?.(() => {
  setPetDetached(false, "浮窗已关闭");
  setStatus("八千代回到面板里了");
});
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
// 滚动条只在真的在滑动时露出来：平静的时候别在右边挂一条（样式见 style.css）
markScrolling(ui.messages);
// 引导向导是**故意**模态的，点遮罩不放人：老版本这里允许点空白关掉引导，结果只关了浮层、
// body.oobe 还挂着 —— 人进了聊天界面，角色面板却永远不会出现（等于绕过了"没配好不放人进去"）。
// 现在只有配好 provider（onSaved → ensurePanel）才会放人。
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
