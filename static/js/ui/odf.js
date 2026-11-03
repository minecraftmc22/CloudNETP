/**
 * OpenDocument 在线预览（.odt / .ods / .odp，以及扁平化的 .fodt）
 *
 * ODF 本质是「zip + content.xml」，和 docx 一样可以纯前端还原：
 * - 文字：解析 text:h / text:p / text:list / table:table
 * - 表格：解析 table:table-row / table:table-cell，展开 number-*-repeated
 * - 演示：解析 draw:page / draw:frame，按 svg:x/y/width/height 绝对定位
 *
 * 不追求像素级还原（那要完整实现 ODF 样式），目标是把结构和文字无损读出来。
 */

const SCRIPT_BASE = "/static/vendor/office/";
const MAX_ROWS = 800;
const MAX_COLS = 80;
const MAX_PAGES = 60;
const CM_TO_PX = 96 / 2.54;

let zipPromise = null;

function loadZip() {
  if (window.JSZip) return Promise.resolve(window.JSZip);
  if (zipPromise) return zipPromise;
  zipPromise = new Promise((resolve, reject) => {
    const node = document.createElement("script");
    node.src = `${SCRIPT_BASE}jszip.min.js`;
    node.async = false;
    node.onload = () => (window.JSZip ? resolve(window.JSZip) : reject(new Error("解压组件未就绪")));
    node.onerror = () => {
      zipPromise = null;
      reject(new Error("预览组件加载失败，请检查网络后重试"));
    };
    document.head.appendChild(node);
  });
  return zipPromise;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 读 content.xml：zip 和扁平 XML 两种形态都支持 */
async function readContent(buffer) {
  const head = new Uint8Array(buffer.slice(0, 4));
  const isZip = head[0] === 0x50 && head[1] === 0x4b; // "PK"
  if (!isZip) {
    return new TextDecoder("utf-8").decode(new Uint8Array(buffer));
  }
  const JSZip = await loadZip();
  const zip = await JSZip.loadAsync(buffer);
  const entry = zip.file("content.xml");
  if (!entry) throw new Error("这个文件里没有 content.xml，可能不是标准的 OpenDocument");
  return entry.async("string");
}

function parseXml(text) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.getElementsByTagName("parsererror").length) {
    throw new Error("文档结构解析失败");
  }
  return doc;
}

/** 把节点里的文字拼出来，处理 ODF 自己的空白/换行标记 */
function inlineText(node) {
  let out = "";
  node.childNodes.forEach((child) => {
    if (child.nodeType === 3) {
      out += child.nodeValue;
      return;
    }
    if (child.nodeType !== 1) return;
    const name = child.localName;
    if (name === "s") {
      const count = Number(child.getAttribute("text:c")) || 1;
      out += " ".repeat(Math.min(count, 80));
    } else if (name === "tab") {
      out += "\t";
    } else if (name === "line-break") {
      out += "\n";
    } else if (name === "note" || name === "annotation" || name === "tracked-changes") {
      /* 批注与修订记录不显示 */
    } else {
      out += inlineText(child);
    }
  });
  return out;
}

/* ------------------------------------------------------------------ */
/* 文字文档 .odt                                                       */
/* ------------------------------------------------------------------ */

function odtList(listEl, depth) {
  const ordered = (listEl.getAttribute("text:style-name") || "").toLowerCase().includes("number");
  const tag = ordered ? "ol" : "ul";
  const items = [];
  Array.from(listEl.children).forEach((li) => {
    if (li.localName !== "list-item" && li.localName !== "list-header") return;
    const parts = [];
    Array.from(li.children).forEach((child) => {
      const name = child.localName;
      if (name === "p" || name === "h") {
        parts.push(inlineText(child).trim());
      } else if (name === "list") {
        parts.push(odtList(child, depth + 1));
      }
    });
    items.push(`<li>${parts.map((p) => (p.startsWith("<") ? p : escapeHtml(p))).join("<br>")}</li>`);
  });
  return `<${tag} class="md-list">${items.join("")}</${tag}>`;
}

function odtTable(tableEl) {
  const rows = [];
  Array.from(tableEl.getElementsByTagName("table:table-row")).forEach((tr) => {
    if (rows.length >= 200) return;
    const cells = [];
    let col = 0;
    Array.from(tr.children).forEach((cell) => {
      if (cell.localName !== "table-cell" && cell.localName !== "covered-table-cell") return;
      const repeat = Math.min(Number(cell.getAttribute("table:number-columns-repeated")) || 1, MAX_COLS);
      const text = Array.from(cell.children)
        .filter((c) => c.localName === "p" || c.localName === "h")
        .map((c) => inlineText(c).trim())
        .join(" ");
      for (let i = 0; i < repeat && col < MAX_COLS; i += 1, col += 1) {
        cells.push(`<td>${escapeHtml(text)}</td>`);
      }
    });
    rows.push(`<tr>${cells.join("")}</tr>`);
  });
  if (!rows.length) return "";
  return `<div class="office__sheet-scroll"><table class="md-table"><tbody>${rows.join("")}</tbody></table></div>`;
}

export async function mountOdfDoc(container, item, url) {
  const buffer = await fetch(url || `/api/nodes/${item.id}/preview`, {
    credentials: "same-origin",
  }).then((r) => {
    if (!r.ok) throw new Error(r.status === 401 ? "登录已失效，请刷新页面" : "文件读取失败");
    return r.arrayBuffer();
  });

  const xml = await readContent(buffer);
  const doc = parseXml(xml);
  const body =
    doc.getElementsByTagName("office:text")[0] ||
    doc.getElementsByTagName("office:body")[0];
  if (!body) throw new Error("这个文档里没有正文内容");

  const blocks = [];
  Array.from(body.children).forEach((el) => {
    const name = el.localName;
    if (name === "h") {
      const level = Math.min(Math.max(Number(el.getAttribute("text:outline-level")) || 1, 1), 6);
      const text = inlineText(el).trim();
      if (text) blocks.push(`<h${level} class="md-h">${escapeHtml(text)}</h${level}>`);
    } else if (name === "p") {
      const text = inlineText(el).trim();
      blocks.push(text ? `<p>${escapeHtml(text)}</p>` : `<p class="md-blank"></p>`);
    } else if (name === "list") {
      blocks.push(odtList(el, 0));
    } else if (name === "table") {
      blocks.push(odtTable(el));
    } else if (name === "section") {
      Array.from(el.children).forEach((child) => {
        const text = inlineText(child).trim();
        if (child.localName === "p" && text) blocks.push(`<p>${escapeHtml(text)}</p>`);
        else if (child.localName === "h") blocks.push(`<h2 class="md-h">${escapeHtml(text)}</h2>`);
      });
    }
  });

  if (!blocks.length) throw new Error("文档里没有可显示的文字");

  container.innerHTML = `<div class="office office--docx"><div class="md-wrap"><article class="md-body odf-doc">${blocks.join(
    ""
  )}</article></div></div>`;
  return () => {
    container.innerHTML = "";
  };
}

/* ------------------------------------------------------------------ */
/* 表格 .ods                                                           */
/* ------------------------------------------------------------------ */

function odsSheets(doc) {
  return Array.from(doc.getElementsByTagName("table:table")).map((table) => {
    const name = table.getAttribute("table:name") || "工作表";
    const rowsData = [];
    let maxCols = 0;

    Array.from(table.children).forEach((row) => {
      if (row.localName !== "table-row" && row.localName !== "table-header-rows") return;
      const source = row.localName === "table-header-rows" ? Array.from(row.children) : [row];
      source.forEach((tr) => {
        if (tr.localName !== "table-row") return;
        const repeatRows = Math.min(Number(tr.getAttribute("table:number-rows-repeated")) || 1, 200);
        const cells = [];
        Array.from(tr.children).forEach((cell) => {
          if (cell.localName !== "table-cell" && cell.localName !== "covered-table-cell") return;
          const repeatCols = Math.min(
            Number(cell.getAttribute("table:number-columns-repeated")) || 1,
            MAX_COLS
          );
          const text = Array.from(cell.children)
            .filter((c) => c.localName === "p" || c.localName === "h")
            .map((c) => inlineText(c).trim())
            .join("\n");
          const type = cell.getAttribute("office:value-type");
          for (let i = 0; i < repeatCols && cells.length < MAX_COLS; i += 1) {
            cells.push({ text, numeric: type === "float" || type === "currency" || type === "percentage" });
          }
        });
        maxCols = Math.max(maxCols, cells.length);
        for (let i = 0; i < repeatRows && rowsData.length < MAX_ROWS; i += 1) {
          rowsData.push(cells);
        }
      });
    });

    return { name, rows: rowsData, cols: Math.min(maxCols, MAX_COLS) };
  });
}

function odsTable(sheet, colLetters) {
  const head = ['<th class="sh-gutter"></th>'];
  for (let c = 0; c < sheet.cols; c += 1) head.push(`<th>${colLetters(c)}</th>`);

  const body = sheet.rows.map((cells, r) => {
    const tds = [`<th class="sh-gutter">${r + 1}</th>`];
    for (let c = 0; c < sheet.cols; c += 1) {
      const cell = cells[c];
      tds.push(
        `<td${cell?.numeric ? ' class="is-num"' : ""}>${escapeHtml(cell?.text ?? "")}</td>`
      );
    }
    return `<tr>${tds.join("")}</tr>`;
  });

  const more =
    sheet.rows.length >= MAX_ROWS
      ? `<p class="office__hint">表格较大，仅显示前 ${MAX_ROWS} 行，完整内容请下载查看。</p>`
      : "";
  return `<div class="office__sheet-scroll"><table class="sh-table"><thead><tr>${head.join(
    ""
  )}</tr></thead><tbody>${body.join("")}</tbody></table></div>${more}`;
}

function colLetter(index) {
  let n = index;
  let out = "";
  do {
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return out;
}

export async function mountOdfSheet(container, item, url) {
  const buffer = await fetch(url || `/api/nodes/${item.id}/preview`, {
    credentials: "same-origin",
  }).then((r) => {
    if (!r.ok) throw new Error(r.status === 401 ? "登录已失效，请刷新页面" : "文件读取失败");
    return r.arrayBuffer();
  });
  const doc = parseXml(await readContent(buffer));
  const sheets = odsSheets(doc).filter((s) => s.rows.length);
  if (!sheets.length) throw new Error("这个表格里没有数据");

  container.innerHTML = `
    <div class="office office--sheet">
      ${
        sheets.length > 1
          ? `<div class="segmented office__tabs" data-tabs>${sheets
              .map(
                (s, i) =>
                  `<button class="segmented__btn${i === 0 ? " is-active" : ""}" type="button" data-sheet="${i}">${escapeHtml(s.name)}</button>`
              )
              .join("")}</div>`
          : `<div class="office__tabname">${escapeHtml(sheets[0].name)}</div>`
      }
      <div class="office__sheet-body" data-body></div>
    </div>`;

  const body = container.querySelector("[data-body]");
  const paint = (index) => {
    body.innerHTML = odsTable(sheets[index], colLetter);
  };
  paint(0);
  container.querySelectorAll("[data-sheet]").forEach((btn) => {
    btn.addEventListener("click", () => {
      container
        .querySelectorAll("[data-sheet]")
        .forEach((b) => b.classList.toggle("is-active", b === btn));
      paint(Number(btn.dataset.sheet));
    });
  });
}

/* ------------------------------------------------------------------ */
/* 演示文稿 .odp                                                       */
/* ------------------------------------------------------------------ */

/** "12.4cm" / "1.1in" / "36pt" → px */
function lengthToPx(raw, fallback) {
  const match = /^(-?[\d.]+)\s*(cm|mm|in|pt|pc|px)?$/.exec(String(raw || "").trim());
  if (!match) return fallback;
  const value = Number(match[1]);
  if (!Number.isFinite(value)) return fallback;
  switch (match[2]) {
    case "cm":
      return value * CM_TO_PX;
    case "mm":
      return (value * CM_TO_PX) / 10;
    case "in":
      return value * 96;
    case "pt":
      return (value * 96) / 72;
    case "pc":
      return value * 16;
    default:
      return value;
  }
}

function pageSize(doc) {
  const props = doc.getElementsByTagName("style:page-layout-properties")[0];
  const width = lengthToPx(props?.getAttribute("fo:page-width"), 0) || 793; // A4 横向 ≈ 793px
  const height = lengthToPx(props?.getAttribute("fo:page-height"), 0) || 446;
  return { width, height };
}

function odpSlides(doc) {
  return Array.from(doc.getElementsByTagName("draw:page")).map((page) => {
    const boxes = [];
    Array.from(page.getElementsByTagName("draw:frame")).forEach((frame) => {
      const x = lengthToPx(frame.getAttribute("svg:x"), 0);
      const y = lengthToPx(frame.getAttribute("svg:y"), 0);
      const w = lengthToPx(frame.getAttribute("svg:width"), 0);
      const h = lengthToPx(frame.getAttribute("svg:height"), 0);
      if (w <= 0 || h <= 0) return;

      const text = Array.from(frame.getElementsByTagName("text:p"))
        .map((p) => inlineText(p).trim())
        .filter(Boolean)
        .join("\n");
      const image = frame.getElementsByTagName("draw:image")[0];
      if (image && !text) {
        boxes.push({ x, y, w, h, image: true });
        return;
      }
      if (!text) return;
      const heading = /presentation|title/i.test(
        `${frame.getAttribute("presentation:class") || ""}${frame.getAttribute("draw:style-name") || ""}`
      );
      boxes.push({ x, y, w, h, text, heading });
    });
    return { name: page.getAttribute("draw:name") || "", boxes };
  });
}

function odpHtml(slides, index, page, width) {
  const slide = slides[index];
  const k = width / page.width;
  const height = Math.round(page.height * k);
  const parts = slide.boxes.map((box) => {
    const style = [
      `left:${((box.x / page.width) * 100).toFixed(3)}%`,
      `top:${((box.y / page.height) * 100).toFixed(3)}%`,
      `width:${((box.w / page.width) * 100).toFixed(3)}%`,
      `height:${((box.h / page.height) * 100).toFixed(3)}%`,
    ].join(";");
    if (box.image) {
      return `<div class="sl-box sl-box--img" style="${style}"><span>图片</span></div>`;
    }
    const size = box.heading ? 30 : 18;
    const lines = box.text
      .split(/\n/)
      .map((line) => escapeHtml(line))
      .join("<br>");
    return `<div class="sl-box" style="${style};font-size:${Math.round(size * k)}px;${
      box.heading ? "font-weight:600" : ""
    }">${lines}</div>`;
  });
  return `<section class="sl-page" style="width:${width}px;height:${height}px">
      ${parts.join("")}
      <span class="sl-no">${index + 1}</span>
    </section>`;
}

export async function mountOdfSlide(container, item, url) {
  const buffer = await fetch(url || `/api/nodes/${item.id}/preview`, {
    credentials: "same-origin",
  }).then((r) => {
    if (!r.ok) throw new Error(r.status === 401 ? "登录已失效，请刷新页面" : "文件读取失败");
    return r.arrayBuffer();
  });
  const doc = parseXml(await readContent(buffer));
  const page = pageSize(doc);
  const slides = odpSlides(doc);
  if (!slides.length) throw new Error("这份演示文稿里没有幻灯片");

  container.innerHTML = `
    <div class="office office--slide">
      <div class="office__slides" data-slides></div>
      ${
        slides.length > MAX_PAGES
          ? `<p class="office__hint">共 ${slides.length} 页，这里只渲染前 ${MAX_PAGES} 页。</p>`
          : ""
      }
    </div>`;
  const host = container.querySelector("[data-slides]");
  const shown = slides.slice(0, MAX_PAGES);

  const paint = () => {
    const width = Math.max(280, Math.min(960, host.clientWidth - 8));
    host.innerHTML = shown.map((_, i) => odpHtml(shown, i, page, width)).join("");
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
  };
}
