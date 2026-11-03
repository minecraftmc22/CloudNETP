/**
 * 旧版 Office 与 RTF 的在线预览（纯前端）
 *
 * .doc / .ppt 是 OLE2 复合文档（不是 zip），浏览器里没有现成库可用，所以这里自己解：
 * - OLE2/CFB：读扇区表 → FAT → 目录 → 取出 WordDocument / PowerPoint Document 流
 * - .doc：走 FIB 的 piece table（CLX），按 CP 区间把正文拼回来；拼不出来再退回 fcMin..fcMac
 * - .ppt：遍历二进制记录，收集 TextCharsAtom(0x0FA0) / TextBytesAtom(0x0FA8)
 * - .rtf：按控制字逐字符解析，跳过字体表/颜色表等非正文组
 *
 * 这些格式的版式信息（分栏、图文混排、动画）无法完整还原，
 * 预览定位是「能读到内容」，所以只做文本级还原，并在界面上明确提示。
 */

const MAX_CHARS = 400_000;

/* ------------------------------------------------------------------ */
/* 小工具                                                             */
/* ------------------------------------------------------------------ */

function u16(view, offset) {
  return view.getUint16(offset, true);
}

function u32(view, offset) {
  return view.getUint32(offset, true);
}

const CP1252 = {
  0x80: "€", 0x82: "‚", 0x83: "ƒ", 0x84: "„", 0x85: "…", 0x86: "†", 0x87: "‡",
  0x88: "ˆ", 0x89: "‰", 0x8a: "Š", 0x8b: "‹", 0x8c: "Œ", 0x8e: "Ž", 0x91: "‘",
  0x92: "’", 0x93: "“", 0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—", 0x98: "˜",
  0x99: "™", 0x9a: "š", 0x9b: "›", 0x9c: "œ", 0x9e: "ž", 0x9f: "Ÿ",
};

function cp1252Char(code) {
  return CP1252[code] ?? String.fromCharCode(code);
}

async function fetchBuffer(item, url) {
  const res = await fetch(url || `/api/nodes/${item.id}/preview`, { credentials: "same-origin" });
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
/* OLE2 / CFB 复合文档                                                 */
/* ------------------------------------------------------------------ */

const END_OF_CHAIN = 0xfffffffe;

export function readOleStreams(buffer) {
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  if (u32(view, 0) !== 0xe011cfd0 || u32(view, 4) !== 0xe11ab1a1) {
    throw new Error("这不是标准的 OLE2 文档（可能是其它程序改过后缀的文件）");
  }

  const sectorSize = 1 << u16(view, 30);
  const miniSize = 1 << u16(view, 32);
  const numFatSectors = u32(view, 44);
  const firstDirSector = u32(view, 48);
  const miniCutoff = u32(view, 56);
  const firstMiniFat = u32(view, 60);
  const firstDifat = u32(view, 68);

  const sectorOffset = (sector) => (sector + 1) * sectorSize;

  // 1) DIFAT → FAT 扇区号列表
  const fatSectors = [];
  for (let i = 0; i < 109; i += 1) {
    const sector = u32(view, 76 + i * 4);
    if (sector >= 0xfffffffa) break;
    fatSectors.push(sector);
  }
  let difat = firstDifat;
  let guard = 0;
  while (difat < 0xfffffffa && guard < 100000) {
    guard += 1;
    const base = sectorOffset(difat);
    const perSector = sectorSize / 4 - 1;
    for (let i = 0; i < perSector; i += 1) {
      const sector = u32(view, base + i * 4);
      if (sector >= 0xfffffffa) break;
      fatSectors.push(sector);
    }
    difat = u32(view, base + perSector * 4);
  }

  // 2) FAT 本体
  const fat = [];
  fatSectors.slice(0, numFatSectors || fatSectors.length).forEach((sector) => {
    const base = sectorOffset(sector);
    for (let i = 0; i < sectorSize / 4; i += 1) fat.push(u32(view, base + i * 4));
  });

  const chainOf = (start) => {
    const out = [];
    let sector = start;
    let hops = 0;
    while (sector < 0xfffffffa && hops < 2_000_000) {
      out.push(sector);
      sector = fat[sector] ?? END_OF_CHAIN;
      hops += 1;
    }
    return out;
  };

  const readChain = (start, size) => {
    const sectors = chainOf(start);
    const out = new Uint8Array(sectors.length * sectorSize);
    sectors.forEach((sector, index) => {
      out.set(bytes.subarray(sectorOffset(sector), sectorOffset(sector) + sectorSize), index * sectorSize);
    });
    return size == null ? out : out.subarray(0, Math.min(size, out.length));
  };

  // 3) 目录项
  const dirData = readChain(firstDirSector, null);
  const dirView = new DataView(dirData.buffer, dirData.byteOffset);
  const entries = [];
  for (let offset = 0; offset + 128 <= dirData.length; offset += 128) {
    const nameLen = u16(dirView, offset + 64);
    if (!nameLen) continue;
    const raw = dirData.subarray(offset, offset + Math.max(0, nameLen - 2));
    const name = new TextDecoder("utf-16le").decode(raw);
    const type = dirData[offset + 66];
    const start = u32(dirView, offset + 116);
    const sizeLow = u32(dirView, offset + 120);
    const sizeHigh = u32(dirView, offset + 124);
    entries.push({ name, type, start, size: sizeHigh * 2 ** 32 + sizeLow });
  }

  // 4) 迷你流（小于 4096 字节的数据存在这里）
  const root = entries.find((entry) => entry.type === 5);
  const miniData = root && root.start < 0xfffffffa ? readChain(root.start, root.size) : new Uint8Array(0);
  const miniFat = [];
  if (firstMiniFat < 0xfffffffa) {
    const data = readChain(firstMiniFat, null);
    const miniView = new DataView(data.buffer, data.byteOffset);
    for (let i = 0; i + 4 <= data.length; i += 4) miniFat.push(u32(miniView, i));
  }

  const readMini = (start, size) => {
    const out = new Uint8Array(size);
    let sector = start;
    let written = 0;
    let hops = 0;
    while (sector < 0xfffffffa && written < size && hops < 2_000_000) {
      const base = sector * miniSize;
      const take = Math.min(miniSize, size - written);
      out.set(miniData.subarray(base, base + take), written);
      written += take;
      sector = miniFat[sector] ?? END_OF_CHAIN;
      hops += 1;
    }
    return out;
  };

  const streams = new Map();
  entries.forEach((entry) => {
    if (entry.type !== 2) return;
    if (!entry.size) {
      streams.set(entry.name, new Uint8Array(0));
      return;
    }
    streams.set(
      entry.name,
      entry.size < miniCutoff ? readMini(entry.start, entry.size) : readChain(entry.start, entry.size)
    );
  });
  return streams;
}

/* ------------------------------------------------------------------ */
/* .doc（Word 97-2003）                                                */
/* ------------------------------------------------------------------ */

function cleanDocText(raw) {
  return raw
    .replace(/\r/g, "\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, (ch) => (ch === "\x07" ? "\t" : ""))
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function readU32LE(array, offset) {
  return (
    (array[offset] | (array[offset + 1] << 8) | (array[offset + 2] << 16) | (array[offset + 3] << 24)) >>> 0
  );
}

function extractDocText(streams) {
  const word = streams.get("WordDocument");
  if (!word || word.length < 0x200) throw new Error("WordDocument 数据流缺失，文件可能已损坏");

  const wordView = new DataView(word.buffer, word.byteOffset);
  const flags = u16(wordView, 0x0a);
  const whichTable = (flags >> 9) & 1;
  const table = streams.get(whichTable ? "1Table" : "0Table");
  const fcMin = u32(wordView, 0x18);
  const fcMac = u32(wordView, 0x1c);

  const plainFallback = () => {
    const end = Math.min(fcMac || word.length, word.length);
    const slice = word.subarray(Math.min(fcMin, end), end);
    return cleanDocText(Array.from(slice, cp1252Char).join(""));
  };

  if (!table || table.length < 0x200) return plainFallback();

  const fcClx = u32(wordView, 0x1a2);
  const lcbClx = u32(wordView, 0x1a6);
  if (!lcbClx || fcClx + lcbClx > table.length) return plainFallback();

  // CLX = 若干 Prc(0x01) + 一个 Pcdt(0x02)
  let index = fcClx;
  const end = fcClx + lcbClx;
  let plc = null;
  while (index < end) {
    const kind = table[index];
    if (kind === 0x01) {
      index += 3 + (table[index + 1] | (table[index + 2] << 8));
    } else if (kind === 0x02) {
      const lcb = readU32LE(table, index + 1);
      plc = table.subarray(index + 5, index + 5 + lcb);
      break;
    } else {
      break;
    }
  }
  if (!plc || plc.length < 16) return plainFallback();

  const pieces = Math.floor((plc.length - 4) / 12);
  const chunks = [];
  for (let piece = 0; piece < pieces; piece += 1) {
    const cpStart = readU32LE(plc, piece * 4);
    const cpEnd = readU32LE(plc, (piece + 1) * 4);
    const count = Math.min(cpEnd - cpStart, MAX_CHARS);
    if (count <= 0) continue;
    const pcdOffset = (pieces + 1) * 4 + piece * 8;
    if (pcdOffset + 8 > plc.length) break;
    const fcRaw = readU32LE(plc, pcdOffset + 2);
    const compressed = (fcRaw & 0x40000000) !== 0;
    const fc = (fcRaw & 0x3fffffff) / (compressed ? 2 : 1);

    if (compressed) {
      for (let i = 0; i < count && fc + i < word.length; i += 1) {
        chunks.push(cp1252Char(word[fc + i]));
      }
    } else {
      for (let i = 0; i < count && fc + i * 2 + 1 < word.length; i += 1) {
        chunks.push(String.fromCharCode(word[fc + i * 2] | (word[fc + i * 2 + 1] << 8)));
      }
    }
    if (chunks.length > MAX_CHARS) break;
  }

  const text = cleanDocText(chunks.join(""));
  return text || plainFallback();
}

/* ------------------------------------------------------------------ */
/* .ppt（PowerPoint 97-2003）                                          */
/* ------------------------------------------------------------------ */

const RT_TEXT_CHARS = 0x0fa0;
const RT_TEXT_BYTES = 0x0fa8;

function extractPptText(streams) {
  const stream = streams.get("PowerPoint Document");
  if (!stream || stream.length < 8) throw new Error("PowerPoint Document 数据流缺失，文件可能已损坏");

  const view = new DataView(stream.buffer, stream.byteOffset);
  const blocks = [];
  let total = 0;

  const walk = (start, end, depth) => {
    let offset = start;
    let steps = 0;
    while (offset + 8 <= end && steps < 200000 && total < MAX_CHARS) {
      steps += 1;
      const verInst = u16(view, offset);
      const type = u16(view, offset + 2);
      const length = u32(view, offset + 4);
      const body = offset + 8;
      if (length > end - body || length < 0) break;

      if (type === RT_TEXT_CHARS) {
        let text = "";
        for (let i = 0; i + 1 < length; i += 2) {
          text += String.fromCharCode(stream[body + i] | (stream[body + i + 1] << 8));
        }
        total += text.length;
        if (text.trim()) blocks.push(text);
      } else if (type === RT_TEXT_BYTES) {
        let text = "";
        for (let i = 0; i < length; i += 1) text += cp1252Char(stream[body + i]);
        total += text.length;
        if (text.trim()) blocks.push(text);
      } else if ((verInst & 0xf) === 0xf && depth < 12) {
        // 容器记录：内容还是一串记录，递归进去找文本原子
        walk(body, body + length, depth + 1);
      }
      offset = body + length;
    }
  };

  walk(0, stream.length, 0);

  const text = blocks
    .join("\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!text) throw new Error("这份演示文稿里没有可提取的文字（可能是纯图片的幻灯片）");
  return text;
}

/* ------------------------------------------------------------------ */
/* .rtf                                                                */
/* ------------------------------------------------------------------ */

const RTF_SKIP = new Set([
  "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata",
  "themedata", "colorschememapping", "datastore", "latentstyles", "listtable",
  "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "mmathPr", "header",
  "headerl", "headerr", "headerf", "footer", "footerl", "footerr", "footerf",
  "fldinst", "bkmkstart", "bkmkend", "filetbl", "revtbl",
]);

export function extractRtfText(source) {
  let out = "";
  let depth = 0;
  let skipFrom = -1; // >=0 表示正处在要跳过的组里
  let i = 0;
  let pendingFallback = false; // \uN 后面那个代替字符要跳过

  while (i < source.length) {
    const ch = source[i];

    if (ch === "{") {
      depth += 1;
      i += 1;
      continue;
    }
    if (ch === "}") {
      if (skipFrom >= 0 && depth === skipFrom) skipFrom = -1;
      depth = Math.max(0, depth - 1);
      i += 1;
      continue;
    }
    if (ch === "\\") {
      const next = source[i + 1];

      // 控制符号
      if (next === "\\" || next === "{" || next === "}") {
        if (skipFrom < 0 && !pendingFallback) out += next;
        pendingFallback = false;
        i += 2;
        continue;
      }

      // \'hh —— 单字节（代码页由 \ansicpg 指定，这里按 cp1252 处理西文）
      if (next === "'") {
        const hex = source.slice(i + 2, i + 4);
        const code = parseInt(hex, 16);
        if (!Number.isNaN(code) && skipFrom < 0) out += cp1252Char(code);
        i += 4;
        continue;
      }

      // \* 表示「不认识就忽略」的可选组
      if (next === "*") {
        if (skipFrom < 0) skipFrom = depth;
        i += 2;
        continue;
      }

      // 控制字
      let j = i + 1;
      let word = "";
      while (j < source.length && /[a-zA-Z]/.test(source[j])) {
        word += source[j];
        j += 1;
      }
      let param = "";
      if (source[j] === "-") {
        param += "-";
        j += 1;
      }
      while (j < source.length && /[0-9]/.test(source[j])) {
        param += source[j];
        j += 1;
      }
      if (source[j] === " ") j += 1; // 控制字后面的一个空格是分隔符

      if (word && RTF_SKIP.has(word) && param === "") {
        if (skipFrom < 0) skipFrom = depth;
      } else if (skipFrom < 0) {
        if (word === "par" || word === "line" || word === "sect" || word === "page") out += "\n";
        else if (word === "tab") out += "\t";
        else if (word === "cell" || word === "row") out += "\t";
        else if (word === "u") {
          const code = Number(param);
          if (Number.isFinite(code)) out += String.fromCharCode(code < 0 ? code + 65536 : code);
          pendingFallback = true;
        } else if (!word) {
          // 纯数字转义（如 \~ 之类）忽略
        }
      }
      i = j;
      continue;
    }

    if (ch === "\r" || ch === "\n") {
      i += 1;
      continue;
    }

    if (skipFrom < 0) {
      if (pendingFallback) {
        pendingFallback = false; // 吃掉 \uN 的替代字符
      } else {
        out += ch;
      }
    }
    i += 1;
  }

  return out.replace(/\n{3,}/g, "\n\n").trim();
}

/* ------------------------------------------------------------------ */
/* 渲染                                                               */
/* ------------------------------------------------------------------ */

function textToHtml(text) {
  const paragraphs = text.split(/\n/);
  const blocks = paragraphs.map((line) => {
    const trimmed = line.replace(/\t+/g, "    ").trimEnd();
    if (!trimmed.trim()) return `<p class="md-blank"></p>`;
    return `<p>${escapeHtml(trimmed)}</p>`;
  });
  return blocks.join("");
}

function mountTextLike(container, text, { title, note }) {
  container.innerHTML = `
    <div class="office office--docx">
      <div class="md-wrap">
        <div class="office__notice">
          <svg class="icon icon--s"><use href="#i-info"/></svg>
          <span>${escapeHtml(note)}</span>
        </div>
        <article class="md-body doc-text">${textToHtml(text)}</article>
      </div>
    </div>`;
  if (title) container.querySelector(".office__notice").setAttribute("title", title);
  return () => {
    container.innerHTML = "";
  };
}

/* ------------------------------------------------------------------ */
/* 对外入口                                                            */
/* ------------------------------------------------------------------ */

export async function mountLegacyDoc(container, item, url) {
  const buffer = await fetchBuffer(item, url);
  const streams = readOleStreams(buffer);
  const text = extractDocText(streams);
  return mountTextLike(container, text, {
    note: "旧版 Word（.doc）二进制格式，只能提取文字，排版与图片会丢失。需要完整效果请下载后用 Word 打开。",
  });
}

export async function mountLegacyPpt(container, item, url) {
  const buffer = await fetchBuffer(item, url);
  const streams = readOleStreams(buffer);
  const text = extractPptText(streams);
  return mountTextLike(container, text, {
    note: "旧版 PowerPoint（.ppt）二进制格式，只能提取文字，版式与图片会丢失。需要完整效果请下载后打开。",
  });
}

export async function mountRtf(container, item, url) {
  const buffer = await fetchBuffer(item, url);
  // RTF 是纯 ASCII 控制流，正文里的非 ASCII 由 \'hh 或 \uN 表达
  const source = new TextDecoder("latin1").decode(new Uint8Array(buffer));
  const text = extractRtfText(source);
  if (!text) throw new Error("这个 RTF 里没有可显示的文字");
  return mountTextLike(container, text, {
    note: "RTF 预览只还原文字与分段，字体、颜色和图片不会显示。",
  });
}
