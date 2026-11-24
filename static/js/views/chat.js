/**
 * 聊天（#/chat、#/chat/<id>）
 *
 * 布局：左边会话列表、右边消息流（手机上只显示一栏，靠 is-thread 切换）。
 *
 * 关键取舍：
 * - **不做 WebSocket**：项目别处都是轮询（消息中心 45s、AI 流式才用 SSE），
 *   聊天用 3 秒一次的增量拉取（`?after=<最后一条 id>`），够快也不会为了聊天单独加一套协议。
 * - **只渲染一次骨架**：切会话时重画整个右栏，收新消息只 `insertAdjacentHTML` 追加，
 *   否则每 3 秒重排一次会把用户正在看的滚动位置冲掉。
 * - **改地址用 replaceState**：选会话时同步 `#/chat/<id>`，但不触发 hashchange，
 *   避免路由把整个视图重建一遍（那样会话列表会闪、输入框会丢焦点）。
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { store } from "../store.js";
import { avatarMarkup } from "../ui/avatar.js";
import { Dialog } from "../ui/dialog.js";
import { pickNodes } from "../ui/file-browser.js";
import { openMenu } from "../ui/menu.js";
import { Toast } from "../ui/toast.js";
import { refreshNoticeBadge } from "../ui/notice.js";

const POLL_MS = 3000;
const LIST_POLL_MS = 20000;

const KINDS = {
  image: "i-image",
  file: "i-file",
  sticker: "i-spark",
};

export function renderChat(root, { sub = "" } = {}) {
  const me = store.get().user || {};
  const state = {
    convs: [],
    current: null,
    messages: [],
    friends: [],
    stickerLimit: 100,
    // 单张表情大小上限（MB），静态与动图分开，后台可调
    stickerMaxMb: 2,
    stickerGifMaxMb: 6,
    groupLimit: 200,
    // 撤回时限（分钟，0 = 不限），由 /api/chat/friends 下发
    recallMinutes: 10,
    stickers: [],
    stickerOpen: false,
    busy: false,
    loadingOlder: false,
    hasMore: false,
    lastId: 0,
    query: "",
    timer: 0,
    listTimer: 0,
    lightbox: null,
  };

  root.innerHTML = H`
    <div class="view-head chat-head">
      <div class="view-title"><svg class="icon"><use href="#i-chat"/></svg>${t("聊天")}</div>
      <span class="view-sub" data-sub></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="new-chat" title="${t("发起单聊")}">
        <svg class="icon"><use href="#i-user-plus"/></svg>
      </button>
      <button class="icon-btn" type="button" data-act="new-group" title="${t("新建群聊")}">
        <svg class="icon"><use href="#i-users"/></svg>
      </button>
    </div>
    <div class="view-body chat-body" data-body>
      <aside class="chat-side">
        <div class="chat-side__search">
          <span class="textbox">
            <svg class="icon icon--s"><use href="#i-search"/></svg>
            <input type="search" data-filter placeholder="${t("搜索会话")}" autocomplete="off">
          </span>
        </div>
        <div class="chat-list" data-list></div>
      </aside>
      <section class="chat-main" data-main>
        <div class="chat-empty">
          <div class="chat-empty__art"><svg class="icon"><use href="#i-chat"/></svg></div>
          <div class="chat-empty__title">${t("选一个会话开始聊天")}</div>
          <div class="chat-empty__desc">${t("只能和好友聊天；也可以在好友页点「发消息」直接开一段。")}</div>
        </div>
      </section>
    </div>`;

  const bodyEl = root.querySelector("[data-body]");
  const listEl = root.querySelector("[data-list]");
  const mainEl = root.querySelector("[data-main]");
  const subEl = root.querySelector("[data-sub]");

  /* ---------------------------------------------------------------- */
  /* 会话列表                                                          */
  /* ---------------------------------------------------------------- */
  function convAvatar(conv) {
    if (conv.kind === "group") {
      return `<span class="chat-ava chat-ava--group"><svg class="icon"><use href="#i-users"/></svg></span>`;
    }
    return `<span class="chat-ava">${avatarMarkup({
      avatarUrl: conv.avatarUrl,
      displayName: conv.title,
      username: conv.title,
    })}</span>`;
  }

  function paintList() {
    const q = state.query.trim().toLowerCase();
    const rows = state.convs.filter((conv) =>
      !q ? true : String(conv.title || "").toLowerCase().includes(q)
    );
    if (!rows.length) {
      listEl.innerHTML = H`
        <div class="chat-list__empty">
          ${state.convs.length ? t("没有匹配的会话") : t("还没有聊天，点右上角找好友开始吧")}
        </div>`;
      return;
    }
    listEl.innerHTML = rows
      .map(
        (conv) => H`
      <button class="chat-item ${state.current?.id === conv.id ? "is-active" : ""}" type="button" data-conv="${conv.id}">
        ${convAvatar(conv)}
        <span class="chat-item__main">
          <span class="chat-item__top">
            <span class="chat-item__name">${escapeHtml(conv.title)}</span>
            <span class="chat-item__time">${formatTime(conv.lastMessageAt)}</span>
          </span>
          <span class="chat-item__bottom">
            <span class="chat-item__preview">${escapeHtml(conv.lastMessage || "")}</span>
            ${conv.unread ? H`<span class="chat-item__badge">${conv.unread > 99 ? "99+" : conv.unread}</span>` : ""}
          </span>
        </span>
      </button>`
      )
      .join("");
  }

  async function loadConversations({ silent = false } = {}) {
    try {
      const data = await api.get("/api/chat/conversations");
      if (!root.isConnected) return;
      state.convs = data.items || [];
      const total = state.convs.reduce((sum, c) => sum + (c.unread || 0), 0);
      subEl.textContent = state.convs.length
        ? total
          ? t("{n} 个会话 · {m} 条未读", { n: state.convs.length, m: total })
          : t("{n} 个会话", { n: state.convs.length })
        : t("还没有会话");
      if (state.current) {
        const fresh = state.convs.find((c) => c.id === state.current.id);
        if (fresh) {
          state.current = fresh;
          paintThreadHead();
        }
      }
      paintList();
      if (!silent) refreshNoticeBadge();
    } catch (err) {
      if (!silent) Toast.error(err?.message || t("会话加载失败"));
    }
  }

  /* ---------------------------------------------------------------- */
  /* 消息流                                                            */
  /* ---------------------------------------------------------------- */
  function timeText(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return sameDay
      ? `${pad(d.getHours())}:${pad(d.getMinutes())}`
      : `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fileCard(node, index, messageId, mine) {
    return H`
      <div class="msg-file">
        <span class="msg-file__icon"><svg class="icon"><use href="#${node.icon || "i-file"}"/></svg></span>
        <span class="msg-file__main">
          <span class="msg-file__name" title="${escapeHtml(node.name)}">${escapeHtml(node.name)}</span>
          <span class="msg-file__meta">${node.folder ? t("文件夹") : formatBytes(node.size)}</span>
        </span>
        ${
          node.folder
            ? H`<span class="msg-file__hint">${t("保存后可查看")}</span>`
            : H`<button class="btn btn--subtle msg-file__btn" type="button"
                     data-act="download" data-mid="${messageId}" data-index="${index}"
                     data-name="${escapeHtml(node.name)}">${t("下载")}</button>`
        }
      </div>`;
  }

  function bodyHtml(msg, mine) {
    if (msg.recalled) {
      return H`<div class="msg__recalled">${mine ? t("你撤回了一条消息") : t("对方撤回了一条消息")}</div>`;
    }
    if (msg.kind === "image") {
      const p = msg.payload || {};
      return H`<button class="msg__image" type="button" data-act="zoom" data-src="/api/chat/media/${escapeHtml(p.name || "")}">
          <img src="/api/chat/media/${escapeHtml(p.name || "")}" alt="${t("图片")}" loading="lazy"
               width="${Number(p.width) || 0}" height="${Number(p.height) || 0}">
        </button>`;
    }
    if (msg.kind === "sticker") {
      const p = msg.payload || {};
      return H`<button class="msg__sticker-btn" type="button" data-act="zoom" data-src="/api/chat/media/${escapeHtml(p.name || "")}">
          <img class="msg__sticker" src="/api/chat/media/${escapeHtml(p.name || "")}" alt="${t("表情")}" loading="lazy">
        </button>`;
    }
    if (msg.kind === "file") {
      const nodes = (msg.payload || {}).nodes || [];
      return H`
        <div class="msg__files">
          ${nodes.map((node, i) => fileCard(node, i, msg.id, mine)).join("")}
        </div>
        ${
          !mine && nodes.length
            ? H`<button class="msg__save" type="button" data-act="save" data-mid="${msg.id}">
                 <svg class="icon icon--s"><use href="#i-download"/></svg>${t("保存到我的文件")}
               </button>`
            : ""
        }`;
    }
    return H`<div class="msg__text">${escapeHtml(msg.body || "")}</div>`;
  }

  function msgHtml(msg) {
    if (msg.kind === "system") {
      return H`<div class="msg msg--system" data-mid="${msg.id}">
          <span class="msg__system">${escapeHtml(msg.body || "")}</span>
        </div>`;
    }
    const mine = msg.sender?.id === me.id;
    const sender = msg.sender || {};
    return H`
      <div class="msg ${mine ? "msg--mine" : ""}" data-mid="${msg.id}" data-kind="${msg.kind}">
        <span class="msg__avatar">${avatarMarkup(sender)}</span>
        <div class="msg__main">
          <div class="msg__meta">
            ${state.current?.kind === "group" && !mine ? H`<span class="msg__name">${escapeHtml(sender.displayName || "")}</span>` : ""}
            <span class="msg__time">${timeText(msg.createdAt)}</span>
            ${msg.forwardedFrom ? H`<span class="msg__tag">${t("转发")}</span>` : ""}
          </div>
          <div class="msg__bubble ${msg.kind === "text" ? "" : "msg__bubble--plain"}">${bodyHtml(msg, mine)}</div>
        </div>
        ${
          msg.recalled
            ? ""
            : H`<button class="msg__more" type="button" data-act="more" data-mid="${msg.id}" aria-label="${t("更多")}">
                 <svg class="icon"><use href="#i-more"/></svg>
               </button>`
        }
      </div>`;
  }

  function paintThreadHead() {
    const head = mainEl.querySelector("[data-thread-head]");
    if (!head || !state.current) return;
    const conv = state.current;
    head.querySelector("[data-thread-title]").textContent = conv.title;
    head.querySelector("[data-thread-sub]").textContent =
      conv.kind === "group" ? t("{n} 人", { n: conv.memberCount }) : t("单聊");
  }

  function paintThread() {
    const conv = state.current;
    if (!conv) {
      mainEl.innerHTML = H`
        <div class="chat-empty">
          <div class="chat-empty__art"><svg class="icon"><use href="#i-chat"/></svg></div>
          <div class="chat-empty__title">${t("选一个会话开始聊天")}</div>
          <div class="chat-empty__desc">${t("只能和好友聊天；也可以在好友页点「发消息」直接开一段。")}</div>
        </div>`;
      return;
    }
    mainEl.innerHTML = H`
      <div class="chat-thread" data-thread-head>
        <button class="icon-btn chat-back" type="button" data-act="back" title="${t("返回会话列表")}">
          <svg class="icon chat-back__icon"><use href="#i-chevron"/></svg>
        </button>
        <span class="chat-thread__ava">${convAvatar(conv)}</span>
        <span class="chat-thread__main">
          <span class="chat-thread__title" data-thread-title>${escapeHtml(conv.title)}</span>
          <span class="chat-thread__sub" data-thread-sub></span>
        </span>
        <span class="grow"></span>
        ${
          conv.kind === "group"
            ? H`<button class="icon-btn" type="button" data-act="group-info" title="${t("群聊信息")}">
                 <svg class="icon"><use href="#i-info"/></svg></button>`
            : ""
        }
      </div>
      <div class="chat-feed" data-feed>
        <button class="chat-more" type="button" data-act="older" hidden>${t("加载更早的消息")}</button>
        <div class="chat-feed__list" data-feed-list></div>
      </div>
      <div class="chat-compose">
        <div class="sticker-panel" data-sticker-panel hidden>
          <div class="sticker-panel__head">
            <span>${t("我的表情")}</span>
            <span class="muted" data-sticker-count></span>
            <span class="muted" data-sticker-hint></span>
            <span class="grow"></span>
            <button class="btn btn--subtle" type="button" data-act="sticker-add">${t("添加表情")}</button>
          </div>
          <div class="sticker-grid" data-sticker-grid></div>
        </div>
        <div class="chat-compose__bar">
          <div class="textarea chat-compose__input">
            <textarea data-input rows="1" maxlength="2000" placeholder="${t("说点什么…（Enter 发送，Shift+Enter 换行）")}"></textarea>
            <div class="chat-compose__tools">
              <button class="chat-tool" type="button" data-act="pick-file" title="${t("发送网盘文件")}">
                <svg class="icon"><use href="#i-folder"/></svg>
              </button>
              <button class="chat-tool" type="button" data-act="pick-image" title="${t("发送图片")}">
                <svg class="icon"><use href="#i-image"/></svg>
              </button>
              <button class="chat-tool" type="button" data-act="open-stickers" title="${t("表情包")}">
                <svg class="icon"><use href="#i-smile"/></svg>
              </button>
              <button class="btn btn--accent chat-send" type="button" data-act="send">
                <svg class="icon icon--s"><use href="#i-send"/></svg><span>${t("发送")}</span>
              </button>
            </div>
          </div>
        </div>
        <input type="file" accept="image/*" data-image-input hidden>
        <input type="file" accept="image/*,.gif" data-sticker-input hidden multiple>
      </div>`;
    paintThreadHead();
    paintFeed();
    if (conv.kind === "group") {
      // 群名跟着消息里的系统提示变，重新拉一次就同步了
    }
    const input = mainEl.querySelector("[data-input]");
    const fit = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(168, input.scrollHeight)}px`;
    };
    input.addEventListener("input", fit);
    fit();
    if (conv.canSend === false) {
      // 被拉黑 / 拉黑了对方：发不出去，就别让人把字打完再被拒
      input.disabled = true;
      input.placeholder = conv.blockedByPeer
        ? t("对方已拒收你的消息")
        : t("你已把对方拉黑，无法发送消息");
      mainEl.querySelectorAll(".chat-compose__tools .chat-tool").forEach((btn) => {
        btn.disabled = true;
      });
      const sendBtn = mainEl.querySelector('[data-act="send"]');
      if (sendBtn) sendBtn.disabled = true;
    }
  }

  function paintFeed() {
    const list = mainEl.querySelector("[data-feed-list]");
    if (!list) return;
    list.innerHTML = state.messages.map(msgHtml).join("");
    const more = mainEl.querySelector('[data-act="older"]');
    if (more) more.hidden = !state.hasMore;
    scrollToEnd(true);
  }

  function scrollToEnd(instant = false) {
    const feed = mainEl.querySelector("[data-feed]");
    if (!feed) return;
    feed.scrollTo({ top: feed.scrollHeight, behavior: instant ? "auto" : "smooth" });
  }

  async function openConversation(convId, { fromRoute = false } = {}) {
    const conv = state.convs.find((c) => c.id === Number(convId));
    if (!conv) {
      // 深链进了一个不在列表里的会话（可能刚被移除）：拉一次列表再试
      await loadConversations({ silent: true });
      const again = state.convs.find((c) => c.id === Number(convId));
      if (!again) {
        if (fromRoute) Toast.info(t("这个会话不在了"));
        return;
      }
      return openConversation(convId, { fromRoute });
    }
    state.current = conv;
    state.messages = [];
    state.lastId = 0;
    state.hasMore = false;
    bodyEl.classList.add("is-thread");
    paintList();
    paintThread();
    // 地址同步但不触发 hashchange，避免整个视图重建
    if (location.hash !== `#/chat/${conv.id}`) {
      history.replaceState(null, "", `#/chat/${conv.id}`);
    }
    mainEl.querySelector("[data-feed-list]").innerHTML = H`
      <div class="chat-loading">${t("加载中…")}</div>`;
    try {
      const data = await api.get(`/api/chat/conversations/${conv.id}/messages`);
      if (!root.isConnected || state.current?.id !== conv.id) return;
      state.messages = data.items || [];
      state.hasMore = Boolean(data.hasMore);
      state.lastId = state.messages.reduce((max, m) => Math.max(max, m.id), 0);
      paintFeed();
      await markRead();
    } catch (err) {
      Toast.error(err?.message || t("消息加载失败"));
    }
  }

  async function markRead() {
    if (!state.current) return;
    try {
      await api.post(`/api/chat/conversations/${state.current.id}/read`, {
        messageId: state.lastId || null,
      });
      const conv = state.convs.find((c) => c.id === state.current.id);
      if (conv) conv.unread = 0;
      paintList();
      refreshNoticeBadge();
    } catch {
      /* 已读失败不影响看消息 */
    }
  }

  async function pollMessages() {
    if (!state.current || document.hidden || state.busy) return;
    try {
      const data = await api.get(
        `/api/chat/conversations/${state.current.id}/messages?after=${state.lastId}`
      );
      const items = data.items || [];
      if (!items.length || !root.isConnected) return;
      const list = mainEl.querySelector("[data-feed-list]");
      if (!list) return;
      const feed = mainEl.querySelector("[data-feed]");
      const atBottom = feed ? feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120 : true;
      list.insertAdjacentHTML("beforeend", items.map(msgHtml).join(""));
      state.messages.push(...items);
      state.lastId = Math.max(state.lastId, ...items.map((m) => m.id));
      if (atBottom) scrollToEnd();
      await markRead();
    } catch {
      /* 轮询失败静默，下一轮再试 */
    }
  }

  async function loadOlder() {
    if (!state.current || state.loadingOlder || !state.messages.length) return;
    state.loadingOlder = true;
    const feed = mainEl.querySelector("[data-feed]");
    const before = state.messages[0].id;
    const prevHeight = feed?.scrollHeight || 0;
    try {
      const data = await api.get(
        `/api/chat/conversations/${state.current.id}/messages?before=${before}`
      );
      const items = data.items || [];
      state.hasMore = items.length >= 30;
      if (items.length) {
        state.messages = [...items, ...state.messages];
        const list = mainEl.querySelector("[data-feed-list]");
        list.insertAdjacentHTML("afterbegin", items.map(msgHtml).join(""));
        if (feed) feed.scrollTop = feed.scrollHeight - prevHeight;
      }
    } catch (err) {
      Toast.error(err?.message || t("加载失败"));
    } finally {
      state.loadingOlder = false;
      const more = mainEl.querySelector('[data-act="older"]');
      if (more) more.hidden = !state.hasMore;
    }
  }

  /* ---------------------------------------------------------------- */
  /* 发送                                                              */
  /* ---------------------------------------------------------------- */
  function bumpConversation(message) {
    const conv = state.convs.find((c) => c.id === message.conversationId);
    if (!conv) return;
    conv.lastMessageAt = message.createdAt;
    conv.lastMessage =
      message.kind === "text"
        ? message.body
        : message.kind === "image"
          ? t("[图片]")
          : message.kind === "sticker"
            ? t("[表情]")
            : t("[文件]");
    state.convs = [conv, ...state.convs.filter((c) => c.id !== conv.id)];
    paintList();
  }

  async function send(kind, payload, body = "") {
    if (!state.current || state.busy) return;
    state.busy = true;
    try {
      const data = await api.post(`/api/chat/conversations/${state.current.id}/messages`, {
        kind,
        body,
        payload,
      });
      const msg = data.message;
      state.messages.push(msg);
      state.lastId = Math.max(state.lastId, msg.id);
      const list = mainEl.querySelector("[data-feed-list]");
      if (list) {
        const placeholder = list.querySelector(".chat-loading");
        if (placeholder) placeholder.remove();
        list.insertAdjacentHTML("beforeend", msgHtml(msg));
        scrollToEnd();
      }
      bumpConversation(msg);
    } catch (err) {
      Toast.error(err?.message || t("发送失败"));
    } finally {
      state.busy = false;
    }
  }

  async function sendText() {
    const input = mainEl.querySelector("[data-input]");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    await send("text", null, text);
  }

  async function uploadImage(file) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      Toast.error(t("只能发图片"));
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      Toast.error(t("图片不能超过 8 MB"));
      return;
    }
    try {
      const data = await api.raw("/api/chat/images", file, "PUT");
      await send("image", { name: data.name, width: data.width, height: data.height });
    } catch (err) {
      Toast.error(err?.message || t("图片上传失败"));
    }
  }

  async function pickFilesToSend() {
    const picked = await pickNodes({ title: t("选择要发送的文件"), mode: "any" });
    if (!picked || !picked.length) return;
    await send("file", { nodeIds: picked.map((node) => node.id) });
  }

  /* ---------------------------------------------------------------- */
  /* 表情包                                                            */
  /* ---------------------------------------------------------------- */
  async function loadStickers() {
    try {
      const data = await api.get("/api/chat/stickers");
      state.stickers = data.items || [];
      state.stickerLimit = data.limit || state.stickerLimit;
      state.stickerMaxMb = data.maxMb || state.stickerMaxMb;
      state.stickerGifMaxMb = data.gifMaxMb || state.stickerGifMaxMb;
      paintStickers();
    } catch {
      /* 表情面板打不开不影响发文字 */
    }
  }

  function paintStickers() {
    const grid = mainEl.querySelector("[data-sticker-grid]");
    if (!grid) return;
    const count = mainEl.querySelector("[data-sticker-count]");
    if (count) count.textContent = `${state.stickers.length}/${state.stickerLimit}`;
    const hint = mainEl.querySelector("[data-sticker-hint]");
    if (hint) {
      hint.textContent = t("单张 ≤ {mb} MB｜GIF ≤ {gif} MB", {
        mb: state.stickerMaxMb,
        gif: state.stickerGifMaxMb,
      });
    }
    if (!state.stickers.length) {
      grid.innerHTML = H`<div class="sticker-grid__empty">${t("还没有表情，点右上角「添加表情」上传一张吧")}</div>`;
      return;
    }
    grid.innerHTML = state.stickers
      .map(
        (s) => H`
      <span class="sticker-item" data-sticker-id="${s.id}">
        <button class="sticker-item__btn" type="button" data-act="send-sticker" data-id="${s.id}" title="${escapeHtml(s.name)}">
          <img src="${escapeHtml(s.url)}" alt="${escapeHtml(s.name)}" loading="lazy">
        </button>
        <button class="sticker-item__del" type="button" data-act="del-sticker" data-id="${s.id}" aria-label="${t("删除表情")}">
          <svg class="icon"><use href="#i-close"/></svg>
        </button>
      </span>`
      )
      .join("");
  }

  function toggleStickers(force) {
    const panel = mainEl.querySelector("[data-sticker-panel]");
    if (!panel) return;
    state.stickerOpen = force === undefined ? !state.stickerOpen : Boolean(force);
    panel.hidden = !state.stickerOpen;
    if (state.stickerOpen) void loadStickers();
  }

  async function deleteSticker(id, name = "") {
    const ok = await Dialog.confirm({
      title: t("删除表情"),
      message: name ? t("把「{name}」从我的表情里删掉？", { name }) : t("从我的表情里删掉这张？"),
      confirmText: t("删除"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/chat/stickers/${id}`);
      state.stickers = state.stickers.filter((s) => s.id !== id);
      paintStickers();
      Toast.success(t("已删除表情"));
    } catch (err) {
      Toast.error(err?.message || t("删除失败"));
    }
  }

  /* ---------------------------------------------------------------- */
  /* 会话右键：清空 / 删除 / 拉黑                                       */
  /* ---------------------------------------------------------------- */
  function leaveThread() {
    state.current = null;
    state.messages = [];
    bodyEl.classList.remove("is-thread");
    history.replaceState(null, "", "#/chat");
    paintThread();
  }

  async function clearChat(conv) {
    const ok = await Dialog.confirm({
      title: t("清空聊天记录"),
      message: t("只清空你这一侧的记录，对方的聊天不受影响。"),
      confirmText: t("清空"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/chat/conversations/${conv.id}/clear`, {});
      if (state.current?.id === conv.id) {
        state.messages = [];
        state.lastId = 0;
        state.hasMore = false;
        paintFeed();
      }
      await loadConversations({ silent: true });
      Toast.success(t("已清空"));
    } catch (err) {
      Toast.error(err?.message || t("清空失败"));
    }
  }

  async function deleteChat(conv) {
    const group = conv.kind === "group";
    const ok = await Dialog.confirm({
      title: group ? t("退出群聊") : t("删除聊天"),
      message: group
        ? t("退出后不会收到这个群的消息，重新加入要群主再邀请一次。")
        : t("聊天会从列表里消失，记录清空。对方再发消息时会重新出现。"),
      confirmText: group ? t("退出") : t("删除"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/chat/conversations/${conv.id}`);
      if (state.current?.id === conv.id) leaveThread();
      await loadConversations({ silent: true });
      Toast.success(group ? t("已退出群聊") : t("已删除聊天"));
    } catch (err) {
      Toast.error(err?.message || t("操作失败"));
    }
  }

  async function blockPeer(conv) {
    if (!conv.peerId) return;
    const ok = await Dialog.confirm({
      title: t("拉黑并删除"),
      message: t("会同时删掉好友关系，你们互相都发不了消息。之后可以在好友页解除。"),
      confirmText: t("拉黑"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.post("/api/chat/blocks", { userId: conv.peerId });
      if (state.current?.id === conv.id) leaveThread();
      await loadConversations({ silent: true });
      Toast.success(t("已拉黑并删除好友"));
    } catch (err) {
      Toast.error(err?.message || t("操作失败"));
    }
  }

  function openConvMenu(conv, x, y) {
    const items = [
      { label: t("清空聊天记录"), icon: "i-restore", onClick: () => clearChat(conv) },
      {
        label: conv.kind === "group" ? t("退出群聊") : t("删除聊天"),
        icon: conv.kind === "group" ? "i-logout" : "i-trash",
        danger: true,
        onClick: () => deleteChat(conv),
      },
    ];
    if (conv.kind === "direct" && conv.peerId && conv.canSend !== false) {
      items.push({
        label: t("拉黑并删除好友"),
        icon: "i-lock",
        danger: true,
        onClick: () => blockPeer(conv),
      });
    }
    openMenu({ x, y, items });
  }

  /* ---------------------------------------------------------------- */
  /* 消息操作：撤回 / 转发 / 举报 / 保存                                */
  /* ---------------------------------------------------------------- */
  function messageById(id) {
    return state.messages.find((m) => m.id === Number(id));
  }

  function openMessageMenu(button, msg) {
    const rect = button.getBoundingClientRect();
    openMessageMenuAt(msg, rect.left, rect.bottom + 6, button);
  }

  function openMessageMenuAt(msg, x, y, anchor = null) {
    const mine = msg.sender?.id === me.id;
    const items = [];
    if (msg.kind === "text" && msg.body) {
      items.push({
        label: t("复制文字"),
        icon: "i-text",
        onClick: async () => {
          const { copyText } = await import("../ui/clipboard.js");
          const ok = await copyText(msg.body);
          ok ? Toast.success(t("已复制")) : Toast.error(t("复制失败"));
        },
      });
    }
    if (msg.kind === "sticker" && !mine) {
      items.push({
        label: t("添加到我的表情"),
        icon: "i-star",
        onClick: () => saveSticker(msg),
      });
    }
    if (msg.kind === "file") {
      items.push({
        label: t("下载文件"),
        icon: "i-download",
        onClick: () => downloadMessageFiles(msg),
      });
      items.push({
        label: t("保存到我的文件"),
        icon: "i-folder",
        onClick: () => saveFiles(msg),
      });
    }
    items.push({
      label: t("转发到其他聊天"),
      icon: "i-share",
      onClick: () => forwardMessage(msg),
    });
    if (mine) {
      // recallable 由服务端按时限算好；超过时限就不给这一项，免得点进去才被拒
      if (msg.recallable !== false) {
        items.push({
          label: t("撤回"),
          icon: "i-restore",
          onClick: () => recallMessage(msg),
        });
      }
    } else {
      items.push({
        label: t("举报这条消息"),
        icon: "i-alert",
        danger: true,
        onClick: () => reportMessage(msg),
      });
    }
    openMenu({ x, y, items, anchor });
  }

  async function saveSticker(msg) {
    try {
      const data = await api.post(`/api/chat/messages/${msg.id}/sticker`, {});
      Toast[data.created === false ? "info" : "success"](
        data.created === false ? t("这个表情已经在你的表情里了") : t("已添加到我的表情")
      );
      if (state.stickerOpen) await loadStickers();
    } catch (err) {
      Toast.error(err?.message || t("添加失败"));
    }
  }

  async function downloadMessageFiles(msg) {
    const nodes = (msg.payload || {}).nodes || [];
    const files = nodes.filter((node) => !node.folder);
    if (!files.length) {
      Toast.info(t("文件夹请用「保存到我的文件」"));
      return;
    }
    for (let i = 0; i < files.length; i += 1) {
      const node = files[i];
      const index = nodes.indexOf(node);
      const a = document.createElement("a");
      a.href = `/api/chat/messages/${msg.id}/files/${index}`;
      a.download = node.name || "";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 浏览器对连续触发的多个下载有节流，多个文件之间稍微错开
      if (i < files.length - 1) await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }

  async function recallMessage(msg) {
    const limit = state.recallMinutes;
    const ok = await Dialog.confirm({
      title: t("撤回这条消息"),
      message: limit
        ? t("撤回后对方也会看到「已撤回」，内容不会留在聊天里。发送后 {n} 分钟内可撤回。", {
            n: limit,
          })
        : t("撤回后对方也会看到「已撤回」，内容不会留在聊天里。"),
      confirmText: t("撤回"),
      danger: true,
    });
    if (!ok) return;
    try {
      const data = await api.post(`/api/chat/messages/${msg.id}/recall`, {});
      const idx = state.messages.findIndex((m) => m.id === msg.id);
      if (idx >= 0) state.messages[idx] = data.message;
      const el = mainEl.querySelector(`.msg[data-mid="${msg.id}"]`);
      if (el) el.outerHTML = msgHtml(data.message);
    } catch (err) {
      Toast.error(err?.message || t("撤回失败"));
    }
  }

  async function saveFiles(msg) {
    try {
      const data = await api.post(`/api/chat/messages/${msg.id}/save`, {});
      Toast.success(
        t("已保存 {n} 项到「{folder}」", { n: data.saved || 0, folder: data.folder || t("聊天文件") })
      );
    } catch (err) {
      Toast.error(err?.message || t("保存失败"));
    }
  }

  async function forwardMessage(msg) {
    const targets = await pickConversations({
      title: t("转发到"),
      confirmText: t("转发"),
      excludeId: msg.conversationId,
    });
    if (!targets.length) return;
    try {
      const data = await api.post(`/api/chat/messages/${msg.id}/forward`, {
        conversationIds: targets,
      });
      Toast.success(t("已转发到 {n} 个会话", { n: data.count || targets.length }));
      await loadConversations({ silent: true });
    } catch (err) {
      Toast.error(err?.message || t("转发失败"));
    }
  }

  async function reportMessage(msg) {
    const reason = await Dialog.prompt({
      title: t("举报这条消息"),
      message: t("管理员会看到这条消息举报时的内容。恶意举报会被记录。"),
      label: t("举报理由"),
      placeholder: t("例如：广告、辱骂、违法内容"),
      confirmText: t("提交举报"),
    });
    if (reason === null) return;
    if (!reason.trim()) {
      Toast.info(t("写一下举报理由"));
      return;
    }
    try {
      await api.post(`/api/chat/messages/${msg.id}/report`, { reason: reason.trim() });
      Toast.success(t("已举报，等管理员处理"));
    } catch (err) {
      Toast.error(err?.message || t("举报失败"));
    }
  }

  /** 转发 / 群聊选人共用：列出会话多选 */
  function pickConversations({ title = "选择会话", confirmText = "确定", excludeId = 0 } = {}) {
    return new Promise((resolve) => {
      const chosen = new Set();
      const backdrop = document.createElement("div");
      backdrop.className = "picker-backdrop";
      backdrop.innerHTML = H`
        <div class="picker picker--friend" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-chat"/></svg>
            <span>${title}</span>
          </div>
          <div class="picker__body friend-pick__list" data-list></div>
          <div class="picker__foot">
            <span class="muted" data-hint>${t("可多选")}</span>
            <span style="flex:1"></span>
            <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
            <button class="btn btn--accent" type="button" data-act="ok" disabled>${confirmText}</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const listEl = backdrop.querySelector("[data-list]");
      const okBtn = backdrop.querySelector('[data-act="ok"]');
      const hintEl = backdrop.querySelector("[data-hint]");
      const close = (value) => {
        document.removeEventListener("keydown", onKey);
        backdrop.remove();
        resolve(value);
      };
      const onKey = (event) => {
        if (event.key === "Escape") close([]);
      };
      document.addEventListener("keydown", onKey);
      backdrop.addEventListener("click", (event) => {
        if (!event.composedPath().includes(backdrop.querySelector(".picker"))) close([]);
      });
      const rows = state.convs.filter((c) => c.id !== Number(excludeId));
      if (!rows.length) {
        listEl.innerHTML = H`<div class="chat-list__empty">${t("还没有别的会话可以转发")}</div>`;
      } else {
        listEl.innerHTML = rows
          .map(
            (c) => H`
          <button class="friend-pick__row" type="button" data-id="${c.id}">
            <span class="friend-pick__avatar">${convAvatar(c)}</span>
            <span class="friend-pick__main">
              <span class="friend-pick__name">${escapeHtml(c.title)}</span>
              <span class="friend-pick__meta">${c.kind === "group" ? t("群聊") : t("单聊")}</span>
            </span>
            <svg class="icon icon--s friend-pick__check"><use href="#i-check"/></svg>
          </button>`
          )
          .join("");
      }
      listEl.addEventListener("click", (event) => {
        const row = event.target.closest("[data-id]");
        if (!row) return;
        const id = Number(row.dataset.id);
        if (chosen.has(id)) chosen.delete(id);
        else chosen.add(id);
        row.classList.toggle("is-active", chosen.has(id));
        okBtn.disabled = chosen.size === 0;
        hintEl.textContent = chosen.size ? t("已选 {n} 个", { n: chosen.size }) : t("可多选");
      });
      okBtn.addEventListener("click", () => close([...chosen]));
      backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close([]));
    });
  }

  /* ---------------------------------------------------------------- */
  /* 新建单聊 / 群聊                                                   */
  /* ---------------------------------------------------------------- */
  async function ensureFriends() {
    if (state.friends.length) return state.friends;
    try {
      const data = await api.get("/api/chat/friends");
      state.friends = data.friends || [];
      state.stickerLimit = data.stickerLimit || state.stickerLimit;
      state.groupLimit = data.groupLimit || state.groupLimit;
      state.recallMinutes = data.recallMinutes ?? state.recallMinutes;
    } catch {
      state.friends = [];
    }
    return state.friends;
  }

  async function newDirectChat() {
    const friends = await ensureFriends();
    if (!friends.length) {
      Toast.info(t("还没有好友，先去「好友」里加一个"));
      location.hash = "#/friends";
      return;
    }
    const picked = await pickFriendLocal({
      title: t("找谁聊天"),
      friends,
    });
    if (!picked) return;
    try {
      const data = await api.post("/api/chat/conversations/direct", { userId: picked.id });
      await loadConversations({ silent: true });
      const conv = data.conversation;
      if (conv) await openConversation(conv.id);
    } catch (err) {
      Toast.error(err?.message || t("打不开这个会话"));
    }
  }

  /** 单人选择（本地版，避免为了选一个人再请求一次好友列表） */
  function pickFriendLocal({ title, friends }) {
    return new Promise((resolve) => {
      let picked = null;
      const backdrop = document.createElement("div");
      backdrop.className = "picker-backdrop";
      backdrop.innerHTML = H`
        <div class="picker picker--friend" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-users"/></svg><span>${title}</span>
          </div>
          <div class="picker__body friend-pick__list" data-list></div>
          <div class="picker__foot">
            <span class="grow"></span>
            <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
            <button class="btn btn--accent" type="button" data-act="ok" disabled>${t("开始聊天")}</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const listEl = backdrop.querySelector("[data-list]");
      const okBtn = backdrop.querySelector('[data-act="ok"]');
      listEl.innerHTML = friends
        .map(
          (f) => H`
        <button class="friend-pick__row" type="button" data-id="${f.id}">
          <span class="friend-pick__avatar">${avatarMarkup(f)}</span>
          <span class="friend-pick__main">
            <span class="friend-pick__name">${escapeHtml(f.displayName)}</span>
            <span class="friend-pick__meta">@${escapeHtml(f.username)}</span>
          </span>
          <svg class="icon icon--s friend-pick__check"><use href="#i-check"/></svg>
        </button>`
        )
        .join("");
      const close = (value) => {
        backdrop.remove();
        resolve(value);
      };
      listEl.addEventListener("click", (event) => {
        const row = event.target.closest("[data-id]");
        if (!row) return;
        picked = friends.find((f) => String(f.id) === row.dataset.id) || null;
        listEl.querySelectorAll("[data-id]").forEach((r) => r.classList.toggle("is-active", r === row));
        okBtn.disabled = !picked;
        if (picked) close(picked);
      });
      okBtn.addEventListener("click", () => picked && close(picked));
      backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close(null));
      backdrop.addEventListener("click", (event) => {
        if (!event.composedPath().includes(backdrop.querySelector(".picker"))) close(null);
      });
    });
  }

  async function newGroupChat() {
    const friends = await ensureFriends();
    if (!friends.length) {
      Toast.info(t("还没有好友，先去「好友」里加一个"));
      location.hash = "#/friends";
      return;
    }
    const result = await groupDialog({ friends, limit: state.groupLimit });
    if (!result) return;
    try {
      const data = await api.post("/api/chat/conversations/group", {
        title: result.title,
        memberIds: result.members,
      });
      await loadConversations({ silent: true });
      const conv = data.conversation;
      if (conv) await openConversation(conv.id);
      Toast.success(t("群聊已创建"));
    } catch (err) {
      Toast.error(err?.message || t("建群失败"));
    }
  }

  function groupDialog({ friends, limit }) {
    return new Promise((resolve) => {
      const chosen = new Set();
      const backdrop = document.createElement("div");
      backdrop.className = "picker-backdrop";
      backdrop.innerHTML = H`
        <div class="picker picker--group" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-users"/></svg><span>${t("新建群聊")}</span>
          </div>
          <div class="group-dialog">
            <label class="field">
              <span class="field__label">${t("群名（可留空，自动按成员拼）")}</span>
              <span class="textbox"><input data-title maxlength="60" placeholder="${t("例如：周末去哪儿")}"></span>
            </label>
            <div class="group-dialog__head">
              <span>${t("选择成员")}</span>
              <span class="muted" data-count>${t("已选 0 人")}</span>
            </div>
            <div class="group-dialog__list" data-list>
              ${friends
                .map(
                  (f) => H`
                <button class="friend-pick__row" type="button" data-id="${f.id}">
                  <span class="friend-pick__avatar">${avatarMarkup(f)}</span>
                  <span class="friend-pick__main">
                    <span class="friend-pick__name">${escapeHtml(f.displayName)}</span>
                    <span class="friend-pick__meta">@${escapeHtml(f.username)}</span>
                  </span>
                  <svg class="icon icon--s friend-pick__check"><use href="#i-check"/></svg>
                </button>`
                )
                .join("")}
            </div>
          </div>
          <div class="picker__foot">
            <span class="muted">${t("最多 {n} 人", { n: limit })}</span>
            <span style="flex:1"></span>
            <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
            <button class="btn btn--accent" type="button" data-act="ok" disabled>${t("创建群聊")}</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const listEl = backdrop.querySelector("[data-list]");
      const okBtn = backdrop.querySelector('[data-act="ok"]');
      const countEl = backdrop.querySelector("[data-count]");
      const close = (value) => {
        backdrop.remove();
        resolve(value);
      };
      listEl.addEventListener("click", (event) => {
        const row = event.target.closest("[data-id]");
        if (!row) return;
        const id = Number(row.dataset.id);
        if (chosen.has(id)) chosen.delete(id);
        else chosen.add(id);
        row.classList.toggle("is-active", chosen.has(id));
        countEl.textContent = t("已选 {n} 人", { n: chosen.size });
        okBtn.disabled = chosen.size === 0 || chosen.size + 1 > limit;
      });
      okBtn.addEventListener("click", () =>
        close({
          title: backdrop.querySelector("[data-title]").value.trim(),
          members: [...chosen],
        })
      );
      backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close(null));
      backdrop.addEventListener("click", (event) => {
        if (!event.composedPath().includes(backdrop.querySelector(".picker"))) close(null);
      });
    });
  }

  async function showGroupInfo() {
    const conv = state.current;
    if (!conv || conv.kind !== "group") return;
    await groupInfoDialog(conv);
  }

  async function groupInfoDialog(conv) {
    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    const paint = () => {
      backdrop.innerHTML = H`
        <div class="picker picker--friend" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-users"/></svg><span>${escapeHtml(conv.title)}</span>
          </div>
          <div class="picker__body">
            <div class="group-info__row">
              <span class="muted">${t("成员 {n} 人", { n: conv.memberCount })}</span>
              <span class="grow"></span>
              ${
                conv.isOwner
                  ? H`<button class="btn btn--subtle" type="button" data-act="rename">${t("改群名")}</button>`
                  : ""
              }
            </div>
            <div class="friend-pick__list">
              ${(conv.members || [])
                .map(
                  (m) => H`
                <div class="friend-pick__row ${m.id === conv.ownerId ? "is-owner" : ""}" data-uid="${m.id}">
                  <span class="friend-pick__avatar">${avatarMarkup(m)}</span>
                  <span class="friend-pick__main">
                    <span class="friend-pick__name">${escapeHtml(m.displayName)}${
                      m.id === me.id ? H` <span class="chip">${t("我")}</span>` : ""
                    }${m.id === conv.ownerId ? H` <span class="chip">${t("群主")}</span>` : ""}</span>
                    <span class="friend-pick__meta">@${escapeHtml(m.username)}</span>
                  </span>
                  ${
                    conv.isOwner && m.id !== me.id && m.id !== conv.ownerId
                      ? H`<button class="btn btn--subtle" type="button" data-act="kick" data-uid="${m.id}">${t("移出")}</button>`
                      : ""
                  }
                </div>`
                )
                .join("")}
            </div>
          </div>
          <div class="picker__foot">
            <button class="btn btn--subtle" type="button" data-act="leave">${t("退出群聊")}</button>
            <span style="flex:1"></span>
            <button class="btn btn--accent" type="button" data-act="close">${t("关闭")}</button>
          </div>
        </div>`;
      backdrop.querySelector('[data-act="close"]').addEventListener("click", () => backdrop.remove());
    };
    paint();
    document.body.appendChild(backdrop);
    const onKey = (event) => {
      if (event.key === "Escape") backdrop.remove();
    };
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", (event) => {
      if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
        return;
      }
      const btn = event.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "rename") {
        void (async () => {
          const name = await Dialog.prompt({
            title: t("改群名"),
            label: t("群名"),
            value: conv.title,
            confirmText: t("保存"),
          });
          if (!name || !name.trim()) return;
          try {
            const data = await api.patch(`/api/chat/conversations/${conv.id}`, {
              title: name.trim(),
            });
            Object.assign(conv, data.conversation || {});
            paint();
            paintThreadHead();
            await loadConversations({ silent: true });
          } catch (err) {
            Toast.error(err?.message || t("改名失败"));
          }
        })();
      } else if (act === "kick") {
        const uid = Number(btn.dataset.uid);
        void (async () => {
          const ok = await Dialog.confirm({
            title: t("移出成员"),
            message: t("他会收到一条通知，之前的消息仍留在群里。"),
            confirmText: t("移出"),
            danger: true,
          });
          if (!ok) return;
          try {
            await api.del(`/api/chat/conversations/${conv.id}/members/${uid}`);
            Toast.success(t("已移出"));
            await loadConversations({ silent: true });
            const fresh = state.convs.find((c) => c.id === conv.id);
            if (fresh) {
              state.current = fresh;
              Object.assign(conv, fresh);
            }
            paint();
            paintThreadHead();
          } catch (err) {
            Toast.error(err?.message || t("操作失败"));
          }
        })();
      } else if (act === "leave") {
        void (async () => {
          const ok = await Dialog.confirm({
            title: t("退出群聊"),
            message: t("退出后不会收到这个群的消息，重新加入要群主再邀请一次。"),
            confirmText: t("退出"),
            danger: true,
          });
          if (!ok) return;
          try {
            await api.post(`/api/chat/conversations/${conv.id}/leave`, {});
            backdrop.remove();
            document.removeEventListener("keydown", onKey);
            state.current = null;
            state.messages = [];
            bodyEl.classList.remove("is-thread");
            history.replaceState(null, "", "#/chat");
            await loadConversations();
            paintThread();
            Toast.success(t("已退出群聊"));
          } catch (err) {
            Toast.error(err?.message || t("操作失败"));
          }
        })();
      }
    });
  }

  /* ---------------------------------------------------------------- */
  /* 事件绑定                                                          */
  /* ---------------------------------------------------------------- */
  root.querySelector('[data-act="new-chat"]').addEventListener("click", () => void newDirectChat());
  root.querySelector('[data-act="new-group"]').addEventListener("click", () => void newGroupChat());

  root.querySelector("[data-filter]").addEventListener("input", (event) => {
    state.query = event.target.value;
    paintList();
  });

  listEl.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-conv]");
    if (!btn) return;
    void openConversation(btn.dataset.conv);
  });

  listEl.addEventListener("contextmenu", (event) => {
    const row = event.target.closest("[data-conv]");
    if (!row) return;
    event.preventDefault();
    const conv = state.convs.find((c) => c.id === Number(row.dataset.conv));
    if (!conv) return;
    openConvMenu(conv, event.clientX, event.clientY);
  });

  mainEl.addEventListener("contextmenu", (event) => {
    // 表情面板里的表情：右键直接删 / 直接发
    const item = event.target.closest("[data-sticker-id]");
    if (item) {
      event.preventDefault();
      const id = Number(item.dataset.stickerId);
      const sticker = state.stickers.find((s) => s.id === id);
      openMenu({
        x: event.clientX,
        y: event.clientY,
        items: [
          {
            label: t("发送这个表情"),
            icon: "i-send",
            onClick: () => {
              toggleStickers(false);
              void send("sticker", { stickerId: id });
            },
          },
          {
            label: t("删除表情"),
            icon: "i-trash",
            danger: true,
            onClick: () => deleteSticker(id, sticker?.name),
          },
        ],
      });
      return;
    }
    // 消息（含表情气泡）：右键等同点「更多」
    const msgEl = event.target.closest(".msg[data-mid]");
    if (!msgEl || msgEl.classList.contains("msg--system")) return;
    const msg = messageById(msgEl.dataset.mid);
    if (!msg) return;
    event.preventDefault();
    openMessageMenuAt(msg, event.clientX, event.clientY);
  });

  mainEl.addEventListener("click", (event) => {
    // 图片/表情点击放大：放在最前面，放大层自己的关闭按钮单独处理
    const zoom = event.target.closest('[data-act="zoom"]');
    if (zoom) {
      openLightbox(zoom.dataset.src);
      return;
    }
    const btn = event.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "more") {
      const msg = messageById(btn.dataset.mid);
      if (msg) openMessageMenu(btn, msg);
      return;
    }
    if (act === "download") {
      const url = `/api/chat/messages/${btn.dataset.mid}/files/${btn.dataset.index}`;
      const a = document.createElement("a");
      a.href = url;
      a.download = btn.dataset.name || "";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }
    if (act === "save") {
      const msg = messageById(btn.dataset.mid);
      if (msg) void saveFiles(msg);
      return;
    }
    if (act === "back") {
      bodyEl.classList.remove("is-thread");
      state.current = null;
      state.messages = [];
      history.replaceState(null, "", "#/chat");
      paintThread();
      return;
    }
    if (act === "older") {
      void loadOlder();
      return;
    }
    if (act === "send") {
      void sendText();
      return;
    }
    if (act === "pick-file") {
      void pickFilesToSend();
      return;
    }
    if (act === "pick-image") {
      mainEl.querySelector("[data-image-input]").click();
      return;
    }
    if (act === "open-stickers") {
      toggleStickers();
      return;
    }
    if (act === "sticker-add") {
      mainEl.querySelector("[data-sticker-input]").click();
      return;
    }
    if (act === "send-sticker") {
      const id = Number(btn.dataset.id);
      toggleStickers(false);
      void send("sticker", { stickerId: id });
      return;
    }
    if (act === "del-sticker") {
      void deleteSticker(Number(btn.dataset.id));
      return;
    }
    if (act === "group-info") {
      void showGroupInfo();
      return;
    }
  });

  mainEl.addEventListener("change", (event) => {
    if (event.target.matches("[data-image-input]")) {
      const file = event.target.files?.[0];
      event.target.value = "";
      void uploadImage(file);
      return;
    }
    if (event.target.matches("[data-sticker-input]")) {
      const files = [...(event.target.files || [])];
      event.target.value = "";
      void addStickers(files);
    }
  });

  mainEl.addEventListener("keydown", (event) => {
    if (!event.target.matches("[data-input]")) return;
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void sendText();
    }
  });

  mainEl.addEventListener(
    "scroll",
    (event) => {
      if (!event.target.matches("[data-feed]")) return;
      if (event.target.scrollTop < 40 && state.hasMore) void loadOlder();
    },
    true
  );

  async function addStickers(files) {
    if (!files.length) return;
    // 不做「现有 + 本次数量 > 上限」的预判：服务端按哈希去重，重复上传
    // 不占名额，客户端一猜反而会把本来能收的图挡掉；让后端给准确结论。
    let added = 0;
    let dup = 0;
    for (const file of files) {
      const isGif = file.type === "image/gif" || /\.gif$/i.test(file.name);
      // 动图上限单独一套（服务端一样按类型判断），这里只提前挡一下，省一次上传
      const limit = isGif ? state.stickerGifMaxMb : state.stickerMaxMb;
      if (!file.type.startsWith("image/")) continue;
      if (file.size > limit * 1024 * 1024) {
        Toast.error(t("「{name}」超过 {limit} MB", { name: file.name, limit }));
        continue;
      }
      try {
        const data = await api.raw("/api/chat/stickers", file, "POST");
        if (data?.created === false) dup += 1;
        else added += 1;
      } catch (err) {
        Toast.error(err?.message || t("「{name}」添加失败", { name: file.name }));
        break;
      }
    }
    await loadStickers();
    if (added) Toast.success(t("已添加 {n} 张表情", { n: added }));
    if (dup) Toast.info(t("有 {n} 张已经在你的表情里了", { n: dup }));
  }

  /* ---------------------------------------------------------------- */
  /* 图片放大                                                          */
  /* ---------------------------------------------------------------- */
  function openLightbox(src) {
    if (!src) return;
    closeLightbox();
    const box = document.createElement("div");
    box.className = "chat-lightbox";
    box.innerHTML = H`<img src="${escapeHtml(src)}" alt="">
      <button class="chat-lightbox__close" type="button" aria-label="${t("关闭")}">
        <svg class="icon"><use href="#i-close"/></svg>
      </button>`;
    box.addEventListener("click", (event) => {
      if (event.target === box || event.target.closest("[aria-label]")) closeLightbox();
    });
    document.body.appendChild(box);
    state.lightbox = box;
    const onKey = (event) => {
      if (event.key === "Escape") closeLightbox();
    };
    document.addEventListener("keydown", onKey, { once: true });
  }

  function closeLightbox() {
    state.lightbox?.remove();
    state.lightbox = null;
  }

  /* ---------------------------------------------------------------- */
  /* 启动                                                              */
  /* ---------------------------------------------------------------- */
  void (async () => {
    await loadConversations({ silent: true });
    if (!root.isConnected) return;
    if (sub) await openConversation(sub, { fromRoute: true });
    state.timer = window.setInterval(() => {
      if (!root.isConnected) {
        window.clearInterval(state.timer);
        window.clearInterval(state.listTimer);
        return;
      }
      void pollMessages();
    }, POLL_MS);
    state.listTimer = window.setInterval(() => {
      if (!root.isConnected) {
        window.clearInterval(state.listTimer);
        return;
      }
      if (!document.hidden) void loadConversations({ silent: true });
    }, LIST_POLL_MS);
  })();
}
