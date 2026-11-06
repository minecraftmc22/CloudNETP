/**
 * 看板娘悬停操作条：锁定（鼠标穿透）/ 对话 / 关闭
 *
 * 三个动作都只影响「本设备」：
 *  - 锁定记在 localStorage，刷新后仍然生效，解锁入口在设置页
 *  - 关闭走用户设置（showMascot），跟右上角菜单里的开关是同一份数据
 */

import { Toast } from "./toast.js";

const LOCK_KEY = "mascot-lock";

export function isMascotLocked() {
  try {
    return localStorage.getItem(LOCK_KEY) === "1";
  } catch {
    return false;
  }
}

/** 锁定 = 让 #mascot 不接指针事件，鼠标直接落到下面的文件上 */
export function setMascotLocked(locked, { silent = false } = {}) {
  const value = Boolean(locked);
  document.getElementById("mascot")?.classList.toggle("is-locked", value);
  try {
    localStorage.setItem(LOCK_KEY, value ? "1" : "0");
  } catch {
    /* 隐私模式忽略 */
  }
  window.dispatchEvent(new CustomEvent("mascot:lock", { detail: { locked: value } }));
  if (!silent) {
    Toast.info(
      value
        ? "看板娘已锁定，鼠标可以穿透了；在「设置 → 装饰」里可以解锁"
        : "已解锁，看板娘又能拖动和戳一戳了"
    );
  }
  return value;
}

/** 启动时把上次的锁定状态套上 */
export function applyMascotLock() {
  const locked = isMascotLocked();
  document.getElementById("mascot")?.classList.toggle("is-locked", locked);
  return locked;
}

export function bindMascotActions({ onChat, onClose } = {}) {
  const bar = document.getElementById("mascot-actions");
  if (!bar) return;
  applyMascotLock();
  // 只会 bind 一次：重复绑定会让「对话」点一下开又关
  if (bar.dataset.bound === "1") return;
  bar.dataset.bound = "1";

  bar.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mascot-act]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const action = button.dataset.mascotAct;
    if (action === "lock") setMascotLocked(true);
    else if (action === "chat") onChat?.();
    else if (action === "close") onClose?.();
  });

  // 长按也没必要拖到按钮上；挡住指针事件，别让拖动逻辑把点击吃掉
  bar.addEventListener("pointerdown", (event) => event.stopPropagation());
}
