/**
 * 分片上传器：先算哈希 → init（可能秒传）→ 并发传分片 → complete 合并。
 *
 * - 断点续传：init 会返回该文件已上传的分片序号，这里只补缺失的；
 * - 失败重试：每个分片最多重试 3 次（指数退避），重传幂等；
 * - 中断：AbortSignal 会在分片边界与 XHR 层同时生效；
 * - 进度：按已完成分片字节 + 在途分片进度合计，节流 120ms 上报。
 */
import { api } from "./api.js";
import { hashFile } from "./sha256.js";

const MAX_HASH_BYTES = 256 * 1024 * 1024; // 超过这个体积不在浏览器算哈希，交给服务端合并时校验
const CONCURRENCY = 3;
const MAX_RETRY = 3;
const REPORT_INTERVAL = 120;

function pickChunkSize(size) {
  if (size <= 32 * 1024 * 1024) return 1024 * 1024; // 1 MiB：小块文件进度更细腻
  if (size <= 256 * 1024 * 1024) return 4 * 1024 * 1024;
  return 8 * 1024 * 1024;
}

function chunkLength(size, chunkSize, index, total) {
  return index < total - 1 ? chunkSize : size - chunkSize * (total - 1);
}

function putChunk(sessionId, index, blob, { signal, onProgress }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("已取消", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/uploads/${sessionId}/${index}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort, { once: true });

    const cleanup = () => signal?.removeEventListener("abort", onAbort);

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total || blob.size);
    });
    xhr.addEventListener("load", () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(blob.size, blob.size);
        resolve();
        return;
      }
      let message = `分片上传失败（${xhr.status}）`;
      try {
        const data = JSON.parse(xhr.responseText);
        if (typeof data?.detail === "string") message = data.detail;
      } catch {
        /* 忽略 */
      }
      const error = new Error(message);
      error.status = xhr.status;
      reject(error);
    });
    xhr.addEventListener("error", () => {
      cleanup();
      const error = new Error("网络中断");
      error.status = 0;
      reject(error);
    });
    xhr.addEventListener("abort", () => {
      cleanup();
      reject(new DOMException("已取消", "AbortError"));
    });
    xhr.send(blob);
  });
}

const sleep = (ms, signal) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("已取消", "AbortError"));
      },
      { once: true }
    );
  });

/**
 * 上传单个文件。
 * @returns {Promise<{node:object, instant:boolean, deduplicated:boolean, sha256:string|null}>}
 */
export async function uploadFile({
  file,
  parentId = null,
  signal,
  sha256: knownSha = null,
  onHash = () => {},
  onPhase = () => {},
  onProgress = () => {},
}) {
  // 暂停后再继续时沿用上次算好的哈希，不必把整个文件重新读一遍
  let sha256 = knownSha || null;

  if (!sha256 && file.size <= MAX_HASH_BYTES) {
    onPhase("hash", 0);
    sha256 = await hashFile(file, {
      signal,
      onProgress: (ratio) => onPhase("hash", ratio),
    });
    onHash(sha256);
  }

  if (signal?.aborted) throw new DOMException("已取消", "AbortError");

  const init = await api.post("/api/uploads/init", {
    name: file.name,
    size: file.size,
    parentId,
    sha256,
    mime: file.type || null,
    chunkSize: pickChunkSize(file.size),
  });

  if (init.instant) {
    onProgress(file.size, file.size);
    return { node: init.node, instant: true, deduplicated: true, sha256 };
  }

  const sessionId = init.sessionId;
  const chunkSize = init.chunkSize;
  const total = init.totalChunks;
  const done = new Set(init.uploaded || []);
  const expected = new Map();
  for (let i = 0; i < total; i += 1) expected.set(i, chunkLength(file.size, chunkSize, i, total));

  let completedBytes = 0;
  done.forEach((index) => {
    completedBytes += expected.get(index) || 0;
  });
  const inFlight = new Map(); // index -> 已上传字节

  const totalLoaded = () => {
    let sum = completedBytes;
    inFlight.forEach((value) => {
      sum += value;
    });
    return Math.min(file.size, sum);
  };

  let lastReport = 0;
  let lastLoaded = totalLoaded();
  let lastTime = performance.now();
  let speed = 0;

  const report = (force = false) => {
    const now = performance.now();
    if (!force && now - lastReport < REPORT_INTERVAL) return;
    lastReport = now;
    const loaded = totalLoaded();
    const deltaTime = (now - lastTime) / 1000;
    if (deltaTime > 0.25) {
      const instant = (loaded - lastLoaded) / deltaTime;
      // 指数平滑，避免数字乱跳
      speed = speed ? speed * 0.65 + instant * 0.35 : instant;
      lastLoaded = loaded;
      lastTime = now;
    }
    onProgress(loaded, file.size, { speed });
  };

  const queue = [];
  for (let i = 0; i < total; i += 1) if (!done.has(i)) queue.push(i);

  onPhase("upload", 0);
  report(true);

  const worker = async () => {
    while (queue.length) {
      if (signal?.aborted) throw new DOMException("已取消", "AbortError");
      const index = queue.shift();
      const start = index * chunkSize;
      const end = start + (expected.get(index) || 0);
      const blob = file.slice(start, end);

      let attempt = 0;
      for (;;) {
        try {
          await putChunk(sessionId, index, blob, {
            signal,
            onProgress: (loaded) => {
              inFlight.set(index, loaded);
              report();
            },
          });
          inFlight.delete(index);
          completedBytes += expected.get(index) || 0;
          report(true);
          break;
        } catch (err) {
          inFlight.delete(index);
          if (err.name === "AbortError" || signal?.aborted) throw err;
          attempt += 1;
          if (attempt >= MAX_RETRY) throw err;
          await sleep(400 * attempt, signal);
        }
      }
    }
  };

  const workers = [];
  for (let i = 0; i < Math.min(CONCURRENCY, queue.length || 1); i += 1) workers.push(worker());
  await Promise.all(workers);

  onPhase("merge", 0);
  const query = sha256 ? `?sha256=${sha256}` : "";
  const result = await api.post(`/api/uploads/${sessionId}/complete${query}`);
  onProgress(file.size, file.size, { speed });
  return {
    node: result.node,
    instant: false,
    deduplicated: Boolean(result.deduplicated),
    sha256: result.sha256 || sha256,
  };
}

/** 放弃上传（服务端删分片）。失败无所谓，服务端会按 TTL 清理。 */
export async function abortUpload(sessionId) {
  if (!sessionId) return;
  try {
    await api.del(`/api/uploads/${sessionId}`);
  } catch {
    /* 忽略 */
  }
}
