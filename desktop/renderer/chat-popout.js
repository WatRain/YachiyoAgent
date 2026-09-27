"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  messages: $("messages"),
  status: $("status"),
  input: $("input"),
  send: $("btn-send"),
  stop: $("btn-stop"),
  shell: $("input-shell"),
  dot: $("chat-dot"),
  toolAskOverlay: $("tool-ask-overlay"),
};

let busy = false;

function scrollToBottom() {
  requestAnimationFrame(() => { ui.messages.scrollTop = ui.messages.scrollHeight; });
}

function renderSnapshot(snapshot) {
  const entries = Array.isArray(snapshot.entries) ? snapshot.entries : [];
  ui.messages.innerHTML = entries.join("");
  scrollToBottom();
}

function renderEntry(entry) {
  const index = Number(entry.index);
  if (!Number.isInteger(index) || index < 0 || typeof entry.html !== "string") return;
  const template = document.createElement("template");
  template.innerHTML = entry.html;
  const node = template.content.firstElementChild;
  if (!node) return;
  const existing = ui.messages.children[index];
  if (existing) {
    for (const attr of Array.from(existing.attributes)) existing.removeAttribute(attr.name);
    for (const attr of Array.from(node.attributes)) existing.setAttribute(attr.name, attr.value);
    existing.innerHTML = node.innerHTML;
  }
  else if (index === ui.messages.children.length) ui.messages.appendChild(node);
  else {
    window.yachiyoShell?.petChatReady?.();
    return;
  }
  scrollToBottom();
}

function applyMeta(meta) {
  if (meta.theme === "light" || meta.theme === "dark") {
    document.documentElement.dataset.theme = meta.theme;
  }
  busy = Boolean(meta.busy);
  ui.dot.classList.toggle("busy", busy);
  ui.send.classList.toggle("hidden", busy);
  ui.stop.classList.toggle("hidden", !busy);
  ui.shell.style.borderColor = busy ? "var(--accent)" : "var(--glass-border)";
  ui.status.textContent = meta.status || "";
  ui.status.classList.toggle("is-empty", Boolean(meta.statusEmpty));
  ui.status.classList.toggle("error", Boolean(meta.isError));
}

function autoGrow() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 96)}px`;
}

function sendMessage() {
  const text = (ui.input.value || "").trim();
  if (!text || busy) return;
  window.yachiyoShell?.petChatSend?.(text);
  busy = true;
  ui.dot.classList.add("busy");
  ui.send.classList.add("hidden");
  ui.stop.classList.remove("hidden");
  ui.shell.style.borderColor = "var(--accent)";
  ui.input.value = "";
  autoGrow();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch { return false; }
  }
}

ui.messages.addEventListener("click", (event) => {
  const button = event.target.closest(".code-copy");
  if (!button) return;
  const code = button.closest(".code-block")?.querySelector("code");
  copyText(code?.textContent || "").then((ok) => {
    button.textContent = ok ? "已复制" : "复制失败";
    button.classList.toggle("bad", !ok);
    setTimeout(() => {
      button.textContent = "复制";
      button.classList.remove("bad");
    }, 1200);
  });
});

ui.send.onclick = sendMessage;
ui.stop.onclick = () => window.yachiyoShell?.petChatStop?.();
ui.input.addEventListener("input", autoGrow);
ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});
$("btn-collapse-chat").onclick = () => window.yachiyoShell?.petChatCollapse?.();

function showToolAsk(message) {
  const id = String(message.id || "");
  if (!id) return;
  const overlay = ui.toolAskOverlay;
  overlay.replaceChildren();
  overlay.classList.remove("hidden", "is-closing");
  overlay.setAttribute("aria-hidden", "false");

  const sheet = document.createElement("div");
  sheet.className = "sheet tool-ask";
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  const title = document.createElement("h2");
  title.textContent = `可以${String(message.label || message.name || "工具")}吗？`;
  sheet.appendChild(title);

  const note = document.createElement("div");
  note.className = "hint";
  note.textContent = "八千代想在你电脑上做这件事，需要你点一下头。";
  sheet.appendChild(note);

  const preview = document.createElement("div");
  preview.className = "tool-ask-preview";
  const code = document.createElement("code");
  code.textContent = String(message.preview || message.name || "");
  preview.appendChild(code);
  sheet.appendChild(preview);

  const actions = document.createElement("div");
  actions.className = "tool-ask-actions";
  const decide = (ok) => {
    overlay.classList.add("hidden");
    overlay.setAttribute("aria-hidden", "true");
    overlay.replaceChildren();
    window.yachiyoShell?.petChatToolDecision?.(id, ok);
  };
  const no = document.createElement("button");
  no.className = "btn ghost grow";
  no.type = "button";
  no.textContent = "拒绝";
  no.onclick = () => decide(false);
  const yes = document.createElement("button");
  yes.className = "btn primary grow";
  yes.type = "button";
  yes.textContent = "允许";
  yes.onclick = () => decide(true);
  actions.append(no, yes);
  sheet.appendChild(actions);
  overlay.appendChild(sheet);
  setTimeout(() => no.focus(), 30);
}

window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || ui.toolAskOverlay.classList.contains("hidden")) return;
  event.preventDefault();
  ui.toolAskOverlay.querySelector(".btn.ghost")?.click();
});

window.yachiyoShell?.onPetChatSnapshot?.(renderSnapshot);
window.yachiyoShell?.onPetChatEntry?.(renderEntry);
window.yachiyoShell?.onPetChatMeta?.(applyMeta);
window.yachiyoShell?.onPetChatToolAsk?.(showToolAsk);
window.yachiyoShell?.petChatReady?.();
