/** 版本检查的共用逻辑：只读 GitHub，不会执行更新。 */
import { api, escapeHtml } from "../api.js";

export function checkForUpdates(force = false) {
  return api.get(`/api/version/check${force ? "?force=1" : ""}`);
}

/** 检查结果 → 一行状态文案（关于页 / 后台共用） */
export function updateResultHtml(data) {
  if (!data) return "";
  if (!data.ok) {
    return `<span class="update-state is-error">检查失败：${escapeHtml(data.error || "未知错误")}</span>`;
  }
  if (data.hasUpdate && data.latest) {
    const commit = data.commit || {};
    const link = commit.url
      ? ` · <a href="${escapeHtml(commit.url)}" target="_blank" rel="noopener">${escapeHtml(
          commit.sha ? commit.sha.slice(0, 7) : "查看提交"
        )}</a>`
      : "";
    return `<span class="update-state is-new">发现新版本 <b>v${escapeHtml(data.latest)}</b>${link}</span>`;
  }
  return `<span class="update-state is-ok">已是最新版本 v${escapeHtml(data.current)}</span>`;
}
