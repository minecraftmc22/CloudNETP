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
import { hoverUserCard, scheduleCloseUserCard } from "../ui/usercard.js";
import {
  hideStickerPreview,
  hideStickerPreviewIfStale,
  hoverStickerPreview,
  scheduleHideStickerPreview,
} from "../ui/sticker-preview.js";
import { openGroupPanel } from "../ui/group-panel.js";
import { openMemberMenu } from "../ui/member-menu.js";

const POLL_MS = 3000;
const LIST_POLL_MS = 20000;

const KINDS = {
  image: "i-image",
  file: "i-file",
  sticker: "i-spark",
};

// 文档级监听（点面板外面收起表情）跟着视图走：每次进聊天页换一个新控制器
let docView = null;

export function renderChat(root, { sub = "" } = {}) {
  // 上一份文档级监听先撤掉，否则点一下会命中一串残留监听（它们操作的是已卸载的旧 DOM）
  docView?.abort();
  docView = typeof AbortController === "function" ? new AbortController() : null;
  hideStickerPreview();

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
    // 正在引用的消息（右键「引用」后挂在输入框上方）
    reply: null,
    // 群聊 @ 提及：草稿里已点的成员 id / 是否 @所有人（切会话或发送后清空）
    mentions: new Set(),
    mentionAll: false,
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
      // 群头像：设过就显示图片，没设还是「一群人」的默认图标
      if (conv.avatarUrl) {
        return `<span class="chat-ava chat-ava--group">${avatarMarkup({
          avatarUrl: conv.avatarUrl,
          displayName: conv.title,
          username: conv.title,
        })}</span>`;
      }
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
          // 别人刚把我禁言 / 解禁：输入框状态跟着走
          paintComposerState();
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
  /** 拉一次当前会话详情：群资料、成员、禁言状态都以服务端为准 */
  async function syncCurrent() {
    const id = state.current?.id;
    if (!id) return;
    try {
      const data = await api.get(`/api/chat/conversations/${id}`);
      if (state.current?.id !== id) return;
      state.current = data.conversation;
      state.convs = state.convs.map((c) => (c.id === id ? data.conversation : c));
      paintThreadHead();
      paintComposerState();
      paintList();
    } catch {
      /* 失败就等下一轮轮询 */
    }
  }

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

  /** 这条 @ 里包含的名字集合（当前会话成员视角） */
  function mentionTokens(msg) {
    const names = new Set();
    if (state.current?.kind !== "group") return names;
    if (msg.mentionAll) names.add(t("所有人"));
    for (const uid of msg.mentions || []) {
      const member = state.current?.members?.find((m) => m.id === uid);
      if (member) {
        names.add(mentionName(member));
        if (member.displayName) names.add(member.displayName);
        if (member.username) names.add(member.username);
      }
    }
    names.delete("");
    return names;
  }

  /** 文本里的 @提及 包成高亮 span；@前必须是行首或空白，邮箱不算 */
  function textWithMentions(msg) {
    const text = msg.body || "";
    const names = mentionTokens(msg);
    if (!names.size) return escapeHtml(text);
    const parts = [];
    let last = 0;
    for (const match of text.matchAll(/@([^\s@，。！？,!?]{1,32})/g)) {
      const idx = match.index;
      const name = match[1];
      if (names.has(name) && (idx === 0 || /[\s\n]/.test(text[idx - 1]))) {
        parts.push(escapeHtml(text.slice(last, idx)));
        parts.push(`<span class="msg__mention">@${escapeHtml(name)}</span>`);
        last = idx + match[0].length;
      }
    }
    parts.push(escapeHtml(text.slice(last)));
    return parts.join("");
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
    return H`<div class="msg__text">${textWithMentions(msg)}</div>`;
  }

  function msgHtml(msg) {
    if (msg.kind === "system") {
      return H`<div class="msg msg--system" data-mid="${msg.id}">
          <span class="msg__system">${escapeHtml(msg.body || "")}</span>
        </div>`;
    }
    const mine = msg.sender?.id === me.id;
    const sender = msg.sender || {};
    // 我被 @ 到（含 @所有人）：气泡加高亮底色
    const mentionMe =
      state.current?.kind === "group" &&
      (msg.mentionAll || (msg.mentions || []).includes(me.id));
    // 备注优先：群里我给他起的名字 > 好友备注（后端已在 members 里合并好）
    const member = state.current?.members?.find((m) => m.id === sender.id);
    const senderName = member?.alias || sender.displayName || "";
    return H`
      <div class="msg ${mine ? "msg--mine" : ""}" data-mid="${msg.id}" data-kind="${msg.kind}">
        <span class="msg__avatar" data-uid="${sender.id || 0}" title="${escapeHtml(senderName)}">${avatarMarkup(
          member ? { ...sender, displayName: senderName } : sender
        )}</span>
        <div class="msg__main">
          <div class="msg__meta">
            ${state.current?.kind === "group" && !mine ? H`<span class="msg__name" data-uid="${sender.id || 0}">${escapeHtml(senderName)}</span>` : ""}
            <span class="msg__time">${timeText(msg.createdAt)}</span>
            ${msg.forwardedFrom ? H`<span class="msg__tag">${t("转发")}</span>` : ""}
          </div>
          <div class="msg__bubble ${msg.kind === "text" ? "" : "msg__bubble--plain"} ${
            mentionMe ? "msg__bubble--mention" : ""
          }">${quoteHtml(msg)}${bodyHtml(
            msg,
            mine
          )}</div>
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
        <div class="chat-reply" data-reply-bar hidden>
          <svg class="icon icon--s chat-reply__icon"><use href="#i-reply"/></svg>
          <span class="chat-reply__main">
            <span class="chat-reply__name" data-reply-name></span>
            <span class="chat-reply__text" data-reply-text></span>
          </span>
          <button class="icon-btn chat-reply__close" type="button" data-act="cancel-reply" title="${t("取消引用")}">
            <svg class="icon icon--s"><use href="#i-close"/></svg>
          </button>
        </div>
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
        <div class="chat-mute" data-mute-bar hidden>
          <svg class="icon icon--s"><use href="#i-mute"/></svg>
          <span data-mute-text></span>
        </div>
        <div class="chat-compose__bar">
          <div class="chat-mention" data-mention hidden></div>
          <div class="textarea chat-compose__input">
            <textarea data-input rows="1" maxlength="2000" placeholder="${t("说点什么…（Enter 发送，Shift+Enter 换行）")}"></textarea>
            <div class="chat-compose__tools">
              ${
                conv.kind === "group"
                  ? H`<button class="chat-tool" type="button" data-act="mention" title="${t("@ 群成员")}">
                      <svg class="icon"><use href="#i-user"/></svg>
                    </button>`
                  : ""
              }
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
    paintReply();
    if (conv.kind === "group") {
      // 群名跟着消息里的系统提示变，重新拉一次就同步了
    }
    const input = mainEl.querySelector("[data-input]");
    const fit = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(168, input.scrollHeight)}px`;
    };
    input.addEventListener("input", () => {
      fit();
      maybeOpenMention();
    });
    fit();
    paintComposerState();
  }

  /** 输入框可用状态：被拉黑 / 被禁言时锁住，并把原因写在输入框里 */
  function paintComposerState() {
    const conv = state.current;
    const input = mainEl.querySelector("[data-input]");
    if (!conv || !input) return;
    const bar = mainEl.querySelector("[data-mute-bar]");
    const tools = mainEl.querySelectorAll(".chat-compose__tools .chat-tool");
    const sendBtn = mainEl.querySelector('[data-act="send"]');
    const lock = (on) => {
      input.disabled = on;
      tools.forEach((btn) => {
        btn.disabled = on;
      });
      if (sendBtn) sendBtn.disabled = on;
    };

    if (conv.canSend === false) {
      // 被拉黑 / 拉黑了对方：发不出去，就别让人把字打完再被拒
      lock(true);
      input.placeholder = conv.blockedByPeer
        ? t("对方已拒收你的消息")
        : t("你已把对方拉黑，无法发送消息");
      if (bar) bar.hidden = true;
      return;
    }

    // 禁言：myMuteUntil 只在还有效时下发，过期后重拉会话就自动解锁
    const until = conv.myMuteUntil ? new Date(conv.myMuteUntil).getTime() : 0;
    if (until > Date.now()) {
      lock(true);
      input.placeholder = t("你已被禁言，暂时不能发言");
      if (bar) {
        bar.hidden = false;
        const text = bar.querySelector("[data-mute-text]");
        if (text) text.textContent = t("你已被禁言，{time} 后才能继续发言", {
          time: timeText(conv.myMuteUntil),
        });
      }
      return;
    }

    lock(false);
    input.placeholder = t("说点什么…（Enter 发送，Shift+Enter 换行）");
    if (bar) bar.hidden = true;
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
    // 引用只在当时那个会话里有意义，换会话就丢掉（否则会把引用挂到别的会话里）
    state.reply = null;
    // @ 提及同理：成员名单是按会话算的
    state.mentions.clear();
    state.mentionAll = false;
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

  async function send(kind, payload, body = "", extra = {}) {
    if (!state.current || state.busy) return;
    state.busy = true;
    // 引用挂在「下一条要发的消息」上：发出去就清掉，避免手滑连发两条都带着引用
    const quoted = state.reply;
    try {
      const data = await api.post(`/api/chat/conversations/${state.current.id}/messages`, {
        kind,
        body,
        payload,
        replyTo: quoted ? quoted.id : null,
        ...extra,
      });
      const msg = data.message;
      state.messages.push(msg);
      state.lastId = Math.max(state.lastId, msg.id);
      if (quoted && state.reply === quoted) cancelReply();
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
      // 刚被禁言：马上同步一次会话把输入框锁上，别等下一轮列表轮询
      if (String(err?.message || "").includes("禁言")) void syncCurrent();
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
    closeMentionPicker();
    // @ 提及随文本一起发；草稿里的名单发完就清
    const extra =
      state.current?.kind === "group" && (state.mentions.size || state.mentionAll)
        ? { mentions: [...state.mentions], mentionAll: state.mentionAll }
        : {};
    state.mentions.clear();
    state.mentionAll = false;
    await send("text", null, text, extra);
  }

  /* ---------------------------------------------------------------- */
  /* 群聊 @ 提及                                                       */
  /* ---------------------------------------------------------------- */
  /** 成员在消息里显示的名字：我起的备注 > 昵称（与消息流一致） */
  function mentionName(member) {
    return member.alias || member.displayName || member.username || "";
  }

  function openMentionPicker() {
    const conv = state.current;
    const box = mainEl.querySelector("[data-mention]");
    if (!conv || conv.kind !== "group" || !box) return;
    const others = (conv.members || []).filter((m) => m.id !== me.id);
    box.innerHTML = H`
      <button type="button" class="chat-mention__item" data-act="mention-all">
        <span class="chat-mention__name">@${t("所有人")}</span>
        <span class="chat-mention__hint">${t("提醒全部群成员")}</span>
      </button>
      ${others
        .map(
          (m) => H`<button type="button" class="chat-mention__item" data-act="mention-uid" data-uid="${m.id}">
            <span class="chat-mention__avatar">${avatarMarkup(m)}</span>
            <span class="chat-mention__name">${escapeHtml(mentionName(m))}</span>
          </button>`
        )
        .join("")}`;
    box.hidden = false;
  }

  function closeMentionPicker() {
    const box = mainEl.querySelector("[data-mention]");
    if (box) box.hidden = true;
  }

  /** 把光标前那个孤立的 @ 替换成 @名字（找不到就接在末尾） */
  function applyMention(name, uid) {
    const input = mainEl.querySelector("[data-input]");
    if (!input || input.disabled) return;
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret);
    const at = before.lastIndexOf("@");
    const lone = at >= 0 && (at === 0 || /[\s\n]/.test(before[at - 1]));
    const token = `@${name} `;
    if (lone) {
      input.value = before.slice(0, at) + token + input.value.slice(caret);
    } else {
      input.value = before + token + input.value.slice(caret);
    }
    const pos = (lone ? at : caret) + token.length;
    input.focus();
    input.setSelectionRange(pos, pos);
    input.dispatchEvent(new Event("input"));
    if (uid == null) {
      state.mentionAll = true;
    } else {
      state.mentionAll = false;
      state.mentions.add(Number(uid));
    }
  }

  /** 光标前是新打的孤立 @（前面是空白或行首）时弹出成员选择 */
  function maybeOpenMention() {
    const input = mainEl.querySelector("[data-input]");
    if (!input) return;
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret);
    if (
      before.endsWith("@") &&
      (before.length === 1 || /[\s\n]/.test(before.slice(-2, -1)))
    ) {
      return openMentionPicker();
    }
    closeMentionPicker();
  }

  /* ---------------------------------------------------------------- */
  /* 引用回复                                                          */
  /* ---------------------------------------------------------------- */
  /** 一条消息在引用里怎么显示（本地对象用，服务端会另给 reply.text） */
  function previewOf(msg) {
    if (!msg) return "";
    if (msg.kind === "text") return (msg.body || "").slice(0, 80);
    if (msg.kind === "image") return t("[图片]");
    if (msg.kind === "sticker") return t("[表情]");
    if (msg.kind === "file") {
      const nodes = (msg.payload || {}).nodes || [];
      return nodes.length === 1
        ? t("[文件] {name}", { name: nodes[0].name || "" })
        : t("[文件] {n} 项", { n: nodes.length });
    }
    return "";
  }

  function nameOf(uid, fallback = "") {
    if (uid && uid === me.id) return t("你");
    const member = state.current?.members?.find((m) => m.id === uid);
    return (member && member.alias) || fallback || "";
  }

  function startReply(msg) {
    if (!msg || msg.recalled) return;
    state.reply = msg;
    paintReply();
    const input = mainEl.querySelector("[data-input]");
    if (input && !input.disabled) input.focus();
  }

  function cancelReply() {
    state.reply = null;
    paintReply();
  }

  function paintReply() {
    const bar = mainEl.querySelector("[data-reply-bar]");
    if (!bar) return;
    const msg = state.reply;
    bar.hidden = !msg;
    if (!msg) return;
    bar.querySelector("[data-reply-name]").textContent = nameOf(msg.sender?.id, msg.sender?.displayName);
    bar.querySelector("[data-reply-text]").textContent = previewOf(msg);
  }

  /** 气泡里那块引用：点一下跳回原消息 */
  function quoteHtml(msg) {
    const quote = msg.reply;
    if (!quote) return "";
    return H`
      <button class="msg__quote" type="button" data-act="jump" data-target="${quote.id}">
        <span class="msg__quote-name">${escapeHtml(
          nameOf(quote.senderId, quote.senderName) || quote.senderName || ""
        )}</span>
        <span class="msg__quote-text">${escapeHtml(quote.text || "")}</span>
      </button>`;
  }

  function jumpToMessage(mid) {
    const el = [...mainEl.querySelectorAll(".msg[data-mid]")].find(
      (node) => Number(node.dataset.mid) === Number(mid)
    );
    if (!el) {
      Toast.info(t("这条消息在更早的聊天记录里，往上翻一翻"));
      return;
    }
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("is-flash");
    void el.offsetWidth;
    el.classList.add("is-flash");
    window.setTimeout(() => el.classList.remove("is-flash"), 1400);
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
    // 重画会换掉格子节点：浮层要是挂在这些旧节点上，收掉；还在排队的悬停不打断
    hideStickerPreviewIfStale();
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
    else hideStickerPreview();
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
    state.reply = null;
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
    // 引用放最前面：这是右键里最常点的一项
    if (!msg.recalled) {
      items.push({
        label: t("引用"),
        icon: "i-reply",
        onClick: () => startReply(msg),
      });
    }
    // 复制：文字复制正文，图片/表情复制图片本身，文件复制文件名
    if (!msg.recalled) {
      items.push({
        label: t("复制"),
        icon: "i-text",
        onClick: () => void copyMessage(msg),
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

  /** 图片转 PNG 再进剪贴板：GIF / WebP 直接写会被拒，画一帧出来 */
  async function toPngBlob(blob) {
    try {
      const bitmap = await createImageBitmap(blob);
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      bitmap.close?.();
      return await new Promise((resolve) => canvas.toBlob((out) => resolve(out), "image/png"));
    } catch {
      return null;
    }
  }

  async function copyMessageImage(msg) {
    const name = (msg.payload || {}).name || "";
    try {
      // 非安全上下文（http 内网）没有 write，直接走链接兜底
      if (!navigator.clipboard?.write || typeof ClipboardItem === "undefined") return false;
      const res = await fetch(`/api/chat/media/${name}`, { credentials: "include" });
      if (!res.ok) return false;
      const blob = await res.blob();
      const png = blob.type === "image/png" ? blob : await toPngBlob(blob);
      if (!png) return false;
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      return true;
    } catch {
      return false;
    }
  }

  async function copyMessage(msg) {
    const { copyText } = await import("../ui/clipboard.js");
    // 图片 / 表情：能进剪贴板就复制图，不能就给链接（http 内网常见）
    if (msg.kind === "image" || msg.kind === "sticker") {
      if (await copyMessageImage(msg)) {
        Toast.success(t("已复制图片"));
        return;
      }
      const name = (msg.payload || {}).name || "";
      const url = `${location.origin}/api/chat/media/${name}`;
      const okLink = await copyText(url);
      okLink ? Toast.success(t("已复制图片链接")) : Toast.error(t("复制失败"));
      return;
    }
    const text =
      msg.kind === "text"
        ? msg.body || ""
        : ((msg.payload || {}).nodes || [])
            .map((node) => node.name)
            .filter(Boolean)
            .join("\n");
    if (!text) {
      Toast.info(t("这条消息没有可以直接复制的内容"));
      return;
    }
    const ok = await copyText(text);
    ok ? Toast.success(t("已复制")) : Toast.error(t("复制失败"));
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

  /** 多选好友（邀请进群用）：返回 id 数组；取消返回 null */
  function pickFriendsMulti({ title, friends, confirmText = "邀请" }) {
    return new Promise((resolve) => {
      const chosen = new Set();
      const backdrop = document.createElement("div");
      backdrop.className = "picker-backdrop";
      backdrop.innerHTML = H`
        <div class="picker picker--friend" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-user-plus"/></svg><span>${title}</span>
          </div>
          <div class="picker__body friend-pick__list" data-list></div>
          <div class="picker__foot">
            <span class="muted" data-count></span>
            <span class="grow"></span>
            <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
            <button class="btn btn--accent" type="button" data-act="ok" disabled>${t(confirmText)}</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const listEl = backdrop.querySelector("[data-list]");
      const okBtn = backdrop.querySelector('[data-act="ok"]');
      const countEl = backdrop.querySelector("[data-count]");
      listEl.innerHTML = friends
        .map(
          (f) => H`
        <button class="friend-pick__row" type="button" data-id="${f.id}">
          <span class="friend-pick__avatar">${avatarMarkup(f)}</span>
          <span class="friend-pick__main">
            <span class="friend-pick__name">${escapeHtml(f.alias || f.displayName)}</span>
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
      const sync = () => {
        okBtn.disabled = !chosen.size;
        countEl.textContent = chosen.size ? t("已选 {n} 人", { n: chosen.size }) : "";
      };
      listEl.addEventListener("click", (event) => {
        const row = event.target.closest("[data-id]");
        if (!row) return;
        const id = Number(row.dataset.id);
        if (chosen.has(id)) chosen.delete(id);
        else chosen.add(id);
        row.classList.toggle("is-active", chosen.has(id));
        sync();
      });
      okBtn.addEventListener("click", () => close([...chosen]));
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
    await openGroupPanel(conv.id, {
      onChanged: async (info) => {
        if (info?.left) {
          state.current = null;
          state.messages = [];
          bodyEl.classList.remove("is-thread");
          history.replaceState(null, "", "#/chat");
          await loadConversations();
          paintThread();
          return;
        }
        await loadConversations({ silent: true });
        const fresh = state.convs.find((c) => c.id === conv.id);
        if (fresh) {
          state.current = fresh;
          paintThreadHead();
        }
        // 群名/头像变了，列表与标题都得跟着换
        paintList();
      },
      onInvite: () => void inviteToGroup(conv),
    });
  }

  /** 邀请好友进群：复用「选好友」面板（只能选自己的好友，与建群同一套规则） */
  async function inviteToGroup(conv) {
    const friends = await ensureFriends();
    if (!friends.length) {
      Toast.info(t("先加几个好友才能邀请"));
      return;
    }
    const ids = await pickFriendsMulti({ title: t("邀请好友进群"), friends });
    if (!ids?.length) return;
    try {
      await api.post(`/api/chat/conversations/${conv.id}/members`, { userIds: ids });
      Toast.success(t("已邀请 {n} 位好友", { n: ids.length }));
      await loadConversations({ silent: true });
      const fresh = state.convs.find((c) => c.id === conv.id);
      if (fresh) {
        state.current = fresh;
        paintThreadHead();
      }
    } catch (err) {
      Toast.error(err?.message || t("邀请失败"));
    }
  }

  /** 加入群聊：搜公开群 / 贴群号 / 贴邀请链接，一个框全接住 */
  async function openJoinGroup() {
    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    const close = () => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (event) => {
      if (event.key === "Escape") close();
    };
    backdrop.innerHTML = H`
      <div class="picker picker--narrow" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-users"/></svg><span>${t("加入群聊")}</span>
        </div>
        <div class="picker__body">
          <div class="searchbox">
            <span class="textbox">
              <svg class="icon icon--s"><use href="#i-search"/></svg>
              <input type="search" data-gq placeholder="${t("搜索公开群，或输入群号")}" autocomplete="off">
            </span>
            <button class="btn" type="button" data-act="search">${t("搜索")}</button>
          </div>
          <p class="muted">${t("私密群搜不到，让群主或管理员发邀请链接给你；点链接会自动进群。")}</p>
          <div data-glist></div>
        </div>
        <div class="picker__foot">
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="close">${t("关闭")}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    document.addEventListener("keydown", onKey);

    const input = backdrop.querySelector("[data-gq]");
    const listBox = backdrop.querySelector("[data-glist]");

    const joinBy = async (body) => {
      try {
        const data = await api.post("/api/chat/groups/join", body);
        Toast.success(data.joined ? t("已加入群聊") : t("你已经在这个群里了"));
        close();
        await loadConversations();
        if (data.conversation?.id) void openConversation(data.conversation.id);
      } catch (err) {
        Toast.error(err?.message || t("加入失败"));
      }
    };

    const paintResults = (items) => {
      if (!items.length) {
        listBox.innerHTML = H`<div class="empty"><div class="empty__title">${t("没有找到公开群")}</div>
            <div class="empty__desc">${t("群号只对公开群有效；私密群需要邀请链接。")}</div></div>`;
        return;
      }
      listBox.innerHTML = H`<div class="friend-pick__list">
          ${items
            .map(
              (g) => H`
            <div class="friend-pick__row" data-gid="${g.id}">
              <span class="friend-pick__avatar">${avatarMarkup({
                avatarUrl: g.avatarUrl,
                displayName: g.title,
                username: g.title,
              })}</span>
              <span class="friend-pick__main">
                <span class="friend-pick__name">${escapeHtml(g.title)}</span>
                <span class="friend-pick__meta">${t("{n} 人", { n: g.memberCount })}${
                  g.groupNo ? H` · ${t("群号 {no}", { no: g.groupNo })}` : ""
                }</span>
                ${g.description ? H`<span class="friend-pick__meta">${escapeHtml(g.description)}</span>` : ""}
              </span>
              <button class="btn btn--accent" type="button" data-act="join" data-gid="${g.id}">${t("加入")}</button>
            </div>`
            )
            .join("")}
        </div>`;
    };

    const search = async () => {
      const q = input.value.trim();
      if (!q) return;
      // 整条邀请链接也能直接粘进来：从 URL 里把 token 抠出来
      const linkMatch = q.match(/\/g\/([A-Za-z0-9]{6,24})/);
      if (linkMatch) {
        await joinBy({ token: linkMatch[1] });
        return;
      }
      try {
        const data = await api.get(`/api/chat/groups/search?q=${encodeURIComponent(q)}`);
        const items = data.items || [];
        // 看起来像群号（纯数字）且没搜到：直接按群号加一次，省一步
        if (!items.length && /^\d{6,12}$/.test(q)) {
          await joinBy({ no: q });
          return;
        }
        paintResults(items);
      } catch (err) {
        Toast.error(err?.message || t("搜索失败"));
      }
    };

    backdrop.addEventListener("click", (event) => {
      if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
        close();
        return;
      }
      const btn = event.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "close") close();
      else if (btn.dataset.act === "search") void search();
      else if (btn.dataset.act === "join") void joinBy({ conversationId: Number(btn.dataset.gid) });
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target === input) {
        event.preventDefault();
        void search();
      }
    });
    input.focus();
  }

  /** 邀请链接（#/chat/join/<token>）：先给群摘要，确认了再进 */
  async function handleInviteRoute(token) {
    let group = null;
    try {
      const data = await api.get(`/api/public/groups/${encodeURIComponent(token)}`);
      group = data.group;
    } catch (err) {
      Toast.error(err?.message || t("邀请链接已失效"));
      history.replaceState(null, "", "#/chat");
      return;
    }
    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    const close = () => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      history.replaceState(null, "", "#/chat");
    };
    const onKey = (event) => {
      if (event.key === "Escape") close();
    };
    backdrop.innerHTML = H`
      <div class="picker picker--narrow" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-users"/></svg><span>${t("群邀请")}</span>
        </div>
        <div class="picker__body">
          <div class="group-invite">
            <span class="group-invite__avatar">${avatarMarkup({
              avatarUrl: group.avatarUrl,
              displayName: group.title,
              username: group.title,
            })}</span>
            <div class="group-invite__text">
              <div class="group-invite__name">${escapeHtml(group.title)}</div>
              <div class="muted">${t("{n} 人", { n: group.memberCount })} · ${
                group.isPublic ? t("公开群") : t("私密群")
              }</div>
              ${group.description ? H`<p class="muted">${escapeHtml(group.description)}</p>` : ""}
            </div>
          </div>
        </div>
        <div class="picker__foot">
          <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
          <span style="flex:1"></span>
          <button class="btn btn--accent" type="button" data-act="join">${t("加入群聊")}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", async (event) => {
      if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
        close();
        return;
      }
      const btn = event.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "cancel") {
        close();
        return;
      }
      if (btn.dataset.act === "join") {
        btn.disabled = true;
        try {
          const data = await api.post("/api/chat/groups/join", { token });
          Toast.success(data.joined ? t("已加入群聊") : t("你已经在这个群里了"));
          close();
          await loadConversations();
          if (data.conversation?.id) void openConversation(data.conversation.id);
        } catch (err) {
          Toast.error(err?.message || t("加入失败"));
          btn.disabled = false;
        }
      }
    });
  }

  /* ---------------------------------------------------------------- */
  /* 事件绑定                                                          */
  /* ---------------------------------------------------------------- */
  root.querySelector('[data-act="new-chat"]').addEventListener("click", () => void newDirectChat());
  root.querySelector('[data-act="new-group"]').addEventListener("click", (event) => {
    // 新建 / 加入二选一：同一个「+」里出菜单，省一个按钮
    const rect = event.currentTarget.getBoundingClientRect();
    openMenu({
      x: rect.left,
      y: rect.bottom + 8,
      anchor: event.currentTarget,
      align: "end",
      items: [
        { label: t("新建群聊"), icon: "i-plus", onClick: () => void newGroupChat() },
        { label: t("加入群聊"), icon: "i-users", onClick: () => void openJoinGroup() },
      ],
    });
  });

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

  /* 头像悬停 2.5 秒出名片；离开就取消（避免鼠标划过一片时连环弹） */
  mainEl.addEventListener("mouseover", (event) => {
    // 我的表情：停一下放大看清楚，浮层里直接发送 / 删除
    const stickerEl = event.target.closest(".sticker-item[data-sticker-id]");
    if (stickerEl) {
      if (stickerEl.contains(event.relatedTarget)) return;
      const sid = Number(stickerEl.dataset.stickerId);
      const sticker = state.stickers.find((s) => s.id === sid);
      if (sticker) {
        // 纯预览：发送就点表情本身，删除用右上角小叉
        hoverStickerPreview(sticker, stickerEl);
      }
      return;
    }
    const ava = event.target.closest(".msg__avatar[data-uid]");
    if (!ava || ava.contains(event.relatedTarget)) return;
    const uid = Number(ava.dataset.uid);
    if (!uid) return;
    hoverUserCard(uid, ava);
  });
  mainEl.addEventListener("mouseout", (event) => {
    const stickerEl = event.target.closest(".sticker-item[data-sticker-id]");
    if (stickerEl) {
      if (stickerEl.contains(event.relatedTarget)) return;
      scheduleHideStickerPreview();
      return;
    }
    const ava = event.target.closest(".msg__avatar[data-uid]");
    if (!ava || ava.contains(event.relatedTarget)) return;
    scheduleCloseUserCard();
  });

  /* 点面板外面就收起「我的表情」：比再去找开关顺手（跟项目里 .popover 的脾气一致） */
  const closeStickersOutside = (event) => {
    if (!state.stickerOpen) return;
    // 面板本身、顶部开关、预览浮层、确认框都算「里面」
    if (event.target.closest?.(".sticker-panel, [data-act=\"open-stickers\"], .sticker-preview, .dialog")) {
      return;
    }
    toggleStickers(false);
  };
  document.addEventListener("pointerdown", closeStickersOutside, {
    capture: true,
    ...(docView ? { signal: docView.signal } : {}),
  });

  /* @ 选择弹层：点外面就收起（.chat-mention 自身和 @ 按钮都算「里面」） */
  const closeMentionOutside = (event) => {
    if (event.target.closest?.(".chat-mention, [data-act=\"mention\"]")) return;
    closeMentionPicker();
  };
  document.addEventListener("pointerdown", closeMentionOutside, {
    capture: true,
    ...(docView ? { signal: docView.signal } : {}),
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
    // 群成员（头像 / 名字）：右键出成员管理（禁言 / 管理员 / 移出）
    const memberHit = event.target.closest(".msg__avatar[data-uid], .msg__name[data-uid]");
    if (memberHit && state.current?.kind === "group") {
      const uid = Number(memberHit.dataset.uid);
      const member = (state.current.members || []).find((m) => m.id === uid);
      if (member) {
        const shown = openMemberMenu({
          conv: state.current,
          member,
          x: event.clientX,
          y: event.clientY,
          onChanged: () => void loadConversations({ silent: true }),
        });
        if (shown) {
          event.preventDefault();
          return;
        }
      }
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
    // 点头像 = 进个人主页（悬停出名片只是预览）
    const avatarHit = event.target.closest(".msg__avatar[data-uid]");
    if (avatarHit && Number(avatarHit.dataset.uid)) {
      location.hash = `#/profile/${avatarHit.dataset.uid}`;
      return;
    }
    // 图片/表情点击放大：放在最前面，放大层自己的关闭按钮单独处理
    const zoom = event.target.closest('[data-act="zoom"]');
    if (zoom) {
      openLightbox(zoom.dataset.src);
      return;
    }
    const btn = event.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "cancel-reply") {
      cancelReply();
      return;
    }
    if (act === "jump") {
      jumpToMessage(btn.dataset.target);
      return;
    }
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
    // @ 选择弹层：点谁 @ 谁；「所有人」是特殊项
    if (act === "mention-all") {
      applyMention(t("所有人"), null);
      closeMentionPicker();
      return;
    }
    if (act === "mention-uid") {
      const uid = Number(btn.dataset.uid);
      const member = state.current?.members?.find((m) => m.id === uid);
      if (member) applyMention(mentionName(member), uid);
      closeMentionPicker();
      return;
    }
    if (act === "send") {
      void sendText();
      return;
    }
    if (act === "mention") {
      const box = mainEl.querySelector("[data-mention]");
      if (box && !box.hidden) closeMentionPicker();
      else openMentionPicker();
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

  /* Ctrl+V 直接粘图：截图工具复制完就能发，不用先存成文件 */
  mainEl.addEventListener("paste", (event) => {
    const input = event.target.closest?.("[data-input]");
    if (!input || input.disabled) return;
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    const file = [...(clipboard.items || [])]
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .find(Boolean);
    // 没有图就让浏览器照常粘文字
    if (!file) return;
    event.preventDefault();
    void uploadImage(file);
  });

  mainEl.addEventListener(
    "scroll",
    (event) => {
      // 表情格子滚走了，浮层还钉在原处就成了幽灵
      if (event.target.matches?.(".sticker-grid")) hideStickerPreview();
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
    // #/chat/join/<token>：邀请链接直接进「加群」确认框
    const invite = /^join\/([A-Za-z0-9]{6,24})$/.exec(sub || "");
    if (invite) await handleInviteRoute(invite[1]);
    else if (sub) await openConversation(sub, { fromRoute: true });
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
