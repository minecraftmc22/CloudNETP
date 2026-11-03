/**
 * Live2D 看板娘
 *
 * - 模型目录：/live2d/model/<模型名>/xxx.model3.json（Cubism 4）
 * - 列表来自 GET /api/live2d/models，服务端每次请求都重新扫描目录，
 *   所以往文件夹里增删模型后，页面轮询即可自动发现（无需改代码 / 重启）。
 * - 依赖本地托管的三个库（static/vendor/live2d/）：
 *     pixi.min.js（6.x）→ live2dcubismcore.min.js → cubism4.min.js（pixi-live2d-display）
 *   任一加载失败或目录里没有模型时，保留原有的云朵 SVG 兜底。
 *
 * 交互刻意不用 canvas 拦截指针事件（看板娘浮在文件区上方，挡住点击很难受）：
 * 改成监听 document 的 pointermove / pointerdown，自己算坐标，
 * 既能跟随视线、点到身体还能播动作，又不影响底下任何操作。
 */

const LIBS = [
  "/static/vendor/live2d/pixi.min.js",
  "/static/vendor/live2d/live2dcubismcore.min.js",
  "/static/vendor/live2d/cubism4.min.js",
];

const API = "/api/live2d/models";
const POLL_MS = 40000;
const STORE_KEY = "mascot-model";
/** 内容高度占舞台高度的比例（1 = 正好铺满，不裁切） */
const FILL = 1;

const scriptCache = new Map();

function loadScript(src) {
  if (scriptCache.has(src)) return scriptCache.get(src);
  const task = new Promise((resolve, reject) => {
    const node = document.createElement("script");
    node.src = src;
    node.async = false;
    node.onload = () => resolve(src);
    node.onerror = () => {
      scriptCache.delete(src);
      reject(new Error(`加载失败 ${src}`));
    };
    document.head.appendChild(node);
  });
  scriptCache.set(src, task);
  return task;
}

async function loadLibs() {
  for (const src of LIBS) await loadScript(src);
  if (!window.PIXI || !window.PIXI.live2d) throw new Error("Live2D 运行库未就绪");
}

function apiUrl() {
  return API;
}

export function initLive2DMascot({ stage, host, bubble, pick, pickLabel, onPoke, onStatus } = {}) {
  if (!stage) return null;

  const state = {
    app: null,
    model: null,
    models: [],
    current: null,
    signature: "",
    trim: null,
    broken: new Set(),
    active: true,
    started: false,
    destroyed: false,
  };

  const controller = {
    refresh: () => refresh(),
    setActive,
    show,
    destroy,
    next,
    get models() {
      return state.models;
    },
  };

  function next() {
    if (state.models.length < 2) return null;
    const index = state.models.findIndex((m) => m.id === state.current?.id);
    const target = state.models[(index + 1) % state.models.length];
    state.broken.delete(target.id);
    loadModel(target);
    return target;
  }

  /* ---------------- 目录扫描 ---------------- */

  async function fetchModels() {
    const res = await fetch(apiUrl(), { cache: "no-store", credentials: "same-origin" });
    if (!res.ok) throw new Error(`模型列表 ${res.status}`);
    return res.json();
  }

  function signatureOf(models) {
    return models.map((m) => `${m.id}|${m.url}|${m.updatedAt}`).join("\n");
  }

  async function refresh() {
    let data;
    try {
      data = await fetchModels();
    } catch (err) {
      console.warn("[live2d]", err.message);
      return;
    }
    const models = Array.isArray(data.models) ? data.models : [];
    state.models = models;
    if (pick) pick.hidden = models.length < 2;
    onStatus?.({ count: models.length, models, dir: data.dir });

    if (!models.length) {
      state.signature = signatureOf(models);
      teardownModel();
      return;
    }

    const current = state.current;
    const keep = models.find((m) => m.id === current?.id && !state.broken.has(m.id));
    // 当前模型还在、文件也没被替换 → 什么都不用做，避免无谓地重载
    if (keep && keep.updatedAt === current.updatedAt) {
      state.signature = signatureOf(models);
      return;
    }

    state.signature = signatureOf(models);
    const saved = readSaved();
    const next =
      keep ||
      models.find((m) => m.id === saved && !state.broken.has(m.id)) ||
      models.find((m) => !state.broken.has(m.id));
    if (!next) {
      teardownModel();
      return;
    }
    await loadModel(next);
  }

  /* ---------------- 渲染 ---------------- */

  async function ensureApp() {
    if (state.app) return state.app;
    await loadLibs();
    const width = Math.max(1, stage.clientWidth);
    const height = Math.max(1, stage.clientHeight);
    const app = new window.PIXI.Application({
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
      width,
      height,
    });
    app.view.className = "mascot__canvas";
    stage.appendChild(app.view);
    state.app = app;
    // 出场时看板娘是关着的（is-hidden）就别白烧 GPU，等打开再启 ticker
    if (!state.active) app.ticker.stop();
    return app;
  }

  async function loadModel(entry) {
    let app;
    try {
      app = await ensureApp();
    } catch (err) {
      console.warn("[live2d]", err.message);
      return;
    }

    try {
      const model = await window.PIXI.live2d.Live2DModel.from(entry.url, {
        autoInteract: false,
        autoUpdate: true,
      });
      if (state.destroyed) {
        model.destroy();
        return;
      }
      if (state.model) {
        state.app.stage.removeChild(state.model);
        state.model.destroy();
      }
      app.stage.addChild(model);
      state.model = model;
      state.current = entry;
      writeSaved(entry.id);
      host?.classList.add("is-live");
      if (pickLabel) pickLabel.textContent = entry.name;
      state.trim = measureTrim(model, app);
      fit();
      Promise.resolve(model.motion?.("Idle")).catch(() => {});
    } catch (err) {
      console.warn("[live2d] 模型加载失败", entry.url, err?.message || err);
      state.broken.add(entry.id);
      teardownModel();
    }
  }

  /**
   * 量出角色在画布里的真实像素范围（占比 0~1）。
   *
   * Cubism 的画布四周留白多少完全看模型作者，直接「按画布高度铺满」会把
   * 头顶贴边的模型（例如 Hiyori）切掉额头。所以先渲染一张快照，扫 alpha
   * 得到内容包围盒；量不出来（画布空白 / 接口不支持）就退回整块画布。
   */
  function measureTrim(model, app) {
    try {
      const full = app.renderer.extract.canvas(model);
      if (!full || !full.width || !full.height) return null;
      const k = 160 / Math.max(full.width, full.height);
      const small = document.createElement("canvas");
      small.width = Math.max(1, Math.round(full.width * k));
      small.height = Math.max(1, Math.round(full.height * k));
      const ctx = small.getContext("2d", { willReadFrequently: true });
      ctx.clearRect(0, 0, small.width, small.height);
      ctx.drawImage(full, 0, 0, small.width, small.height);
      const { data } = ctx.getImageData(0, 0, small.width, small.height);
      let x0 = small.width;
      let y0 = small.height;
      let x1 = -1;
      let y1 = -1;
      for (let y = 0; y < small.height; y += 1) {
        for (let x = 0; x < small.width; x += 1) {
          if (data[(y * small.width + x) * 4 + 3] > 12) {
            if (x < x0) x0 = x;
            if (x > x1) x1 = x;
            if (y < y0) y0 = y;
            if (y > y1) y1 = y;
          }
        }
      }
      if (x1 < 0 || y1 < 0) return null;
      const trim = {
        x0: x0 / small.width,
        y0: y0 / small.height,
        x1: (x1 + 1) / small.width,
        y1: (y1 + 1) / small.height,
      };
      // 极端情况（内容只占极小一角）不可信，按整块画布处理
      if (trim.x1 - trim.x0 < 0.12 || trim.y1 - trim.y0 < 0.12) return null;
      return trim;
    } catch (err) {
      console.warn("[live2d] 内容边界测量失败", err?.message || err);
      return null;
    }
  }

  function fit() {
    const { app, model } = state;
    if (!app || !model) return;
    const width = Math.max(1, stage.clientWidth);
    const height = Math.max(1, stage.clientHeight);
    app.renderer.resize(width, height);

    // 注意：model.width / height 是「乘过缩放」的结果，拿它当基准会自我迭代成 1
    const baseW = model.internalModel?.originalWidth || model.width || 1;
    const baseH = model.internalModel?.originalHeight || model.height || 1;
    const t = state.trim || { x0: 0, y0: 0, x1: 1, y1: 1 };
    const contentW = Math.max(1, (t.x1 - t.x0) * baseW);
    const contentH = Math.max(1, (t.y1 - t.y0) * baseH);

    // 高度优先铺满，宽度最多允许 2% 溢出（窄屏下先保证人完整）
    const scale = Math.min((height * FILL) / contentH, (width * 1.02) / contentW);
    const centerX = ((t.x0 + t.x1) / 2) * baseW;

    model.anchor.set(0, 0);
    model.scale.set(scale);
    model.x = width / 2 - centerX * scale;
    model.y = height - t.y1 * baseH * scale;
  }

  function teardownModel() {
    host?.classList.remove("is-live");
    state.current = null;
    state.trim = null;
    if (pickLabel) pickLabel.textContent = "换一个";
    if (state.model && state.app) {
      state.app.stage.removeChild(state.model);
      state.model.destroy();
      state.model = null;
    }
  }

  function show() {
    host?.classList.remove("is-hidden");
  }

  function setActive(active) {
    state.active = Boolean(active);
    if (!state.app) return;
    if (state.active) {
      state.app.ticker.start();
      fit();
    } else {
      state.app.ticker.stop();
    }
  }

  function destroy() {
    state.destroyed = true;
    window.clearInterval(pollTimer);
    if (state.model) state.model.destroy();
    if (state.app) state.app.destroy(true, { children: true });
    state.app = null;
    state.model = null;
  }

  /* ---------------- 交互 ---------------- */

  let lastMove = 0;

  function onPointerMove(event) {
    if (!state.active || !state.model || !state.app) return;
    const now = performance.now();
    if (now - lastMove < 40) return;
    lastMove = now;
    const rect = state.app.view.getBoundingClientRect();
    if (!rect.width) return;
    const inside =
      event.clientX > rect.left - rect.width &&
      event.clientX < rect.right + rect.width * 0.4 &&
      event.clientY > rect.top - 120 &&
      event.clientY < rect.bottom + 80;
    if (!inside) return;
    const k = state.app.screen.width / rect.width;
    state.model.focus((event.clientX - rect.left) * k, (event.clientY - rect.top) * k);
  }

  function onPointerDown(event) {
    if (!state.active || !state.model || !state.app) return;
    const rect = state.app.view.getBoundingClientRect();
    // 只认身体那一片，避免点边角空白也算“戳她”
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    if (x < rect.width * 0.16 || x > rect.width * 0.84) return;
    if (y < rect.height * 0.12) return;
    try {
      Promise.resolve(state.model.motion("TapBody")).catch(() => {
        Promise.resolve(state.model.motion("Idle")).catch(() => {});
      });
    } catch {
      /* 模型没有对应动作组就忽略 */
    }
    say(onPoke);
  }

  function say(fn) {
    if (typeof fn === "function") fn();
    else if (bubble) {
      bubble.classList.add("is-on");
      window.setTimeout(() => bubble.classList.remove("is-on"), 4200);
    }
  }

  /* ---------------- 轮询 / 生命周期 ---------------- */

  let pollTimer = 0;

  function startPolling() {
    window.clearInterval(pollTimer);
    pollTimer = window.setInterval(() => {
      if (document.hidden || !state.active) return;
      refresh();
    }, POLL_MS);
  }

  function onVisibility() {
    if (document.hidden) {
      if (state.app) state.app.ticker.stop();
      return;
    }
    if (state.active && state.app) state.app.ticker.start();
    refresh();
  }

  const onResize = () => fit();

  pick?.addEventListener("click", () => {
    next();
  });

  // 右上角「看板娘」开关只是切 #mascot 的 is-hidden，这里跟着停/开渲染，别白烧 GPU
  if (host && "MutationObserver" in window) {
    new MutationObserver(() => {
      if (!state.started) return;
      setActive(!host.classList.contains("is-hidden"));
    }).observe(host, { attributes: true, attributeFilter: ["class"] });
  }

  if (!state.started) {
    state.started = true;
    state.active = !host?.classList.contains("is-hidden");
    document.addEventListener("pointermove", onPointerMove, { passive: true });
    document.addEventListener("pointerdown", onPointerDown, { passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onVisibility);
    window.addEventListener("resize", onResize);
    window.setTimeout(() => {
      refresh();
      startPolling();
    }, 600);
  }

  return controller;
}

function readSaved() {
  try {
    return localStorage.getItem(STORE_KEY) || "";
  } catch {
    return "";
  }
}

function writeSaved(id) {
  try {
    localStorage.setItem(STORE_KEY, id);
  } catch {
    /* 隐私模式忽略 */
  }
}
