/**
 * 在线预览器：全屏浮层。
 * 图片可缩放/拖动，音视频用原生播放器，PDF 用浏览器内置查看器，文本按代码展示。
 * 只负责展示，不修改任何文件。
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { store } from "../store.js";
import {
  buildVideoPlugins,
  loadRuntime,
  normalizeChapters,
  normalizeDanmaku,
  playerTheme,
} from "./artplayer-plugins.js";

const SUPPORTED = new Set(["image", "video", "audio", "pdf", "text"]);

const KIND_ICON = {
  image: "i-image",
  video: "i-star",
  audio: "i-star",
  pdf: "i-file",
  text: "i-file",
  none: "i-file",
};

const TEXT_PREVIEW_BYTES = 1_000_000;
const ZOOM_MIN = 1;
const ZOOM_MAX = 6;
const ARTPLAYER_SRC = "/static/vendor/artplayer/artplayer.min.js";

let active = null;
let artplayerPromise = null;

/** 按需加载 ArtPlayer（UMD 单文件，样式已内联，无 CDN 依赖） */
function loadArtplayer() {
  if (window.Artplayer) return Promise.resolve(window.Artplayer);
  if (!artplayerPromise) {
    artplayerPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = ARTPLAYER_SRC;
      script.async = true;
      script.onload = () => {
        if (!window.Artplayer) {
          reject(new Error("播放器初始化失败"));
          return;
        }
        window.Artplayer.LOG_VERSION = false; // 关掉控制台版本水印
        resolve(window.Artplayer);
      };
      script.onerror = () => {
        artplayerPromise = null;
        reject(new Error("播放器脚本加载失败"));
      };
      document.head.appendChild(script);
    });
  }
  return artplayerPromise;
}

export function canPreview(item) {
  return item?.type === "file" && SUPPORTED.has(item.previewKind);
}

export function isViewerOpen() {
  return Boolean(active);
}

export function closeViewer() {
  if (!active) return;
  const { host, keyHandler, restoreOverflow, player } = active;
  active = null;
  document.removeEventListener("keydown", keyHandler);
  document.body.style.overflow = restoreOverflow;
  host.classList.remove("is-open");
  if (player) {
    try {
      player.destroy(false);
    } catch {
      /* 播放器已销毁时忽略 */
    }
  }
  window.setTimeout(() => host.remove(), 220);
}

const SIDECAR_EMPTY = {
  subtitles: [],
  thumbnails: null,
  chapters: null,
  danmaku: null,
  chapterData: [],
  danmakuData: [],
};

/**
 * 缩略图 VTT 通常写成 `sprite.jpg#xywh=...` 这样的相对路径，
 * 浏览器会以为它在预览接口旁边，所以先把文件名换成真实预览地址，再包成 Blob URL。
 */
async function rewriteThumbnailVtt(url, siblings) {
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) return null;
    let text = await res.text();
    for (const [name, id] of Object.entries(siblings || {})) {
      if (!text.includes(name)) continue;
      text = text.split(name).join(`/api/nodes/${id}/preview`);
    }
    return URL.createObjectURL(new Blob([text], { type: "text/vtt;charset=utf-8" }));
  } catch {
    return null;
  }
}

/** 取同名伴生文件（字幕 / 缩略图 / 章节 / 弹幕），失败就当没有，绝不影响播放。 */
async function loadSidecars(node) {
  const fetchJson = (url) =>
    fetch(url, { credentials: "same-origin" })
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null);

  try {
    const info = await api.get(`/api/nodes/${node.id}/sidecars`);
    if (!info) return SIDECAR_EMPTY;
    const [chapters, danmaku, thumbnails] = await Promise.all([
      info.chapters ? fetchJson(info.chapters) : null,
      info.danmaku ? fetchJson(info.danmaku) : null,
      info.thumbnails ? rewriteThumbnailVtt(info.thumbnails, info.siblings) : null,
    ]);
    return {
      ...SIDECAR_EMPTY,
      ...info,
      thumbnails,
      subtitles: Array.isArray(info.subtitles) ? info.subtitles : [],
      chapterData: normalizeChapters(chapters),
      danmakuData: normalizeDanmaku(danmaku),
    };
  } catch {
    return SIDECAR_EMPTY;
  }
}

/**
 * @param {object} item 节点数据（含 id / name / size / previewKind）
 * @param {{onDownload?: (item:object)=>void}} options
 */
export function openViewer(item, { onDownload } = {}) {
  closeViewer();

  const kind = canPreview(item) ? item.previewKind : "none";
  const previewUrl = `/api/nodes/${item.id}/preview`;
  const downloadUrl = `/api/nodes/${item.id}/download`;

  const host = document.createElement("div");
  host.className = "viewer";
  host.innerHTML = `
    <div class="viewer__backdrop" data-act="close"></div>
    <div class="viewer__frame" role="dialog" aria-modal="true" aria-label="${escapeHtml(item.name)}">
      <header class="viewer__bar">
        <div class="viewer__ident">
          <span class="viewer__badge"><svg class="icon"><use href="#${KIND_ICON[kind]}"/></svg></span>
          <span class="viewer__name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          <span class="viewer__meta" data-meta>${formatBytes(item.size)}</span>
        </div>
        <div class="viewer__tools">
          ${
            kind === "image"
              ? `<div class="viewer__zoom">
                   <button class="icon-btn" type="button" data-act="zoom-out" title="缩小">−</button>
                   <button class="viewer__zoom-label" type="button" data-act="zoom-reset" title="恢复原始大小" data-zoom>100%</button>
                   <button class="icon-btn" type="button" data-act="zoom-in" title="放大">+</button>
                 </div>`
              : ""
          }
          ${kind === "text" ? `<button class="btn btn--subtle" type="button" data-act="copy">复制</button>` : ""}
          <a class="btn btn--subtle" href="${downloadUrl}">
            <svg class="icon icon--s"><use href="#i-download"/></svg>下载
          </a>
          <button class="icon-btn" type="button" data-act="close" title="关闭（Esc）">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </div>
      </header>
      <div class="viewer__body" data-body></div>
      <div class="viewer__load" data-load><div class="spinner"></div></div>
    </div>
  `;

  document.body.appendChild(host);
  const restoreOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  window.requestAnimationFrame(() => host.classList.add("is-open"));

  const bodyEl = host.querySelector("[data-body]");
  const loadEl = host.querySelector("[data-load]");
  const metaEl = host.querySelector("[data-meta]");
  const zoomLabel = host.querySelector("[data-zoom]");

  /* ---------------- 状态与通用动作 ---------------- */
  let zoom = 1;
  let imgEl = null;
  let stageEl = null;
  let fitW = 0; // 100% 时的显示宽度，作为放大基准
  let pendingZoom = false; // 图片未加载完就点了缩放
  let player = null; // ArtPlayer 实例（视频预览）

  const hideLoader = () => loadEl.classList.add("is-hidden");

  const fail = (message) => {
    hideLoader();
    bodyEl.innerHTML = `
      <div class="viewer__fallback">
        <div class="viewer__fallback-art"><svg class="icon"><use href="#i-alert"/></svg></div>
        <div class="viewer__fallback-title">预览失败</div>
        <div class="viewer__fallback-desc">${escapeHtml(message)}</div>
        <a class="btn btn--accent" href="${downloadUrl}">
          <svg class="icon icon--s"><use href="#i-download"/></svg>下载到本地
        </a>
      </div>`;
  };

  const syncZoom = () => {
    if (imgEl) {
      // 100%：按容器自适应（不把小图拉糊）；>100%：按「100% 显示宽 × 倍率」，
      // 不能用容器百分比——240px 小图在 1250px 容器里 125% 会变成 1500px 爆屏
      if (zoom <= 1) {
        imgEl.style.width = "";
        imgEl.style.maxWidth = "100%";
        imgEl.style.maxHeight = "100%";
      } else if (fitW) {
        imgEl.style.width = `${Math.round(fitW * zoom)}px`;
        imgEl.style.maxWidth = "none";
        imgEl.style.maxHeight = "none";
      } else {
        pendingZoom = true; // 图片还没加载完，等 load 事件里补算
      }
    }
    if (zoomLabel) zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    host.querySelector('[data-act="zoom-in"]')?.toggleAttribute("disabled", zoom >= ZOOM_MAX);
    host.querySelector('[data-act="zoom-out"]')?.toggleAttribute("disabled", zoom <= ZOOM_MIN);
  };

  const applyZoom = (value) => {
    if (!imgEl) return;
    zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(value * 100) / 100));
    syncZoom();
  };

  /* ---------------- ArtPlayer 视频播放器 ---------------- */
  async function mountVideo(container, node) {
    const ext = (node.ext || "." + (node.name.split(".").pop() || "")).toLowerCase();
    const sidecars = await loadSidecars(node);
    const runtimeName = ext === ".m3u8" ? "hls" : ext === ".mpd" ? "dash" : null;
    const runtime = runtimeName ? await loadRuntime(runtimeName) : null;

    try {
      const Artplayer = await loadArtplayer();
      if (!document.contains(container)) return null;

      const bundle = await buildVideoPlugins({ ext, sidecars, meta: store.get().meta });
      const playable = Boolean(runtimeName && runtime);

      const instance = new Artplayer({
        container,
        // 预览地址没有扩展名，显式告知类型，否则 ArtPlayer 无法判断
        url: previewUrl,
        type: playable ? runtimeName : "mp4",
        lang: "zh-cn",
        theme: playerTheme(),
        volume: 0.8,
        autoplay: false,
        hotkey: true,
        setting: true,
        playbackRate: true,
        aspectRatio: true,
        flip: true,
        fullscreen: true,
        fullscreenWeb: true,
        pip: true,
        miniProgressBar: true,
        airplay: true,
        lock: true,
        autoSize: false,
        plugins: bundle.plugins,
        customType: {
          // 分片流：m3u8 走 hls.js，mpd 走 dash.js（播放列表里的分片地址需为绝对地址）
          m3u8: (video, url, art) => {
            if (!runtime?.isSupported?.()) {
              video.src = url;
              return;
            }
            if (art.hls) art.hls.destroy();
            const hls = new runtime();
            hls.loadSource(url);
            hls.attachMedia(video);
            art.hls = hls;
            art.once("url", () => hls.destroy());
          },
          mpd: (video, url, art) => {
            if (!runtime?.MediaPlayer) {
              art.notice.show = "当前环境不支持 DASH 播放";
              return;
            }
            if (art.dash) art.dash.reset();
            const dash = runtime.MediaPlayer().create();
            dash.initialize(video, url, false);
            art.dash = dash;
            art.once("url", () => dash.reset());
          },
        },
        moreVideoAttr: {
          controls: false,
          playsinline: true,
          "webkit-playsinline": true,
          preload: "metadata",
        },
      });

      // 章节要等时长已知再写，否则插件会因时间点越界报错
      instance.on("video:loadedmetadata", () => {
        if (!bundle.chapter || !sidecars.chapterData?.length) return;
        try {
          bundle.chapter.update({ chapters: sidecars.chapterData });
        } catch (err) {
          console.warn("[viewer] 章节数据不合法，已忽略", err);
        }
      });
      instance.on("ready", hideLoader);
      instance.on("video:error", () => fail("视频无法播放，可能是编码不受浏览器支持。"));
      instance.on("destroy", () => {
        if (player === instance) player = null;
      });
      if (bundle.names.length) {
        metaEl.dataset.plugins = bundle.names.join(",");
      }
      return instance;
    } catch (err) {
      console.warn("[viewer] ArtPlayer 初始化失败，回退原生播放器", err);
      // 播放器或插件异常时退回原生 <video>，保证还能看
      if (!document.contains(container)) return null;
      container.innerHTML = `<video class="viewer__video" src="${previewUrl}" controls preload="metadata" playsinline></video>`;
      const video = container.querySelector("video");
      video?.addEventListener("loadeddata", hideLoader, { once: true });
      video?.addEventListener("error", () => fail("视频无法播放，可能是编码不受浏览器支持。"));
      return null;
    }
  }

  /* ---------------- 各类内容 ---------------- */
  if (kind === "image") {
    bodyEl.innerHTML = `<div class="viewer__stage" data-stage>
        <img class="viewer__img" data-img src="${previewUrl}" alt="${escapeHtml(item.name)}" draggable="false">
      </div>`;
    stageEl = bodyEl.querySelector("[data-stage]");
    imgEl = bodyEl.querySelector("[data-img]");
    imgEl.addEventListener("load", () => {
      hideLoader();
      // 记录 100% 时的实际显示尺寸作为缩放基准（大图此时是被缩小适配的）
      fitW = imgEl.clientWidth || imgEl.naturalWidth;
      if (pendingZoom) syncZoom();
      metaEl.textContent = `${formatBytes(item.size)} · ${imgEl.naturalWidth}×${imgEl.naturalHeight}`;
    });
    imgEl.addEventListener("error", () => fail("图片解码失败，可能已损坏或格式不受支持。"));
    syncZoom();
    bindImagePan(stageEl, () => zoom);
  } else if (kind === "video") {
    bodyEl.innerHTML = `<div class="viewer__stage is-video">
        <div class="viewer__player" data-player></div>
      </div>`;
    mountVideo(bodyEl.querySelector("[data-player]"), item).then((instance) => {
      if (!instance) return;
      if (!host.isConnected) {
        instance.destroy(false);
        return;
      }
      player = instance;
      if (active && active.host === host) active.player = instance;
    });
  } else if (kind === "audio") {
    bodyEl.innerHTML = `<div class="viewer__audio">
        <div class="viewer__audio-art"><svg class="icon"><use href="#i-star"/></svg></div>
        <div class="viewer__audio-name">${escapeHtml(item.name)}</div>
        <audio src="${previewUrl}" controls preload="metadata"></audio>
      </div>`;
    const audio = bodyEl.querySelector("audio");
    audio.addEventListener("loadedmetadata", hideLoader, { once: true });
    audio.addEventListener("error", () => fail("音频无法播放，可能是编码不受浏览器支持。"));
  } else if (kind === "pdf") {
    bodyEl.innerHTML = `<iframe class="viewer__pdf" src="${previewUrl}" title="${escapeHtml(item.name)}"></iframe>`;
    window.setTimeout(hideLoader, 500);
  } else if (kind === "text") {
    bodyEl.innerHTML = `<pre class="viewer__code" data-code></pre>`;
    const codeEl = bodyEl.querySelector("[data-code]");
    api
      .get(`/api/nodes/${item.id}/text`)
      .then((data) => {
        codeEl.textContent = data.content || "";
        const extra = [formatBytes(item.size), data.encoding.toUpperCase()];
        if (data.truncated) extra.push(`仅显示前 ${formatBytes(TEXT_PREVIEW_BYTES)}`);
        metaEl.textContent = extra.join(" · ");
        hideLoader();
      })
      .catch((err) => fail(err.message));
  } else {
    bodyEl.innerHTML = `<div class="viewer__fallback">
        <div class="viewer__fallback-art"><svg class="icon"><use href="#i-file"/></svg></div>
        <div class="viewer__fallback-title">暂不支持此格式</div>
        <div class="viewer__fallback-desc">${escapeHtml(item.name)} · ${formatBytes(item.size)}<br>可以下载到本地后再打开。</div>
        <a class="btn btn--accent" href="${downloadUrl}">
          <svg class="icon icon--s"><use href="#i-download"/></svg>下载到本地
        </a>
      </div>`;
    hideLoader();
  }

  /* ---------------- 交互 ---------------- */
  host.querySelectorAll('[data-act="close"]').forEach((el) => el.addEventListener("click", closeViewer));

  host.querySelector('a[href$="/download"]')?.addEventListener("click", () => {
    if (onDownload) onDownload(item);
  });

  host.querySelector('[data-act="zoom-in"]')?.addEventListener("click", () => applyZoom(zoom + 0.25));
  host.querySelector('[data-act="zoom-out"]')?.addEventListener("click", () => applyZoom(zoom - 0.25));
  host.querySelector('[data-act="zoom-reset"]')?.addEventListener("click", () => applyZoom(1));

  host.querySelector('[data-act="copy"]')?.addEventListener("click", async (event) => {
    const codeEl = bodyEl.querySelector("[data-code]");
    if (!codeEl) return;
    try {
      await navigator.clipboard.writeText(codeEl.textContent || "");
      const btn = event.currentTarget;
      btn.textContent = "已复制";
      window.setTimeout(() => (btn.textContent = "复制"), 1400);
    } catch {
      /* 剪贴板被拒时静默失败，用户可以手动选中 */
    }
  });

  // Ctrl/⌘ + 滚轮缩放（普通滚轮留给滚动）
  stageEl?.addEventListener(
    "wheel",
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      applyZoom(zoom + (event.deltaY < 0 ? 0.25 : -0.25));
    },
    { passive: false }
  );

  const keyHandler = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeViewer();
      return;
    }
    if (kind !== "image") return;
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      applyZoom(zoom + 0.25);
    } else if (event.key === "-") {
      event.preventDefault();
      applyZoom(zoom - 0.25);
    } else if (event.key === "0") {
      event.preventDefault();
      applyZoom(1);
    }
  };
  document.addEventListener("keydown", keyHandler);
  active = { host, keyHandler, restoreOverflow, player };

  host.querySelector('button[data-act="close"]')?.focus({ preventScroll: true });

  /* ---------------- 放大后按住拖动平移 ---------------- */
  function bindImagePan(stage, getZoom) {
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let startLeft = 0;
    let startTop = 0;

    stage.addEventListener("pointerdown", (event) => {
      if (getZoom() <= 1) return;
      dragging = true;
      startX = event.clientX;
      startY = event.clientY;
      startLeft = stage.scrollLeft;
      startTop = stage.scrollTop;
      stage.classList.add("is-grabbing");
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      stage.scrollLeft = startLeft - (event.clientX - startX);
      stage.scrollTop = startTop - (event.clientY - startY);
    });
    const stop = () => {
      dragging = false;
      stage.classList.remove("is-grabbing");
    };
    stage.addEventListener("pointerup", stop);
    stage.addEventListener("pointercancel", stop);
  }
}
