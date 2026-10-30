/**
 * ArtPlayer 插件层。
 *
 * 所有插件与 HLS/DASH 运行时都放在 static/vendor 下本地加载，不走 CDN；
 * 每个插件都是「按需加载 + 失败降级」——任何一个加载失败都只让该功能消失，
 * 播放器本体照常工作。
 *
 * 各插件的启用条件（见 buildVideoPlugins）：
 *   ambilight          始终启用（二次元氛围光）
 *   danmuku            始终启用，数据来自同名 <video>.danmaku.json
 *   multiple-subtitles 同目录存在同名 .vtt/.srt/.ass 时启用
 *   vtt-thumbnail      同目录存在 <video>.thumbs.vtt 时启用
 *   chapter            同目录存在 <video>.chapters.json 时启用
 *   chromecast         仅当 .env 里 PREVIEW_CHROMECAST=true
 *   vast               仅当 .env 里 PREVIEW_VAST_TAG 非空
 *   hls-control        .m3u8 且 hls.js 加载成功
 *   dash-control       .mpd 且 dash.js 加载成功
 */

const PLUGIN_DIR = "/static/vendor/artplayer/plugins/";
const RUNTIME = {
  hls: { src: "/static/vendor/hls.min.js", global: "Hls" },
  dash: { src: "/static/vendor/dash.min.js", global: "dashjs" },
};

export const PLUGIN_SPECS = {
  ambilight: { file: "ambilight.js", global: "artplayerPluginAmbilight", label: "氛围光" },
  danmuku: { file: "danmuku.js", global: "artplayerPluginDanmuku", label: "弹幕" },
  subtitles: { file: "multiple-subtitles.js", global: "artplayerPluginMultipleSubtitles", label: "多字幕" },
  chapter: { file: "chapter.js", global: "artplayerPluginChapter", label: "章节" },
  thumbnail: { file: "vtt-thumbnail.js", global: "artplayerPluginVttThumbnail", label: "进度缩略图" },
  chromecast: { file: "chromecast.js", global: "artplayerPluginChromecast", label: "投屏" },
  hls: { file: "hls-control.js", global: "artplayerPluginHlsControl", label: "HLS 清晰度" },
  dash: { file: "dash-control.js", global: "artplayerPluginDashControl", label: "DASH 清晰度" },
  vast: { file: "vast.js", global: "artplayerPluginVast", label: "贴片广告" },
};

const scriptCache = new Map();

/** 注入一次脚本；重复调用复用同一个 Promise。 */
function loadScript(src) {
  if (scriptCache.has(src)) return scriptCache.get(src);
  const task = new Promise((resolve, reject) => {
    const existed = document.querySelector(`script[data-src="${src}"]`);
    if (existed?.dataset.loaded === "1") {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.dataset.src = src;
    script.onload = () => {
      script.dataset.loaded = "1";
      resolve();
    };
    script.onerror = () => {
      scriptCache.delete(src);
      script.remove();
      reject(new Error(`脚本加载失败：${src}`));
    };
    document.head.appendChild(script);
  });
  scriptCache.set(src, task);
  return task;
}

/** 加载单个插件，返回它的全局工厂函数；失败返回 null（不影响播放）。 */
export async function loadPlugin(name) {
  const spec = PLUGIN_SPECS[name];
  if (!spec) return null;
  if (window[spec.global]) return window[spec.global];
  try {
    await loadScript(PLUGIN_DIR + spec.file);
    return window[spec.global] || null;
  } catch (err) {
    console.warn("[viewer] 插件不可用：", name, err.message);
    return null;
  }
}

/** 加载 HLS / DASH 运行时（hls.js / dash.js），失败返回 null。 */
export async function loadRuntime(name) {
  const spec = RUNTIME[name];
  if (!spec) return null;
  if (window[spec.global]) return window[spec.global];
  try {
    await loadScript(spec.src);
    return window[spec.global] || null;
  } catch (err) {
    console.warn("[viewer] 运行时不可用：", name, err.message);
    return null;
  }
}

/** 当前实际可用的插件清单（调试用）。 */
export function pluginStatus() {
  const status = {};
  for (const [name, spec] of Object.entries(PLUGIN_SPECS)) {
    status[name] = Boolean(window[spec.global]);
  }
  status.hlsRuntime = Boolean(window.Hls);
  status.dashRuntime = Boolean(window.dashjs);
  return status;
}

const ACCENT_FALLBACK = "#c084fc";

function accentColor() {
  return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || ACCENT_FALLBACK;
}

/** 弹幕数据归一化：接受 [{"time":12.5,"text":"前排","color":"#fff","mode":0}]。 */
export function normalizeDanmaku(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item) => item && Number.isFinite(Number(item.time)) && String(item.text ?? "").trim())
    .map((item) => ({
      time: Math.max(0, Number(item.time)),
      text: String(item.text).trim().slice(0, 200),
      color: /^#[0-9a-f]{6}$/i.test(item.color || "") ? item.color : "#FFFFFF",
      mode: [0, 1, 2].includes(Number(item.mode)) ? Number(item.mode) : 0,
    }))
    .sort((a, b) => a.time - b.time);
}

/** 章节数据归一化：按开始时间排序，空缺处补空标题，最后一段用 Infinity 交给插件补齐。 */
export function normalizeChapters(raw) {
  if (!Array.isArray(raw)) return [];
  const list = raw
    .filter((item) => item && Number.isFinite(Number(item.start)))
    .map((item) => ({
      start: Math.max(0, Number(item.start)),
      end: Number.isFinite(Number(item.end)) ? Math.max(0, Number(item.end)) : null,
      title: String(item.title ?? "").slice(0, 120),
    }))
    .sort((a, b) => a.start - b.start);
  return list.map((item, index) => ({
    start: item.start,
    end: item.end && item.end > item.start ? item.end : list[index + 1]?.start ?? Infinity,
    title: item.title,
  }));
}

/**
 * 组装视频插件列表。
 * @param {{ext?: string, sidecars?: object, meta?: object}} ctx
 * @returns {Promise<{plugins: Function[], chapter: object|null, hls: boolean, dash: boolean, names: string[]}>}
 */
export async function buildVideoPlugins({ ext = "", sidecars = {}, meta = null }) {
  const preview = meta?.preview || {};
  const isHls = ext === ".m3u8";
  const isDash = ext === ".mpd";

  const want = [
    "ambilight",
    "danmuku",
    ...(sidecars.subtitles?.length ? ["subtitles"] : []),
    ...(sidecars.thumbnails ? ["thumbnail"] : []),
    ...(sidecars.chapters ? ["chapter"] : []),
    ...(preview.chromecast ? ["chromecast"] : []),
    ...(preview.vastTag ? ["vast"] : []),
    ...(isHls ? ["hls"] : []),
    ...(isDash ? ["dash"] : []),
  ];

  const loaded = new Map();
  await Promise.all(
    want.map(async (name) => {
      const factory = await loadPlugin(name);
      if (factory) loaded.set(name, factory);
    })
  );

  const plugins = [];
  const names = [];
  let chapter = null;

  const push = (name, factory, option) => {
    try {
      if (name === "chapter") {
        // 先挂空章节，等元数据就绪再写入，避免时长未知时插件抛错
        plugins.push((art) => {
          chapter = factory({ chapters: [] })(art);
          return chapter;
        });
      } else {
        plugins.push(factory(option));
      }
      names.push(name);
    } catch (err) {
      console.warn("[viewer] 插件初始化失败：", name, err);
    }
  };

  if (loaded.has("ambilight")) {
    push("ambilight", loaded.get("ambilight"), { blur: "42px", opacity: 0.55, frequency: 8 });
  }
  if (loaded.has("danmuku")) {
    push("danmuku", loaded.get("danmuku"), {
      danmuku: Array.isArray(sidecars.danmakuData) ? sidecars.danmakuData : [],
      speed: 5,
      fontSize: 22,
      opacity: 0.95,
      antiOverlap: true,
      synchronousPlayback: true,
      theme: "dark",
    });
  }
  if (loaded.has("subtitles")) {
    push("subtitles", loaded.get("subtitles"), {
      subtitles: sidecars.subtitles.map((item) => ({
        name: item.lang ? `${item.lang} · ${item.name}` : item.name,
        url: item.url,
        type: item.type,
      })),
    });
  }
  if (loaded.has("thumbnail")) {
    push("thumbnail", loaded.get("thumbnail"), {
      vtt: sidecars.thumbnails,
      style: { width: "180px", height: "102px", "border-radius": "8px" },
    });
  }
  if (loaded.has("chapter")) {
    push("chapter", loaded.get("chapter"), { chapters: [] });
  }
  if (loaded.has("chromecast")) {
    push("chromecast", loaded.get("chromecast"), {});
  }
  if (loaded.has("vast")) {
    const tag = preview.vastTag;
    try {
      // VAST 依赖 Google IMA SDK，失败时只留一条告警，不阻塞播放
      const task = loaded.get("vast")(async ({ playUrl }) => {
        await playUrl(tag, { autoPlayAdBreaks: true });
      });
      if (task && typeof task.then === "function") task.catch((err) => console.warn("[viewer] VAST 插件失败", err));
      plugins.push(task);
      names.push("vast");
    } catch (err) {
      console.warn("[viewer] VAST 插件初始化失败", err);
    }
  }
  if (loaded.has("hls")) {
    push("hls", loaded.get("hls"), {
      quality: { title: "清晰度", auto: "自动", getName: (level) => level.name || `${level.height}P` },
    });
  }
  if (loaded.has("dash")) {
    push("dash", loaded.get("dash"), { quality: { title: "清晰度", auto: "自动" } });
  }

  return {
    plugins,
    names,
    get chapter() {
      return chapter;
    },
    hls: isHls && Boolean(window.Hls),
    dash: isDash && Boolean(window.dashjs),
  };
}

/** 主题色变化时同步给播放器（弹幕、进度条都吃这个色）。 */
export function playerTheme() {
  return accentColor();
}
