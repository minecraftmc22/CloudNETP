/**
 * 在线预览器：全屏浮层。
 * 图片可缩放/拖动，音视频用原生播放器，PDF 用浏览器内置查看器，文本按代码展示。
 * 只负责展示，不修改任何文件。
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { H } from "../i18n.js";
import { store } from "../store.js";
import { renderMarkdown } from "./markdown.js";
import { mountOffice, OFFICE_KINDS } from "./office.js";
import {
  buildVideoPlugins,
  loadRuntime,
  normalizeChapters,
  normalizeDanmaku,
  playerTheme,
} from "./artplayer-plugins.js";

const SUPPORTED = new Set([
  "image",
  "video",
  "audio",
  "pdf",
  "text",
  "markdown",
  "docx",
  "sheet",
  "slide",
  "odoc",
  "osheet",
  "oslide",
  "rtf",
  "doc",
  "ppt",
]);

const KIND_ICON = {
  image: "i-image",
  video: "i-video",
  audio: "i-music",
  pdf: "i-pdf",
  text: "i-text",
  markdown: "i-text",
  docx: "i-doc",
  doc: "i-doc",
  odoc: "i-doc",
  rtf: "i-doc",
  sheet: "i-sheet",
  osheet: "i-sheet",
  slide: "i-slide",
  oslide: "i-slide",
  ppt: "i-slide",
  none: "i-file",
  toolarge: "i-file",
};

/** 预览器顶部显示的类型名 */
function officeLabel(kind) {
  const labels = {
    docx: "Word 文档",
    doc: "Word 97-2003（仅文字）",
    odoc: "OpenDocument 文字",
    rtf: "RTF 富文本（仅文字）",
    sheet: "电子表格",
    osheet: "OpenDocument 表格",
    slide: "演示文稿",
    oslide: "OpenDocument 演示",
    ppt: "PowerPoint 97-2003（仅文字）",
  };
  return labels[kind] || "Office";
}

const TEXT_PREVIEW_BYTES = 1_000_000;
// 与后端 node_service.PREVIEW_MAX_BYTES 保持一致：文档类超过这个体积不给预览
const PREVIEW_MAX_BYTES = 50 * 1024 * 1024;
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
  const { host, keyHandler, restoreOverflow, player, cleanup } = active;
  try {
    cleanup?.fn?.();
  } catch {
    /* 清理失败不影响关闭 */
  }
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
  // 清掉 SMTC 挂载（媒体浮窗条目随预览关闭消失）
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = null;
      navigator.mediaSession.playbackState = "none";
    } catch {
      /* 忽略 */
    }
  }
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

  // toolarge 不是「不支持」，是文档类超过体积上限，提示语要说清楚
  const kind =
    item?.previewKind === "toolarge" ? "toolarge" : canPreview(item) ? item.previewKind : "none";
  const previewUrl = `/api/nodes/${item.id}/preview`;
  const downloadUrl = `/api/nodes/${item.id}/download`;

  const host = document.createElement("div");
  host.className = "viewer";
  host.innerHTML = H`
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
          ${
            kind === "text" || kind === "markdown"
              ? `<button class="btn btn--subtle" type="button" data-act="copy">复制</button>`
              : ""
          }
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
  const cleanup = { fn: null }; // Office 预览的清理钩子（blob URL / 监听器）

  const hideLoader = () => loadEl.classList.add("is-hidden");

  const fail = (message) => {
    hideLoader();
    bodyEl.innerHTML = H`
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
      container.innerHTML = H`<video class="viewer__video" src="${previewUrl}" controls preload="metadata" playsinline></video>`;
      const video = container.querySelector("video");
      video?.addEventListener("loadeddata", hideLoader, { once: true });
      video?.addEventListener("error", () => fail("视频无法播放，可能是编码不受浏览器支持。"));
      return null;
    }
  }

  /* ---------------- 音频：全屏歌词播放器 ---------------- */
  async function mountAudio(root, node) {
    const audio = root.querySelector("[data-lyr-audio]");
    const songEl = root.querySelector("[data-lyr-song]");
    const artistEl = root.querySelector("[data-lyr-artist]");
    const listEl = root.querySelector("[data-lyr-list]");
    const emptyEl = root.querySelector("[data-lyr-empty]");
    const stageEl = root.querySelector("[data-lyr-stage]");
    const curEl = root.querySelector("[data-lyr-cur]");
    const durEl = root.querySelector("[data-lyr-dur]");
    const seekEl = root.querySelector("[data-lyr-seek]");
    const volEl = root.querySelector("[data-lyr-vol]");
    const toggleBtn = root.querySelector("[data-lyr-toggle]");
    const icPlay = root.querySelector("[data-lyr-ic-play]");
    const icPause = root.querySelector("[data-lyr-ic-pause]");
    const coverImgs = [root.querySelector("[data-lyr-cover-img]"), root.querySelector("[data-lyr-big-img]")];
    const bgEl = root.querySelector("[data-lyr-bg]");
    const coverUrl = `/api/nodes/${node.id}/cover`;

    const fmt = (sec) => {
      if (!Number.isFinite(sec)) return "--:--";
      const s = Math.max(0, Math.round(sec));
      return H`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };

    let lines = [];
    let seeking = false;
    let userScrollUntil = 0;

    const showCover = () => {
      coverImgs.forEach((img) => {
        if (!img) return;
        img.src = coverUrl;
        img.hidden = false;
      });
      bgEl.style.backgroundImage = `url("${coverUrl}")`;
    };
    // 封面加载失败（无内嵌封面）就保持默认插画，404 静默
    root.querySelector("[data-lyr-cover-img]")?.addEventListener("error", () => {
      coverImgs.forEach((img) => img && (img.hidden = true));
    });

    /* --- 歌词渲染 --- */
    function renderLines(data) {
      lines = (data.lines || []).map((l) => ({ ...l }));
      // 对唱：按 agent 首次出现顺序左右分边（无 agent 的行保持默认）
      const agentSides = new Map();
      let agentCount = 0;
      listEl.innerHTML = "";
      lines.forEach((line) => {
        const li = document.createElement("li");
        li.className = "lyr__line";
        if (line.agent) {
          if (!agentSides.has(line.agent)) {
            agentSides.set(line.agent, agentCount % 2 === 0 ? "is-left" : "is-right");
            agentCount += 1;
          }
          li.classList.add(agentSides.get(line.agent));
        }
        const orig = document.createElement("div");
        orig.className = "lyr__orig";
        if (Array.isArray(line.words) && line.words.length) {
          line.wordEls = line.words.map((w) => {
            const span = document.createElement("span");
            span.className = "lyr__w";
            span.textContent = w.text;
            orig.appendChild(span);
            return span;
          });
        } else {
          const span = document.createElement("span");
          span.className = "lyr__w";
          span.textContent = line.text || "";
          orig.appendChild(span);
          line.lineEl = span; // 行级歌词：整行作为一个渐变单元做卡拉OK填充
        }
        li.appendChild(orig);
        if (line.trans) {
          const t = document.createElement("div");
          t.className = "lyr__trans";
          t.textContent = line.trans;
          li.appendChild(t);
        }
        if (line.bg) {
          const b = document.createElement("div");
          b.className = "lyr__bgv";
          b.textContent = line.bg;
          li.appendChild(b);
        }
        li.addEventListener("click", () => {
          if (line.end > line.start) audio.currentTime = line.start / 1000; // 静态文本不可跳播
          if (audio.paused) audio.play().catch(() => {});
        });
        listEl.appendChild(li);
        line.el = li;
      });
    }

    let activeSet = new Set();
    function setActives(idxs) {
      const next = new Set(idxs);
      let changed = next.size !== activeSet.size;
      if (!changed) for (const i of next) if (!activeSet.has(i)) { changed = true; break; }
      if (!changed) return;
      for (const i of activeSet) {
        if (next.has(i)) continue;
        const prev = lines[i];
        if (prev) {
          prev.el.classList.remove("is-active");
          (prev.wordEls || []).forEach((el) => el && el.style.removeProperty("--p"));
          prev.lineEl?.style.removeProperty("--p");
        }
      }
      for (const i of next) lines[i]?.el.classList.add("is-active");
      activeSet = next;
      // 对唱两行可能同唱：滚动以第一行为准
      const first = lines[idxs[0]];
      if (first && performance.now() > userScrollUntil) {
        listEl.scrollTo({
          top: first.el.offsetTop - stageEl.clientHeight * 0.36,
          behavior: "smooth",
        });
      }
    }

    function collectActive(ms) {
      const out = [];
      for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.start <= ms && ms < l.end) out.push(i);
      }
      return out;
    }

    function paint() {
      if (!document.contains(root)) return; // 关闭后停止循环
      const ms = audio.currentTime * 1000;

      let actives = collectActive(ms);
      if (!actives.length && activeSet.size) {
        // 行间小空隙保持上一行，间奏才熄灭（对唱取所有活跃行里最晚的行尾）
        let lastEnd = 0;
        for (const i of activeSet) lastEnd = Math.max(lastEnd, lines[i].end);
        if (ms < lastEnd + 9000) actives = [...activeSet];
      }
      setActives(actives);

      for (const i of activeSet) {
        const line = lines[i];
        if (line.wordEls && line.words) {
          for (let j = 0; j < line.words.length; j++) {
            const w = line.words[j];
            const dur = w.duration || w.end - w.start || 1;
            const p = ms >= w.end ? 1 : ms >= w.start ? (ms - w.start) / dur : 0;
            line.wordEls[j].style.setProperty("--p", p.toFixed(3));
          }
        } else if (line.lineEl && line.end > line.start) {
          // 行级歌词：按整行进度做卡拉OK式渐变填充
          const p = Math.min(1, Math.max(0, (ms - line.start) / (line.end - line.start)));
          line.lineEl.style.setProperty("--p", p.toFixed(3));
        }
      }

      if (!seeking) {
        const d = audio.duration || 0;
        if (d > 0) {
          const pct = (audio.currentTime / d) * 100;
          seekEl.value = Math.round((audio.currentTime / d) * 1000);
          seekEl.style.setProperty("--sp", `${pct}%`);
        }
        curEl.textContent = fmt(audio.currentTime);
      }
      window.requestAnimationFrame(paint);
    }
    window.requestAnimationFrame(paint);

    /* --- SMTC：MediaSession API（Windows 媒体浮窗 / 蓝牙耳机按键桥接） --- */
    const canMediaSession = "mediaSession" in navigator;
    const syncPositionState = () => {
      if (!canMediaSession || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
      try {
        navigator.mediaSession.setPositionState({
          duration: audio.duration,
          position: Math.min(audio.currentTime, audio.duration),
          playbackRate: audio.playbackRate || 1,
        });
      } catch {
        /* duration 异常时忽略 */
      }
    };
    const syncMediaSessionState = () => {
      if (!canMediaSession) return;
      try {
        navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing";
        syncPositionState();
      } catch {
        /* 忽略 */
      }
    };
    if (canMediaSession) {
      const actions = {
        play: () => audio.play().catch(() => {}),
        pause: () => audio.pause(),
        stop: () => {
          audio.pause();
          audio.currentTime = 0;
        },
        seekbackward: (details) => {
          audio.currentTime = Math.max(0, audio.currentTime - (details.seekOffset || 10));
        },
        seekforward: (details) => {
          audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + (details.seekOffset || 10));
        },
        seekto: (details) => {
          if (Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime;
        },
      };
      for (const [action, fn] of Object.entries(actions)) {
        try {
          navigator.mediaSession.setActionHandler(action, fn);
        } catch {
          /* 不支持的动作忽略 */
        }
      }
    }

    /* --- 控制栏 --- */
    const syncPlayIcon = () => {
      // 注意：SVG 元素没有 hidden IDL 属性，el.hidden=true 只是 expando，
      // 必须操作真实 attribute 才能被 [hidden] 样式命中（否则图标永远不切换）
      icPlay.toggleAttribute("hidden", !audio.paused);
      icPause.toggleAttribute("hidden", audio.paused);
      syncMediaSessionState();
    };
    const togglePlay = () => {
      if (audio.paused) audio.play().catch(() => {});
      else audio.pause();
    };
    toggleBtn.addEventListener("click", togglePlay);
    root.querySelector("[data-lyr-bigcover]")?.addEventListener("click", togglePlay);
    audio.addEventListener("play", syncPlayIcon);
    audio.addEventListener("pause", syncPlayIcon);
    syncPlayIcon();

    audio.addEventListener("loadedmetadata", () => {
      durEl.textContent = fmt(audio.duration);
      hideLoader();
      syncPositionState();
    });
    audio.addEventListener("seeked", syncPositionState);
    audio.addEventListener("timeupdate", syncPositionState);
    audio.addEventListener("error", () => {
      fail("音频无法播放，可能是编码不受浏览器支持。");
    });
    audio.addEventListener("ended", () => setActives([]));

    seekEl.addEventListener("input", () => {
      seeking = true;
      const d = audio.duration || 0;
      if (d > 0) {
        const t = (Number(seekEl.value) / 1000) * d;
        curEl.textContent = fmt(t);
        seekEl.style.setProperty("--sp", `${Number(seekEl.value) / 10}%`);
      }
    });
    seekEl.addEventListener("change", () => {
      const d = audio.duration || 0;
      if (d > 0) audio.currentTime = (Number(seekEl.value) / 1000) * d;
      seeking = false;
    });
    volEl.addEventListener("input", () => {
      audio.volume = Number(volEl.value) / 100;
      volEl.style.setProperty("--vp", `${volEl.value}%`);
    });
    audio.volume = Number(volEl.value) / 100;

    root.querySelector("[data-lyr-mode]")?.addEventListener("click", () => {
      root.classList.toggle("is-art");
    });

    // 手动滚动歌词时暂停自动跟随 3 秒
    const pauseFollow = () => {
      userScrollUntil = performance.now() + 3000;
    };
    listEl.addEventListener("wheel", pauseFollow, { passive: true });
    listEl.addEventListener("touchmove", pauseFollow, { passive: true });

    /* --- 数据加载：先元数据（本地解析，快），再歌词（走代理） --- */
    let meta = null;
    try {
      meta = await api.get(`/api/nodes/${node.id}/audio-meta`);
    } catch {
      meta = null;
    }
    if (!document.contains(root)) return;

    const title = meta?.title?.trim() || node.name.replace(/\.[^.]+$/, "");
    const artist = meta?.artist?.trim() || "";
    songEl.textContent = title;
    artistEl.textContent = [artist, meta?.album?.trim()].filter(Boolean).join(" — ");
    metaEl.textContent = [artist || null, meta?.duration ? fmt(meta.duration) : formatBytes(node.size)]
      .filter(Boolean)
      .join(" · ");
    if (meta?.hasCover) showCover();

    // SMTC 元数据：标题 / 歌手 / 专辑 / 封面（Windows 媒体浮窗直接显示）
    if (canMediaSession) {
      try {
        navigator.mediaSession.metadata = new MediaMetadata({
          title,
          artist: artist || undefined,
          album: meta?.album?.trim() || undefined,
          artwork: meta?.hasCover ? [{ src: coverUrl, sizes: "512x512" }] : [],
        });
      } catch {
        /* MediaMetadata 不可用时忽略 */
      }
    }

    /* --- 歌词加载：走解析接口（自动=内嵌→在线→无，也可被每首歌的偏好覆盖） --- */
    let artDueEmpty = false;
    const showEmpty = (msg) => {
      emptyEl.textContent = msg;
      emptyEl.hidden = false;
      if (!root.classList.contains("is-art")) {
        root.classList.add("is-art"); // 没歌词就落在封面视图
        artDueEmpty = true;
      }
    };
    const loadLyrics = async () => {
      lines = [];
      activeSet = new Set();
      listEl.innerHTML = "";
      emptyEl.hidden = true;
      root.classList.remove("is-static");
      if (artDueEmpty) {
        root.classList.remove("is-art");
        artDueEmpty = false;
      }
      let data;
      try {
        data = await api.get(`/api/nodes/${node.id}/lyrics`);
      } catch (err) {
        if (!document.contains(root)) return;
        showEmpty(err?.message || "歌词加载失败");
        return;
      }
      if (!document.contains(root)) return;
      if (!data.lines || !data.lines.length) {
        showEmpty(data.error || "这首歌没有歌词");
        return;
      }
      if (data.unsynced) root.classList.add("is-static"); // 纯文本歌词：不高亮不跟随
      renderLines(data);
    };

    /* --- 歌词设置面板：来源（每首歌） + 样式（跟随账号） --- */
    const panel = root.querySelector("[data-lyr-panel]");
    const panelCard = root.querySelector("[data-lyr-panel-card]");
    const segEl = root.querySelector("[data-lyr-srcseg]");
    const thumbEl = root.querySelector("[data-lyr-thumb]");
    const segHintEl = root.querySelector("[data-lyr-srchint]");
    const statusEl = root.querySelector("[data-lyr-panel-status]");
    const searchInput = root.querySelector("[data-lyr-search]");
    const resultsEl = root.querySelector("[data-lyr-results]");
    const customEl = root.querySelector("[data-lyr-custom]");
    const sizeInput = root.querySelector("[data-lyr-size]");
    const sizeVal = root.querySelector("[data-lyr-size-val]");
    const transInput = root.querySelector("[data-lyr-trans]");
    const swatchesEl = root.querySelector("[data-lyr-swatches]");
    const colorInput = root.querySelector("[data-lyr-color]");
    const panes = {};
    root.querySelectorAll("[data-srcpane]").forEach((el) => (panes[el.dataset.srcpane] = el));

    const SRC_HINT = {
      auto: "按优先级自动匹配：音频内嵌歌词 → 在线搜索 → 无歌词",
      song: "到歌词库手动挑这首歌用的歌词，点结果即启用",
      custom: "直接使用自己粘贴的歌词文本（支持 LRC）",
      none: "播放时不显示歌词",
    };

    // 样式跟随账号：读全局设置，即时应用 + 防抖保存
    const lyrStyle = {
      fontSize: 22,
      hotColor: "",
      showTrans: true,
      ...(store.get().user?.settings?.lyricsStyle || {}),
    };
    const applyStyle = () => {
      root.style.setProperty("--lyr-size", `${lyrStyle.fontSize}px`);
      if (lyrStyle.hotColor) root.style.setProperty("--lyr-hot", lyrStyle.hotColor);
      else root.style.removeProperty("--lyr-hot");
      root.classList.toggle("no-trans", lyrStyle.showTrans === false);
    };
    let styleTimer = 0;
    const saveStyle = () => {
      window.clearTimeout(styleTimer);
      styleTimer = window.setTimeout(() => {
        api
          .patch("/api/me/settings", { lyricsStyle: { ...lyrStyle } })
          .then(() => {
            const u = store.get().user;
            if (u?.settings) u.settings.lyricsStyle = { ...lyrStyle };
          })
          .catch(() => {});
      }, 400);
    };

    const SWATCHES = [
      { label: "跟随主题", value: "" },
      { label: "白", value: "#ffffff" },
      { label: "金", value: "#ffd166" },
      { label: "青", value: "#7ee8fa" },
      { label: "紫", value: "#d8b4fe" },
      { label: "粉", value: "#ff8fb8" },
    ];
    const syncSwatchActive = () => {
      [...swatchesEl.children].forEach((el, i) =>
        el.classList.toggle("is-active", SWATCHES[i].value === (lyrStyle.hotColor || ""))
      );
    };
    SWATCHES.forEach((sw) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "lyr__swatch";
      b.title = sw.label;
      b.setAttribute("aria-label", `高亮色：${sw.label}`);
      if (sw.value) b.style.background = sw.value;
      else b.classList.add("lyr__swatch--auto");
      b.addEventListener("click", () => {
        lyrStyle.hotColor = sw.value;
        colorInput.value = sw.value || "#ffffff";
        syncSwatchActive();
        applyStyle();
        saveStyle();
      });
      swatchesEl.appendChild(b);
    });
    sizeInput.value = lyrStyle.fontSize;
    sizeVal.textContent = `${lyrStyle.fontSize}px`;
    sizeInput.addEventListener("input", () => {
      lyrStyle.fontSize = Number(sizeInput.value) || 22;
      sizeVal.textContent = `${lyrStyle.fontSize}px`;
      applyStyle();
      saveStyle();
    });
    transInput.checked = lyrStyle.showTrans !== false;
    transInput.addEventListener("change", () => {
      lyrStyle.showTrans = transInput.checked;
      applyStyle();
      saveStyle();
    });
    colorInput.value = lyrStyle.hotColor || "#ffffff";
    colorInput.addEventListener("input", () => {
      lyrStyle.hotColor = colorInput.value;
      syncSwatchActive();
      applyStyle();
      saveStyle();
    });
    applyStyle();

    let paneMode = "auto";
    const setPanelMode = (mode) => {
      paneMode = mode;
      segEl.querySelectorAll("[data-src]").forEach((b) => b.classList.toggle("is-active", b.dataset.src === mode));
      const btn = segEl.querySelector(`[data-src="${mode}"]`);
      if (btn && thumbEl && btn.offsetWidth) {
        thumbEl.style.width = `${btn.offsetWidth}px`;
        thumbEl.style.transform = `translateX(${btn.offsetLeft - 3}px)`;
      }
      Object.entries(panes).forEach(([key, el]) => (el.hidden = key !== mode));
      segHintEl.textContent = SRC_HINT[mode] || "";
    };
    const status = (msg) => {
      statusEl.textContent = msg;
      statusEl.hidden = false;
      window.setTimeout(() => (statusEl.hidden = true), 2400);
    };
    const applyPref = async (body) => {
      try {
        await api.put(`/api/nodes/${node.id}/lyrics-pref`, body);
        closePanel();
        loadLyrics();
      } catch (err) {
        status(err?.message || "保存失败");
      }
    };

    /* 搜索歌词库（搜索指定歌词） */
    const doSearch = async () => {
      const q = searchInput.value.trim();
      if (!q) return;
      resultsEl.innerHTML = H`<p class="lyr__panel-hint">搜索中…</p>`;
      try {
        const data = await api.get(`/api/lyrics/search?q=${encodeURIComponent(q)}`);
        if (!document.contains(root)) return;
        if (!data.items?.length) {
          resultsEl.innerHTML = H`<p class="lyr__panel-hint">没有找到，换个关键词试试</p>`;
          return;
        }
        resultsEl.innerHTML = "";
        data.items.forEach((it) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "lyr__result";
          btn.innerHTML =
            H`<span class="lyr__result-title">${escapeHtml(it.title || "未知标题")}</span>` +
            `<span class="lyr__result-artist">${escapeHtml([it.artist, it.album].filter(Boolean).join(" · "))}</span>`;
          btn.addEventListener("click", () =>
            applyPref({ mode: "song", songId: it.id, songTitle: it.title || "", songArtist: it.artist || "" })
          );
          resultsEl.appendChild(btn);
        });
      } catch (err) {
        if (!document.contains(root)) return;
        resultsEl.innerHTML = H`<p class="lyr__panel-hint">${escapeHtml(err?.message || "搜索失败")}</p>`;
      }
    };
    let searchTimer = 0;
    searchInput.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(doSearch, 450);
    });
    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        window.clearTimeout(searchTimer);
        doSearch();
      }
    });
    root.querySelector("[data-lyr-save-custom]").addEventListener("click", () => {
      if (!customEl.value.trim()) {
        status("先粘贴歌词内容");
        return;
      }
      applyPref({ mode: "custom", customLyrics: customEl.value });
    });
    root.querySelector("[data-lyr-use-auto]").addEventListener("click", () => applyPref({ mode: "auto" }));

    segEl.querySelectorAll("[data-src]").forEach((b) => {
      b.addEventListener("click", () => {
        const mode = b.dataset.src;
        setPanelMode(mode);
        if (mode === "song" && !searchInput.value.trim() && qTitle) {
          searchInput.value = qTitle; // 预填元数据里的歌名，直接搜
          doSearch();
        }
        if (mode === "auto") applyPref({ mode: "auto" });
        if (mode === "none") applyPref({ mode: "none" });
      });
    });

    const closePanel = () => {
      panel.hidden = true;
    };
    const openPanel = async () => {
      panel.hidden = false;
      try {
        const pref = await api.get(`/api/nodes/${node.id}/lyrics-pref`);
        if (!document.contains(root) || panel.hidden) return;
        customEl.value = pref.customLyrics || "";
        requestAnimationFrame(() => setPanelMode(pref.mode || "auto"));
        if ((pref.mode || "auto") === "song" && pref.song) {
          searchInput.value = pref.song.title || "";
          resultsEl.innerHTML = H`<p class="lyr__panel-hint">当前：${escapeHtml(
            [pref.song.title, pref.song.artist].filter(Boolean).join(" — ") || "指定歌曲"
          )}，重新搜索可更换</p>`;
        }
      } catch {
        requestAnimationFrame(() => setPanelMode("auto"));
      }
    };
    root.querySelector("[data-lyr-settings]").addEventListener("click", (event) => {
      event.stopPropagation();
      if (panel.hidden) openPanel();
      else closePanel();
    });
    root.querySelector("[data-lyr-panel-close]").addEventListener("click", closePanel);
    panel.addEventListener("click", (event) => {
      if (!panelCard.contains(event.target)) closePanel();
    });

    // 文件名兜底：常见「歌手 - 歌名.mp3」格式
    let qArtist = artist;
    let qTitle = title;
    if (!artist && title.includes(" - ")) {
      const [a, ...rest] = title.split(" - ");
      if (rest.length) {
        qArtist = a.trim();
        qTitle = rest.join(" - ").trim();
      }
    }

    loadLyrics();
    hideLoader();
  }

  /* ---------------- 各类内容 ---------------- */
  if (kind === "image") {
    bodyEl.innerHTML = H`<div class="viewer__stage" data-stage>
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
    bodyEl.innerHTML = H`<div class="viewer__stage is-video">
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
    bodyEl.innerHTML = H`
      <div class="lyr" data-lyr>
        <div class="lyr__bg" data-lyr-bg></div>
        <div class="lyr__veil"></div>
        <header class="lyr__head">
          <div class="lyr__cover" data-lyr-cover>
            <img data-lyr-cover-img alt="" hidden>
            <svg class="icon"><use href="#i-star"/></svg>
          </div>
          <div class="lyr__titles">
            <div class="lyr__song" data-lyr-song>${escapeHtml(item.name)}</div>
            <div class="lyr__artist" data-lyr-artist></div>
          </div>
        </header>
        <div class="lyr__stage" data-lyr-stage>
          <ul class="lyr__list" data-lyr-list></ul>
          <div class="lyr__artview" data-lyr-art>
            <div class="lyr__bigcover" data-lyr-bigcover>
              <img data-lyr-big-img alt="" hidden>
              <svg class="icon"><use href="#i-star"/></svg>
            </div>
          </div>
          <div class="lyr__empty" data-lyr-empty hidden></div>
        </div>
        <div class="lyr__bar">
          <button class="lyr__toggle" type="button" data-lyr-toggle title="播放 / 暂停（空格）">
            <svg data-lyr-ic-pause viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>
            <svg data-lyr-ic-play viewBox="0 0 24 24" fill="currentColor" hidden><path d="M8 5.6c0-1 1.1-1.6 2-1.1l9 5.9c.8.5.8 1.7 0 2.2l-9 5.9c-.9.5-2-.1-2-1.1V5.6z"/></svg>
          </button>
          <span class="lyr__time" data-lyr-cur>0:00</span>
          <input class="lyr__seek" data-lyr-seek type="range" min="0" max="1000" value="0" aria-label="播放进度">
          <span class="lyr__time" data-lyr-dur>--:--</span>
          <div class="lyr__vol">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 9.5v5c0 .6.4 1 1 1h3l4.3 3.6c.7.6 1.7.1 1.7-.8V5.7c0-.9-1-1.4-1.7-.8L8 8.5H5c-.6 0-1 .4-1 1zm12.5 1.5c0-1.3-.6-2.4-1.5-3.1v6.2c.9-.7 1.5-1.8 1.5-3.1zm-1.5-7v2.1c2.3.8 4 3 4 5.4s-1.7 4.6-4 5.4v2.1c3.4-.9 6-4 6-7.5s-2.6-6.6-6-7.5z"/></svg>
            <input data-lyr-vol type="range" min="0" max="100" value="80" aria-label="音量">
          </div>
          <button class="lyr__mode" type="button" data-lyr-mode title="歌词 / 封面">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h10M4 18h13"/></svg>
          </button>
          <button class="lyr__mode" type="button" data-lyr-settings title="歌词设置">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.2 13.8a1.6 1.6 0 0 0 .32 1.76l.06.06a1.9 1.9 0 1 1-2.7 2.7l-.05-.06a1.6 1.6 0 0 0-1.76-.32 1.6 1.6 0 0 0-.97 1.47v.17a1.9 1.9 0 1 1-3.8 0v-.09a1.6 1.6 0 0 0-1.05-1.47 1.6 1.6 0 0 0-1.76.32l-.06.06a1.9 1.9 0 1 1-2.7-2.7l.06-.05a1.6 1.6 0 0 0 .32-1.76 1.6 1.6 0 0 0-1.47-.97H3.5a1.9 1.9 0 1 1 0-3.8h.09a1.6 1.6 0 0 0 1.47-1.05 1.6 1.6 0 0 0-.32-1.76l-.06-.06a1.9 1.9 0 1 1 2.7-2.7l.05.06a1.6 1.6 0 0 0 1.76.32h.08a1.6 1.6 0 0 0 .97-1.47V3.5a1.9 1.9 0 1 1 3.8 0v.09a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.76-.32l.06-.06a1.9 1.9 0 1 1 2.7 2.7l-.06.05a1.6 1.6 0 0 0-.32 1.76v.08a1.6 1.6 0 0 0 1.47.97h.17a1.9 1.9 0 1 1 0 3.8h-.09a1.6 1.6 0 0 0-1.47.97z"/></svg>
          </button>
        </div>
        <div class="lyr__panel" data-lyr-panel hidden>
          <div class="lyr__panel-card" data-lyr-panel-card>
            <header class="lyr__panel-head">
              <span class="lyr__panel-title">歌词设置</span>
              <button class="lyr__panel-close" type="button" data-lyr-panel-close aria-label="关闭">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
              </button>
            </header>
            <div class="lyr__panel-body">
              <div class="lyr__panel-label">歌词来源</div>
              <div class="segmented" data-lyr-srcseg>
                <div class="segmented__thumb" data-lyr-thumb></div>
                <button class="segmented__btn" type="button" data-src="auto">自动</button>
                <button class="segmented__btn" type="button" data-src="song">搜索指定</button>
                <button class="segmented__btn" type="button" data-src="custom">使用文本</button>
                <button class="segmented__btn" type="button" data-src="none">无歌词</button>
              </div>
              <p class="lyr__panel-hint" data-lyr-srchint></p>
              <div class="lyr__srcpane" data-srcpane="auto" hidden>
                <button class="btn btn--accent" type="button" data-lyr-use-auto>改回自动匹配</button>
              </div>
              <div class="lyr__srcpane" data-srcpane="song" hidden>
                <div class="textbox lyr__searchbox">
                  <svg class="icon"><use href="#i-search"/></svg>
                  <input data-lyr-search placeholder="搜索歌名 / 歌手" maxlength="80">
                </div>
                <div class="lyr__results" data-lyr-results></div>
              </div>
              <div class="lyr__srcpane" data-srcpane="custom" hidden>
                <textarea class="lyr__custom" data-lyr-custom rows="6" placeholder="粘贴 LRC 歌词，支持 [mm:ss.xx] 行级与 <mm:ss.xx> 词级时间戳"></textarea>
                <button class="btn btn--accent" type="button" data-lyr-save-custom>保存并使用</button>
              </div>
              <div class="lyr__panel-label">歌词样式</div>
              <div class="lyr__stylerow">
                <span>字号</span>
                <input type="range" min="16" max="34" step="1" data-lyr-size aria-label="歌词字号">
                <span class="lyr__styleval" data-lyr-size-val></span>
              </div>
              <div class="lyr__stylerow">
                <span>显示翻译</span>
                <label class="switch"><input type="checkbox" data-lyr-trans><span class="switch__track"><span class="switch__knob"></span></span></label>
              </div>
              <div class="lyr__stylerow">
                <span>高亮色</span>
                <div class="lyr__swatches" data-lyr-swatches></div>
                <input type="color" data-lyr-color title="自定义高亮色" aria-label="自定义高亮色">
              </div>
              <p class="lyr__panel-status" data-lyr-panel-status hidden></p>
            </div>
          </div>
        </div>
        <audio data-lyr-audio src="${previewUrl}" preload="metadata"></audio>
      </div>`;
    mountAudio(bodyEl.querySelector("[data-lyr]"), item);
  } else if (kind === "pdf") {
    bodyEl.innerHTML = H`<iframe class="viewer__pdf" src="${previewUrl}" title="${escapeHtml(item.name)}"></iframe>`;
    window.setTimeout(hideLoader, 500);
  } else if (kind === "text") {
    bodyEl.innerHTML = H`<pre class="viewer__code" data-code></pre>`;
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
  } else if (kind === "markdown") {
    bodyEl.innerHTML = H`<div class="md-wrap"><article class="md-body" data-md></article><pre data-code hidden></pre></div>`;
    const article = bodyEl.querySelector("[data-md]");
    const sourceEl = bodyEl.querySelector("[data-code]");
    api
      .get(`/api/nodes/${item.id}/text`)
      .then((data) => {
        const text = data.content || "";
        sourceEl.textContent = text;
        article.innerHTML = renderMarkdown(text);
        const extra = [formatBytes(item.size), "Markdown"];
        if (data.truncated) extra.push(`仅解析前 ${formatBytes(TEXT_PREVIEW_BYTES)}`);
        metaEl.textContent = extra.join(" · ");
        hideLoader();
      })
      .catch((err) => fail(err.message));
  } else if (OFFICE_KINDS.has(kind)) {
    bodyEl.innerHTML = H`<div class="office-host" data-office></div>`;
    mountOffice(bodyEl.querySelector("[data-office]"), item, kind)
      .then((dispose) => {
        cleanup.fn = typeof dispose === "function" ? dispose : null;
        metaEl.textContent = `${formatBytes(item.size)} · ${officeLabel(kind)}`;
        hideLoader();
      })
      .catch((err) => fail(err.message));
  } else if (kind === "toolarge") {
    bodyEl.innerHTML = H`<div class="viewer__fallback">
        <div class="viewer__fallback-art"><svg class="icon"><use href="#i-download"/></svg></div>
        <div class="viewer__fallback-title">文件太大，不在线预览</div>
        <div class="viewer__fallback-desc">${escapeHtml(item.name)} · ${formatBytes(item.size)}<br>超过 ${formatBytes(PREVIEW_MAX_BYTES)} 的文档需要下载到本地打开。</div>
        <a class="btn btn--accent" href="${downloadUrl}">
          <svg class="icon icon--s"><use href="#i-download"/></svg>下载到本地
        </a>
      </div>`;
    hideLoader();
  } else {
    bodyEl.innerHTML = H`<div class="viewer__fallback">
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
      // 歌词设置面板先关，再关预览器
      const lyrPanel = bodyEl.querySelector("[data-lyr-panel]");
      if (lyrPanel && !lyrPanel.hidden) {
        lyrPanel.hidden = true;
        return;
      }
      closeViewer();
      return;
    }
    if (kind === "audio") {
      const audio = bodyEl.querySelector("[data-lyr-audio]");
      if (audio && (event.key === " " || event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault();
        if (event.key === " ") {
          if (audio.paused) audio.play().catch(() => {});
          else audio.pause();
        } else {
          audio.currentTime = Math.max(
            0,
            Math.min(audio.duration || Infinity, audio.currentTime + (event.key === "ArrowRight" ? 5 : -5))
          );
        }
        return;
      }
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
  active = { host, keyHandler, restoreOverflow, player, cleanup };

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
