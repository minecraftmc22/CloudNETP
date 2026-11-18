/**
 * 「发给用户」待接收内容的浏览弹层。
 *
 * 接收方在同意之前可以先看看里面是什么：双击文件夹进下一层，双击文件用通用预览器打开
 * （预览走 /api/nodes/{id}/preview —— 待接收的副本在 node_service 里对接收方放行）。
 * 触屏没有双击，改成单击即打开。
 *
 * 用法：openSendBrowser(send, { onAccept })
 * 返回 Promise<boolean>：true = 在弹层里点了「同意并存入」。
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { H, t } from "../i18n.js";
import { canPreview, openViewer } from "./viewer.js";

const isTouch = () => Boolean(window.matchMedia?.("(hover: none)")?.matches);

export function openSendBrowser(send, { onAccept } = {}) {
  return new Promise((resolve) => {
    let closed = false;
    let trail = [{ id: null, name: send?.name || t("快传") }];
    let busy = false;

    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    backdrop.innerHTML = H`
      <div class="picker send-browse" role="dialog" aria-modal="true" aria-label="${t("查看收到的文件")}">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-eye"/></svg>
          <span>${t("同意前先看看里面是什么")}</span>
          ${send?.peer ? H`<span class="muted">${t("来自 {name}", { name: escapeHtml(send.peer.displayName || send.peer.username || "") })}</span>` : ""}
        </div>
        <div class="picker__crumb" data-crumb></div>
        <div class="picker__body" data-list></div>
        <div class="picker__foot">
          <span class="muted" data-hint></span>
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="close">${t("关闭")}</button>
          <button class="btn btn--accent" type="button" data-act="accept">
            <svg class="icon icon--s"><use href="#i-check"/></svg>
            ${t("同意并存入")}
          </button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);

    const crumbEl = backdrop.querySelector("[data-crumb]");
    const listEl = backdrop.querySelector("[data-list]");
    const hintEl = backdrop.querySelector("[data-hint]");

    function close(result) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
      resolve(Boolean(result));
    }

    function onKey(event) {
      if (event.key === "Escape") {
        // 预览器开着时 Esc 只关预览器（它也监听 document，别一次关掉两层）
        if (document.querySelector(".viewer")) return;
        event.preventDefault();
        close(false);
      }
    }
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) close(false);
    });
    backdrop.querySelector('[data-act="close"]').addEventListener("click", () => close(false));
    backdrop.querySelector('[data-act="accept"]').addEventListener("click", () => {
      if (!onAccept) return close(false);
      close(true);
      onAccept();
    });

    function renderCrumb() {
      crumbEl.innerHTML = trail
        .map(
          (item, index) =>
            H`<button type="button" data-index="${index}">${escapeHtml(item.name)}</button>${
              index < trail.length - 1
                ? '<svg class="icon" style="width:11px;height:11px"><use href="#i-chevron"/></svg>'
                : ""
            }`
        )
        .join("");
      crumbEl.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          trail = trail.slice(0, Number(btn.dataset.index) + 1);
          load();
        });
      });
    }

    function openItem(item) {
      if (item.type === "folder") {
        trail = [...trail, { id: item.id, name: item.name }];
        load();
        return;
      }
      if (!canPreview(item)) {
        hintEl.textContent = t("这种类型不支持在线预览，同意后可以在网盘里下载");
        return;
      }
      openViewer(item);
    }

    async function load() {
      if (busy) return;
      busy = true;
      renderCrumb();
      listEl.innerHTML =
        '<div class="picker__empty picker__loading"><span class="spinner"></span>加载中…</div>';
      const current = trail[trail.length - 1].id;
      try {
        const params = current === null ? "" : `?parentId=${current}`;
        const data = await api.get(`/api/quick/sends/${send.id}/items${params}`);
        const items = data.items || [];
        // 面包屑以服务端为准：从列表点进来时只知道名字，路径要它补
        trail = (data.breadcrumb || []).map((part) => ({ id: part.id, name: part.name }));
        if (!trail.length) trail = [{ id: null, name: send?.name || t("快传") }];
        renderCrumb();
        hintEl.textContent = t("双击文件夹进去看看，双击文件可以直接预览");
        listEl.innerHTML = items.length
          ? items
              .map(
                (item) => H`
            <button class="send-row" type="button" data-id="${item.id}" data-type="${item.type}">
              <svg class="icon"><use href="#${item.icon || "i-file"}"/></svg>
              <span class="send-row__main">
                <span class="send-row__name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
                <span class="send-row__meta">${
                  item.type === "folder"
                    ? t("{n} 项", { n: item.childCount ?? 0 })
                    : formatBytes(item.size)
                }</span>
              </span>
              <svg class="icon icon--chev"><use href="#${item.type === "folder" ? "i-chevron" : "i-eye"}"/></svg>
            </button>`
              )
              .join("")
          : H`<div class="picker__empty">${t("这里是空的")}</div>`;

        listEl.querySelectorAll(".send-row").forEach((row) => {
          const item = items.find((node) => node.id === Number(row.dataset.id));
          if (!item) return;
          row.addEventListener("dblclick", () => openItem(item));
          // 触屏双击不可靠：手机上点一下就等于打开
          row.addEventListener("click", () => {
            if (isTouch()) openItem(item);
          });
        });
      } catch (err) {
        listEl.innerHTML = H`<div class="picker__empty">${escapeHtml(err.message)}</div>`;
      } finally {
        busy = false;
      }
    }

    load();
  });
}
