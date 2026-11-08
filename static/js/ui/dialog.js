import { H } from "../i18n.js";
/**
 * 全站唯一的模态对话框实现（禁止使用原生 alert / confirm / prompt）
 */

const ICONS = { info: "i-info", warn: "i-alert", danger: "i-alert", success: "i-check" };
const host = () => document.getElementById("dialog-host");
const icon = (name) => H`<svg class="icon"><use href="#${name}"/></svg>`;
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let active = null;
/**
 * 关闭动画序号：closeDialog 的延时清空只对「自己那一轮」有效。
 * 不加这个的话，连着开第二个对话框（表单提交 → 立刻弹提示）会被前一个
 * 的 180ms 定时器清空内容，只剩下一个全屏的透明遮罩挡住所有点击。
 */
let closeToken = 0;

function closeDialog(result) {
  if (!active) return;
  const { el, resolve, keyHandler, restoreFocus } = active;
  active = null;
  document.removeEventListener("keydown", keyHandler);
  const dialog = el.querySelector(".dialog");
  if (dialog) {
    dialog.style.transform = "scale(0.96) translateY(8px)";
    dialog.style.opacity = "0";
  }
  el.classList.remove("is-open");
  const token = ++closeToken;
  window.setTimeout(() => {
    if (token !== closeToken) return; // 期间又开了新对话框，别动它
    el.innerHTML = "";
    el.classList.remove("is-open");
    if (restoreFocus && document.contains(restoreFocus)) restoreFocus.focus();
  }, 180);
  resolve(result);
}

function buildMarkup({ title, message, tone, fields, confirmText, cancelText, showCancel }) {
  return H`
    <div class="dialog-backdrop"></div>
    <div class="dialog" role="dialog" aria-modal="true" aria-label="${title || "对话框"}">
      <div class="dialog__title ${tone === "danger" ? "is-danger" : ""}">
        ${icon(ICONS[tone] || ICONS.info)}
        <span>${title || ""}</span>
      </div>
      ${message ? `<div class="dialog__body">${message}</div>` : ""}
      ${fields
        .map((f) =>
          f.type === "select"
            ? `
        <label class="field" data-field="${f.name}">
          ${f.label ? `<span class="field__label">${f.label}</span>` : ""}
          <span class="selectbox">
            <select name="${f.name}">
              ${(f.options || [])
                .map(
                  (o) =>
                    H`<option value="${esc(o.value)}"${String(o.value) === String(f.value ?? "") ? " selected" : ""}>${esc(
                      o.label
                    )}</option>`
                )
                .join("")}
            </select>
            <svg class="icon icon--s"><use href="#i-chevron-down"/></svg>
          </span>
          <span class="field__error" data-error hidden></span>
        </label>`
            : `
        <label class="field" data-field="${f.name}">
          ${f.label ? `<span class="field__label">${f.label}</span>` : ""}
          <span class="textbox">
            <input type="${f.type || "text"}" name="${f.name}" value="${esc(f.value)}"
                   placeholder="${esc(f.placeholder)}" autocomplete="off" spellcheck="false">
          </span>
          <span class="field__error" data-error hidden></span>
        </label>`
        )
        .join("")}
      <div class="dialog__actions">
        ${showCancel ? `<button class="btn btn--subtle" data-act="cancel" type="button">${cancelText}</button>` : ""}
        <button class="btn ${tone === "danger" ? "btn--danger" : "btn--accent"}" data-act="ok" type="button">${confirmText}</button>
      </div>
    </div>`;
}

function open(options) {
  return new Promise((resolve) => {
    if (active) closeDialog(false);

    const el = host();
    el.innerHTML = buildMarkup(options);
    el.classList.add("is-open");
    // 必须在写入新内容之后作废上一个关闭动画的清空定时器
    closeToken += 1;

    const inputs = [...el.querySelectorAll("input, select")];
    const restoreFocus = document.activeElement;

    const submit = () => {
      const values = {};
      let firstInvalid = null;
      for (const input of inputs) {
        const wrap = input.closest("[data-field]");
        const errEl = wrap?.querySelector("[data-error]");
        const field = options.fields.find((f) => f.name === input.name);
        const problem = field?.validate ? field.validate(input.value) : null;
        if (problem) {
          if (errEl) {
            errEl.textContent = problem;
            errEl.hidden = false;
          }
          firstInvalid = firstInvalid || input;
        } else if (errEl) {
          errEl.hidden = true;
        }
        values[input.name] = input.value;
      }
      if (firstInvalid) {
        firstInvalid.focus();
        return;
      }
      closeDialog(inputs.length ? values : true);
    };

    el.querySelector('[data-act="ok"]').addEventListener("click", submit);
    el.querySelector('[data-act="cancel"]')?.addEventListener("click", () => closeDialog(false));
    el.querySelector(".dialog-backdrop").addEventListener("click", () => closeDialog(false));

    inputs.forEach((input) => {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submit();
        }
      });
      input.addEventListener("input", () => {
        const errEl = input.closest("[data-field]")?.querySelector("[data-error]");
        if (errEl) errEl.hidden = true;
      });
    });

    const keyHandler = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeDialog(false);
      }
    };
    document.addEventListener("keydown", keyHandler);

    active = { el, resolve, keyHandler, restoreFocus };
    window.requestAnimationFrame(() => (inputs[0] || el.querySelector('[data-act="ok"]')).focus());
  });
}

export const Dialog = {
  /** 确认框 → Promise<boolean> */
  confirm({
    title = "确认操作",
    message = "",
    confirmText = "确定",
    cancelText = "取消",
    danger = false,
  } = {}) {
    return open({
      title,
      message,
      tone: danger ? "danger" : "info",
      confirmText,
      cancelText,
      showCancel: true,
      fields: [],
    });
  },

  /** 提示框 → Promise<void> */
  async alert({ title = "提示", message = "", confirmText = "好的", tone = "info" } = {}) {
    await open({ title, message, tone, confirmText, cancelText: "", showCancel: false, fields: [] });
  },

  /** 多字段表单 → Promise<Record<string, string> | null> */
  async form({ title = "", message = "", fields = [], confirmText = "保存", cancelText = "取消" } = {}) {
    const result = await open({
      title,
      message,
      tone: "info",
      confirmText,
      cancelText,
      showCancel: true,
      fields,
    });
    if (!result) return null;
    return result;
  },

  /** 输入框 → Promise<string|null> */
  async prompt({
    title = "输入",
    message = "",
    label = "",
    value = "",
    placeholder = "",
    confirmText = "确定",
    validate,
  } = {}) {
    const result = await open({
      title,
      message,
      tone: "info",
      confirmText,
      cancelText: "取消",
      showCancel: true,
      fields: [{ name: "value", label, value, placeholder, validate }],
    });
    if (!result) return null;
    return result.value;
  },
};
