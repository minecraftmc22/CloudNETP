/**
 * 通用任务进度面板：给「移动」这类点一下就完成的操作一个看得见的进度。
 * 上传有自己的队列面板（带速度/续传），这里只处理一行一件事的批量操作。
 *
 * 用法：
 *   const r = await runTask({
 *     title: "正在移动", icon: "i-folder", items,
 *     run: (item) => api.post(`/api/nodes/${item.id}/move`, { parentId }),
 *   });
 */
import { escapeHtml } from "../api.js";

import { H, t } from "../i18n.js";
const MIN_VISIBLE_MS = 900; // 再快也要停留这么久，否则用户看不到"正在移动"
const HOLD_MS = 1200; // 完成后的停留时间
const sleep = (ms) => new Promise((done) => window.setTimeout(done, ms));

export async function runTask({
  title = "正在处理",
  doneVerb,
  icon = "i-folder",
  items = [],
  run,
  host = document.body,
} = {}) {
  if (!items.length) return { total: 0, done: 0, failed: 0, canceled: 0 };

  const verb = doneVerb || title.replace(/^正在/, "");
  const started = performance.now();
  const rows = items.map((item, index) => ({ item, index, state: "pending", error: "" }));
  let canceled = false;

  const panel = document.createElement("div");
  panel.className = "task-panel";
  panel.setAttribute("role", "status");
  panel.innerHTML = H`
    <div class="task-panel__head">
      <svg class="icon icon--s"><use href="#${icon}"/></svg>
      <span class="task-panel__title" data-title>${escapeHtml(t(title))}…</span>
      <button class="task-panel__act" type="button" data-cancel>取消</button>
    </div>
    <div class="task-panel__list">
      ${rows
        .map(
          (row) => H`
        <div class="task-item" data-row="${row.index}">
          <div class="task-item__top">
            <span class="task-item__name" title="${escapeHtml(row.item.name)}">${escapeHtml(row.item.name)}</span>
            <span class="task-item__state" data-state>排队中</span>
          </div>
          <div class="progress"><div class="progress__bar" data-fill></div></div>
        </div>`
        )
        .join("")}
    </div>`;

  host.appendChild(panel);
  const titleEl = panel.querySelector("[data-title]");
  const cancelBtn = panel.querySelector("[data-cancel]");

  const rowEl = (index) => panel.querySelector(`[data-row="${index}"]`);

  function paint(row, state) {
    row.state = state;
    const el = rowEl(row.index);
    if (!el) return;
    const stateEl = el.querySelector("[data-state]");
    const fill = el.querySelector("[data-fill]");
    const bar = fill?.parentElement;

    el.classList.toggle("is-active", state === "active");
    el.classList.toggle("is-done", state === "done");
    el.classList.toggle("is-error", state === "error");
    el.classList.toggle("is-canceled", state === "canceled");
    bar?.classList.toggle("is-indeterminate", state === "active");

    const text = {
      pending: t("排队中"),
      active: t("{verb}中", { verb: t(verb) }),
      done: t("已完成"),
      error: t("失败"),
      canceled: t("已取消"),
    }[state];
    if (stateEl) stateEl.textContent = state === "error" && row.error ? row.error : text;
    if (stateEl) stateEl.title = row.error || text;
    if (fill) fill.style.width = state === "done" ? "100%" : state === "active" ? "100%" : "0";
  }

  function paintHeader() {
    const done = rows.filter((row) => row.state === "done").length;
    const failed = rows.filter((row) => row.state === "error").length;
    const settled = rows.filter((row) => row.state !== "pending" && row.state !== "active").length;
    if (settled >= rows.length) {
      titleEl.textContent = failed
        ? t("{verb}完成，{n} 项失败", { verb: t(verb), n: failed })
        : t("{verb}完成 · 共 {n} 项", { verb: t(verb), n: done });
    } else if (rows.length > 1) {
      titleEl.textContent = `${t(title)} ${Math.min(settled + 1, rows.length)} / ${rows.length}`;
    } else {
      titleEl.textContent = `${t(title)}…`;
    }
  }

  cancelBtn?.addEventListener("click", () => {
    canceled = true;
    cancelBtn.disabled = true;
    cancelBtn.textContent = t("正在停下…");
  });

  paintHeader();

  for (const row of rows) {
    if (canceled) {
      paint(row, "canceled");
      continue;
    }
    paint(row, "active");
    paintHeader();
    try {
      await run(row.item, row.index);
      paint(row, "done");
    } catch (err) {
      row.error = err?.message || t("操作失败");
      paint(row, "error");
    }
    paintHeader();
  }

  const done = rows.filter((row) => row.state === "done").length;
  const failed = rows.filter((row) => row.state === "error").length;
  const skipped = rows.filter((row) => row.state === "canceled").length;
  if (cancelBtn) cancelBtn.hidden = true;

  if (titleEl) {
    titleEl.textContent = failed
      ? t("{verb}完成：成功 {ok} 项，失败 {bad} 项", { verb: t(verb), ok: done, bad: failed })
      : skipped
        ? t("已停止，完成 {n} 项", { n: done })
        : t("{verb}完成 · 共 {n} 项", { verb: t(verb), n: done });
  }

  const elapsed = performance.now() - started;
  await sleep(Math.max(0, MIN_VISIBLE_MS - elapsed));
  panel.classList.add("is-done");
  await sleep(HOLD_MS);
  panel.classList.add("is-out");
  await sleep(220);
  panel.remove();

  return { total: rows.length, done, failed, canceled: skipped, stopped: canceled };
}
