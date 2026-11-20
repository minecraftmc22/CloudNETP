/**
 * 文件夹选择器（移动文件时选目标目录）
 * 用法：const targetId = await pickFolder({ excludeIds: [folder.id, ...] })
 *   excludeIds：本次正在移动的文件夹 id（含自身与子孙都不能当目标）。
 *   这些行**照样列出来、只置灰标注「正在移动」**——直接过滤掉的话，用户看到的就是
 *   「我明明选了 3 个，列表里怎么少了一个」，误以为丢了文件。
 * 返回值：null = 根目录（"我的文件"），undefined = 用户取消。
 */
import { api, escapeHtml } from "../api.js";

import { H, t } from "../i18n.js";
const ROOT = { id: null, name: "我的文件" };

export function pickFolder({
  title = "移动到",
  excludeIds = [],
  excludeId = null,
  confirmText = "移到这里",
} = {}) {
  const blocked = new Set(
    [...(Array.isArray(excludeIds) ? excludeIds : [excludeIds]), excludeId]
      .filter((id) => id !== null && id !== undefined && id !== "")
      .map((id) => Number(id))
  );
  return new Promise((resolve) => {
    let trail = [ROOT];
    let closed = false;

    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    backdrop.innerHTML = H`
      <div class="picker" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-folder"/></svg>
          <span>${title}</span>
        </div>
        <div class="picker__crumb" data-crumb></div>
        <div class="picker__body" data-list></div>
        <div class="picker__foot">
          <span class="muted" data-target></span>
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="cancel">取消</button>
          <button class="btn btn--accent" type="button" data-act="ok">${confirmText}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);

    const listEl = backdrop.querySelector("[data-list]");
    const crumbEl = backdrop.querySelector("[data-crumb]");
    const targetEl = backdrop.querySelector("[data-target]");
    const okBtn = backdrop.querySelector('[data-act="ok"]');

    function syncOk() {
      // 走不到被移动的文件夹里（行是禁用的），这里只是兜底：万一从面包屑回到它上面
      okBtn.disabled = blocked.has(Number(currentId()));
    }

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
        close(undefined);
      }
    }
    document.addEventListener("keydown", onKey);

    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) close(undefined);
    });
    backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close(undefined));
    backdrop.querySelector('[data-act="ok"]').addEventListener("click", () => close(currentId()));

    function currentId() {
      return trail[trail.length - 1].id;
    }

    function renderCrumb() {
      crumbEl.innerHTML = trail
        .map(
          (item, index) =>
            H`<button type="button" data-index="${index}">${item.name}</button>${
              index < trail.length - 1 ? '<svg class="icon" style="width:11px;height:11px"><use href="#i-chevron"/></svg>' : ""
            }`
        )
        .join("");
      crumbEl.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          trail = trail.slice(0, Number(btn.dataset.index) + 1);
          load();
        });
      });
      targetEl.textContent = t("目标：{path}", { path: trail.map((part) => part.name).join(" / ") });
      syncOk();
    }

    async function load() {
      renderCrumb();
      listEl.innerHTML =
        '<div class="picker__empty picker__loading"><span class="spinner"></span>加载中…</div>';
      let data;
      try {
        const params = new URLSearchParams({ sort: "name", order: "asc" });
        const current = currentId();
        if (current !== null && current !== undefined) params.set("parentId", String(current));
        data = await api.get(`/api/nodes?${params.toString()}`);
      } catch (err) {
        listEl.innerHTML = H`<div class="picker__empty">${err.message}</div>`;
        return;
      }
      const folders = (data.items || []).filter((item) => item.type === "folder");
      if (!folders.length) {
        listEl.innerHTML = H`<div class="picker__empty">${t("这里没有子文件夹，可直接移到这里")}</div>`;
        return;
      }
      // 一个都不隐藏：正在移动的那几行置灰标注，剩下的照常可点。
      // 全被置灰时也不清空列表 —— 至少让人看见「它们确实在这里」。
      listEl.innerHTML = folders
        .map((item) => {
          const isBlocked = blocked.has(Number(item.id));
          const safeName = escapeHtml(item.name);
          return H`
          <button class="picker__row${isBlocked ? " is-blocked" : ""}" type="button"
                  data-id="${item.id}" data-name="${safeName}"${isBlocked ? " disabled" : ""}>
            <svg class="icon"><use href="#i-folder"/></svg>
            <span>${safeName}</span>
            ${isBlocked
              ? H`<span class="picker__tag">${t("正在移动")}</span>`
              : '<svg class="icon icon--chev"><use href="#i-chevron"/></svg>'}
          </button>`;
        })
        .join("");
      listEl.querySelectorAll(".picker__row").forEach((row) => {
        if (row.disabled) return;
        row.addEventListener("click", () => {
          trail = [...trail, { id: Number(row.dataset.id), name: row.dataset.name }];
          load();
        });
      });
    }

    load();
  });
}
