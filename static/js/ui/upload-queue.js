/**
 * 上传队列（全局单例，与视图无关）
 *
 * 以前队列跟着「我的文件」视图走，切到别的页面视图一重建，任务就全丢了；
 * 现在队列和面板都挂在这里，切页、进设置、看分享都不影响后台继续传。
 *
 * - 面板挂在 body 上（position: fixed）：`.app-shell` 自己是 z-index: 2 的层叠上下文，
 *   放在里面永远压不过 z-index: 4 的看板娘，右下角按钮会点不动。
 * - 并发 2 个文件，单文件内部再并发 3 个分片（见 uploader.js）。
 * - 暂停 = abort 当前分片请求：已传分片留在服务端，继续时 init 会返回已传列表接着传；
 *   哈希结果缓存在 job 上，继续时不用把整个文件再读一遍。
 */
import { escapeHtml, formatBytes, formatEta, formatSpeed, percent } from "../api.js";
import { H, t } from "../i18n.js";
import { Toast } from "./toast.js";
import { uploadFile } from "../uploader.js";

const CONCURRENCY = 2;
const COLLAPSE_KEY = "cloudnetp.uploadCollapsed";
/** 同一批全部结束到把这些条目从列表里收走之间的停顿 */
const LINGER_MS = 2600;

/** @type {Array<object>} */
const jobs = [];
/** @type {Map<string, {ids: string[], settled: Set<string>}>} */
const batches = new Map();

let panelEl = null;
let collapsed = localStorage.getItem(COLLAPSE_KEY) === "1";
let running = 0;
let seq = 0;
let listener = null;
let unloadGuard = false;

/** 视图订阅：一批传完 / 有任务结束时通知它刷新列表和配额 */
export function onChange(fn) {
  listener = fn;
}

function notify() {
  try {
    listener?.();
  } catch {
    /* 视图已经卸载之类，忽略 */
  }
}

/* ------------------------------------------------------------------ */
/* 入队与调度                                                          */
/* ------------------------------------------------------------------ */

/** entries: [{ file, parentId }] */
export function enqueue(entries) {
  const items = entries.filter((entry) => entry.file && entry.file.size > 0);
  if (!items.length) {
    Toast.warn(t("空文件不会被上传"));
    return;
  }

  const batchId = `b${Date.now()}-${seq++}`;
  const created = items.map(({ file, parentId }) => ({
    id: `u${Date.now()}-${seq++}`,
    batchId,
    file,
    parentId,
    loaded: 0,
    phase: "pending",
    ratio: 0,
    speed: 0,
    status: "pending",
    instant: false,
    dedup: false,
    error: "",
    sha256: null,
    pauseRequested: false,
    controller: null,
  }));
  batches.set(batchId, { ids: created.map((job) => job.id), settled: new Set() });

  // 追加到队列：正在传的、暂停的都保留，只清掉已经结束的
  for (let i = jobs.length - 1; i >= 0; i -= 1) {
    if (jobs[i].status === "done" || jobs[i].status === "canceled") jobs.splice(i, 1);
  }
  jobs.push(...created);

  // 收起是用户自己选的，不因为来了新任务就弹开（失败时才会展开）
  renderPanel();
  guardUnload();
  pump();
}

function pump() {
  while (running < CONCURRENCY) {
    const job = jobs.find((item) => item.status === "pending");
    if (!job) break;
    running += 1;
    runJob(job)
      .catch(() => {})
      .finally(() => {
        running -= 1;
        pump();
      });
  }
  renderHeader();
}

async function runJob(job) {
  if (job.status !== "pending") return;
  job.error = "";
  job.pauseRequested = false;
  job.controller = new AbortController();
  job.status = "active";
  job.phase = "hash";
  job.speed = 0;
  syncRow(job);
  renderHeader();

  try {
    const result = await uploadFile({
      file: job.file,
      parentId: job.parentId,
      signal: job.controller.signal,
      sha256: job.sha256,
      onHash: (sha) => {
        job.sha256 = sha;
      },
      onPhase: (phase, ratio) => {
        job.phase = phase;
        job.ratio = ratio;
        syncRow(job);
      },
      onProgress: (loaded, _total, extra) => {
        job.loaded = loaded;
        job.speed = extra?.speed || 0;
        syncRow(job);
      },
    });
    job.status = "done";
    job.instant = Boolean(result.instant);
    job.dedup = Boolean(result.deduplicated);
    job.loaded = job.file.size;
    job.speed = 0;
    job.phase = "done";
  } catch (err) {
    job.speed = 0;
    if (err?.name === "AbortError") {
      // 主动暂停 vs 取消：同一个 abort，靠这个标记区分
      job.status = job.pauseRequested ? "paused" : "canceled";
    } else {
      job.status = "error";
      job.error = err?.message || t("上传失败");
    }
  }

  job.pauseRequested = false;
  job.controller = null;
  syncRow(job);
  renderHeader();
  guardUnload();

  // 失败就把面板展开：重试按钮就在这一行上（失败原因显示在该行 meta 里）
  if (job.status === "error" && collapsed) setCollapsed(false);

  if (job.status !== "paused") settle(job);
}

/** 一条任务走到终点（完成 / 失败 / 取消）时结算它所在的批次 */
function settle(job) {
  const batch = batches.get(job.batchId);
  if (!batch || batch.settled.has(job.id)) return;
  batch.settled.add(job.id);
  notify();
  if (batch.settled.size < batch.ids.length) return;
  batches.delete(job.batchId);
  finishBatch(batch);
}

function finishBatch(batch) {
  const members = batch.ids.map((id) => jobs.find((job) => job.id === id)).filter(Boolean);
  const ok = members.filter((job) => job.status === "done").length;
  const failed = members.filter((job) => job.status === "error").length;
  const canceled = members.filter((job) => job.status === "canceled").length;
  const instant = members.filter((job) => job.instant).length;

  if (failed) Toast.error(t("{n} 个文件上传失败", { n: failed }));
  else if (canceled) Toast.info(t("已取消 {n} 个，成功上传 {m} 个", { n: canceled, m: ok }));
  else if (ok && instant === ok) Toast.success(t("{n} 个文件秒传完成，没有产生新的上传流量", { n: ok }));
  else Toast.success(t("已上传 {n} 个文件", { n: ok }));

  window.setTimeout(() => {
    const ids = new Set(batch.ids);
    for (let i = jobs.length - 1; i >= 0; i -= 1) {
      const job = jobs[i];
      // 只留下失败的，方便重试
      if (ids.has(job.id) && job.status !== "error") jobs.splice(i, 1);
    }
    renderPanel();
    guardUnload();
  }, LINGER_MS);
}

/* ------------------------------------------------------------------ */
/* 暂停 / 继续 / 重试 / 取消                                            */
/* ------------------------------------------------------------------ */
function pauseJob(job) {
  if (job.status === "pending") {
    job.status = "paused";
    syncRow(job);
    renderHeader();
    return;
  }
  if (job.status !== "active") return;
  job.pauseRequested = true;
  job.controller?.abort();
}

function resumeJob(job) {
  if (job.status !== "paused") return;
  job.status = "pending";
  job.loaded = 0;
  job.ratio = 0;
  job.speed = 0;
  job.phase = "hash";
  job.error = "";
  syncRow(job);
  renderHeader();
  guardUnload();
  pump();
}

function pauseAll() {
  jobs.forEach((job) => {
    if (job.status === "active" || job.status === "pending") pauseJob(job);
  });
}

function resumeAll() {
  jobs.forEach((job) => {
    if (job.status === "paused") resumeJob(job);
  });
  pump();
}

async function retryJob(job) {
  if (job.status === "active" || job.status === "pending" || job.status === "paused") return;
  job.status = "pending";
  job.phase = "hash";
  job.loaded = 0;
  job.ratio = 0;
  job.speed = 0;
  job.error = "";
  job.instant = false;
  job.dedup = false;
  job.batchId = `b${Date.now()}-${seq++}`;
  batches.set(job.batchId, { ids: [job.id], settled: new Set() });
  syncRow(job);
  renderHeader();
  guardUnload();
  pump();
}

function cancelJob(job) {
  // 已经结束的条目，✕ 的意思是「从列表里去掉」
  if (job.status === "error" || job.status === "canceled" || job.status === "done") {
    removeJob(job);
    return;
  }
  job.pauseRequested = false;
  if (job.status === "paused") {
    job.status = "canceled";
    settle(job);
    syncRow(job);
    renderHeader();
    guardUnload();
    return;
  }
  if (job.status === "pending") {
    job.status = "canceled";
    settle(job);
    syncRow(job);
    renderHeader();
    guardUnload();
    return;
  }
  job.controller?.abort();
}

function removeJob(job) {
  const index = jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) jobs.splice(index, 1);
  renderPanel();
  guardUnload();
}

/* ------------------------------------------------------------------ */
/* 面板渲染                                                            */
/* ------------------------------------------------------------------ */
function jobPercent(job) {
  if (job.status === "done") return 100;
  if (job.phase === "hash") return Math.round((job.ratio || 0) * 100);
  if (job.phase === "upload") return percent(job.loaded, job.file.size);
  return 0;
}

function jobStateText(job) {
  if (job.status === "done") return job.instant ? t("秒传") : t("完成");
  if (job.status === "error") return t("失败");
  if (job.status === "canceled") return t("已取消");
  if (job.status === "paused") return t("已暂停");
  if (job.status === "pending") return t("排队中");
  if (job.phase === "merge") return t("合并中");
  if (job.phase === "hash") return t("校验 {n}%", { n: Math.round((job.ratio || 0) * 100) });
  return `${jobPercent(job)}%`;
}

function jobMetaText(job) {
  if (job.status === "error") return job.error;
  if (job.status === "canceled") return t("已中断，重新拖入可续传");
  if (job.status === "paused") {
    const done = formatBytes(job.loaded);
    return t("停在 {done}，点继续接着传", { done });
  }
  if (job.status === "done") {
    if (job.dedup && !job.instant) return t("内容已存在，秒级入库");
    return formatBytes(job.file.size);
  }
  const parts = [`${formatBytes(job.loaded)} / ${formatBytes(job.file.size)}`];
  const speed = formatSpeed(job.speed);
  if (speed) parts.push(speed);
  const eta = formatEta(job.file.size - job.loaded, job.speed);
  if (eta) parts.push(eta);
  return parts.join(" · ");
}

function buildPanel() {
  const panel = document.createElement("div");
  panel.className = "upload-panel";
  panel.innerHTML = H`
    <div class="upload-panel__head">
      <svg class="icon icon--s"><use href="#i-upload"/></svg>
      <span class="upload-panel__title" data-upload-title></span>
      <button class="upload-panel__act" type="button" data-pause-all></button>
      <button class="icon-btn upload-panel__toggle" type="button" data-toggle
              aria-expanded="true" aria-label="${t("开关上传列表")}" title="${t("收起上传列表")}">
        <svg class="icon"><use href="#i-chevron-down"/></svg>
      </button>
      <button class="icon-btn" type="button" data-close title="${t("关闭并清空")}">
        <svg class="icon"><use href="#i-close"/></svg>
      </button>
    </div>
    <div class="upload-panel__list" data-upload-list></div>`;

  panel.querySelector("[data-toggle]").addEventListener("click", () => setCollapsed(!collapsed));
  panel.querySelector("[data-pause-all]").addEventListener("click", () => {
    hasRunning() ? pauseAll() : resumeAll();
  });
  panel.querySelector("[data-close]").addEventListener("click", () => {
    // 只清掉已结束的条目；还在跑的不动，避免「关掉面板 = 静默丢队列」
    for (let i = jobs.length - 1; i >= 0; i -= 1) {
      const status = jobs[i].status;
      if (status === "done" || status === "error" || status === "canceled") jobs.splice(i, 1);
    }
    if (!jobs.length) {
      panel.remove();
      panelEl = null;
      return;
    }
    renderPanel();
  });

  document.body.appendChild(panel);
  panelEl = panel;
  return panel;
}

function hasRunning() {
  return jobs.some((job) => job.status === "active" || job.status === "pending");
}

function renderPanel() {
  if (!jobs.length) {
    panelEl?.remove();
    panelEl = null;
    return;
  }
  const panel = panelEl || buildPanel();
  const list = panel.querySelector("[data-upload-list]");
  const signature = jobs.map((job) => job.id).join(",");
  if (list.dataset.sig !== signature) {
    list.dataset.sig = signature;
    list.innerHTML = jobs
      .map(
        (job) => H`
      <div class="upload-item" data-job="${job.id}">
        <div class="upload-item__top">
          <span class="upload-item__name" title="${escapeHtml(job.file.name)}">${escapeHtml(job.file.name)}</span>
          <span class="upload-item__state" data-state></span>
          <button class="upload-item__act" type="button" data-pause="${job.id}"
                  title="${t("暂停")}" aria-label="${t("暂停")}">
            <svg class="icon"><use href="#i-pause"/></svg>
          </button>
          <button class="upload-item__retry" type="button" data-retry="${job.id}" title="${t("重新上传")}">
            <svg class="icon"><use href="#i-refresh"/></svg>
          </button>
          <button class="upload-item__close" type="button" data-cancel="${job.id}" title="${t("取消这个文件")}">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </div>
        <div class="progress" data-bar><div class="progress__bar" data-fill></div></div>
        <div class="upload-item__meta" data-meta></div>
      </div>`
      )
      .join("");

    list.querySelectorAll("[data-pause]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const job = jobs.find((item) => item.id === btn.dataset.pause);
        if (!job) return;
        if (job.status === "paused") resumeJob(job);
        else pauseJob(job);
      });
    });
    list.querySelectorAll("[data-retry]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const job = jobs.find((item) => item.id === btn.dataset.retry);
        if (job) retryJob(job);
      });
    });
    list.querySelectorAll("[data-cancel]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const job = jobs.find((item) => item.id === btn.dataset.cancel);
        if (job) cancelJob(job);
      });
    });
  }

  applyCollapsed();
  jobs.forEach(syncRow);
  renderHeader();
}

function setCollapsed(value) {
  collapsed = Boolean(value);
  localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  applyCollapsed();
}

function applyCollapsed() {
  if (!panelEl) return;
  panelEl.classList.toggle("is-collapsed", collapsed);
  const btn = panelEl.querySelector("[data-toggle]");
  if (btn) {
    btn.title = collapsed ? t("展开上传列表") : t("收起上传列表");
    btn.setAttribute("aria-expanded", String(!collapsed));
  }
}

function syncRow(job) {
  const row = panelEl?.querySelector(`[data-job="${job.id}"]`);
  if (!row) return;
  row.classList.toggle("is-error", job.status === "error");
  row.classList.toggle("is-done", job.status === "done");
  row.classList.toggle("is-canceled", job.status === "canceled");
  row.classList.toggle("is-paused", job.status === "paused");
  row.classList.toggle(
    "is-active",
    job.status === "active" || job.status === "pending"
  );
  row.classList.toggle("is-instant", job.status === "done" && job.instant);

  const stateEl = row.querySelector("[data-state]");
  if (stateEl) stateEl.textContent = jobStateText(job);
  const metaEl = row.querySelector("[data-meta]");
  if (metaEl) metaEl.textContent = jobMetaText(job);

  // 是否显示由 .is-active / .is-paused / .is-error 这几个类决定（见 files.css）
  const pauseBtn = row.querySelector("[data-pause]");
  if (pauseBtn) {
    const paused = job.status === "paused";
    pauseBtn.title = paused ? t("继续") : t("暂停");
    pauseBtn.setAttribute("aria-label", pauseBtn.title);
    pauseBtn.querySelector("use")?.setAttribute("href", paused ? "#i-play" : "#i-pause");
  }

  const closeBtn = row.querySelector("[data-cancel]");
  if (closeBtn) closeBtn.title = job.status === "active" ? t("取消这个文件") : t("从列表移除");

  const bar = row.querySelector("[data-bar]");
  const fill = row.querySelector("[data-fill]");
  const indeterminate = job.status === "active" && (job.phase === "hash" || job.phase === "merge");
  if (bar) bar.classList.toggle("is-indeterminate", indeterminate);
  if (fill) fill.style.width = indeterminate ? "100%" : `${jobPercent(job)}%`;
}

function renderHeader() {
  if (!panelEl) return;
  const title = panelEl.querySelector("[data-upload-title]");
  if (!title) return;
  const active = jobs.filter((job) => job.status === "active" || job.status === "pending");
  const paused = jobs.filter((job) => job.status === "paused");
  const done = jobs.filter((job) => job.status === "done").length;
  const failed = jobs.filter((job) => job.status === "error").length;

  if (active.length) {
    const speed = formatSpeed(active.reduce((sum, job) => sum + (job.speed || 0), 0));
    title.textContent = t("正在上传 {n} 个文件", { n: active.length }) + (speed ? ` · ${speed}` : "");
  } else if (paused.length) {
    title.textContent = t("已暂停 {n} 个文件", { n: paused.length });
  } else if (failed) {
    title.textContent = t("{n} 个失败 · {m} 个已完成", { n: failed, m: done });
  } else {
    title.textContent = t("{n} 个文件已处理完", { n: jobs.length });
  }

  const act = panelEl.querySelector("[data-pause-all]");
  if (act) {
    const running = active.length > 0;
    act.textContent = running ? t("全部暂停") : t("全部继续");
    act.hidden = !running && !paused.length;
  }
}

/** 退出登录时清空：正在传的直接取消，面板一并移除 */
export function reset() {
  jobs.forEach((job) => {
    job.pauseRequested = false;
    if (job.status === "active") job.controller?.abort();
  });
  jobs.length = 0;
  batches.clear();
  panelEl?.remove();
  panelEl = null;
  guardUnload();
}

/* ------------------------------------------------------------------ */
/* 离开页面前的提醒                                                    */
/* ------------------------------------------------------------------ */
const onBeforeUnload = (event) => {
  if (!jobs.some((job) => job.status === "active")) return;
  event.preventDefault();
  event.returnValue = "";
};

function guardUnload() {
  const need = jobs.some((job) => job.status === "active");
  if (need && !unloadGuard) {
    window.addEventListener("beforeunload", onBeforeUnload);
    unloadGuard = true;
  } else if (!need && unloadGuard) {
    window.removeEventListener("beforeunload", onBeforeUnload);
    unloadGuard = false;
  }
}
