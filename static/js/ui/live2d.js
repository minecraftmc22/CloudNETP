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

import { t } from "../i18n.js";

const LIBS = [
  "/static/vendor/live2d/pixi.min.js",
  "/static/vendor/live2d/live2dcubismcore.min.js",
  "/static/vendor/live2d/cubism4.min.js",
];

const API = "/api/live2d/models";
const POLL_MS = 40000;
const STORE_KEY = "mascot-model";
const SIZE_KEY = "mascot-size";
const POS_KEY = "mascot-pos";
const WHEEL_KEY = "mascot-wheel";
/** 内容高度占舞台高度的比例（1 = 正好铺满，不裁切） */
const FILL = 1;

const scriptCache = new Map();

/** 页面上唯一那只看板娘：CDP娘 页要把她整只搬进页面里（不复制第二个实例） */
let activeMascot = null;

export function getMascotController() {
  return activeMascot;
}

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

/* ---------------- 大小 / 位置（记在 localStorage，按设备保存） ---------------- */

export function getMascotScale() {
  const value = Number(localStorage.getItem(SIZE_KEY));
  return Number.isFinite(value) && value >= 60 && value <= 180 ? Math.round(value) : 100;
}

/** 0.6–1.8 倍；返回实际生效的百分比 */
export function setMascotScale(percent) {
  const value = Math.max(60, Math.min(180, Math.round(Number(percent) || 100)));
  document.documentElement.style.setProperty("--mascot-scale", (value / 100).toFixed(2));
  try {
    localStorage.setItem(SIZE_KEY, String(value));
  } catch {
    /* 隐私模式忽略 */
  }
  window.dispatchEvent(new CustomEvent("mascot:resize", { detail: { percent: value } }));
  return value;
}

/** 悬停时能不能用滚轮改大小（设置 → 装饰 里的开关，按设备记住） */
export function isMascotWheelZoom() {
  try {
    return localStorage.getItem(WHEEL_KEY) !== "0";
  } catch {
    return true;
  }
}

export function setMascotWheelZoom(on) {
  const enabled = Boolean(on);
  try {
    localStorage.setItem(WHEEL_KEY, enabled ? "1" : "0");
  } catch {
    /* 隐私模式忽略 */
  }
  window.dispatchEvent(new CustomEvent("mascot:wheel-zoom", { detail: { enabled } }));
  return enabled;
}

function readSavedPos() {
  try {
    const pos = JSON.parse(localStorage.getItem(POS_KEY) || "null");
    if (pos && Number.isFinite(pos.dx) && Number.isFinite(pos.dy)) return { dx: pos.dx, dy: pos.dy };
  } catch {
    /* 忽略损坏数据 */
  }
  return { dx: 0, dy: 0 };
}

export function initLive2DMascot({ stage, host, bubble, pick, pickLabel, onPoke, onStatus } = {}) {
  if (!stage) return null;

  // 尺寸先落到根元素，CSS 用 --mascot-scale 算宽高；位置靠 --mascot-dx/dy 平移
  document.documentElement.style.setProperty("--mascot-scale", (getMascotScale() / 100).toFixed(2));

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
    offset: readSavedPos(),
    // 非空 = 正被她所在的那一页「收养」（CDP娘 页），现场是 { hidden, container }
    embedded: null,
  };

  const controller = {
    refresh: () => refresh(),
    setActive,
    show,
    destroy,
    next,
    embed: (container) => embed(container),
    unembed,
    refit: () => fit(),
    get embedded() {
      return Boolean(state.embedded);
    },
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
      // 云朵宽 158、Live2D 宽 232：切过去时先关掉宽高过渡再量，
      // 否则 clientWidth 还是过渡起点的窄尺寸，画布会被 CSS 拉扁
      host?.classList.add("no-size-anim");
      host?.classList.add("is-live");
      void stage.offsetWidth;
      if (pickLabel) pickLabel.textContent = entry.name;
      state.trim = measureTrim(model, app);
      fit();
      host?.classList.remove("no-size-anim");
      // 换模型后尺寸变了（云朵 158 → Live2D 232）：这里的中心会在宽高过渡
      // 中间被量到，绝不能记 —— transitionend 落定后会统一重记。
      // 万一这次尺寸没变、transition 不触发，兜底在落定后补记一次。
      window.setTimeout(() => {
        if (!centerFrac) rememberCenter();
      }, 350);
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
    if (pickLabel) pickLabel.textContent = t("换一个");
    if (state.model && state.app) {
      state.app.stage.removeChild(state.model);
      state.model.destroy();
      state.model = null;
    }
  }

  function show() {
    host?.classList.remove("is-hidden");
    // 每次露面都按当前视口再夹一次：换设备、改缩放后旧偏移可能已经把她推出屏幕
    applyOffset();
  }

  /* ---------------- 被页面收养（CDP娘 页） ----------------
     不复制第二个 Live2D 实例：直接把 #mascot 整只搬进页面里的舞台容器，
     交给 CSS 撑满容器，走之前再搬回 body 恢复右下角悬浮。
     手机上 #mascot 本来是 display:none（views.css），所以这一步也是
     「手机上第一次真正看见她活动」的入口。 */

  let embedTimer = 0;
  let embedRaf = 0;
  let embedObs = null;

  function embed(container) {
    if (!host || !container) return false;
    if (state.embedded) unembed();
    state.embedded = {
      hidden: host.classList.contains("is-hidden"),
      container,
    };
    host.classList.add("is-embedded");
    // 用户之前把看板娘收起来过：这一页是她自己的名片，仍然请她出来
    host.classList.remove("is-hidden");
    container.appendChild(host);
    // 页面里她是「站着」的，不跟右下角锚点的平移量
    document.documentElement.style.setProperty("--mascot-dx", "0px");
    document.documentElement.style.setProperty("--mascot-dy", "0px");
    setActive(true);

    // 搬完要等布局落定再量，否则量到的还是 display:none 时的 0 宽高
    window.cancelAnimationFrame(embedRaf);
    embedRaf = window.requestAnimationFrame(() => {
      embedRaf = window.requestAnimationFrame(() => {
        embedRaf = 0;
        fit();
      });
    });
    window.clearTimeout(embedTimer);
    embedTimer = window.setTimeout(fit, 200);

    if ("ResizeObserver" in window) {
      if (embedObs) embedObs.disconnect();
      embedObs = new ResizeObserver(() => onResize());
      embedObs.observe(container);
    }
    return true;
  }

  function unembed() {
    const info = state.embedded;
    if (!host || !info) return false;
    state.embedded = null;
    window.clearTimeout(embedTimer);
    window.cancelAnimationFrame(embedRaf);
    embedRaf = 0;
    if (embedObs) {
      embedObs.disconnect();
      embedObs = null;
    }
    host.classList.remove("is-embedded");
    document.body.appendChild(host);
    if (info.hidden) host.classList.add("is-hidden");
    setActive(!host.classList.contains("is-hidden"));
    // 回到右下角：把之前记住的偏移重新落一遍
    applyOffset(true);
    window.requestAnimationFrame(fit);
    window.setTimeout(fit, 140);
    rememberCenter();
    return true;
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
    window.removeEventListener("mascot:reset", resetPlacement);
    if (state.model) state.model.destroy();
    if (state.app) state.app.destroy(true, { children: true });
    state.app = null;
    state.model = null;
  }

  /** 设置页「重置看板娘位置」：回到默认偏移，并清掉本机记住的位置 */
  function resetPlacement() {
    state.offset = { dx: 0, dy: 0 };
    applyOffset(true);
    rememberCenter();
  }
  window.addEventListener("mascot:reset", resetPlacement);

  /* ---------------- 交互 ---------------- */

  let lastMove = 0;
  const clamp1 = (v) => Math.max(-1, Math.min(1, v));

  function onPointerMove(event) {
    if (!state.active || !state.model || !state.app) return;
    const now = performance.now();
    if (now - lastMove < 40) return;
    lastMove = now;

    // 直接驱动 focusController：它收的是 -1..1 的方向值（内部会平滑过渡），
    // 用 model.focus() 反而要传 PIXI 世界坐标，换算容易错。
    const focus = state.model.internalModel?.focusController;
    const view = state.app.view;
    if (!focus || !view) return;
    const rect = view.getBoundingClientRect();
    if (!rect.width) return;

    // 以脸部（画布上半部分）为中心，按整个窗口的尺度归一化，鼠标在屏幕任何位置都能牵动视线
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height * 0.28;
    const nx = clamp1((event.clientX - cx) / (window.innerWidth * 0.5));
    const ny = clamp1(-(event.clientY - cy) / (window.innerHeight * 0.5));
    focus.focus(nx, ny);
  }

  /* ---------------- 拖动 / 点击 ---------------- */

  function clampOffset(dx, dy) {
    if (!host) return { dx: 0, dy: 0 };
    const w = host.offsetWidth || 232;
    const h = host.offsetHeight || 320;
    const visible = 48; // 往左上推时，至少留 48px 在屏幕里，免得拖丢了找不回来
    // 基准位置是 right:14px / bottom:6px：
    // - 往右下走会让身体跑出屏幕（125% 缩放后尤其明显，只剩边角抠不住），所以上界就是 0；
    // - 往左上走则允许，但必须留一截在视口里。
    const minDx = visible + 14 - window.innerWidth;
    const maxDx = 0;
    const minDy = visible + 6 - window.innerHeight;
    const maxDy = 0;
    if (maxDx <= minDx || maxDy <= minDy) return { dx: 0, dy: 0 };
    return {
      dx: Math.max(minDx, Math.min(maxDx, dx)),
      // 宽高变化（滚轮放大、换模型）后可能超出上下边，这里一并收回视口
      dy: Math.max(minDy, Math.min(maxDy, dy)),
    };
  }

  // 位置变了要通知外面（聊天框跟着看板娘走），用 rAF 合并频繁的 pointermove
  let moveFrame = 0;
  function notifyMove() {
    if (moveFrame) return;
    moveFrame = window.requestAnimationFrame(() => {
      moveFrame = 0;
      window.dispatchEvent(new CustomEvent("mascot:move", { detail: { ...state.offset } }));
    });
  }

  function applyOffset(save = false) {
    state.offset = clampOffset(state.offset.dx, state.offset.dy);
    // 存整数：反复按比例重算时小数会越积越歪，也免得 localStorage 里一串 17 位小数
    state.offset = { dx: Math.round(state.offset.dx), dy: Math.round(state.offset.dy) };
    document.documentElement.style.setProperty("--mascot-dx", `${state.offset.dx}px`);
    document.documentElement.style.setProperty("--mascot-dy", `${state.offset.dy}px`);
    if (save) {
      try {
        localStorage.setItem(POS_KEY, JSON.stringify(state.offset));
      } catch {
        /* 隐私模式忽略 */
      }
    }
    notifyMove();
  }

  /* ---------------- 位置锚定：视口 / 尺寸变了别乱窜 ----------------
     dx/dy 是绝对 px。浏览器页面缩放（±10%）或改窗口宽度后视口变了，
     同一个 dx 在新视口里的相对位置就变了 —— 人明明没动，看板娘却往边上
     滑一截。所以另记一份「她中心在视口里的比例」（centerFrac），视口一变
     就按比例放回去。同理，滚轮 / 滑杆改大小时她是贴着右下角往外长的，
     拖到屏幕中间再缩放会一路往左上飘；停手后按同一比例回正，
     视觉上就是围着原地变大变小，而不是「乱窜」。

     注意 centerFrac 只在「用户真的把她放在某处」时才更新（拖动结束、
     重置、首次加载）。宽高过渡中间那些半成品位置绝不能记：贴角放大时
     画布尺寸一直在变，记下来下次回正会照着被顶歪的位置算，越缩越偏。 */
  const viewport = { w: window.innerWidth, h: window.innerHeight };
  let centerFrac = null;

  /** 贴角 = 用户没挪过她（偏移还是 0）：这种状态锁定右下角，不参与比例回正 */
  function isPinned() {
    return !state.offset.dx && !state.offset.dy;
  }

  function rememberCenter() {
    if (!host || host.classList.contains("is-hidden")) return;
    // 贴角时不能记中心：滚轮放大是贴着 right/bottom 往外长的，中心必然被顶到
    // 左上一点点。记下这个「被顶歪的比例」，下次缩小就会照着它往回拉一截 ——
    // 看板娘从此永久离开角落，越滚越偏（滚轮一动位置就乱跑的根因）。
    if (isPinned()) {
      centerFrac = null;
      return;
    }
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    centerFrac = {
      cx: (r.left + r.width / 2) / window.innerWidth,
      cy: (r.top + r.height / 2) / window.innerHeight,
    };
  }

  /**
   * 把看板娘挪回「记忆里那个占屏比例」。
   *
   * 返回 false = 这次没回正（贴角 / 补偿被边界夹掉），调用方就不要顺手
   * rememberCenter()：半途而废的位置一旦被记住，之后每次都对不齐。
   */
  function restoreCenter() {
    // 被页面收养时她的位置由容器决定，没有「占屏比例」可言
    if (state.embedded) return false;
    if (!centerFrac || !host || host.classList.contains("is-hidden")) return false;
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const wantLeft = centerFrac.cx * window.innerWidth - r.width / 2;
    const wantTop = centerFrac.cy * window.innerHeight - r.height / 2;
    const dx = state.offset.dx + (wantLeft - r.left);
    const dy = state.offset.dy + (wantTop - r.top);
    if (Math.abs(dx - state.offset.dx) < 0.5 && Math.abs(dy - state.offset.dy) < 0.5) return true;
    // 想让她往右下走、但已经贴边（dx 上界就是 0）：夹掉之后位置就"按比例"
    // 不了。宁可原地不动，也别落在一个两边不靠的地方。
    const fitted = clampOffset(dx, dy);
    if (Math.abs(fitted.dx - dx) > 0.5 || Math.abs(fitted.dy - dy) > 0.5) return false;
    state.offset = { dx, dy };
    applyOffset(true);
    return true;
  }

  function pokeAt(clientX, clientY) {
    if (!state.active || !state.model || !state.app) return;
    const rect = state.app.view.getBoundingClientRect();
    if (!rect.width) return;
    // 只认身体那一片，避免点边角空白也算“戳她”
    const x = clientX - rect.left;
    const y = clientY - rect.top;
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

  let drag = null;
  // 页面收养时没有「拖动」的概念，只记一下按下的位置，抬手时判断是不是戳了一下
  let tap = null;

  function onHostDown(event) {
    if (!host || host.classList.contains("is-hidden")) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    if (event.target.closest("[data-mascot-pick]")) return;
    if (state.embedded) {
      tap = { x: event.clientX, y: event.clientY };
      return;
    }
    drag = {
      sx: event.clientX,
      sy: event.clientY,
      dx: state.offset.dx,
      dy: state.offset.dy,
      moved: false,
      id: event.pointerId,
    };
    try {
      host.setPointerCapture(event.pointerId);
    } catch {
      /* 某些内核不支持捕获，退化成普通拖动 */
    }
    host.classList.add("is-dragging");
    event.preventDefault();
  }

  function onHostMove(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const mx = event.clientX - drag.sx;
    const my = event.clientY - drag.sy;
    if (!drag.moved && Math.abs(mx) + Math.abs(my) > 6) drag.moved = true;
    if (!drag.moved) return;
    state.offset = { dx: drag.dx + mx, dy: drag.dy + my };
    applyOffset();
    event.preventDefault();
  }

  function endDrag(event, canceled) {
    if (!drag || event.pointerId !== drag.id) return;
    const current = drag;
    drag = null;
    try {
      host?.releasePointerCapture(current.id);
    } catch {
      /* 忽略 */
    }
    host?.classList.remove("is-dragging");
    if (current.moved) {
      applyOffset(true);
      // 人放下的位置才是「她想待的地方」，之后缩放 / 改窗口都按这个比例回正
      rememberCenter();
      return;
    }
    if (!canceled) pokeAt(event.clientX, event.clientY);
  }

  const onHostUp = (event) => {
    if (state.embedded) {
      const start = tap;
      tap = null;
      // 手指没怎么移动才算「戳一下」，否则就是在划页面
      if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 14) {
        pokeAt(event.clientX, event.clientY);
      }
      return;
    }
    endDrag(event, false);
  };

  const onHostCancel = (event) => endDrag(event, true);

  // 落盘一次：旧版本留下的偏移可能已经越界，夹回来的值顺手写回去，别每次加载都重算。
  // 这里刻意不 rememberCenter()：此刻还是云朵尺寸（158），记下来的中心是错的，
  // 之后模型加载长到 232 时按它「回正」就会凭空挪一截 —— 位置漂移的根源之一。
  // 中心记忆等尺寸落定后由 transitionend / 拖动结束 / 重置来刷新。
  applyOffset(true);

  /* ---------------- 悬停滚轮改大小 ---------------- */

  let sizeTag = null;
  let sizeTimer = 0;
  let wheelDelta = 0;
  let wheelFrame = 0;

  function showSizeTag(percent) {
    if (!host) return;
    if (!sizeTag) {
      sizeTag = document.createElement("span");
      sizeTag.className = "mascot__size";
      sizeTag.setAttribute("aria-hidden", "true");
      host.appendChild(sizeTag);
    }
    sizeTag.textContent = `${percent}%`;
    // 重新触发一下小弹跳：连滚时数字每跳一档都跟着「哒」一下
    sizeTag.classList.remove("is-tick");
    void sizeTag.offsetWidth;
    sizeTag.classList.add("is-tick");
    sizeTag.classList.add("is-on");
    window.clearTimeout(sizeTimer);
    sizeTimer = window.setTimeout(() => sizeTag?.classList.remove("is-on"), 900);
  }

  /* 滚轮缩放的收尾标记：宽高过渡走完再让画布按最终尺寸重排 */
  let zoomTimer = 0;
  function markZooming() {
    if (!host) return;
    host.classList.add("is-zooming");
    window.clearTimeout(zoomTimer);
    zoomTimer = window.setTimeout(() => host?.classList.remove("is-zooming"), 300);
  }

  function onHostWheel(event) {
    if (!host || host.classList.contains("is-hidden")) return;
    // 在页面里她是「站在那儿」的，滚轮留给页面滚动，别改她的大小
    if (state.embedded) return;
    // 关掉开关就让页面照常滚动，别把滚轮吞掉
    if (!isMascotWheelZoom()) return;
    event.preventDefault();
    wheelDelta += event.deltaY;
    if (wheelFrame) return;
    wheelFrame = window.requestAnimationFrame(() => {
      wheelFrame = 0;
      const delta = wheelDelta;
      wheelDelta = 0;
      if (!delta) return;
      // 一格滚轮约 100，触控板一次只给几十：折算成 2~8 个百分点
      const step = Math.max(-8, Math.min(8, Math.round(-delta / 14))) || (delta < 0 ? 2 : -2);
      const current = getMascotScale();
      const next = Math.max(60, Math.min(180, current + step));
      // 已经顶到头也把当前值亮一下，让人知道是到边界了而不是没反应
      if (next === current) {
        showSizeTag(current);
        return;
      }
      // 只改目标值，宽高的动效交给 CSS 过渡；连滚时它会从当前位置继续追
      markZooming();
      showSizeTag(setMascotScale(next));
    });
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

  let fitRaf = 0;
  let fitTimer = 0;
  let shapeRaf = 0;

  const onResize = () => {
    // 被页面收养时：宽高由容器决定，只按容器尺寸重排画布，
    // 别掺和「按视口比例回正」那套右下角锚点逻辑（会把她推出画面）
    if (state.embedded) {
      window.clearTimeout(fitTimer);
      if (!fitRaf) {
        fitRaf = window.requestAnimationFrame(() => {
          fitRaf = 0;
          fit();
        });
      }
      fitTimer = window.setTimeout(fit, 200);
      return;
    }
    // 视口尺寸变了（浏览器页面缩放 / 改窗口宽高）才把位置按比例放回去；
    // 只是改看板娘大小（mascot:resize）时视口没变，不能动她的位置
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const viewportChanged = vw !== viewport.w || vh !== viewport.h;
    viewport.w = vw;
    viewport.h = vh;
    if (viewportChanged) {
      restoreCenter();
      // 宽高是过渡过去的（视口变化也会带动 vw 基准变化），落定后再精确一次
      window.setTimeout(restoreCenter, 340);
    }
    applyOffset();
    // 宽高带过渡动画：缩放过程中先别按中间尺寸反复重排画布，
    // 等滚轮停下、过渡走完再按最终大小对齐一次（默认 220ms 过渡，留点余量）。
    if (!host?.classList.contains("is-zooming") && !fitRaf) {
      fitRaf = window.requestAnimationFrame(() => {
        fitRaf = 0;
        fit();
      });
    }
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(fit, 280);
  };

  pick?.addEventListener("click", () => {
    next();
  });

  // 宽高过渡结束（滚轮缩放、切模型、窗口尺寸变化）时按最终尺寸再对齐一次：
  // 画布 backing 停在旧尺寸就会被 CSS 拉扁，必须补这一刀
  host?.addEventListener("transitionend", (event) => {
    if (event.target !== host) return;
    if (event.propertyName !== "width" && event.propertyName !== "height") return;
    if (shapeRaf) return;
    shapeRaf = window.requestAnimationFrame(() => {
      shapeRaf = 0;
      fit();
      // 先按记忆里的中心回正（没有记忆就不动她），再把**落定后**的中心记下来。
      // 记忆绝不能在过渡中间刷新：半成品位置一旦被记住，之后每次刷新都会
      // 照着它修正，看板娘就会自己挪来挪去（0.6.13 修的位置漂移就是这个）。
      // 回正没成功（贴角 / 被边界夹住）时更不能记：那记下的是歪掉的位置。
      if (centerFrac && !restoreCenter()) return;
      rememberCenter();
    });
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
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onVisibility);
    window.addEventListener("resize", onResize);
    // 拖动 / 点击戳一戳都挂在看板娘自己身上（现在它接指针事件了）
    host?.addEventListener("pointerdown", onHostDown);
    host?.addEventListener("pointermove", onHostMove);
    host?.addEventListener("pointerup", onHostUp);
    host?.addEventListener("pointercancel", onHostCancel);
    // 指针捕获被系统收走时也要收尾，避免卡在拖动状态
    host?.addEventListener("lostpointercapture", (event) => endDrag(event, true));
    // 悬停时滚轮改大小（设置 → 装饰 里可关）
    host?.addEventListener("wheel", onHostWheel, { passive: false });
    // 设置页改大小后重新贴合画布
    window.addEventListener("mascot:resize", onResize);
    window.setTimeout(() => {
      refresh();
      startPolling();
    }, 600);
  }

  activeMascot = controller;
  // 视图可能比看板娘先渲染完（深链直接进 #/mascot），拿到实例后再通知一次
  window.dispatchEvent(new CustomEvent("mascot:ready"));
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
