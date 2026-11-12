/**
 * 文件夹选择器（移动文件时选目标目录）
 * 用法：const targetId = await pickFolder({ excludeId: node.id })
 * 返回值：null = 根目录（"我的文件"），undefined = 用户取消。
 */
import { api } from "../api.js";

import { H } from "../i18n.js";
const ROOT = { id: null, name: "我的文件" };

export function pickFolder({ title = "移动到", excludeId = null, confirmText = "移到这里" } = {}) {
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
      targetEl.textContent = `目标：${trail.map((t) => t.name).join(" / ")}`;
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
      const folders = (data.items || []).filter(
        (item) => item.type === "folder" && item.id !== excludeId
      );
      if (!folders.length) {
        listEl.innerHTML = '<div class="picker__empty">这里没有子文件夹，可直接移到这里</div>';
        return;
      }
      listEl.innerHTML = folders
        .map(
          (item) => H`
          <button class="picker__row" type="button" data-id="${item.id}" data-name="${item.name}">
            <svg class="icon"><use href="#i-folder"/></svg>
            <span>${item.name}</span>
            <svg class="icon icon--chev"><use href="#i-chevron"/></svg>
          </button>`
        )
        .join("");
      listEl.querySelectorAll(".picker__row").forEach((row) => {
        row.addEventListener("click", () => {
          trail = [...trail, { id: Number(row.dataset.id), name: row.dataset.name }];
          load();
        });
      });
    }

    load();
  });
}
