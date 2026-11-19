/**
 * 邮箱验证码：发码 + 倒计时按钮。
 *
 * **秒数以服务端为准**：发送成功用响应里的 `retryAfter`；被限流（429/400）时用
 * 错误体里的 `retryAfter`。前端只负责按这个数字倒数显示，
 * 真正的拦截永远在后端（`app/email_code.py`）。
 */
import { api } from "../api.js";
import { t } from "../i18n.js";

const DEVICE_KEY = "cloudnetp.device";

/** 设备标识：只用于「同一设备发太多次」的限流，不含任何用户信息 */
export function deviceId() {
  let id = "";
  try {
    id = localStorage.getItem(DEVICE_KEY) || "";
  } catch {
    /* 隐私模式下 localStorage 可能不可用，退回临时值 */
  }
  if (!id) {
    id =
      window.crypto?.randomUUID?.() ||
      `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
    try {
      localStorage.setItem(DEVICE_KEY, id);
    } catch {
      /* 存不下就只在本次会话里用 */
    }
  }
  return id.slice(0, 64);
}

/**
 * 在按钮上倒计时。
 * @param {HTMLButtonElement} button
 * @param {number} seconds 服务端给的冷却秒数
 * @param {string} idleLabel 冷却结束后的文案（中文原文，会过 t()）
 */
export function startCountdown(button, seconds, idleLabel = "重新发送") {
  if (!button) return;
  const idle = button.dataset.idleLabel || idleLabel;
  button.dataset.idleLabel = idle;

  window.clearInterval(Number(button.dataset.timer) || 0);
  let left = Math.max(0, Math.round(Number(seconds) || 0));
  if (left <= 0) {
    button.disabled = false;
    button.textContent = t(idle);
    return;
  }

  let timer = 0;
  const paint = () => {
    if (left <= 0) {
      window.clearInterval(timer);
      button.disabled = false;
      button.textContent = t(idle);
      return;
    }
    button.textContent = t("{n} 秒后重发", { n: left });
    left -= 1;
  };

  button.disabled = true;
  paint();
  timer = window.setInterval(paint, 1000);
  button.dataset.timer = String(timer);
}

/**
 * 发码：自动带上设备标识，成功后按服务端返回的秒数开始倒计时。
 * 失败时如果带了 retryAfter（正在冷却），也照样开始倒计时，别让用户反复点。
 */
export async function sendCode({ url, scene, body = {}, button }) {
  if (button) button.disabled = true;
  try {
    const res = await api.post(url, { ...body, scene, device: deviceId() });
    startCountdown(button, res.retryAfter);
    return res;
  } catch (err) {
    const wait = err?.payload?.detail?.retryAfter;
    if (wait) startCountdown(button, wait);
    else if (button) button.disabled = false;
    throw err;
  }
}
