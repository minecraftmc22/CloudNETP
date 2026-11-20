/**
 * 下拉框浮层：把系统原生的白色弹层换成跟界面同款的 acrylic 浮层。
 *
 * 做法是「渐进增强」——不开新控件，而是拦下 `<select>` 的原生弹出：
 *   - 闭合态还是原生 select，所以各页面既有的尺寸 / 边框 / 字体样式全都照旧；
 *   - 鼠标点开时 preventDefault 挡住系统弹层，自己画一层列表（支持键盘、分组、禁用项）；
 *   - 触屏（pointerType === "touch"）不接管，手机继续用系统选择器，那套在移动端更好用。
 * 选中后写回 select.value 并派发 change / input，所有既有事件处理器都不用改。
 */

const MENU_ID = "ui-select-menu";
const ACTIVE = "is-active";
const SELECTED = "is-selected";

/** 当前打开的浮层状态 */
let state = null;

function menuNode() {
  let el = document.getElementById(MENU_ID);
  if (!el) {
    el = document.createElement("div");
    el.id = MENU_ID;
    el.className = "ui-select__menu";
    el.hidden = true;
    el.setAttribute("role", "listbox");
    document.body.appendChild(el);
  }
  return el;
}

function rowOf(opt) {
  return {
    value: opt.value,
    label: opt.label || opt.textContent || "",
    disabled: opt.disabled,
  };
}

/** 把 options 拍平成可渲染的条目（optgroup 用小标题分节） */
function collect(select) {
  const rows = [];
  for (const child of select.children) {
    if (child.tagName === "OPTGROUP") {
      rows.push({ group: child.label || "" });
      for (const opt of child.children) {
        if (opt.tagName === "OPTION") rows.push(rowOf(opt));
      }
    } else if (child.tagName === "OPTION") {
      rows.push(rowOf(child));
    }
  }
  return rows;
}

function position(select, el) {
  const rect = select.getBoundingClientRect();
  el.style.minWidth = `${Math.max(rect.width, 132)}px`;
  el.style.left = "0px";
  el.style.top = "0px";
  const box = el.getBoundingClientRect();
  const pad = 8;
  const left = Math.min(rect.left, window.innerWidth - box.width - pad);
  let top = rect.bottom + 4;
  if (top + box.height > window.innerHeight - pad) {
    const above = rect.top - box.height - 4;
    top = above >= pad ? above : Math.max(pad, window.innerHeight - box.height - pad);
  }
  el.style.left = `${Math.max(pad, left)}px`;
  el.style.top = `${Math.max(pad, top)}px`;
}

function paint(rows) {
  const el = menuNode();
  el.innerHTML = "";
  rows.forEach((row, index) => {
    if (row.group !== undefined) {
      const head = document.createElement("div");
      head.className = "ui-select__group";
      head.textContent = row.group;
      el.appendChild(head);
      return;
    }
    const item = document.createElement("div");
    item.className = "ui-select__item";
    item.setAttribute("role", "option");
    item.dataset.index = String(index);
    item.textContent = row.label;
    if (row.disabled) item.classList.add("is-disabled");
    el.appendChild(item);
  });
}

function selectableItems() {
  return [...menuNode().querySelectorAll(".ui-select__item:not(.is-disabled)")];
}

/** 高亮指定行号（options 里的下标），找不到就落在第一个可选项上 */
function highlight(rowIndex) {
  const items = selectableItems();
  if (!items.length) return;
  const target = items.find((item) => Number(item.dataset.index) === rowIndex) || items[0];
  items.forEach((item) => item.classList.toggle(ACTIVE, item === target));
  target.scrollIntoView({ block: "nearest" });
  state.index = Number(target.dataset.index);
}

function move(step) {
  const items = selectableItems();
  if (!items.length || !state) return;
  const at = items.findIndex((item) => Number(item.dataset.index) === state.index);
  let next;
  if (!Number.isFinite(step)) next = step > 0 ? items.length - 1 : 0;
  else if (at < 0) next = step > 0 ? 0 : items.length - 1;
  else next = (at + step + items.length) % items.length;
  highlight(Number(items[next].dataset.index));
}

function chosen(select, value) {
  if (select.value === value) return;
  select.value = value;
  select.dispatchEvent(new Event("input", { bubbles: true }));
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function close() {
  if (!state) return;
  const { select, el, onKey, onDown, onScroll } = state;
  el.hidden = true;
  el.innerHTML = "";
  select.removeAttribute("aria-expanded");
  document.removeEventListener("keydown", onKey, true);
  document.removeEventListener("pointerdown", onDown, true);
  window.removeEventListener("scroll", onScroll, true);
  window.removeEventListener("resize", close);
  state = null;
}

function openSelect(select) {
  close();
  const el = menuNode();
  const rows = collect(select);
  if (!rows.some((row) => row.group === undefined && !row.disabled)) return;

  paint(rows);
  el.hidden = false;
  select.setAttribute("aria-expanded", "true");
  state = { select, el, index: -1 };
  position(select, el);

  const current = rows.findIndex(
    (row) => row.group === undefined && String(row.value) === String(select.value)
  );
  el.querySelectorAll(".ui-select__item").forEach((item) => {
    item.classList.toggle(SELECTED, Number(item.dataset.index) === current);
  });
  highlight(current);

  const onKey = (event) => {
    const key = event.key;
    if (key === "Escape") {
      event.preventDefault();
      close();
      select.focus({ preventScroll: true });
    } else if (key === "ArrowDown") {
      event.preventDefault();
      move(1);
    } else if (key === "ArrowUp") {
      event.preventDefault();
      move(-1);
    } else if (key === "Home") {
      event.preventDefault();
      move(-Infinity);
    } else if (key === "End") {
      event.preventDefault();
      move(Infinity);
    } else if (key === "Enter" || key === " ") {
      event.preventDefault();
      const active = el.querySelector(`.ui-select__item.${ACTIVE}`);
      if (active) {
        const row = rows[Number(active.dataset.index)];
        if (row && !row.disabled) chosen(select, row.value);
      }
      close();
      select.focus({ preventScroll: true });
    } else if (key === "Tab") {
      close();
    }
  };

  const onDown = (event) => {
    // 点到别的下拉框交给「开菜单」那个监听处理，这里别抢着关
    if (event.target.closest?.("select")) return;
    if (!event.composedPath().includes(el)) close();
  };

  const onScroll = (event) => {
    if (event.target instanceof Node && el.contains(event.target)) return;
    close();
  };

  document.addEventListener("keydown", onKey, true);
  document.addEventListener("pointerdown", onDown, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", close);

  state.onKey = onKey;
  state.onDown = onDown;
  state.onScroll = onScroll;
}

function init() {
  // 开菜单：只接鼠标，触屏留给系统选择器
  document.addEventListener(
    "pointerdown",
    (event) => {
      const select = event.target.closest?.("select");
      if (!select || select.disabled || select.multiple || Number(select.size) > 1) return;
      if (event.pointerType === "touch") return;
      event.preventDefault();
      if (state && state.select === select) close();
      else openSelect(select);
    },
    true
  );

  // 键盘唤出（没打开时）：↑ ↓ Enter 空格 都算
  document.addEventListener(
    "keydown",
    (event) => {
      if (state) return;
      const select = event.target.closest?.("select");
      if (!select || select.disabled) return;
      const key = event.key;
      if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
        event.preventDefault();
        openSelect(select);
      }
    },
    true
  );

  // 点条目：pointerdown 比 click 早，先选中再收起，避免原生 select 抢焦点
  document.addEventListener(
    "pointerdown",
    (event) => {
      if (!state) return;
      const item = event.target.closest?.(".ui-select__item");
      if (!item || !state.el.contains(item)) return;
      event.preventDefault();
      const rows = collect(state.select);
      const row = rows[Number(item.dataset.index)];
      if (!row || row.disabled) return;
      const select = state.select;
      chosen(select, row.value);
      close();
      select.focus({ preventScroll: true });
    },
    true
  );

  // 指针滑过跟随高亮，键盘与鼠标不打架
  document.addEventListener("pointermove", (event) => {
    if (!state) return;
    const item = event.target.closest?.(".ui-select__item");
    if (!item || !state.el.contains(item) || item.classList.contains("is-disabled")) return;
    highlight(Number(item.dataset.index));
  });
}

export { init as initSelectUI, close as closeSelectUI };
