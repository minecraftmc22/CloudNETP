/**
 * 看板娘对话面板
 *
 * - 点看板娘悬停条上的「对话」打开，右下角浮层
 * - 流式输出（SSE）：正文逐字出、思考过程单独一块、工具调用显示成小胶囊
 * - 只有配置里打开「图片输入」时才显示发图按钮
 * - 没配置时给一条「去设置」的引导，而不是让人对着输入框发呆
 */

import { escapeHtml } from "../api.js";
import { renderMarkdown } from "./markdown.js";
import { Toast } from "./toast.js";

const GREETING = "我是 CDP娘，住在你的云盘里～ 想找文件、看看还剩多少空间，直接跟我说。";
const SUGGESTIONS = ["我最近上传了什么？", "容量还剩多少？", "帮我找个文件"];
const HISTORY_LIMIT = 20;
const MAX_IMAGES = 3;
const MAX_IMAGE_BYTES = 12 * 1024 * 1024;
const IMAGE_MAX_SIDE = 1024;

const state = {
  open: false,
  busy: false,
  ready: false,
  vision: false,
  model: "",
  messages: [], // {role, content, images?}
  images: [], // 待发送的 data URL
  ctrl: null,
  panel: null,
  feed: null,
  input: null,
  sendBtn: null,
  modelEl: null,
  banner: null,
  attachEl: null,
  imageBtn: null,
  fileInput: null,
  greeting: true,
};

let configTimer = 0;

/* ------------------------------------------------------------------ */
/* 构建                                                               */
/* ------------------------------------------------------------------ */
function ensurePanel() {
  if (state.panel) return state.panel;

  const panel = document.createElement("section");
  panel.className = "mchat";
  panel.id = "mascot-chat";
  panel.setAttribute("aria-label", "和 CDP娘 对话");
  panel.innerHTML = `
    <header class="mchat__head">
      <span class="mchat__avatar"><img src="/assets/brand-avatar.png" alt=""></span>
      <div class="mchat__who">
        <b>CDP娘</b>
        <span data-mchat-model>未配置</span>
      </div>
      <button class="icon-btn mchat__icon" type="button" data-mchat-new title="新对话" aria-label="新对话">
        <svg class="icon icon--s"><use href="#i-plus"/></svg>
      </button>
      <button class="icon-btn mchat__icon" type="button" data-mchat-close title="收起" aria-label="收起">
        <svg class="icon icon--s"><use href="#i-close"/></svg>
      </button>
    </header>
    <div class="mchat__banner" data-mchat-banner hidden>
      <span data-mchat-banner-text>还没有配置 AI 接口</span>
      <button class="btn btn--subtle btn--sm" type="button" data-mchat-settings>去设置</button>
    </div>
    <div class="mchat__feed" data-mchat-feed></div>
    <form class="mchat__compose" data-mchat-form>
      <div class="mchat__attach" data-mchat-attach hidden></div>
      <textarea class="mchat__input" data-mchat-input rows="1" maxlength="4000"
                placeholder="和 CDP娘 说点什么…"></textarea>
      <div class="mchat__foot">
        <button class="mchat__mini" type="button" data-mchat-image title="发送图片">
          <svg class="icon icon--s"><use href="#i-image"/></svg>
        </button>
        <input type="file" accept="image/*" hidden data-mchat-file>
        <span class="mchat__tip">Enter 发送 · Shift+Enter 换行</span>
        <button class="btn btn--primary btn--sm mchat__send" type="submit" data-mchat-send>
          <svg class="icon icon--s"><use href="#i-send"/></svg><span>发送</span>
        </button>
      </div>
    </form>
  `;
  document.body.appendChild(panel);

  state.panel = panel;
  state.feed = panel.querySelector("[data-mchat-feed]");
  state.input = panel.querySelector("[data-mchat-input]");
  state.sendBtn = panel.querySelector("[data-mchat-send]");
  state.modelEl = panel.querySelector("[data-mchat-model]");
  state.banner = panel.querySelector("[data-mchat-banner]");
  state.attachEl = panel.querySelector("[data-mchat-attach]");
  state.imageBtn = panel.querySelector("[data-mchat-image]");
  state.fileInput = panel.querySelector("[data-mchat-file]");

  panel.querySelector("[data-mchat-close]").addEventListener("click", closeMascotChat);
  panel.querySelector("[data-mchat-new]").addEventListener("click", () => {
    resetChat();
    Toast.info("开了一段新对话");
  });
  panel.querySelector("[data-mchat-settings]").addEventListener("click", () => {
    location.hash = "#/settings";
    closeMascotChat();
  });
  state.imageBtn.addEventListener("click", () => state.fileInput.click());
  state.fileInput.addEventListener("change", () => {
    const file = state.fileInput.files?.[0];
    state.fileInput.value = "";
    if (file) attachImage(file);
  });

  panel.querySelector("[data-mchat-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy) stopStream();
    else send(state.input.value);
  });

  state.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!state.busy) send(state.input.value);
    }
  });
  state.input.addEventListener("input", autoGrow);
  state.input.addEventListener("focus", () => refreshConfig());

  state.feed.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-say]");
    if (chip) {
      send(chip.dataset.say);
      return;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.open) closeMascotChat();
  });

  return panel;
}

function autoGrow() {
  const input = state.input;
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(140, input.scrollHeight)}px`;
}

/* ------------------------------------------------------------------ */
/* 打开 / 关闭                                                        */
/* ------------------------------------------------------------------ */
export function isMascotChatOpen() {
  return state.open;
}

export function openMascotChat() {
  const panel = ensurePanel();
  panel.classList.add("is-open");
  state.open = true;
  refreshConfig(true);
  if (state.greeting) renderGreeting();
  window.setTimeout(() => state.input?.focus(), 60);
  scrollFeed(true);
}

export function closeMascotChat() {
  if (!state.panel) return;
  state.panel.classList.remove("is-open");
  state.open = false;
  if (state.busy) stopStream();
}

export function toggleMascotChat() {
  if (state.open) closeMascotChat();
  else openMascotChat();
}

/* ------------------------------------------------------------------ */
/* 配置                                                               */
/* ------------------------------------------------------------------ */
async function refreshConfig(force = false) {
  if (!force && configTimer && Date.now() - configTimer < 20000) return;
  configTimer = Date.now();
  try {
    const res = await fetch("/api/ai/config", { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    const cfg = data.config || {};
    state.ready = Boolean(cfg.ready);
    state.vision = Boolean(cfg.vision);
    state.model = cfg.model || "";
    if (state.modelEl) {
      state.modelEl.textContent = state.ready ? state.model : cfg.hasKey ? "还没选模型" : "未配置";
    }
    if (state.imageBtn) state.imageBtn.hidden = !state.vision;
    if (state.banner) {
      state.banner.hidden = state.ready;
      if (!state.ready) {
        const text = state.banner.querySelector("[data-mchat-banner-text]");
        if (text) {
          text.textContent = cfg.baseUrl
            ? "还差一步：填好密钥和模型就能聊了"
            : "先去填一下 API 地址和密钥吧";
        }
      }
    }
  } catch {
    /* 配置拿不到就按未配置处理，不影响面板本身 */
  }
}

/* ------------------------------------------------------------------ */
/* 消息渲染                                                           */
/* ------------------------------------------------------------------ */
function resetChat() {
  state.messages = [];
  state.images = [];
  state.greeting = true;
  state.feed.innerHTML = "";
  renderAttach();
  renderGreeting();
  autoGrow();
}

function renderGreeting() {
  state.greeting = false;
  const wrap = document.createElement("div");
  wrap.className = "mchat__hello";
  wrap.innerHTML = `
    <p>${escapeHtml(GREETING)}</p>
    <div class="mchat__chips">
      ${SUGGESTIONS.map((text) => `<button type="button" class="mchat__chip" data-say="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("")}
    </div>`;
  state.feed.appendChild(wrap);
}

function timeNow() {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

function appendUser(text, images) {
  state.feed.querySelector(".mchat__hello")?.remove();
  const row = document.createElement("div");
  row.className = "mchat__msg is-me";
  const thumbs = (images || [])
    .map((url) => `<img class="mchat__thumb" src="${url}" alt="附件">`)
    .join("");
  row.innerHTML = `
    ${thumbs ? `<div class="mchat__thumbs">${thumbs}</div>` : ""}
    ${text ? `<div class="mchat__bubble">${escapeHtml(text)}</div>` : ""}
    <time class="mchat__time">${timeNow()}</time>`;
  state.feed.appendChild(row);
  scrollFeed();
}

function appendBot() {
  state.feed.querySelector(".mchat__hello")?.remove();
  const row = document.createElement("div");
  row.className = "mchat__msg is-bot";
  row.innerHTML = `
    <details class="mchat__think" hidden>
      <summary class="mchat__think-head">思考过程</summary>
      <div class="mchat__think-body"></div>
    </details>
    <div class="mchat__tools"></div>
    <div class="mchat__bubble mchat__bubble--bot is-streaming"></div>`;
  state.feed.appendChild(row);
  scrollFeed();
  return {
    row,
    think: row.querySelector(".mchat__think"),
    thinkBody: row.querySelector(".mchat__think-body"),
    tools: row.querySelector(".mchat__tools"),
    bubble: row.querySelector(".mchat__bubble"),
  };
}

function appendNotice(text, tone = "info") {
  const row = document.createElement("div");
  row.className = `mchat__notice is-${tone}`;
  row.textContent = text;
  state.feed.appendChild(row);
  scrollFeed();
}

function scrollFeed(force = false) {
  const feed = state.feed;
  if (!feed) return;
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 140;
  if (force || nearBottom) feed.scrollTop = feed.scrollHeight;
}

/* 思考 / 工具 块的增量更新 */
let renderTimer = 0;
function scheduleRender(target, text) {
  if (renderTimer) return;
  renderTimer = window.setTimeout(() => {
    renderTimer = 0;
    target.bubble.innerHTML = renderMarkdown(text) || "";
    scrollFeed();
  }, 90);
}

function toolEntry(container, data) {
  let node = container.querySelector(`[data-tool="${CSS.escape(data.name || "tool")}"]`);
  if (!node) {
    node = document.createElement("details");
    node.className = "mchat__tool";
    node.dataset.tool = data.name || "tool";
    node.innerHTML = `<summary><span class="mchat__dot"></span><span data-tool-label></span></summary><pre data-tool-result></pre>`;
    container.appendChild(node);
  }
  const label = node.querySelector("[data-tool-label]");
  const pre = node.querySelector("[data-tool-result]");
  if (data.status === "running") {
    node.classList.add("is-running");
    label.textContent = `正在${data.label || "处理"}…`;
  } else {
    node.classList.remove("is-running");
    label.textContent = `已${data.label || "完成"}`;
    if (data.result) pre.textContent = data.result;
  }
  return node;
}

/* ------------------------------------------------------------------ */
/* 图片                                                               */
/* ------------------------------------------------------------------ */
function renderAttach() {
  if (!state.attachEl) return;
  state.attachEl.hidden = state.images.length === 0;
  state.attachEl.innerHTML = state.images
    .map(
      (url, index) => `<span class="mchat__attach-item">
        <img src="${url}" alt="">
        <button type="button" data-drop="${index}" aria-label="移除">
          <svg class="icon icon--s"><use href="#i-close"/></svg>
        </button>
      </span>`
    )
    .join("");
}

async function attachImage(file) {
  if (!state.vision) {
    Toast.warn("这个模型没开「图片输入」，去设置里打开再试");
    return;
  }
  if (state.images.length >= MAX_IMAGES) {
    Toast.warn(`一次最多带 ${MAX_IMAGES} 张图`);
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    Toast.warn("图片太大了（超过 12MB）");
    return;
  }
  try {
    const url = await shrinkImage(file);
    state.images.push(url);
    renderAttach();
    state.attachEl.querySelectorAll("[data-drop]").forEach((button) => {
      button.addEventListener("click", () => {
        state.images.splice(Number(button.dataset.drop), 1);
        renderAttach();
      });
    });
  } catch {
    Toast.error("这张图读不出来，换一张试试");
  }
}

/** 压到长边 1024 的 jpeg，既省 token 又避免超出各家 payload 限制 */
function shrinkImage(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      const long = Math.max(img.width, img.height) || 1;
      const scale = Math.min(1, IMAGE_MAX_SIDE / long);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(img.width * scale));
      canvas.height = Math.max(1, Math.round(img.height * scale));
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      resolve(canvas.toDataURL("image/jpeg", 0.82));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("decode failed"));
    };
    img.src = url;
  });
}

/* ------------------------------------------------------------------ */
/* 发送                                                               */
/* ------------------------------------------------------------------ */
function historyPayload() {
  const tail = state.messages.slice(-HISTORY_LIMIT);
  const lastUserIndex = tail.map((m) => m.role).lastIndexOf("user");
  return tail.map((message, index) => ({
    role: message.role,
    content: message.content,
    // 历史图片不再重发（既贵又慢），只带最新那条
    images: index === lastUserIndex ? message.images || [] : [],
  }));
}

async function send(text) {
  if (state.busy) return;
  const content = String(text || "").trim();
  const images = state.images.slice();
  if (!content && !images.length) return;

  if (!state.ready) {
    await refreshConfig(true);
    if (!state.ready) {
      appendNotice("还没配置 AI 接口，去「设置 → 看板娘 → 对话设置」填一下吧", "warn");
      if (state.banner) state.banner.hidden = false;
      return;
    }
  }

  state.input.value = "";
  autoGrow();
  state.images = [];
  renderAttach();

  state.messages.push({ role: "user", content, images });
  appendUser(content, images);

  await streamReply();
}

async function streamReply() {
  const target = appendBot();
  const ctrl = new AbortController();
  state.ctrl = ctrl;
  setBusy(true);

  let text = "";
  let thinking = "";
  let serverError = "";

  try {
    const res = await fetch("/api/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      credentials: "same-origin",
      body: JSON.stringify({ messages: historyPayload() }),
      signal: ctrl.signal,
    });

    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("session:expired"));
      throw new Error("登录状态已失效");
    }
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      const detail = typeof data?.detail === "string" ? data.detail : `请求失败（${res.status}）`;
      throw new Error(detail);
    }

    await readSSE(res, (event, data) => {
      if (event === "delta") {
        text += data.text || "";
        scheduleRender(target, text);
        return;
      }
      if (event === "think") {
        thinking += data.text || "";
        target.think.hidden = false;
        target.think.open = true;
        target.thinkBody.textContent = thinking;
        scrollFeed();
        return;
      }
      if (event === "tool") {
        toolEntry(target.tools, data);
        scrollFeed();
        return;
      }
      if (event === "meta") {
        if (state.modelEl && data.model) state.modelEl.textContent = data.model;
        return;
      }
      if (event === "error") {
        serverError = data.message || "接口返回错误";
      }
    });
  } catch (err) {
    if (err.name === "AbortError") {
      appendNotice("已停止", "muted");
    } else {
      serverError = err.message || "对话失败";
    }
  } finally {
    window.clearTimeout(renderTimer);
    renderTimer = 0;
    target.bubble.classList.remove("is-streaming");
    target.bubble.innerHTML = renderMarkdown(text) || (serverError ? "" : "<p>……</p>");
    if (target.row.querySelector(".mchat__think-body")?.textContent) {
      target.think.open = false;
    }
    setBusy(false);
    state.ctrl = null;

    if (serverError) {
      appendNotice(serverError, "warn");
      if (text) state.messages.push({ role: "assistant", content: text });
    } else if (text) {
      state.messages.push({ role: "assistant", content: text });
    }
    scrollFeed();
  }
}

function stopStream() {
  state.ctrl?.abort();
  state.ctrl = null;
  setBusy(false);
}

function setBusy(busy) {
  state.busy = busy;
  if (!state.sendBtn) return;
  state.sendBtn.classList.toggle("is-stop", busy);
  state.sendBtn.querySelector("span").textContent = busy ? "停止" : "发送";
  state.sendBtn.querySelector("use").setAttribute("href", busy ? "#i-close" : "#i-send");
}

async function readSSE(res, onEvent) {
  const reader = res.body?.getReader();
  if (!reader) throw new Error("浏览器不支持流式读取");
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      const dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        try {
          onEvent(event, JSON.parse(dataLines.join("\n")));
        } catch {
          /* 单个事件坏了不影响后面的 */
        }
      }
      cut = buffer.indexOf("\n\n");
    }
  }
}
