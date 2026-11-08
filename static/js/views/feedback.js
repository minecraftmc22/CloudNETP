/**
 * 意见反馈：提交建议 / 问题，查看自己的历史反馈与处理状态。
 */
import { api, escapeHtml, formatTime } from "../api.js";
import { H } from "../i18n.js";
import { store } from "../store.js";
import { Toast } from "../ui/toast.js";

const KINDS = [
  { id: "suggestion", label: "建议" },
  { id: "bug", label: "问题" },
  { id: "other", label: "其他" },
];

const STATUS_CLASS = { 0: "is-pending", 1: "is-done" };

function itemHtml(item) {
  const title = item.title
    ? H`<div class="fb-item__title">${escapeHtml(item.title)}</div>`
    : "";
  return H`
    <article class="fb-item">
      <div class="fb-item__top">
        <span class="fb-tag fb-tag--${escapeHtml(item.kind)}">${escapeHtml(item.kindLabel)}</span>
        <span class="fb-status ${STATUS_CLASS[item.status] || "is-pending"}">${escapeHtml(item.statusLabel)}</span>
        <span class="fb-item__time">${escapeHtml(formatTime(item.createdAt))}</span>
      </div>
      ${title}
      <p class="fb-item__content">${escapeHtml(item.content)}</p>
      ${
        item.contact
          ? H`<div class="fb-item__meta">联系方式：${escapeHtml(item.contact)}</div>`
          : ""
      }
    </article>`;
}

export function renderFeedback(root) {
  const { user, meta } = store.get();
  let kind = "suggestion";
  let submitting = false;

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-mail"/></svg>意见反馈</div>
      <span class="view-sub">问题、建议或想要的功能，都可以写在这里</span>
      <a class="btn btn--subtle btn--sm fb-back" href="#/about">
        <svg class="icon icon--s" style="transform:rotate(180deg)"><use href="#i-chevron"/></svg>关于
      </a>
    </div>
    <div class="view-body">
      <div class="feedback">
        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-mail"/></svg>写一条反馈</div>
          <div class="feedback-intro">
            反馈只有管理员能看到；如果是登录或上传问题，附上大致时间和操作步骤会更容易定位。
            ${
              meta?.contactEmail || meta?.contactQq
                ? H`<span class="feedback-intro__contact">也可以直接联系管理员：${
                    meta.contactEmail ? escapeHtml(meta.contactEmail) : ""
                  }${meta.contactEmail && meta.contactQq ? " / " : ""}${
                    meta.contactQq ? H`QQ ${escapeHtml(meta.contactQq)}` : ""
                  }</span>`
                : ""
            }
          </div>
          <form class="feedback-form" data-fb-form novalidate>
            <div class="fb-kinds" data-kinds>
              ${KINDS.map(
                (k) =>
                  H`<button class="fb-kind ${k.id === kind ? "is-active" : ""}" type="button" data-kind="${k.id}">${k.label}</button>`
              ).join("")}
            </div>
            <label class="field">
              <span class="field__label">标题（选填）</span>
              <span class="textbox"><input name="title" maxlength="120" placeholder="一句话概括"></span>
            </label>
            <label class="field">
              <span class="field__label">内容</span>
              <textarea class="feedback-textarea" name="content" rows="6" maxlength="2000"
                        placeholder="遇到了什么？期望是什么？" required></textarea>
              <span class="feedback-count" data-count>0 / 2000</span>
            </label>
            <label class="field">
              <span class="field__label">联系方式（选填）</span>
              <span class="textbox"><input name="contact" maxlength="200"
                value="${escapeHtml(user?.email || "")}" placeholder="邮箱 / QQ，方便回复你"></span>
            </label>
            <div class="feedback-actions">
              <span class="muted">提交后管理员会在后台看到，处理状态会显示在下方</span>
              <button class="btn btn--accent" type="submit" data-submit>提交反馈</button>
            </div>
          </form>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-clock"/></svg>我的反馈
            <span class="feedback-total" data-total></span>
          </div>
          <div class="feedback-list" data-list>
            <div class="admin-loading"><span class="spinner"></span></div>
          </div>
        </section>
      </div>
    </div>`;

  const form = root.querySelector("[data-fb-form]");
  const textarea = form.querySelector('[name="content"]');
  const counter = root.querySelector("[data-count]");
  const submitBtn = root.querySelector("[data-submit]");
  const listEl = root.querySelector("[data-list]");
  const totalEl = root.querySelector("[data-total]");

  root.querySelectorAll("[data-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      kind = btn.dataset.kind;
      root.querySelectorAll("[data-kind]").forEach((b) => b.classList.toggle("is-active", b === btn));
    });
  });

  const paintCount = () => {
    counter.textContent = `${textarea.value.length} / 2000`;
  };
  textarea.addEventListener("input", paintCount);

  async function loadMine() {
    try {
      const data = await api.get("/api/feedback");
      const items = data.items || [];
      totalEl.textContent = items.length ? H`最近 ${items.length} 条` : "";
      if (!items.length) {
        listEl.innerHTML = H`<div class="feedback-empty">
          <svg class="icon"><use href="#i-mail"/></svg>
          <span>还没有提交过反馈</span>
        </div>`;
        return;
      }
      listEl.innerHTML = items.map(itemHtml).join("");
    } catch (err) {
      listEl.innerHTML = H`<div class="feedback-empty is-error">${escapeHtml(err.message)}</div>`;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    const content = textarea.value.trim();
    if (!content) {
      Toast.warn("先写点内容吧");
      textarea.focus();
      return;
    }
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = "提交中…";
    try {
      await api.post("/api/feedback", {
        kind,
        title: form.elements.title.value.trim(),
        content,
        contact: form.elements.contact.value.trim(),
      });
      Toast.success("反馈已提交，感谢！");
      form.elements.title.value = "";
      textarea.value = "";
      paintCount();
      await loadMine();
    } catch (err) {
      Toast.error(err.message);
    } finally {
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "提交反馈";
    }
  });

  loadMine();
}
