/**
 * 后端接口封装 + 通用格式化工具
 */

export class ApiError extends Error {
  constructor(message, status = 0, code = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code || status;
  }
}

async function request(path, { method = "GET", body, headers = {}, raw = false } = {}) {
  const opts = {
    method,
    headers: { Accept: "application/json", ...headers },
    credentials: "same-origin",
  };

  if (body !== undefined && body !== null) {
    if (raw) {
      opts.body = body;
      opts.headers["Content-Type"] = "application/octet-stream";
    } else {
      opts.body = JSON.stringify(body);
      opts.headers["Content-Type"] = "application/json";
    }
  }

  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError("网络连接失败，请检查服务是否在运行", 0);
  }

  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json().catch(() => null) : null;

  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new CustomEvent("session:expired"));
    }
    throw new ApiError(errorText(data, res.status), res.status, data?.code);
  }
  return data;
}

/** 把后端各种形态的 detail 统一成可读文案（422 校验错误是数组） */
function errorText(data, status) {
  const detail = data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => (typeof item === "string" ? item : item?.msg || item?.loc?.join(".") || ""))
      .filter(Boolean);
    if (parts.length) return `参数有误：${parts.join("；")}`;
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return `请求失败（${status}）`;
}

export const api = {
  get: (p) => request(p),
  post: (p, body) => request(p, { method: "POST", body }),
  put: (p, body) => request(p, { method: "PUT", body }),
  patch: (p, body) => request(p, { method: "PATCH", body }),
  del: (p, body) => request(p, { method: "DELETE", body }),
  raw: (p, blob, method = "PUT") => request(p, { method, body: blob, raw: true }),
};

/* ------------------------------------------------------------------ */
/* 格式化                                                             */
/* ------------------------------------------------------------------ */
const UNITS = ["B", "KB", "MB", "GB", "TB"];

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  let n = Number(bytes);
  if (n < 1024) return `${n} B`;
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${UNITS[i]}`;
}

export function percent(part, total) {
  if (!total) return 0;
  return Math.min(100, Math.max(0, Math.round((part / total) * 100)));
}

export function formatSpeed(bytesPerSecond) {
  const value = Number(bytesPerSecond) || 0;
  if (value < 1) return "";
  return `${formatBytes(value)}/s`;
}

export function formatEta(remainingBytes, bytesPerSecond) {
  const speed = Number(bytesPerSecond) || 0;
  if (speed < 1 || remainingBytes <= 0) return "";
  const seconds = Math.round(remainingBytes / speed);
  if (seconds < 60) return `剩 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `剩 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `剩 ${hours} 小时 ${minutes % 60} 分`;
}

export function formatTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  const minute = 60_000;
  if (diff < minute) return "刚刚";
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  if (diff < 7 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))} 天前`;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}
