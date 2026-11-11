/**
 * 多选文件浏览器：快传里选要发出去的东西。
 *
 * 用法：
 *   const nodes = await pickNodes({ title: "选择文件", mode: "file" });   // 只选文件
 *   const nodes = await pickNodes({ title: "选择目标目录", mode: "folder" });
 *   const nodes = await pickNodes({ title: "选择要发的文件", mode: "any" }); // 文件与文件夹都能选
 *
 * 返回值：选中的 node 数组；取消返回 null。
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { H, t } from "../i18n.js";

const ROOT = () => ({ id: null, name: t("我的文件") });

export function pickNodes({ title = "选择文件", mode = "any", confirmText = "确定", excludeId = null } = {}) {
  return new Promise((resolve) => {
    let trail = [ROOT()];
    /** id -> node，点选顺序即发送顺序 */
    const chosen = new Map();
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
          <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
          <button class="btn btn--accent" type="button" data-act="ok" disabled>${confirmText}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);

    const listEl = backdrop.querySelector("[data-list]");
    const crumbEl = backdrop.querySelector("[data-crumb]");
    const targetEl = backdrop.querySelector("[data-target]");
    const okBtn = backdrop.querySelector('[data-act="ok"]');

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
      }
    }
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) close(null);
    });
    backdrop.querySelector('[data-act="cancel"]').addEventListener("click", () => close(null));
    okBtn.addEventListener("click", () => close([...chosen.values()]));

    function currentId() {
      return trail[trail.length - 1].id;
    }

    function paintFoot() {
      const count = chosen.size;
      okBtn.disabled = count === 0;
      okBtn.textContent = count ? t("确定（{n}）", { n: count }) : confirmText;
      targetEl.textContent = count
        ? t("已选 {n} 项", { n: count })
        : t("位置：{path}", { path: trail.map((item) => item.name).join(" / ") });
    }

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

    async function load() {
      renderCrumb();
      paintFoot();
      listEl.innerHTML = `<div class="picker__empty">${t("加载中…")}</div>`;
      let data;
      try {
        const params = new URLSearchParams({ sort: "name", order: "asc" });
        const current = currentId();
        if (current !== null && current !== undefined) params.set("parentId", String(current));
        data = await api.get(`/api/nodes?${params.toString()}`);
      } catch (err) {
        listEl.innerHTML = H`<div class="picker__empty">${escapeHtml(err.message)}</div>`;
        return;
      }

      // 只选文件时文件夹仍然要显示（不然没法往下翻），只是不可勾选
      const items = (data.items || []).filter((item) => {
        if (item.id === excludeId) return false;
        if (mode === "folder") return item.type === "folder";
        return true;
      });
      if (!items.length) {
        listEl.innerHTML = `<div class="picker__empty">${t("这里没有可选的内容")}</div>`;
        return;
      }

      listEl.innerHTML = items
        .map((item) => {
          const on = chosen.has(item.id);
          const selectable = item.type === "file" || mode !== "file";
          return H`
          <div class="picker__row${on ? " is-on" : ""}${selectable ? "" : " is-locked"}"
               data-id="${item.id}" data-type="${item.type}">
            <svg class="icon"><use href="#${item.icon}"/></svg>
            <span class="picker__name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
            <span class="picker__size">${item.type === "file" ? formatBytes(item.size) : t("{n} 项", { n: item.count || 0 })}</span>
            ${
              item.type === "folder"
                ? '<button class="picker__enter" type="button" data-enter aria-label="进入文件夹"><svg class="icon icon--chev"><use href="#i-chevron"/></svg></button>'
                : ""
            }
            <svg class="icon picker__check"><use href="#i-check"/></svg>
          </div>`;
        })
        .join("");

      listEl.querySelectorAll(".picker__row").forEach((row) => {
        const id = Number(row.dataset.id);
        row.querySelector("[data-enter]")?.addEventListener("click", (event) => {
          event.stopPropagation();
          trail = [...trail, { id, name: row.querySelector(".picker__name").textContent }];
          load();
        });
        row.addEventListener("click", () => {
          if (row.classList.contains("is-locked")) return;
          const node = items.find((item) => item.id === id);
          if (!node) return;
          if (chosen.has(id)) {
            chosen.delete(id);
            row.classList.remove("is-on");
          } else {
            chosen.set(id, node);
            row.classList.add("is-on");
          }
          paintFoot();
        });
        row.addEventListener("dblclick", () => {
          if (row.dataset.type === "folder") {
            trail = [...trail, { id, name: row.querySelector(".picker__name").textContent }];
            load();
          }
        });
      });
    }

    load();
  });
}
