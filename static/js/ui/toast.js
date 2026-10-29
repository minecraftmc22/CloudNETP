/**
 * 右上角 Toast（InfoBar 风格）
 */
const HOST_ID = "toast-host";

const ICON = {
  info: "i-info",
  success: "i-check",
  error: "i-alert",
  warn: "i-alert",
};

export function showToast(message, { type = "info", duration = 3200 } = {}) {
  const host = document.getElementById(HOST_ID);
  if (!host) return;

  const el = document.createElement("div");
  el.className = `toast toast--${type}`;
  el.innerHTML = `
    <span class="toast__icon"><svg class="icon icon--s"><use href="#${ICON[type] || ICON.info}"/></svg></span>
    <span class="toast__text"></span>`;
  el.querySelector(".toast__text").textContent = message;
  host.appendChild(el);

  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    el.classList.add("is-out");
    window.setTimeout(() => el.remove(), 220);
  };

  el.addEventListener("click", dismiss);
  timer = window.setTimeout(dismiss, Math.max(1600, duration));
  return dismiss;
}

export const Toast = {
  show: showToast,
  info: (m, o) => showToast(m, { ...o, type: "info" }),
  success: (m, o) => showToast(m, { ...o, type: "success" }),
  error: (m, o) => showToast(m, { ...o, type: "error" }),
  warn: (m, o) => showToast(m, { ...o, type: "warn" }),
};
