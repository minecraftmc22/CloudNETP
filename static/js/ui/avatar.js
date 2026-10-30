/**
 * 头像渲染小工具：有图用图，没图用昵称首字（渐变底）。
 */
import { escapeHtml } from "../api.js";

export function initialOf(user) {
  const name = (user?.displayName || user?.username || "?").trim();
  return (name[0] || "?").toUpperCase();
}

export function avatarMarkup(user) {
  if (user?.avatarUrl) {
    return `<img class="avatar-img" src="${escapeHtml(user.avatarUrl)}" alt="" draggable="false">`;
  }
  return `<span class="avatar-initial">${escapeHtml(initialOf(user))}</span>`;
}
