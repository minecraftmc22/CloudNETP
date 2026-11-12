/**
 * 看板娘对话面板
 *
 * - 点看板娘悬停条上的「对话」打开，右下角浮层
 * - 流式输出（SSE）：正文逐字出、思考过程单独一块、工具调用显示成小胶囊
 * - 只有配置里打开「图片输入」时才显示发图按钮
 * - 没配置时给一条「去设置」的引导，而不是让人对着输入框发呆
 */

import { escapeHtml } from "../api.js";
import { H, getLang, t } from "../i18n.js";
import { renderMarkdown } from "./markdown.js";
import { Toast } from "./toast.js";

const GREETING = "嘀嘀——系统就绪！我是CDP娘，你的专属数据管家～ 管理员，今天有什么数据需要我帮你守护呀？";
const SUGGESTIONS = ["我最近上传了什么？", "容量还剩多少？", "帮我看看文件夹乱不乱"];
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
  messages: [], // {uid, role, content, images?, el?, errEl?, failed?}
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
  editBar: null,
  usageEl: null,
  // token 消耗：turn = 当前这一问，session = 本次对话累计（新对话清零）
  usage: {
    show: false, // 设置里开着 + 不是走管理员默认接口
    available: false, // 服务端 meta 说本轮统计不统计
    live: false, // 还在流式输出（数字是估算的）
    turn: { input: 0, output: 0 },
    session: { input: 0, output: 0 },
  },
  greeting: true,
  editing: null, // {uid} 正在编辑哪条已发送的消息
};

// 每条消息一个稳定 id：删除 / 编辑之后数组下标会变，靠它定位
let uidSeq = 0;

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
  panel.innerHTML = H`
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
      <div class="mchat__editing" data-mchat-editing hidden>
        <span>${t("正在编辑已发送的消息")}</span>
        <button type="button" data-mchat-edit-cancel>${t("取消")}</button>
      </div>
      <div class="mchat__attach" data-mchat-attach hidden></div>
      <textarea class="mchat__input" data-mchat-input rows="1" maxlength="4000"
                placeholder="和 CDP娘 说点什么…"></textarea>
      <div class="mchat__foot">
        <button class="mchat__mini" type="button" data-mchat-image title="发送图片">
          <svg class="icon icon--s"><use href="#i-image"/></svg>
        </button>
        <input type="file" accept="image/*" hidden data-mchat-file>
        <span class="mchat__tip">Enter 发送 · Shift+Enter 换行</span>
        <span class="mchat__usage" data-mchat-usage hidden></span>
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
  state.editBar = panel.querySelector("[data-mchat-editing]");
  state.usageEl = panel.querySelector("[data-mchat-usage]");

  panel.querySelector("[data-mchat-edit-cancel]").addEventListener("click", cancelEdit);

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
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const item = itemOf(button);
    if (!item) return;
    const act = button.dataset.act;
    if (act === "retry") retry(item);
    else if (act === "edit") startEdit(item);
    else if (act === "del") confirmRemove(button, item);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.open) closeMascotChat();
  });

  // 看板娘被拖动 / 窗口变化时，聊天框跟着她走
  window.addEventListener("mascot:move", followMascot);
  window.addEventListener("mascot:resize", followMascot);
  window.addEventListener("resize", followMascot);
  // 设置页刚改完 AI 配置（显示 token / 图片输入）时立刻生效，不用等下一次轮询
  window.addEventListener("ai:config-changed", () => refreshConfig(true));

  return panel;
}

/**
 * 把聊天框贴到看板娘旁边：默认在她左边、底边对齐；
 * 左边放不下就翻到右边，再放不下就贴屏幕边（始终留在视口里）。
 */
function followMascot() {
  const panel = state.panel;
  const mascot = document.getElementById("mascot");
  if (!panel || !mascot || !state.open) return;

  // 手机上聊天框是整屏抽屉，位置交给 CSS
  if (window.innerWidth <= 768) {
    panel.style.left = "";
    panel.style.top = "";
    panel.style.right = "";
    panel.style.bottom = "";
    return;
  }

  const rect = mascot.getBoundingClientRect();
  const width = panel.offsetWidth || 374;
  const height = panel.offsetHeight || 520;
  const gap = 14;
  const margin = 12;
  const topLimit = 64;

  let left = rect.left - width - gap;
  if (left < margin) {
    const rightSide = rect.right + gap;
    left = rightSide + width + margin <= window.innerWidth ? rightSide : window.innerWidth - width - margin;
  }
  left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));

  let top = rect.bottom - height;
  top = Math.max(topLimit, Math.min(top, window.innerHeight - height - margin));

  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
  panel.style.right = "auto";
  panel.style.bottom = "auto";
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

/** 应用启动时先拉一次配置：管理员关掉 AI 时，操作条上的「对话」按钮立即收起 */
export function primeChatConfig() {
  refreshConfig(true);
}

export function openMascotChat() {
  const panel = ensurePanel();
  panel.classList.add("is-open");
  state.open = true;
  followMascot();
  window.requestAnimationFrame(followMascot);
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
    const aiOn = cfg.aiEnabled !== false;
    // 总开关关掉时把操作条上的「对话」按钮一起收起来（CSS 按这个属性隐藏）
    document.documentElement.dataset.ai = aiOn ? "on" : "off";
    state.ready = Boolean(cfg.ready);
    state.vision = aiOn && Boolean(cfg.vision);
    // 用默认接口时用户看不到地址，但模型名可以显示（来自 defaultModel）
    state.model = cfg.model || cfg.defaultModel || "";
    if (state.modelEl) {
      state.modelEl.textContent = !aiOn
        ? "已关闭"
        : state.ready
          ? state.model || "默认接口"
          : cfg.hasKey
            ? "还没选模型"
            : "未配置";
    }
    if (state.imageBtn) state.imageBtn.hidden = !state.vision;
    // 走管理员默认接口时账单不归用户，服务端也不下发用量，这里一并收起来
    state.usage.show = aiOn && cfg.source !== "default" && cfg.showTokens !== false;
    paintUsage();
    if (state.banner) {
      state.banner.hidden = state.ready;
      if (!state.ready) {
        const text = state.banner.querySelector("[data-mchat-banner-text]");
        if (text) {
          text.textContent = !aiOn
            ? "管理员已关闭 AI 功能"
            : cfg.source === "default"
              ? "默认接口还没配完整，联系管理员看看"
              : cfg.baseUrl
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
  state.usage.turn = { input: 0, output: 0 };
  state.usage.session = { input: 0, output: 0 };
  state.usage.live = false;
  paintUsage();
  clearEdit();
  renderAttach();
  renderGreeting();
  autoGrow();
}

/** 从被点的按钮找到它所在的消息对象（DOM 上只存 uid） */
function itemOf(node) {
  const uid = Number(node.closest(".mchat__msg")?.dataset.uid ?? -1);
  return state.messages.find((m) => m.uid === uid) || null;
}

function renderGreeting() {
  state.greeting = false;
  const wrap = document.createElement("div");
  wrap.className = "mchat__hello";
  wrap.innerHTML = H`
    <p>${escapeHtml(GREETING)}</p>
    <div class="mchat__chips">
      ${SUGGESTIONS.map((text) => H`<button type="button" class="mchat__chip" data-say="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("")}
    </div>`;
  state.feed.appendChild(wrap);
}

function timeNow() {
  const now = new Date();
  return H`${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ */
/* Token 消耗                                                          */
/* ------------------------------------------------------------------ */
const CJK_RE = /[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/g;

/** 估算 token：中日韩一个字算一个，其余按 4 字符一个。只用于流式过程中先给个大概 */
function estimateTokens(text) {
  const raw = String(text || "");
  if (!raw) return 0;
  const cjk = (raw.match(CJK_RE) || []).length;
  return Math.round(cjk + (raw.length - cjk) / 4);
}

function fmtTokens(value) {
  const n = Math.max(0, Math.round(Number(value) || 0));
  if (n >= 1000000) return `${(n / 1000000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 10000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

/** 底部那一行：本轮 ↑输入 ↓输出；服务端给了准确用量就把 ≈ 摘掉 */
function paintUsage() {
  const el = state.usageEl;
  if (!el) return;
  const { show, available, live, turn, session } = state.usage;
  const hasTurn = turn.input > 0 || turn.output > 0;
  const hasSession = session.input > 0 || session.output > 0;

  if (!show || !available || (!hasTurn && !hasSession)) {
    el.hidden = true;
    el.textContent = "";
    el.removeAttribute("title");
    return;
  }

  el.hidden = false;
  // 用默认接口时不上报用量；这里再兜一次，避免刚开面板那一瞬闪出旧数字
  const body = t("输入 {i} · 输出 {o}", {
    i: fmtTokens(turn.input),
    o: fmtTokens(turn.output),
  });
  el.textContent = live ? `≈ ${body}` : body;
  el.title = t("本轮 输入 {i} / 输出 {o}；本次对话累计 输入 {si} / 输出 {so}", {
    i: turn.input,
    o: turn.output,
    si: session.input,
    so: session.output,
  });
}

/** 一问一答的开始：这一轮的数字从「估算的输入 + 0 输出」起跳 */
function startUsageTurn(promptText) {
  state.usage.turn = { input: estimateTokens(promptText), output: 0 };
  state.usage.available = true;
  state.usage.live = true;
  paintUsage();
}

/** 服务端给的 done.usage 落地：覆盖本轮的估算值，并累进会话 */
function settleUsage(usage) {
  // 服务商不给用量：数字保留估算值，≈ 也别摘掉，免得把概数当成准数
  if (!usage) {
    paintUsage();
    return;
  }
  const input = Math.max(0, Math.round(Number(usage.input) || 0));
  const output = Math.max(0, Math.round(Number(usage.output) || 0));
  state.usage.live = false;
  state.usage.turn = { input, output };
  state.usage.session = {
    input: state.usage.session.input + input,
    output: state.usage.session.output + output,
  };
  state.usage.available = true;
  paintUsage();
}

function appendUser(item) {
  state.feed.querySelector(".mchat__hello")?.remove();
  const row = document.createElement("div");
  row.className = "mchat__msg is-me";
  row.dataset.uid = String(item.uid);
  const thumbs = (item.images || [])
    .map((url) => H`<img class="mchat__thumb" src="${url}" alt="附件">`)
    .join("");
  row.innerHTML = H`
    <div class="mchat__line">
      <button class="mchat__retry" type="button" data-act="retry" aria-label="${t("重新发送")}" hidden>
        <svg class="icon icon--s"><use href="#i-refresh"/></svg>
      </button>
      <div class="mchat__col">
        ${thumbs ? `<div class="mchat__thumbs">${thumbs}</div>` : ""}
        ${item.content ? `<div class="mchat__bubble">${escapeHtml(item.content)}</div>` : ""}
      </div>
    </div>
    <time class="mchat__time">${timeNow()}</time>
    <div class="mchat__ops">
      <button class="mchat__op" type="button" data-act="edit">${t("编辑")}</button>
      <button class="mchat__op" type="button" data-act="del">${t("删除")}</button>
    </div>`;
  state.feed.appendChild(row);
  scrollFeed();
  return row;
}

function appendBot(item) {
  state.feed.querySelector(".mchat__hello")?.remove();
  const row = document.createElement("div");
  row.className = "mchat__msg is-bot";
  row.dataset.uid = String(item.uid);
  row.innerHTML = H`
    <details class="mchat__think" hidden>
      <summary class="mchat__think-head">${t("思考过程")}</summary>
      <div class="mchat__think-body"></div>
    </details>
    <div class="mchat__bubble mchat__bubble--bot is-streaming"></div>
    <details class="mchat__tools" hidden>
      <summary class="mchat__tools-head">
        <svg class="icon"><use href="#i-bolt"/></svg>
        <span>${t("使用工具")}</span>
        <span class="mchat__tools-count" data-tool-count>0</span>
      </summary>
      <div class="mchat__tools-body"></div>
    </details>
    <div class="mchat__ops">
      <button class="mchat__op" type="button" data-act="del">${t("删除")}</button>
    </div>`;
  // 展开「使用工具」时顺带把每条结果也打开：一次点击就能看到全部细节
  const toolsEl = row.querySelector(".mchat__tools");
  toolsEl.addEventListener("toggle", (event) => {
    if (event.target !== toolsEl || !toolsEl.open) return;
    toolsEl.querySelectorAll(".mchat__tool").forEach((item) => {
      item.open = true;
    });
  });
  state.feed.appendChild(row);
  scrollFeed();
  return {
    row,
    think: row.querySelector(".mchat__think"),
    thinkBody: row.querySelector(".mchat__think-body"),
    tools: row.querySelector(".mchat__tools"),
    toolsBody: row.querySelector(".mchat__tools-body"),
    toolCount: row.querySelector("[data-tool-count]"),
    bubble: row.querySelector(".mchat__bubble"),
  };
}

function appendNotice(text, tone = "info") {
  const row = document.createElement("div");
  row.className = `mchat__notice is-${tone}`;
  row.textContent = text;
  state.feed.appendChild(row);
  scrollFeed();
  return row;
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
    // 还没拿到服务端的准确用量，先按已收到的字符估一个，让数字跟着字一起长
    state.usage.turn.output = estimateTokens(text);
    paintUsage();
  }, 90);
}

function toolEntry(container, data) {
  const group = container.closest(".mchat__tools");
  if (group) {
    group.hidden = false;
    const count = container.querySelectorAll(".mchat__tool").length;
    const badge = group.querySelector("[data-tool-count]");
    if (badge) badge.textContent = String(count);
  }
  // 用调用 id 做键：同一轮调两次同一个工具、或者连着几轮调同一个，都要各自一条
  const key = data.id || data.name || "tool";
  let node = container.querySelector(`[data-tool="${CSS.escape(key)}"]`);
  if (!node) {
    node = document.createElement("details");
    node.className = "mchat__tool";
    node.dataset.tool = key;
    node.innerHTML = H`<summary><span class="mchat__dot"></span><span data-tool-label></span></summary><pre data-tool-result></pre>`;
    container.appendChild(node);
  }
  const label = node.querySelector("[data-tool-label]");
  const pre = node.querySelector("[data-tool-result]");
  if (data.write) node.classList.add("is-write");
  if (data.status === "running") {
    node.classList.add("is-running");
    label.textContent = `正在${data.label || "处理"}…`;
  } else {
    node.classList.remove("is-running");
    label.textContent = `已${data.label || "完成"}`;
    if (data.result) pre.textContent = data.result;
  }
  if (group) {
    const badge = group.querySelector("[data-tool-count]");
    if (badge) badge.textContent = String(container.querySelectorAll(".mchat__tool").length);
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
      (url, index) => H`<span class="mchat__attach-item">
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
  const lastUser = tail.map((m) => m.role).lastIndexOf("user");
  const out = [];
  tail.forEach((message, index) => {
    const previous = out[out.length - 1];
    // 删掉一条回答后可能出现两条连着说的同角色消息，有的接口（Anthropic）会直接拒收，先合并
    if (previous && previous.role === message.role) {
      previous.content = [previous.content, message.content].filter(Boolean).join("\n\n");
      if (index === lastUser && message.images?.length) previous.images = message.images;
      return;
    }
    out.push({
      role: message.role,
      content: message.content,
      // 历史图片不再重发（既贵又慢），只带最新那条
      images: index === lastUser ? message.images || [] : [],
    });
  });
  // 第一条提问被删掉后可能以 assistant 开头，首条必须是 user
  while (out.length && out[0].role !== "user") out.shift();
  return out;
}

async function send(text) {
  if (state.busy) return;
  const content = String(text || "").trim();
  const images = state.images.slice();
  if (!content && !images.length) return;

  if (!state.ready) {
    await refreshConfig(true);
    if (!state.ready) {
      const off = document.documentElement.dataset.ai === "off";
      appendNotice(
        off ? t("管理员已关闭 AI 功能，暂时聊不了") : t("还没配置 AI 接口，去「设置 → 看板娘 → 对话设置」填一下吧"),
        "warn"
      );
      if (state.banner) state.banner.hidden = false;
      return;
    }
  }

  // 编辑已发送的消息：改完再发，等同于从那条重新开始，后面的问答一起作废
  const editingUid = state.editing?.uid;
  if (editingUid) {
    const at = state.messages.findIndex((m) => m.uid === editingUid);
    if (at >= 0) dropFrom(at);
    clearEdit();
  }

  state.input.value = "";
  autoGrow();
  state.images = [];
  renderAttach();

  const item = { uid: ++uidSeq, role: "user", content, images };
  state.messages.push(item);
  item.el = appendUser(item);

  await streamReply(item);
}

async function streamReply(item) {
  const reply = { uid: ++uidSeq, role: "assistant", content: "" };
  const target = appendBot(reply);
  const ctrl = new AbortController();
  state.ctrl = ctrl;
  setBusy(true);
  // 输入用量按这次真正发出去的历史估算；服务端 done 里有准数时会覆盖
  startUsageTurn(state.messages.map((m) => m.content || "").join("\n"));

  let text = "";
  let thinking = "";
  let serverError = "";
  let stopped = false;

  try {
    const res = await fetch("/api/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      credentials: "same-origin",
      body: JSON.stringify({ messages: historyPayload(), lang: getLang() }),
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
        toolEntry(target.toolsBody, data);
        scrollFeed();
        return;
      }
      if (event === "meta") {
        if (state.modelEl && data.model) state.modelEl.textContent = data.model;
        // 服务端明确说「这轮不统计用量」（例如走管理员默认接口）就别算了
        state.usage.available = data.usage !== false;
        paintUsage();
        return;
      }
      if (event === "done") {
        settleUsage(data.usage);
        return;
      }
      if (event === "notice") {
        appendNotice(data.message || "工具调用已达上限，先按已有结果回答", "warn");
        return;
      }
      if (event === "error") {
        serverError = data.message || "接口返回错误";
      }
    });
  } catch (err) {
    if (err.name === "AbortError") {
      stopped = true;
    } else {
      serverError = err.message || t("对话失败");
    }
  } finally {
    window.clearTimeout(renderTimer);
    renderTimer = 0;
    target.bubble.classList.remove("is-streaming");
    // 没拿到准数（流被中断 / 服务商不给用量）：按最终文本再估一次，别停在半路
    if (state.usage.live) {
      state.usage.turn.output = estimateTokens(text);
      paintUsage();
    }
    if (target.row.querySelector(".mchat__think-body")?.textContent) {
      target.think.open = false;
    }
    setBusy(false);
    state.ctrl = null;

    // 失败：一个字没吐出来就把空泡泡收掉，只在用户那条消息左边亮出「重新发送」；
    // 已经吐了一半就留着，让用户至少能看到收到哪儿了。
    const failed = Boolean(serverError) || (stopped && !text.trim());
    reply.content = text;
    if (text.trim()) {
      target.bubble.innerHTML = renderMarkdown(text);
      reply.el = target.row;
      state.messages.push(reply);
    } else {
      target.row.remove();
    }
    markFailed(item, failed);
    if (serverError) {
      item.errEl = appendNotice(serverError, "warn");
    } else if (failed) {
      item.errEl = appendNotice(t("已停止，这次没收到回复"), "muted");
    } else if (stopped) {
      appendNotice(t("已停止"), "muted");
    }
    scrollFeed();
  }
}

/** 给用户消息打「失败」标记：左边那句「重新发送」只在这时露出来 */
function markFailed(item, failed) {
  item.failed = Boolean(failed);
  if (!item.el) return;
  item.el.classList.toggle("is-failed", item.failed);
  const button = item.el.querySelector('[data-act="retry"]');
  if (button) button.hidden = !item.failed;
}

async function retry(item) {
  if (state.busy || item.role !== "user") return;
  const at = state.messages.indexOf(item);
  if (at < 0) return;
  // 这一轮之后的回答（含半截的）都作废，重新问一次
  dropFrom(at + 1);
  markFailed(item, false);
  item.errEl?.remove();
  item.errEl = null;
  await streamReply(item);
}

/** 从 index 开始（含）把消息与对应的 DOM 一起丢掉 */
function dropFrom(index) {
  for (let i = state.messages.length - 1; i >= index; i -= 1) {
    const message = state.messages[i];
    message.el?.remove();
    message.errEl?.remove();
    if (state.editing?.uid === message.uid) clearEdit();
  }
  state.messages.length = Math.min(Math.max(0, index), state.messages.length);
}

function removeItem(item) {
  const at = state.messages.indexOf(item);
  if (at < 0) return;
  // 删掉自己的提问时，顺带把它的回答一起删掉，免得留下没头没尾的一段
  const withReply = item.role === "user" && state.messages[at + 1]?.role === "assistant";
  const count = withReply ? 2 : 1;
  const removed = state.messages.splice(at, count);
  removed.forEach((message) => {
    message.el?.remove();
    message.errEl?.remove();
    if (state.editing?.uid === message.uid) clearEdit();
  });
  Toast.success(t("已删除这条消息"));
}

/** 删除要按两下（第二下 3 秒内有效），比弹窗轻，也不至于误删 */
function confirmRemove(button, item) {
  if (state.busy) return;
  if (button.dataset.armed === "1") {
    window.clearTimeout(Number(button.dataset.timer || 0));
    delete button.dataset.armed;
    button.textContent = t("删除");
    removeItem(item);
    return;
  }
  button.dataset.armed = "1";
  button.textContent = t("确认删除");
  const timer = window.setTimeout(() => {
    delete button.dataset.armed;
    button.textContent = t("删除");
  }, 3000);
  button.dataset.timer = String(timer);
}

/* ------------------------------------------------------------------ */
/* 编辑已发送的消息                                                    */
/* ------------------------------------------------------------------ */
function startEdit(item) {
  if (state.busy || item.role !== "user" || !item.el) return;
  if (state.editing?.uid === item.uid) {
    state.input.focus();
    return;
  }
  clearEdit();
  state.editing = { uid: item.uid };
  item.el.classList.add("is-editing");
  state.input.value = item.content || "";
  state.images = (item.images || []).slice(0, MAX_IMAGES);
  renderAttach();
  autoGrow();
  if (state.editBar) state.editBar.hidden = false;
  state.input.focus();
  const end = state.input.value.length;
  state.input.setSelectionRange(end, end);
}

function clearEdit() {
  const uid = state.editing?.uid;
  if (uid) {
    state.messages
      .find((m) => m.uid === uid)
      ?.el?.classList.remove("is-editing");
  }
  state.editing = null;
  if (state.editBar) state.editBar.hidden = true;
}

/** 取消编辑：把放回输入框的内容与图片清掉，原来的消息不动 */
function cancelEdit() {
  if (!state.editing) return;
  clearEdit();
  state.input.value = "";
  autoGrow();
  state.images = [];
  renderAttach();
  state.input.focus();
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
