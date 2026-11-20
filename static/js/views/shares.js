/**
 * 我的分享：列出自己创建的链接，复制 / 打开 / 取消。
 *
 * - 分页：服务端分页，每页条数可选（默认 10，按设备记住）
 * - 「历史过期分享」：过期 / 已取消 / 次数用完的记录收进另一个列表，可以逐条删除或直接清空
 * - 已失效的记录还能「重新分享」（按原文件与时长生成新链接）
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
    // active 只看还能用的 / history 只看历史过期
    scope: "active",
    counts: { active: 0, history: 0 },
  };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title">
        <svg class="icon"><use href="#i-share"/></svg>
        <span>我的分享</span>
      </div>
      <span class="view-sub" data-summary></span>
      <div class="grow"></div>
      <button class="btn btn--subtle listbar__toggle" type="button" data-act="history" title="">
        <svg class="icon icon--s" data-history-icon><use href="#i-clock"/></svg>
        <span data-history-label>历史过期分享</span>
        <span class="listbar__count" data-history-count hidden>0</span>
      </button>
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
  const toggleEl = root.querySelector('[data-act="history"]');
  const labelEl = root.querySelector("[data-history-label]");
  const countEl = root.querySelector("[data-history-count]");
  const toggleIconEl = root.querySelector("[data-history-icon]");

  const absUrl = (share) => H`${siteOrigin()}${share.url}`;
  const history = () => state.scope === "history";

  function paintToggle() {
    const dead = state.counts.history;
    countEl.textContent = String(dead);
    countEl.hidden = !dead;
    labelEl.textContent = history() ? t("返回当前分享") : t("历史过期分享");
    toggleEl.title = history()
      ? t("回到还能用的分享列表")
      : t("过期、已取消、次数用完的分享都在这里");
    toggleEl.classList.toggle("is-on", history());
    toggleIconEl.setAttribute("href", history() ? "#i-chevron" : "#i-clock");
    toggleIconEl.style.transform = history() ? "rotate(90deg)" : "";
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api.get(
        `/api/shares?page=${state.page}&pageSize=${state.pageSize}&scope=${state.scope}`
      );
      state.items = data.items || [];
      state.total = data.total || 0;
      state.pages = data.pages || 1;
      state.page = data.page || 1;
      state.counts = data.counts || { active: state.total, history: 0 };
      render();
    } catch (err) {
      pagerEl.hidden = true;
      paintToggle();
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
    pagerEl.hidden = false;
    pagerEl.innerHTML = H`
      <span class="share-pager__info">${t("共 {n} 个链接 · 第 {page}/{pages} 页", {
        n: state.total,
        page: state.page,
        pages: state.pages,
      })}</span>
      <span class="grow"></span>
      ${
        history() && state.counts.history
          ? H`<button class="btn btn--subtle btn--danger" type="button" data-act="clear-history">
              <svg class="icon icon--s"><use href="#i-trash"/></svg>${t("清空历史")}
            </button>`
          : ""
      }
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
        <svg class="icon" style="transform:rotate(180deg)"><use href="#i-chevron"/></svg>
      </button>
      <button class="icon-btn" type="button" data-page="next" title="${t("下一页")}"
              ${state.page >= state.pages ? "disabled" : ""}>
        <svg class="icon"><use href="#i-chevron"/></svg>
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
    pagerEl.querySelector('[data-act="clear-history"]')?.addEventListener("click", async () => {
      const ok = await Dialog.confirm({
        title: t("清空历史记录"),
        message: t("把 {n} 条已失效的分享记录全部删掉。网盘里的文件不受影响，只是这些链接的痕迹不再留在列表里。", {
          n: state.counts.history,
        }),
        confirmText: t("清空"),
        danger: true,
      });
      if (!ok) return;
      try {
        const data = await api.del("/api/shares/history");
        Toast.success(t("已删除 {n} 条记录", { n: data.deleted || 0 }));
        state.page = 1;
        await load();
      } catch (err) {
        Toast.error(err.message);
      }
    });
  }

  function render() {
    paintToggle();
    summaryEl.textContent = state.total
      ? history()
        ? t("历史过期 · {total} 条", { total: state.total })
        : t("{total} 个链接 · 当前第 {page} 页", { total: state.total, page: state.page })
      : "";

    if (!state.items.length) {
      pagerEl.hidden = true;
      if (state.page > 1) {
        // 删掉最后一页的最后一条时，回退一页
        state.page = Math.max(1, state.page - 1);
        load();
        return;
      }
      listEl.innerHTML = history()
        ? H`<div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-clock"/></svg></div>
            <div class="empty__title">没有历史分享</div>
            <div class="empty__desc">过期、已取消或次数用完的分享会自动收进这里，到时候可以在这里删除记录。</div>
            <button class="btn btn--subtle" type="button" data-act="back" style="margin-top:14px">
              <svg class="icon icon--s" style="transform:rotate(90deg)"><use href="#i-chevron"/></svg>${t("返回当前分享")}
            </button>
          </div>`
        : H`<div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-share"/></svg></div>
            <div class="empty__title">还没有分享过文件</div>
            <div class="empty__desc">在「我的文件」里右键任意文件或文件夹，选择「分享链接」即可生成。</div>
            <a class="btn btn--accent" href="#/files" style="margin-top:14px">
              <svg class="icon icon--s"><use href="#i-folder"/></svg>去我的文件
            </a>
          </div>`;
      listEl.querySelector('[data-act="back"]')?.addEventListener("click", () => switchScope());
      return;
    }

    listEl.innerHTML = state.items
      .map((share) => {
        const isDead = share.status !== "active";
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
          Toast.success(t("分享已取消，记录收进历史过期分享"));
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
          // 新的链接出现在「当前分享」里，跟着切回去
          state.scope = "active";
          state.page = 1;
          await load();
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });

    renderPager();
  }

  async function switchScope() {
    state.scope = history() ? "active" : "history";
    state.page = 1;
    await load();
  }

  toggleEl.addEventListener("click", switchScope);
  root.querySelector('[data-act="refresh"]').addEventListener("click", load);
  paintToggle();
  load();
}
