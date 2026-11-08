/** 版本检查的共用逻辑：只读 GitHub，不会执行更新。 */
import { api, escapeHtml } from "../api.js";

import { H } from "../i18n.js";
export function checkForUpdates(force = false) {
  return api.get(`/api/version/check${force ? "?force=1" : ""}`);
}

/** 检查结果 → 一行状态文案（关于页 / 后台共用） */
export function updateResultHtml(data) {
  if (!data) return "";
  if (!data.ok) {
    return H`<span class="update-state is-error">检查失败：${escapeHtml(data.error || "未知错误")}</span>`;
  }
  if (data.hasUpdate && data.latest) {
    const commit = data.commit || {};
    const link = commit.url
      ? ` · <a href="${escapeHtml(commit.url)}" target="_blank" rel="noopener">${escapeHtml(
          commit.sha ? commit.sha.slice(0, 7) : "查看提交"
        )}</a>`
      : "";
    return H`<span class="update-state is-new">发现新版本 <b>v${escapeHtml(data.latest)}</b>${link}</span>`;
  }
  return H`<span class="update-state is-ok">已是最新版本 v${escapeHtml(data.current)}</span>`;
}

/** 执行更新的结果 → 一行状态文案 */
export function applyResultHtml(data) {
  if (!data) return "";
  if (!data.ok) {
    const files = data.files && data.files.length ? `（${data.files.join("、")}）` : "";
    return H`<span class="update-state is-error">更新失败：${escapeHtml(data.error || "未知错误")}${escapeHtml(files)}</span>`;
  }
  if (!data.updated) {
    return H`<span class="update-state is-ok">${escapeHtml(data.message || "已经是最新版本")}</span>`;
  }
  const version = data.toVersion ? `v${escapeHtml(data.toVersion)}` : "最新代码";
  const commit = data.commit ? ` · ${escapeHtml(data.commit)}` : "";
  const restart =
    data.restart === "spawn" || data.restart === "reload"
      ? "，服务正在重启"
      : "，请手动重启服务";
  return H`<span class="update-state is-new">已更新到 ${version}${commit}${restart}</span>`;
}
