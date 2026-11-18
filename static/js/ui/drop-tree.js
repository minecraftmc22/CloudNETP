/**
 * 拖入文件夹：把 DataTransfer 展开成「文件 + 相对路径」，再按路径逐级建目录。
 *
 * 「我的文件」和快传取件码的拖放区共用这一套，两边的行为必须一致：
 *
 * - **每次都新建一份目录**（和 Windows 资源管理器一样）。之前同名目录是「复用」的，
 *   于是把 147 拖进 256 再拖一次，第二次直接并进 256/147，看着就像被覆盖了；
 *   现在由服务端 unique_name 给出 147 (1)，两次的内容各归各的。
 * - 同一次拖拽内部仍按路径串联：`a/b` 和 `a/c` 共用刚建好的 `a`，不会各建一个。
 * - 空目录也保留；系统垃圾文件（.DS_Store 等）跳过；读不出的单个文件跳过。
 */
import { api } from "../api.js";

export const DROP_MAX_FILES = 2000;
export const DROP_MAX_DEPTH = 12;
const DROP_IGNORED = /^(\.DS_Store|Thumbs\.db|desktop\.ini)$/i;

const entryFile = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));
const readDirBatch = (reader) =>
  new Promise((resolve, reject) => reader.readEntries(resolve, reject));

/**
 * 展开拖入的内容。
 * @returns {Promise<{files: {file: File, relPath: string}[], dirs: string[], truncated: boolean}>}
 *          relPath / dirs 都是相对拖入根目录的路径（不带首尾斜杠）
 */
export async function collectDropped(dataTransfer) {
  const items = [...(dataTransfer.items || [])];
  const roots = items
    .map((item) => (item.kind === "file" && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  const files = [];
  const dirs = [];
  /** 同一文件只收一次：某些实现里 readEntries 会重复吐出同一批，别把文件传 N 遍 */
  const seenFiles = new Set();
  const seenDirs = new Set();

  if (!roots.length) {
    return {
      files: [...(dataTransfer.files || [])].map((file) => ({ file, relPath: "" })),
      dirs,
      truncated: false,
    };
  }

  let truncated = false;

  const pushFile = async (entry, prefix) => {
    if (DROP_IGNORED.test(entry.name)) return;
    const relPath = prefix + entry.name;
    try {
      const file = await entryFile(entry);
      const key = `${relPath}\u0000${file.size}\u0000${file.lastModified}`;
      if (seenFiles.has(key)) return;
      seenFiles.add(key);
      files.push({ file, relPath });
    } catch {
      /* 单个文件读不出来就跳过，不影响其它文件 */
    }
  };

  const walk = async (dir, prefix, depth) => {
    if (depth > DROP_MAX_DEPTH) {
      truncated = true;
      return;
    }
    const key = prefix.replace(/\/$/, "");
    if (seenDirs.has(key)) return;
    seenDirs.add(key);
    dirs.push(key);
    const reader = dir.createReader();
    for (;;) {
      let batch = [];
      try {
        batch = await readDirBatch(reader);
      } catch {
        break;
      }
      if (!batch.length) break;
      for (const child of batch) {
        if (files.length >= DROP_MAX_FILES) {
          truncated = true;
          return;
        }
        if (child.isFile) await pushFile(child, prefix);
        else if (child.isDirectory) await walk(child, `${prefix}${child.name}/`, depth + 1);
      }
    }
  };

  for (const entry of roots) {
    if (entry.isFile) await pushFile(entry, "");
    else if (entry.isDirectory) await walk(entry, `${entry.name}/`, 0);
  }
  return { files, dirs, truncated };
}

/**
 * 按相对路径逐级建目录，返回 ensure(path) → folderId。
 *
 * 拖进来的东西每次都新建（重名由服务端加序号），只有「本批自己建的」才复用，
 * 所以同一批里的 a/b、a/c 会落在同一个 a 下面。
 */
export async function ensureDropTree(dirPaths, rootParent) {
  const dirIds = new Map(); // "a/b" -> folderId
  const inFlight = new Map(); // "a/b" -> Promise<folderId>（同一路径并发只建一次）

  async function resolve(path) {
    if (dirIds.has(path)) return dirIds.get(path);
    let parentId = rootParent;
    let prefix = "";
    for (const part of path.split("/").filter(Boolean)) {
      prefix = prefix ? `${prefix}/${part}` : part;
      if (dirIds.has(prefix)) {
        parentId = dirIds.get(prefix);
        continue;
      }
      const res = await api.post("/api/nodes/folder", { parentId, name: part });
      const id = res.node.id;
      dirIds.set(prefix, id);
      parentId = id;
    }
    return parentId;
  }

  function ensure(path) {
    if (!path) return Promise.resolve(rootParent);
    if (dirIds.has(path)) return Promise.resolve(dirIds.get(path));
    if (!inFlight.has(path)) {
      const task = resolve(path).finally(() => inFlight.delete(path));
      inFlight.set(path, task);
    }
    return inFlight.get(path);
  }

  const ordered = [...new Set(dirPaths.filter(Boolean))].sort(
    (a, b) => a.split("/").length - b.split("/").length
  );
  for (const path of ordered) await ensure(path);
  return ensure;
}

/** 相对路径 → 所在目录的相对路径（根目录下的文件回空串） */
export function dirOf(relPath) {
  const slash = (relPath || "").lastIndexOf("/");
  return slash > 0 ? relPath.slice(0, slash) : "";
}
