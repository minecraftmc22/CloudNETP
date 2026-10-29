/**
 * 轻量弹出菜单（右键菜单 / 用户菜单共用）
 */
let current = null;

function closeMenu() {
  if (!current) return;
  current.el.remove();
  document.removeEventListener("mousedown", current.onDocDown, true);
  document.removeEventListener("keydown", current.onKey);
  window.removeEventListener("resize", closeMenu);
  current = null;
}

export function openMenu({ x = 0, y = 0, items = [], header = null, anchor = null, align = "start" } = {}) {
  closeMenu();
  const el = document.createElement("div");
  el.className = "popover";
  el.setAttribute("role", "menu");

  if (header) {
    el.insertAdjacentHTML(
      "beforeend",
      `<div class="menu-head"><div class="menu-head__name"></div><div class="menu-head__meta"></div></div>`
    );
    el.querySelector(".menu-head__name").textContent = header.name || "";
    el.querySelector(".menu-head__meta").textContent = header.meta || "";
  }

  items.forEach((item) => {
    if (!item || item.sep) {
      el.insertAdjacentHTML("beforeend", '<div class="menu-sep"></div>');
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `menu-item ${item.danger ? "menu-item--danger" : ""}`;
    btn.setAttribute("role", "menuitem");
    btn.innerHTML = `${item.icon ? `<svg class="icon icon--s"><use href="#${item.icon}"/></svg>` : ""}<span></span>`;
    btn.querySelector("span").textContent = item.label;
    if (item.disabled) {
      btn.disabled = true;
      btn.style.opacity = "0.45";
    }
    btn.addEventListener("click", () => {
      closeMenu();
      item.onClick?.();
    });
    el.appendChild(btn);
  });

  document.body.appendChild(el);

  // 定位：贴着锚点或鼠标点
  const rect = el.getBoundingClientRect();
  let left = x;
  let top = y;
  if (anchor) {
    const a = anchor.getBoundingClientRect();
    left = align === "end" ? a.right - rect.width : a.left;
    top = a.bottom + 6;
  }
  const pad = 8;
  left = Math.max(pad, Math.min(left, window.innerWidth - rect.width - pad));
  if (top + rect.height > window.innerHeight - pad) {
    top = anchor ? anchor.getBoundingClientRect().top - rect.height - 6 : window.innerHeight - rect.height - pad;
  }
  el.style.left = `${left}px`;
  el.style.top = `${Math.max(pad, top)}px`;

  const onDocDown = (e) => {
    if (!el.contains(e.target)) closeMenu();
  };
  const onKey = (e) => {
    if (e.key === "Escape") closeMenu();
  };
  document.addEventListener("mousedown", onDocDown, true);
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", closeMenu);

  current = { el, onDocDown, onKey };
  return el;
}

export { closeMenu };
