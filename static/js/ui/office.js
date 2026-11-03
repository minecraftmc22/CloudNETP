/**
 * Office 在线预览（纯前端，零构建）
 *
 * - .docx/.docm → docx-preview（保样式渲染成 HTML）
 * - .xlsx/.xls/.csv/.tsv → SheetJS（自己拼表格，限制行列数防止卡死）
 * - .pptx → 自带的极简解析：解 zip，按 EMU 坐标把形状绝对定位还原成幻灯片
 * - .odt/.ods/.odp → odf.js：同样是 zip，读 content.xml
 * - .doc/.ppt → docparse.js：OLE2 复合文档，做文本级提取
 * - .rtf → docparse.js：控制字解析
 *
 * 所有解析都发生在浏览器里，服务端只负责把二进制流出来（/api/nodes/{id}/preview），
 * 所以不会把用户的文档交给任何外部服务。
 */

import { mountOdfDoc, mountOdfSheet, mountOdfSlide } from "./odf.js";
import { mountLegacyDoc, mountLegacyPpt, mountRtf } from "./docparse.js";

const SCRIPT_BASE = "/static/vendor/office/";const SLIDE_MAX = 60; // 演示文稿最多渲染这么多页，超出提示下载
const SHEET_MAX_ROWS = 800;
const SHEET_MAX_COLS = 80;

const scriptCache = new Map();

function loadScript(src) {
  if (scriptCache.has(src)) return scriptCache.get(src);
  const task = new Promise((resolve, reject) => {
    const node = document.createElement("script");
    node.src = src;
    node.async = false;
    node.onload = () => resolve();
    node.onerror = () => {
      scriptCache.delete(src);
      reject(new Error("预览组件加载失败，请检查网络后重试"));
    };
    document.head.appendChild(node);
  });
  scriptCache.set(src, task);
  return task;
}

async function loadDocxLib() {
  await loadScript(`${SCRIPT_BASE}jszip.min.js`);
  await loadScript(`${SCRIPT_BASE}docx-preview.min.js`);
  if (!window.docx || !window.docx.renderAsync) throw new Error("docx 预览组件未就绪");
  return window.docx;
}

async function loadXlsxLib() {
  await loadScript(`${SCRIPT_BASE}xlsx.mini.min.js`);
  if (!window.XLSX) throw new Error("表格组件未就绪");
  return window.XLSX;
}

async function loadZipLib() {
  await loadScript(`${SCRIPT_BASE}jszip.min.js`);
  if (!window.JSZip) throw new Error("压缩包组件未就绪");
  return window.JSZip;
}

async function fetchArrayBuffer(item, url) {
  const href = url || `/api/nodes/${item.id}/preview`;
  const res = await fetch(href, { credentials: "same-origin" });
  if (!res.ok) throw new Error(res.status === 401 ? "登录已失效，请刷新页面" : "文件读取失败");
  return res.arrayBuffer();
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ------------------------------------------------------------------ */
/* Word                                                               */
/* ------------------------------------------------------------------ */

export async function mountDocx(container, item, url) {
  const docx = await loadDocxLib();
  const buffer = await fetchArrayBuffer(item, url);
  container.innerHTML = `<div class="office office--docx" data-docx></div>`;
  const host = container.querySelector("[data-docx]");
  await docx.renderAsync(buffer, host, null, {
    className: "docx",
    inWrapper: true,
    breakPages: true,
    ignoreLastRenderedPageBreak: true,
    experimental: true,
    useBase64URL: true,
  });
  return () => {
    host.innerHTML = "";
  };
}

/* ------------------------------------------------------------------ */
/* Excel                                                              */
/* ------------------------------------------------------------------ */

function sheetTable(ws, XLSX) {
  const ref = ws["!ref"] || "A1";
  const range = XLSX.utils.decode_range(ref);
  const rows = Math.min(range.e.r - range.s.r + 1, SHEET_MAX_ROWS);
  const cols = Math.min(range.e.c - range.s.c + 1, SHEET_MAX_COLS);

  const head = ["<th class=\"sh-gutter\"></th>"];
  for (let c = 0; c < cols; c += 1) {
    head.push(`<th>${XLSX.utils.encode_col(range.s.c + c)}</th>`);
  }

  const body = [];
  for (let r = 0; r < rows; r += 1) {
    const cells = [`<th class="sh-gutter">${range.s.r + r + 1}</th>`];
    for (let c = 0; c < cols; c += 1) {
      const addr = XLSX.utils.encode_cell({ r: range.s.r + r, c: range.s.c + c });
      const cell = ws[addr];
      const text = cell ? (cell.w ?? (cell.v == null ? "" : String(cell.v))) : "";
      const numeric = cell && cell.t === "n";
      cells.push(`<td${numeric ? ' class="is-num"' : ""}>${escapeHtml(text)}</td>`);
    }
    body.push(`<tr>${cells.join("")}</tr>`);
  }

  const more =
    range.e.r - range.s.r + 1 > rows || range.e.c - range.s.c + 1 > cols
      ? `<p class="office__hint">表格较大，仅显示前 ${rows} 行 / ${cols} 列，完整内容请下载查看。</p>`
      : "";
  return `<div class="office__sheet-scroll"><table class="sh-table"><thead><tr>${head.join("")}</tr></thead><tbody>${body.join("")}</tbody></table></div>${more}`;
}

export async function mountSheet(container, item, url) {
  const XLSX = await loadXlsxLib();
  const buffer = await fetchArrayBuffer(item, url);
  const wb = XLSX.read(new Uint8Array(buffer), { type: "array" });
  const names = wb.SheetNames || [];
  if (!names.length) throw new Error("这个表格里没有工作表");

  container.innerHTML = `
    <div class="office office--sheet">
      ${
        names.length > 1
          ? `<div class="segmented office__tabs" data-tabs>${names
              .map(
                (name, i) =>
                  `<button class="segmented__btn${i === 0 ? " is-active" : ""}" type="button" data-sheet="${i}">${escapeHtml(name)}</button>`
              )
              .join("")}</div>`
          : `<div class="office__tabname">${escapeHtml(names[0])}</div>`
      }
      <div class="office__sheet-body" data-body></div>
    </div>`;
  const body = container.querySelector("[data-body]");
  const paint = (index) => {
    body.innerHTML = sheetTable(wb.Sheets[names[index]], XLSX);
  };
  paint(0);
  container.querySelectorAll("[data-sheet]").forEach((btn) => {
    btn.addEventListener("click", () => {
      container.querySelectorAll("[data-sheet]").forEach((b) => b.classList.toggle("is-active", b === btn));
      paint(Number(btn.dataset.sheet));
    });
  });
}

/* ------------------------------------------------------------------ */
/* PowerPoint                                                         */
/* ------------------------------------------------------------------ */

const EMU_PER_PX = 9525; // 96 dpi

function textOf(xmlNode) {
  return Array.from(xmlNode.getElementsByTagName("a:t"))
    .map((t) => t.textContent || "")
    .join("");
}

function parseSeries(xml, tag) {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  return Array.from(doc.getElementsByTagName(`p:${tag}`));
}

/** 取 <a:off>/<a:ext>（EMU），没有就返回 null */
function readXfrm(el) {
  const off = el.getElementsByTagName("a:off")[0];
  const ext = el.getElementsByTagName("a:ext")[0];
  if (!off || !ext) return null;
  return {
    x: Number(off.getAttribute("x")) || 0,
    y: Number(off.getAttribute("y")) || 0,
    cx: Number(ext.getAttribute("cx")) || 0,
    cy: Number(ext.getAttribute("cy")) || 0,
  };
}

function slideOrder(zip) {
  const pres = zip.file("ppt/presentation.xml");
  if (!pres) throw new Error("不是有效的 PowerPoint 文件");
  return Promise.all([
    pres.async("string"),
    zip.file("ppt/_rels/presentation.xml.rels")?.async("string") ?? "",
  ]).then(([xml, relsXml]) => {
    const size = /<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"/.exec(xml) || [];
    const rels = new Map();
    const relRe = /<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"/g;
    let m;
    while ((m = relRe.exec(relsXml))) {
      const target = m[2].replace(/^\.\.\//, "").replace(/^\//, "");
      rels.set(m[1], `ppt/${target}`);
    }
    const order = [];
    const idRe = /<p:sldId[^>]*r:id="([^"]+)"/g;
    while ((m = idRe.exec(xml))) order.push(rels.get(m[1]));
    return {
      cx: Number(size[1]) || 12192000,
      cy: Number(size[2]) || 6858000,
      slides: order.filter(Boolean),
    };
  });
}

async function parseSlide(xml, relsXml, layoutXml, zip, media, deck) {
  const rels = new Map();
  const relRe = /<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"/g;
  let m;
  while ((m = relRe.exec(relsXml || ""))) {
    rels.set(m[1], m[2].replace(/^\.\.\//, "").replace(/^\//, ""));
  }

  // 占位符（标题 / 正文）通常不写自己的坐标，位置继承自版式；
  // 不先把版式里的位置读出来，整页就会「什么都没有」。
  const phPos = new Map();
  if (layoutXml) {
    const layoutDoc = new DOMParser().parseFromString(layoutXml, "application/xml");
    Array.from(layoutDoc.getElementsByTagName("p:sp")).forEach((sp) => {
      const xfrm = readXfrm(sp);
      if (!xfrm || !xfrm.cx || !xfrm.cy) return;
      const ph = sp.getElementsByTagName("p:ph")[0];
      if (!ph) return;
      const idx = ph.getAttribute("idx");
      const type = ph.getAttribute("type") || "body";
      phPos.set(`idx:${idx ?? ""}`, xfrm);
      if (!phPos.has(`type:${type}`)) phPos.set(`type:${type}`, xfrm);
    });
  }

  const doc = new DOMParser().parseFromString(xml, "application/xml");
  const shapes = [];

  // 文本框 / 自选图形
  Array.from(doc.getElementsByTagName("p:sp")).forEach((sp) => {
    const text = textOf(sp).trim();
    if (!text) return;
    const ph = sp.getElementsByTagName("p:ph")[0];
    const phIdx = ph?.getAttribute("idx");
    const phType = ph?.getAttribute("type") || (ph ? "body" : "");
    let xfrm = readXfrm(sp);
    if ((!xfrm || !xfrm.cx || !xfrm.cy) && ph) {
      xfrm =
        phPos.get(`idx:${phIdx ?? ""}`) ||
        phPos.get(`type:${phType}`) ||
        phPos.get("type:body") ||
        null;
    }
    if (!xfrm) {
      // 版式里也找不到（有些文件把坐标全放在母版），按比例给个能看的兜底区域
      const isTitle = phType === "title" || phType === "ctrTitle";
      shapes.push({
        x: deck.cx * 0.06,
        y: deck.cy * (isTitle ? 0.05 : 0.28),
        cx: deck.cx * 0.88,
        cy: deck.cy * (isTitle ? 0.2 : 0.62),
        text,
        size: isTitle ? 32 : 18,
        bold: isTitle,
      });
      return;
    }
    const sizes = Array.from(sp.getElementsByTagName("a:rPr"))
      .map((r) => Number(r.getAttribute("sz")) || 0)
      .filter(Boolean);
    const bold = Array.from(sp.getElementsByTagName("a:rPr")).some(
      (r) => r.getAttribute("b") === "1"
    );
    shapes.push({
      ...xfrm,
      text,
      size: sizes.length ? Math.max(...sizes) / 100 : phType.includes("itle") ? 32 : 18,
      bold,
    });
  });

  // 图片（读内容要走 JSZip 的 async()，所以用 for...of 而不是 forEach）
  for (const pic of Array.from(doc.getElementsByTagName("p:pic"))) {
    const xfrm = readXfrm(pic);
    const blip = pic.getElementsByTagName("a:blip")[0];
    const rid = blip?.getAttribute("r:embed");
    if (!xfrm || !rid) continue;
    const target = rels.get(rid);
    if (!target) continue;
    const key = target.startsWith("ppt/") ? target : `ppt/${target}`;
    if (!zip.file(key)) continue;
    if (!media.has(key)) {
      // JSZip 3 移除了 asUint8Array 等同步方法，读内容只能走 async()
      const bytes = await zip.file(key).async("uint8array");
      const blob = new Blob([bytes], { type: "image/png" });
      media.set(key, URL.createObjectURL(blob));
    }
    shapes.push({ ...xfrm, img: media.get(key) });
  }

  // 表格：按行把文字拼起来，保留可读性
  Array.from(doc.getElementsByTagName("a:tbl")).forEach((tbl) => {
    const xfrm = readXfrm(tbl);
    if (!xfrm) return;
    const rows = Array.from(tbl.getElementsByTagName("a:tr")).map((tr) =>
      Array.from(tr.getElementsByTagName("a:tc")).map((tc) => textOf(tc).trim())
    );
    if (rows.length) shapes.push({ ...xfrm, rows });
  });

  return shapes;
}

function slideHtml(deck, index, width) {
  const slide = deck.slides[index];
  const k = width / (deck.cx / EMU_PER_PX); // 实际像素 / 原始像素
  const height = Math.round((deck.cy / EMU_PER_PX) * k);
  const parts = slide.shapes.map((s) => {
    const style = [
      `left:${((s.x / deck.cx) * 100).toFixed(3)}%`,
      `top:${((s.y / deck.cy) * 100).toFixed(3)}%`,
      `width:${((s.cx / deck.cx) * 100).toFixed(3)}%`,
      `height:${((s.cy / deck.cy) * 100).toFixed(3)}%`,
    ].join(";");
    if (s.img) {
      return `<img class="sl-img" style="${style}" src="${s.img}" alt="">`;
    }
    if (s.rows) {
      const rows = s.rows
        .map(
          (cells) =>
            `<tr>${cells.map((c) => `<td>${escapeHtml(c)}</td>`).join("")}</tr>`
        )
        .join("");
      return `<div class="sl-box" style="${style}"><table class="sl-table"><tbody>${rows}</tbody></table></div>`;
    }
    const px = Math.max(9, Math.round(s.size * (96 / 72) * k));
    const lines = s.text.split(/\n/).map((line) => escapeHtml(line)).join("<br>");
    return `<div class="sl-box" style="${style};font-size:${px}px;${
      s.bold ? "font-weight:600" : ""
    }">${lines}</div>`;
  });
  return `<section class="sl-page" style="width:${width}px;height:${height}px">
      ${parts.join("")}
      <span class="sl-no">${index + 1}</span>
    </section>`;
}

export async function mountSlide(container, item, url) {
  const JSZip = await loadZipLib();
  const buffer = await fetchArrayBuffer(item, url);
  const zip = await JSZip.loadAsync(buffer);
  const meta = await slideOrder(zip);

  const total = meta.slides.length;
  if (!total) throw new Error("这份演示文稿里没有幻灯片");

  const media = new Map();
  const slides = [];
  const baseDeck = { cx: meta.cx, cy: meta.cy };
  for (const path of meta.slides.slice(0, SLIDE_MAX)) {
    const xml = await zip.file(path)?.async("string");
    if (!xml) continue;
    const relsPath = path.replace(/slides\/([^/]+)$/, "slides/_rels/$1.rels");
    const relsXml = (await zip.file(relsPath)?.async("string")) || "";
    // 版式文件里才有占位符的坐标
    const layoutTarget = /Target="([^"]*slideLayout[^"]*)"/.exec(relsXml)?.[1];
    const layoutPath = layoutTarget
      ? layoutTarget.replace(/^\.\.\//, "").replace(/^\//, "").replace(/^(?!ppt\/)/, "ppt/")
      : "";
    const layoutXml = layoutPath ? (await zip.file(layoutPath)?.async("string")) || "" : "";
    slides.push({ shapes: await parseSlide(xml, relsXml, layoutXml, zip, media, baseDeck) });
  }

  const deck = { ...baseDeck, slides };
  container.innerHTML = `
    <div class="office office--slide">
      <div class="office__slides" data-slides></div>
      ${
        total > SLIDE_MAX
          ? `<p class="office__hint">共 ${total} 页，这里只渲染前 ${SLIDE_MAX} 页。</p>`
          : ""
      }
    </div>`;
  const host = container.querySelector("[data-slides]");

  const paint = () => {
    const width = Math.max(280, Math.min(960, host.clientWidth - 8));
    host.innerHTML = deck.slides.map((_, i) => slideHtml(deck, i, width)).join("");
  };
  paint();

  let timer = 0;
  const onResize = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(paint, 120);
  };
  window.addEventListener("resize", onResize);

  return () => {
    window.removeEventListener("resize", onResize);
    window.clearTimeout(timer);
    media.forEach((url) => URL.revokeObjectURL(url));
  };
}

export const OFFICE_KINDS = new Set([
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

export function mountOffice(container, item, kind, url) {
  if (kind === "docx") return mountDocx(container, item, url);
  if (kind === "sheet") return mountSheet(container, item, url);
  if (kind === "slide") return mountSlide(container, item, url);
  if (kind === "odoc") return mountOdfDoc(container, item, url);
  if (kind === "osheet") return mountOdfSheet(container, item, url);
  if (kind === "oslide") return mountOdfSlide(container, item, url);
  if (kind === "rtf") return mountRtf(container, item, url);
  if (kind === "doc") return mountLegacyDoc(container, item, url);
  if (kind === "ppt") return mountLegacyPpt(container, item, url);
  return Promise.reject(new Error("该类型暂不支持预览"));
}
