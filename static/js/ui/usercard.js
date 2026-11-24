/**
 * 用户名片浮层（群聊/聊天里鼠标停在头像上 2.5 秒出现）
 *
 * 数据走 /api/users/<id>/card，服务端按对方设定的可见性过滤，
 * 名片只给摘要（前 3 个社交账号），详情点进个人主页看。
 */
import { api, escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";
import { avatarMarkup } from "./avatar.js";
import { Dialog } from "./dialog.js";
import { Toast } from "./toast.js";

let currentNode = null;
let currentAnchor = null;
let hideTimer = 0;
let requestToken = 0;

export const LINK_LABELS = {
  email: "邮箱",
  qq: "QQ",
  wechat: "微信",
  x: "X",
  line: "Line",
  google: "Google",
  discord: "Discord",
  youtube: "YouTube",
  netease: "网易云音乐",
  spotify: "Spotify",
  website: "个人网站",
  weibo: "微博",
  github: "GitHub",
  steam: "Steam",
  bilibili: "Bilibili",
  douyin: "抖音 / TikTok",
  facebook: "Facebook",
  instagram: "Instagram",
};

export const LINK_ICONS = {
  email: "i-mail",
  qq: "i-chat",
  wechat: "i-chat",
  x: "i-arrow-up-right",
  line: "i-chat",
  google: "i-search",
  discord: "i-users",
  youtube: "i-play",
  netease: "i-music",
  spotify: "i-music",
  website: "i-link",
  weibo: "i-arrow-up-right",
  github: "i-github",
  steam: "i-play",
  bilibili: "i-video",
  douyin: "i-video",
  facebook: "i-users",
  instagram: "i-image",
};

export function closeUserCard() {
  window.clearTimeout(hideTimer);
  currentNode?.remove();
  currentNode = null;
  currentAnchor = null;
}

/** 鼠标进入头像：2.5 秒后出名片；离开就取消（避免划过去一片全弹出来） */
export function hoverUserCard(userId, anchor, { delay = 2500 } = {}) {
  if (!userId || !anchor) return;
  closeUserCard();
  hideTimer = window.setTimeout(() => {
    void showUserCard(userId, anchor);
  }, delay);
}

export function cancelHoverCard() {
  window.clearTimeout(hideTimer);
}

/**
 * 鼠标离开头像：稍等一下再收。
 *
 * 只清计时器的话，已经弹出来的名片会一直挂在屏幕上（要再划进去再出来才消失）。
 * 留 300ms 是给「从头像挪到名片上」留路：名片自己的 mouseenter 会把这个计时器清掉。
 */
export function scheduleCloseUserCard(delay = 300) {
  window.clearTimeout(hideTimer);
  hideTimer = window.setTimeout(closeUserCard, delay);
}

function place(node, anchor) {
  const rect = anchor.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const gap = 10;
  let left = rect.right + gap;
  if (left + box.width > window.innerWidth - 8) left = Math.max(8, rect.left - box.width - gap);
  let top = rect.top + rect.height / 2 - box.height / 2;
  top = Math.max(8, Math.min(top, window.innerHeight - box.height - 8));
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
}

export async function showUserCard(userId, anchor) {
  const token = ++requestToken;
  let card;
  try {
    const data = await api.get(`/api/users/${userId}/card`);
    card = data.card;
  } catch {
    return; // 名片拉不到就当没有，不打扰
  }
  if (token !== requestToken || !anchor.isConnected) return;
  closeUserCard();
  currentAnchor = anchor;

  const relation = card.relation || "none";
  const ops = [];
  if (relation === "self") {
    ops.push(`<button class="btn btn--subtle" type="button" data-card-act="profile">${t("我的主页")}</button>`);
  } else if (relation === "friends") {
    ops.push(`<button class="btn btn--accent" type="button" data-card-act="chat">${t("发消息")}</button>`);
  } else if (relation === "pending_out") {
    ops.push(`<button class="btn btn--subtle" type="button" disabled>${t("已申请")}</button>`);
  } else if (relation === "pending_in") {
    ops.push(`<button class="btn btn--accent" type="button" data-card-act="incoming">${t("去处理")}</button>`);
  } else if (relation === "blocked") {
    ops.push(`<span class="chip chip--danger">${t("已拉黑")}</span>`);
  } else {
    ops.push(`<button class="btn btn--accent" type="button" data-card-act="add">${t("加好友")}</button>`);
  }
  if (relation !== "self") {
    ops.push(`<button class="btn btn--subtle" type="button" data-card-act="profile">${t("查看主页")}</button>`);
  }

  const node = document.createElement("div");
  node.className = "usercard";
  node.setAttribute("role", "dialog");
  const name = card.alias || card.displayName;
  const links = (card.links || [])
    .map(
      (link) => H`
      <div class="usercard__link">
        <svg class="icon icon--s"><use href="#${LINK_ICONS[link.key] || "i-link"}"/></svg>
        <span class="usercard__link-label">${t(LINK_LABELS[link.key] || link.key)}</span>
        <span class="usercard__link-value">${escapeHtml(link.value)}</span>
      </div>`
    )
    .join("");

  node.innerHTML = H`
    <div class="usercard__head">
      <span class="usercard__avatar">${avatarMarkup(card)}</span>
      <div class="usercard__names">
        <div class="usercard__name">${escapeHtml(name)}</div>
        <div class="usercard__meta">@${escapeHtml(card.username || "")}${
          card.alias ? H` · ${escapeHtml(card.displayName)}` : ""
        }</div>
      </div>
    </div>
    ${card.bio ? H`<div class="usercard__bio">${escapeHtml(card.bio)}</div>` : ""}
    ${links ? H`<div class="usercard__links">${links}</div>` : ""}
    <div class="usercard__ops">${ops.join("")}</div>`;

  document.body.appendChild(node);
  place(node, anchor);
  currentNode = node;

  node.addEventListener("mouseenter", () => window.clearTimeout(hideTimer));
  node.addEventListener("mouseleave", () => {
    hideTimer = window.setTimeout(closeUserCard, 260);
  });
  node.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-card-act]");
    if (!btn) return;
    const act = btn.dataset.cardAct;
    if (act === "profile") {
      closeUserCard();
      location.hash = `#/profile/${userId}`;
      return;
    }
    if (act === "incoming") {
      closeUserCard();
      location.hash = "#/friends";
      return;
    }
    if (act === "chat") {
      closeUserCard();
      void openChat(userId);
      return;
    }
    if (act === "add") {
      closeUserCard();
      void addFriend(userId, card.displayName);
    }
  });
}

async function openChat(userId) {
  try {
    const data = await api.post("/api/chat/conversations/direct", { userId: Number(userId) });
    const conv = data?.conversation;
    location.hash = conv ? `#/chat/${conv.id}` : "#/chat";
  } catch (err) {
    Toast.error(err?.message || t("打不开这个会话"));
  }
}

async function addFriend(userId, name = "") {
  const message = await Dialog.prompt({
    title: t("发送好友申请"),
    message: t("可以附一句说明，让对方知道你是谁（可留空）"),
    label: t("附言"),
    placeholder: t("例如：我是群里的小明"),
    confirmText: t("发送申请"),
  });
  if (message === null) return;
  try {
    await api.post("/api/friends/requests", { userId: Number(userId), message });
    Toast.success(t("好友申请已发送"));
  } catch (err) {
    Toast.error(err?.message || t("发送失败"));
  }
  void name;
}

// 点空白 / 滚动 / Esc 都收起
document.addEventListener(
  "mousedown",
  (event) => {
    if (!currentNode) return;
    if (currentNode.contains(event.target) || currentAnchor?.contains(event.target)) return;
    closeUserCard();
  },
  true
);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeUserCard();
});
window.addEventListener("scroll", closeUserCard, true);
window.addEventListener("resize", closeUserCard);
