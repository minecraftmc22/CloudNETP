/**
 * 我的分享：列出自己创建的链接，复制 / 打开 / 取消。
 *
 * - 分页：服务端分页，每页条数可选（默认 10，按设备记住）
 * - 已取消 / 已过期的记录：可以「重新分享」（生成新链接）或「删除记录」
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
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

const PAGE_SIZES = [10, 20, 50];
const SIZE_KEY = "cloudnetp.sharePageSize";

export function renderShares(root) {
  const stored = Number(localStorage.getItem(SIZE_KEY));
  const state = {
    items: [],
    loading: false,
    page: 1,
    pageSize: PAGE_SIZES.includes(stored) ? stored : 10,
    pages: 1,
    total: 0,
  };

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
      <div class="share-pager" data-pager hidden></div>
    </div>`;

  const listEl = root.querySelector("[data-list]");
  const summaryEl = root.querySelector("[data-summary]");
  const pagerEl = root.querySelector("[data-pager]");

  const absUrl = (share) => H`${siteOrigin()}${share.url}`;
  const dead = (share) => share.status !== "active";

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api.get(
        `/api/shares?page=${state.page}&pageSize=${state.pageSize}`
      );
      state.items = data.items || [];
      state.total = data.total || 0;
      state.pages = data.pages || 1;
      state.page = data.page || 1;
      render();
    } catch (err) {
      pagerEl.hidden = true;
      listEl.innerHTML = H`<div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
          <div class="empty__title">加载失败</div>
          <div class="empty__desc">${escapeHtml(err.message)}</div>
        </div>`;
    } finally {
      state.loading = false;
    }
  }

  function renderPager() {
    if (state.total <= PAGE_SIZES[0] && state.pages <= 1 && state.items.length <= state.pageSize) {
      // 只有一页时也给「每页显示」入口，方便提前改设置
    }
    pagerEl.hidden = false;
    pagerEl.innerHTML = H`
      <span class="share-pager__info">${t("共 {n} 个链接 · 第 {page}/{pages} 页", {
        n: state.total,
        page: state.page,
        pages: state.pages,
      })}</span>
      <span class="grow"></span>
      <label class="share-pager__size">
        <span>${t("每页")}</span>
        <select data-page-size>
          ${PAGE_SIZES.map(
            (size) => H`<option value="${size}" ${size === state.pageSize ? "selected" : ""}>${size}</option>`
          ).join("")}
        </select>
        <span>${t("个")}</span>
      </label>
      <button class="icon-btn" type="button" data-page="prev" title="${t("上一页")}"
              ${state.page <= 1 ? "disabled" : ""}>
        <svg class="icon"><use href="#i-chevron"/></svg>
      </button>
      <button class="icon-btn" type="button" data-page="next" title="${t("下一页")}"
              ${state.page >= state.pages ? "disabled" : ""}>
        <svg class="icon" style="transform:rotate(180deg)"><use href="#i-chevron"/></svg>
      </button>`;

    pagerEl.querySelector("[data-page-size]").addEventListener("change", (event) => {
      state.pageSize = Number(event.target.value) || 10;
      localStorage.setItem(SIZE_KEY, String(state.pageSize));
      state.page = 1;
      load();
    });
    pagerEl.querySelector('[data-page="prev"]').addEventListener("click", () => {
      if (state.page <= 1) return;
      state.page -= 1;
      load();
    });
    pagerEl.querySelector('[data-page="next"]').addEventListener("click", () => {
      if (state.page >= state.pages) return;
      state.page += 1;
      load();
    });
  }

  function render() {
    summaryEl.textContent = state.total
      ? t("{total} 个链接 · 当前第 {page} 页", { total: state.total, page: state.page })
      : "";

    if (!state.items.length) {
      pagerEl.hidden = true;
      if (state.page > 1) {
        // 删掉最后一页的最后一条时，回退一页
        state.page = Math.max(1, state.page - 1);
        load();
        return;
      }
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
        const isDead = dead(share);
        const kindLabel =
          share.type === "multi"
            ? t("合集 {n} 项", { n: share.itemCount })
            : share.type === "folder"
              ? t("文件夹")
              : formatBytes(share.size);
        const meta = [
          kindLabel,
          t("创建于 {when}", { when: formatTime(share.createdAt) }),
          share.hasCode ? t("有提取码") : t("免提取码"),
          share.maxDownloads
            ? t("已下载 {n}/{max}", { n: share.downloadCount, max: share.maxDownloads })
            : t("已下载 {n} 次", { n: share.downloadCount }),
          share.expiresAt
            ? t("到期 {date}", {
                date: new Date(share.expiresAt).toLocaleDateString(
                  document.documentElement.lang || "zh-CN"
                ),
              })
            : t("永久有效"),
        ].join(" · ");

        return H`
        <div class="share-row${isDead ? " is-dead" : ""}" data-id="${share.id}">
          <span class="share-row__icon"><svg class="icon"><use href="#${share.icon}"/></svg></span>
          <div class="share-row__main">
            <div class="share-row__name" title="${escapeHtml(share.name)}">${escapeHtml(share.name)}</div>
            <div class="share-row__meta">${meta}</div>
          </div>
          <span class="share-row__status is-${share.status}">${t(STATUS_TEXT[share.status] || share.status)}</span>
          <div class="share-row__actions">
            ${
              isDead
                ? H`
              <button class="btn btn--subtle" type="button" data-act="renew"
                      ${share.status === "missing" ? "disabled" : ""}>
                <svg class="icon icon--s"><use href="#i-refresh"/></svg>${t("重新分享")}
              </button>
              <button class="icon-btn" type="button" data-act="forget" title="${t("删除记录")}">
                <svg class="icon"><use href="#i-trash"/></svg>
              </button>`
                : H`
              <button class="btn btn--subtle" type="button" data-act="copy">
                <svg class="icon icon--s"><use href="#i-link"/></svg>${t("复制链接")}
              </button>
              <button class="icon-btn" type="button" data-act="open" title="${t("在新标签页打开")}">
                <svg class="icon"><use href="#i-arrow-up-right"/></svg>
              </button>
              <button class="icon-btn" type="button" data-act="revoke" title="${t("取消分享")}">
                <svg class="icon"><use href="#i-trash"/></svg>
              </button>`
            }
          </div>
        </div>`;
      })
      .join("");

    listEl.querySelectorAll(".share-row").forEach((row) => {
      const share = state.items.find((s) => s.id === Number(row.dataset.id));
      if (!share) return;

      row.querySelector('[data-act="copy"]')?.addEventListener("click", async () => {
        const ok = await copyText(absUrl(share));
        ok ? Toast.success(t("链接已复制")) : Toast.error(t("复制失败，请手动选中"));
      });

      row.querySelector('[data-act="open"]')?.addEventListener("click", () => {
        window.open(absUrl(share), "_blank", "noopener");
      });

      row.querySelector('[data-act="revoke"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("取消分享"),
          message: t("取消后「{name}」的链接立即失效，已经发出去的链接也打不开了。", {
            name: escapeHtml(share.name),
          }),
          confirmText: t("取消分享"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/shares/${share.id}`);
          Toast.success(t("分享已取消"));
          await load();
        } catch (err) {
          Toast.error(err.message);
        }
      });

      row.querySelector('[data-act="forget"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("删除这条分享记录"),
          message: t("只删掉列表里的记录，网盘里的文件不受影响。删了就不能再从这里重新分享了。"),
          confirmText: t("删除记录"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/shares/${share.id}`);
          Toast.success(t("记录已删除"));
          await load();
        } catch (err) {
          Toast.error(err.message);
        }
      });

      row.querySelector('[data-act="renew"]')?.addEventListener("click", async () => {
        try {
          const data = await api.post(`/api/shares/${share.id}/renew`);
          const fresh = data.share;
          await copyText(absUrl(fresh));
          if (fresh.extractCode) {
            await Dialog.alert({
              title: t("已重新分享"),
              message: t("新链接已经复制到剪贴板。新的提取码是 {code}，记得一起发出去。", {
                code: fresh.extractCode,
              }),
            });
          } else {
            Toast.success(t("已重新分享，新链接已复制"));
          }
          await load();
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });

    renderPager();
  }

  root.querySelector('[data-act="refresh"]').addEventListener("click", load);
  load();
}
