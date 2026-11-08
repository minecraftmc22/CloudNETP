/**
 * 我的分享：列出自己创建的链接，复制 / 打开 / 取消。
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H } from "../i18n.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { copyText } from "../ui/share.js";
import { siteOrigin } from "../store.js";

const STATUS_TEXT = {
  active: "有效",
  expired: "已过期",
  revoked: "已取消",
  exhausted: "次数已用完",
  missing: "文件已删除",
};

export function renderShares(root) {
  const state = { items: [], loading: false };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title">
        <svg class="icon"><use href="#i-share"/></svg>
        <span>我的分享</span>
      </div>
      <span class="view-sub" data-summary></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新列表">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="view-body">
      <div class="share-list" data-list>
        <div class="share-loading"><span class="spinner"></span></div>
      </div>
    </div>`;

  const listEl = root.querySelector("[data-list]");
  const summaryEl = root.querySelector("[data-summary]");

  const absUrl = (share) => H`${siteOrigin()}${share.url}`;

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api.get("/api/shares");
      state.items = data.items || [];
      render();
    } catch (err) {
      listEl.innerHTML = H`<div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
          <div class="empty__title">加载失败</div>
          <div class="empty__desc">${escapeHtml(err.message)}</div>
        </div>`;
    } finally {
      state.loading = false;
    }
  }

  function render() {
    const active = state.items.filter((s) => s.status === "active").length;
    summaryEl.textContent = state.items.length
      ? H`${state.items.length} 个链接 · ${active} 个有效`
      : "";

    if (!state.items.length) {
      listEl.innerHTML = H`<div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-share"/></svg></div>
          <div class="empty__title">还没有分享过文件</div>
          <div class="empty__desc">在「我的文件」里右键任意文件或文件夹，选择「分享链接」即可生成。</div>
          <a class="btn btn--accent" href="#/files" style="margin-top:14px">
            <svg class="icon icon--s"><use href="#i-folder"/></svg>去我的文件
          </a>
        </div>`;
      return;
    }

    listEl.innerHTML = state.items
      .map((share) => {
        const dead = share.status !== "active";
        const kindLabel =
          share.type === "multi"
            ? H`合集 ${share.itemCount} 项`
            : share.type === "folder"
              ? "文件夹"
              : formatBytes(share.size);
        const meta = [
          kindLabel,
          `创建于 ${formatTime(share.createdAt)}`,
          share.hasCode ? "有提取码" : "免提取码",
          share.maxDownloads ? H`已下载 ${share.downloadCount}/${share.maxDownloads}` : H`已下载 ${share.downloadCount} 次`,
          share.expiresAt ? H`到期 ${new Date(share.expiresAt).toLocaleDateString("zh-CN")}` : "永久有效",
        ].join(" · ");

        return H`
        <div class="share-row${dead ? " is-dead" : ""}" data-id="${share.id}">
          <span class="share-row__icon"><svg class="icon"><use href="#${share.icon}"/></svg></span>
          <div class="share-row__main">
            <div class="share-row__name" title="${escapeHtml(share.name)}">${escapeHtml(share.name)}</div>
            <div class="share-row__meta">${meta}</div>
          </div>
          <span class="share-row__status is-${share.status}">${STATUS_TEXT[share.status] || share.status}</span>
          <div class="share-row__actions">
            <button class="btn btn--subtle" type="button" data-act="copy" ${dead ? "disabled" : ""}>
              <svg class="icon icon--s"><use href="#i-link"/></svg>复制链接
            </button>
            <button class="icon-btn" type="button" data-act="open" title="在新标签页打开" ${dead ? "disabled" : ""}>
              <svg class="icon"><use href="#i-arrow-up-right"/></svg>
            </button>
            <button class="icon-btn" type="button" data-act="revoke" title="取消分享"
                    ${share.status === "revoked" ? "disabled" : ""}>
              <svg class="icon"><use href="#i-trash"/></svg>
            </button>
          </div>
        </div>`;
      })
      .join("");

    listEl.querySelectorAll(".share-row").forEach((row) => {
      const share = state.items.find((s) => s.id === Number(row.dataset.id));
      if (!share) return;

      row.querySelector('[data-act="copy"]')?.addEventListener("click", async () => {
        const ok = await copyText(absUrl(share));
        ok ? Toast.success("链接已复制") : Toast.error("复制失败，请手动选中");
      });

      row.querySelector('[data-act="open"]')?.addEventListener("click", () => {
        window.open(absUrl(share), "_blank", "noopener");
      });

      row.querySelector('[data-act="revoke"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: "取消分享",
          message: H`取消后「${escapeHtml(share.name)}」的链接立即失效，已经发出去的链接也打不开了。`,
          confirmText: "取消分享",
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/shares/${share.id}`);
          Toast.success("分享已取消");
          await load();
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });
  }

  root.querySelector('[data-act="refresh"]').addEventListener("click", load);
  load();
}
