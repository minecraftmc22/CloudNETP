/**
 * 选好友小弹窗：快传「发给用户」和聊天「发起单聊」共用。
 *
 * 只列已经通过申请的好友（数据来自 /api/chat/friends），带一个过滤框。
 * 返回好友卡片对象，取消返回 null。
 */
import { api, escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";
import { avatarMarkup } from "./avatar.js";

export function pickFriend({ title = "选择好友", confirmText = "确定" } = {}) {
  return new Promise((resolve) => {
    let friends = [];
    let closed = false;
    let picked = null;
    let query = "";

    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    backdrop.innerHTML = H`
      <div class="picker picker--friend" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-users"/></svg>
          <span>${title}</span>
        </div>
        <div class="friend-pick__search">
          <span class="textbox">
            <svg class="icon icon--s"><use href="#i-search"/></svg>
            <input type="search" data-q placeholder="${t("搜昵称或用户名")}" autocomplete="off">
          </span>
        </div>
        <div class="picker__body friend-pick__list" data-list></div>
        <div class="picker__foot">
          <span class="muted" data-hint>${t("点一下选中")}</span>
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
          <button class="btn btn--accent" type="button" data-act="ok" disabled>${confirmText}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);

    const listEl = backdrop.querySelector("[data-list]");
    const okBtn = backdrop.querySelector('[data-act="ok"]');
    const hintEl = backdrop.querySelector("[data-hint]");

    function close(value) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
      resolve(value);
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        close(null);
      } else if (event.key === "Enter" && picked) {
        event.preventDefault();
        close(picked);
      }
    }
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", (event) => {
      if (!event.composedPath().includes(backdrop.querySelector(".picker"))) close(null);
    });

    function paint() {
      const q = query.trim().toLowerCase();
      const rows = friends.filter((f) => {
        if (!q) return true;
        return (
          String(f.displayName || "").toLowerCase().includes(q) ||
          String(f.username || "").toLowerCase().includes(q)
        );
      });
      if (!rows.length) {
        listEl.innerHTML = H`
          <div class="empty" style="padding:26px 12px">
            <div class="empty__title">${friends.length ? t("没有匹配的好友") : t("还没有好友，先去「好友」里加一个吧")}</div>
          </div>`;
        return;
      }
      listEl.innerHTML = rows
        .map(
          (f) => H`
        <button class="friend-pick__row ${picked?.id === f.id ? "is-active" : ""}" type="button" data-id="${f.id}">
          <span class="friend-pick__avatar">${avatarMarkup(f)}</span>
          <span class="friend-pick__main">
            <span class="friend-pick__name">${escapeHtml(f.displayName)}</span>
            <span class="friend-pick__meta">@${escapeHtml(f.username)}</span>
          </span>
          <svg class="icon icon--s friend-pick__check"><use href="#i-check"/></svg>
        </button>`
        )
        .join("");
    }

    listEl.addEventListener("click", (event) => {
      const row = event.target.closest("[data-id]");
      if (!row) return;
      picked = friends.find((f) => String(f.id) === row.dataset.id) || null;
      okBtn.disabled = !picked;
      hintEl.textContent = picked ? `@${picked.username}` : t("点一下选中");
      paint();
    });

    backdrop.querySelector("[data-q]").addEventListener("input", (event) => {
      query = event.target.value;
      paint();
    });
    backdrop.querySelector('[data-act="ok"]').addEventListener("click", () => {
      if (picked) close(picked);
    });
    backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close(null));

    listEl.innerHTML = H`<div class="muted" style="padding:20px;text-align:center">${t("加载中…")}</div>`;
    api
      .get("/api/chat/friends")
      .then((data) => {
        if (closed) return;
        friends = data.friends || [];
        paint();
      })
      .catch(() => {
        if (closed) return;
        listEl.innerHTML = H`<div class="empty" style="padding:26px 12px"><div class="empty__title">${t("好友列表加载失败")}</div></div>`;
      });
  });
}
