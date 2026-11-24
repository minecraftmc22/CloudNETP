/**
 * 群聊信息面板（四个页签：资料 / 成员 / 文件 / 分享）
 *
 * 只负责「群的设置」这一屏，不碰消息流：
 * - 资料：群名、简介、群头像、公开/私密（只有群主能改公开性）
 * - 成员：群内备注、设/取消管理员、移出成员、邀请
 * - 文件：群共享文件的上传/下载/删除与配额
 * - 分享：群号、邀请链接、二维码
 *
 * 改完通知外层刷新会话列表（onChanged），避免聊天页标题还是旧的。
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { avatarMarkup } from "./avatar.js";
import { copyText } from "./clipboard.js";
import { Dialog } from "./dialog.js";
import { Toast } from "./toast.js";

const TABS = [
  { id: "info", label: "资料", icon: "i-info" },
  { id: "members", label: "成员", icon: "i-users" },
  { id: "files", label: "群文件", icon: "i-folder" },
  { id: "share", label: "分享", icon: "i-share" },
];

export async function openGroupPanel(convId, { onChanged, onInvite } = {}) {
  let conv = null;
  let files = null;
  let share = null;
  let tab = "info";
  let busy = false;

  const backdrop = document.createElement("div");
  backdrop.className = "picker-backdrop";
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event) => {
    if (event.key === "Escape") close();
  };

  async function loadConv() {
    const data = await api.get(`/api/chat/conversations/${convId}`);
    conv = data.conversation;
  }

  async function loadFiles() {
    try {
      files = await api.get(`/api/chat/conversations/${convId}/files`);
    } catch {
      files = { items: [], used: 0, quota: 0, fileMax: 0 };
    }
  }

  async function loadShare() {
    try {
      share = await api.get(`/api/chat/conversations/${convId}/share`);
    } catch {
      share = null;
    }
  }

  function infoPane() {
    const canManage = conv.canManage;
    return H`
      <div class="group-info">
        <div class="group-info__head">
          <button class="group-avatar group-avatar--edit" type="button" data-act="pick-avatar"
                  ${canManage ? "" : "disabled"} title="${t("换群头像")}">
            ${
              conv.avatarUrl
                ? H`<img src="${escapeHtml(conv.avatarUrl)}" alt="">`
                : H`<svg class="icon"><use href="#i-users"/></svg>`
            }
            ${canManage ? H`<span class="group-avatar__hint">${t("换头像")}</span>` : ""}
          </button>
          <div class="group-info__title">
            <div class="group-info__name">${escapeHtml(conv.title)}</div>
            <div class="muted">${t("群号 {no}", { no: conv.groupNo || "—" })} · ${t("{n} 人", {
              n: conv.memberCount,
            })} · ${conv.isPublic ? t("公开群") : t("私密群")}</div>
          </div>
        </div>
        ${
          canManage
            ? H`
          <label class="field">
            <span class="field__label">${t("群名称")}</span>
            <span class="textbox"><input type="text" maxlength="60" data-f="title" value="${escapeHtml(conv.title)}"></span>
          </label>
          <label class="field">
            <span class="field__label">${t("群简介")}</span>
            <span class="textbox"><textarea rows="3" maxlength="200" data-f="description"
                  placeholder="${t("介绍一下这个群…")}">${escapeHtml(conv.description || "")}</textarea></span>
          </label>
          <label class="group-switch ${conv.isOwner ? "" : "is-disabled"}">
            <input type="checkbox" data-f="isPublic" ${conv.isPublic ? "checked" : ""} ${
              conv.isOwner ? "" : "disabled"
            }>
            <span>
              <span class="group-switch__title">${t("公开群")}</span>
              <span class="group-switch__desc">${t("公开后别人能搜到群号并直接加入；私密群只能用邀请链接进。")}</span>
            </span>
          </label>
          <div class="group-info__ops">
            <button class="btn btn--accent" type="button" data-act="save-info">${t("保存资料")}</button>
          </div>`
            : H`<p class="muted">${conv.description || t("这个群还没有简介")}</p>`
        }
      </div>`;
  }

  function memberRow(m) {
    const isOwner = m.id === conv.ownerId;
    const isMe = m.id === conv.me?.id;
    const canKick =
      !isMe && !isOwner && (conv.isOwner || (conv.myRole === "admin" && !m.isAdmin));
    const canAdmin = conv.isOwner && !isOwner;
    return H`
      <div class="group-member" data-uid="${m.id}">
        <span class="group-member__avatar">${avatarMarkup({ ...m, displayName: m.alias || m.displayName })}</span>
        <span class="group-member__main">
          <span class="group-member__name">
            ${escapeHtml(m.alias || m.displayName)}
            ${m.alias ? H`<span class="muted">(${escapeHtml(m.displayName)})</span>` : ""}
            ${isOwner ? H`<span class="chip chip--accent">${t("群主")}</span>` : ""}
            ${m.isAdmin ? H`<span class="chip">${t("管理员")}</span>` : ""}
            ${isMe ? H`<span class="chip">${t("我")}</span>` : ""}
          </span>
          <span class="group-member__meta">@${escapeHtml(m.username || "")}</span>
        </span>
        <span class="group-member__ops">
          ${!isMe ? H`<button class="btn btn--subtle" type="button" data-act="note" data-uid="${m.id}">${t("备注")}</button>` : ""}
          ${
            canAdmin
              ? H`<button class="btn btn--subtle" type="button" data-act="toggle-admin" data-uid="${m.id}"
                     data-on="${m.isAdmin ? "1" : "0"}">${m.isAdmin ? t("取消管理员") : t("设为管理员")}</button>`
              : ""
          }
          ${
            canKick
              ? H`<button class="btn btn--subtle btn--danger" type="button" data-act="kick" data-uid="${m.id}">${t("移出")}</button>`
              : ""
          }
        </span>
      </div>`;
  }

  function membersPane() {
    return H`
      <div class="group-members">
        <div class="group-members__head">
          <span class="muted">${t("成员 {n} 人", { n: conv.memberCount })}</span>
          <span class="grow"></span>
          ${
            conv.canManage
              ? H`<button class="btn btn--subtle" type="button" data-act="invite">${t("邀请好友")}</button>`
              : ""
          }
        </div>
        <div class="group-members__list">${(conv.members || []).map(memberRow).join("")}</div>
        <p class="muted group-members__hint">${t("备注只对你自己可见：在群里看到的是你起的名字，别人看到的还是他自己的。")}</p>
      </div>`;
  }

  function filesPane() {
    if (!files) {
      return H`<div class="empty"><div class="empty__title">${t("正在加载…")}</div></div>`;
    }
    const pct = files.quota ? Math.min(100, Math.round((files.used / files.quota) * 100)) : 0;
    return H`
      <div class="group-files">
        <div class="group-files__bar">
          <div class="group-files__meter"><span style="width:${pct}%"></span></div>
          <div class="muted">${formatBytes(files.used)} / ${formatBytes(files.quota)}</div>
          <span class="grow"></span>
          <button class="btn btn--accent" type="button" data-act="upload-file">${t("上传文件")}</button>
        </div>
        ${
          files.items?.length
            ? H`<div class="group-files__list">
                ${files.items
                  .map(
                    (f) => H`
                  <div class="group-file" data-fid="${f.id}">
                    <svg class="icon"><use href="#i-file"/></svg>
                    <span class="group-file__main">
                      <span class="group-file__name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
                      <span class="group-file__meta">${formatBytes(f.size)} · ${escapeHtml(
                        f.uploaderName
                      )} · ${formatTime(f.createdAt)}</span>
                    </span>
                    <button class="btn btn--subtle" type="button" data-act="download-file" data-fid="${f.id}">${t("下载")}</button>
                    ${
                      f.canDelete
                        ? H`<button class="btn btn--subtle btn--danger" type="button" data-act="delete-file"
                               data-fid="${f.id}" data-name="${escapeHtml(f.name)}">${t("删除")}</button>`
                        : ""
                    }
                  </div>`
                  )
                  .join("")}
              </div>`
            : H`<div class="empty">
                <div class="empty__art"><svg class="icon"><use href="#i-folder"/></svg></div>
                <div class="empty__title">${t("群里还没有文件")}</div>
                <div class="empty__desc">${t("上传的文件群里所有人都能下载，不占个人网盘空间。")}</div>
              </div>`
        }
      </div>`;
  }

  function sharePane() {
    if (!share) {
      return H`<div class="empty"><div class="empty__title">${t("正在加载…")}</div></div>`;
    }
    const link = share.url || "";
    return H`
      <div class="group-share">
        <div class="group-share__row">
          <span class="group-share__label">${t("群号")}</span>
          <span class="group-share__value">${escapeHtml(share.groupNo || "")}</span>
          <button class="btn btn--subtle" type="button" data-act="copy-no">${t("复制")}</button>
        </div>
        <div class="group-share__row">
          <span class="group-share__label">${t("邀请链接")}</span>
          <span class="textbox group-share__link"><input type="text" readonly value="${escapeHtml(link)}"></span>
          <button class="btn btn--subtle" type="button" data-act="copy-link">${t("复制")}</button>
        </div>
        ${
          share.qr
            ? H`<div class="group-share__qr">
                 <img src="${share.qr}" alt="${t("群二维码")}" width="180" height="180">
                 <div class="muted">${t("扫码打开邀请链接")}</div>
               </div>`
            : ""
        }
        <div class="group-share__ops">
          <button class="btn btn--subtle" type="button" data-act="reset-share">${t("重置邀请链接")}</button>
          <span class="grow"></span>
          <button class="btn btn--subtle btn--danger" type="button" data-act="leave">${t("退出群聊")}</button>
        </div>
        <p class="muted">${t("公开群所有人都能通过群号搜到；私密群只能通过这条邀请链接加入。重置后旧链接立刻失效。")}</p>
      </div>`;
  }

  function paint() {
    if (!conv) {
      backdrop.innerHTML = H`<div class="picker picker--group"><div class="picker__body">
          <div class="empty"><div class="empty__title">${t("正在加载…")}</div></div>
        </div></div>`;
      return;
    }
    const body =
      tab === "info" ? infoPane() : tab === "members" ? membersPane() : tab === "files" ? filesPane() : sharePane();
    backdrop.innerHTML = H`
      <div class="picker picker--group" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-users"/></svg>
          <span>${escapeHtml(conv.title)}</span>
        </div>
        <div class="segmented group-tabs">
          ${TABS.map(
            (item) => H`<button class="segmented__btn ${tab === item.id ? "is-active" : ""}" type="button"
                      data-gtab="${item.id}">
                      <svg class="icon icon--s"><use href="#${item.icon}"/></svg>${t(item.label)}
                    </button>`
          ).join("")}
        </div>
        <div class="picker__body">${body}</div>
        <div class="picker__foot">
          <span class="muted">${conv.isPublic ? t("公开群") : t("私密群")} · ${t("{n} 人", {
            n: conv.memberCount,
          })}</span>
          <span style="flex:1"></span>
          <button class="btn btn--accent" type="button" data-act="close">${t("关闭")}</button>
        </div>
        <input type="file" accept="image/*" data-avatar-input hidden>
        <input type="file" data-file-input hidden multiple>
      </div>`;
  }

  async function reloadAll() {
    await loadConv();
    if (tab === "files") await loadFiles();
    if (tab === "share") await loadShare();
    paint();
  }

  async function run(fn, okText) {
    if (busy) return;
    busy = true;
    try {
      await fn();
      if (okText) Toast.success(okText);
      await reloadAll();
      onChanged?.();
    } catch (err) {
      Toast.error(err?.message || t("操作失败"));
    } finally {
      busy = false;
    }
  }

  async function setNote(uid) {
    const member = (conv.members || []).find((m) => m.id === uid);
    const alias = await Dialog.prompt({
      title: t("设置群内备注"),
      message: t("只对你自己可见，留空则恢复对方的昵称。"),
      label: t("备注"),
      value: member?.alias || "",
      placeholder: member?.displayName || "",
      confirmText: t("保存"),
    });
    if (alias === null) return;
    await run(
      () => api.put(`/api/chat/conversations/${convId}/members/${uid}/note`, { userId: uid, alias }),
      t("备注已更新")
    );
  }

  async function uploadFiles(list) {
    if (!list?.length) return;
    let done = 0;
    let failed = 0;
    for (const file of list) {
      try {
        await api.raw(
          `/api/chat/conversations/${convId}/files?name=${encodeURIComponent(file.name)}`,
          file,
          "PUT"
        );
        done += 1;
      } catch (err) {
        failed += 1;
        Toast.error(err?.message || t("「{name}」上传失败", { name: file.name }));
      }
    }
    if (done) Toast.success(t("已上传 {n} 个文件", { n: done }));
    if (done || failed) {
      await reloadAll();
      onChanged?.();
    }
  }

  async function uploadAvatar(file) {
    if (!file) return;
    await run(async () => {
      await api.raw(
        `/api/chat/conversations/${convId}/avatar`,
        new Blob([await file.arrayBuffer()], { type: file.type || "image/png" }),
        "PUT"
      );
    }, t("群头像已更新"));
  }

  paint();
  document.body.appendChild(backdrop);
  document.addEventListener("keydown", onKey);
  await reloadAll();

  backdrop.addEventListener("click", async (event) => {
    if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
      close();
      return;
    }
    const tabBtn = event.target.closest("[data-gtab]");
    if (tabBtn) {
      tab = tabBtn.dataset.gtab;
      if (tab === "files" && !files) await loadFiles();
      if (tab === "share" && !share) await loadShare();
      paint();
      return;
    }
    const btn = event.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "close") {
      close();
    } else if (act === "save-info") {
      const title = backdrop.querySelector('[data-f="title"]').value.trim();
      const description = backdrop.querySelector('[data-f="description"]').value.trim();
      const isPublic = backdrop.querySelector('[data-f="isPublic"]').checked;
      await run(
        () => api.patch(`/api/chat/conversations/${convId}`, { title, description, isPublic }),
        t("群资料已保存")
      );
    } else if (act === "pick-avatar") {
      backdrop.querySelector("[data-avatar-input]").click();
    } else if (act === "invite") {
      onInvite?.();
    } else if (act === "note") {
      await setNote(Number(btn.dataset.uid));
    } else if (act === "toggle-admin") {
      const uid = Number(btn.dataset.uid);
      const on = btn.dataset.on !== "1";
      await run(
        () => api.post(`/api/chat/conversations/${convId}/admins`, { userId: uid, admin: on }),
        on ? t("已设为管理员") : t("已取消管理员")
      );
    } else if (act === "kick") {
      const uid = Number(btn.dataset.uid);
      const ok = await Dialog.confirm({
        title: t("移出成员"),
        message: t("他会收到一条通知，之前的消息仍留在群里。"),
        confirmText: t("移出"),
        danger: true,
      });
      if (ok) {
        await run(
          () => api.del(`/api/chat/conversations/${convId}/members/${uid}`),
          t("已移出")
        );
      }
    } else if (act === "upload-file") {
      backdrop.querySelector("[data-file-input]").click();
    } else if (act === "download-file") {
      const a = document.createElement("a");
      a.href = `/api/chat/group-files/${btn.dataset.fid}/download`;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else if (act === "delete-file") {
      const ok = await Dialog.confirm({
        title: t("删除群文件"),
        message: t("「{name}」会从群里彻底删掉，所有人都不能再下载。", { name: btn.dataset.name || "" }),
        confirmText: t("删除"),
        danger: true,
      });
      if (ok) {
        await run(() => api.del(`/api/chat/group-files/${btn.dataset.fid}`), t("已删除"));
      }
    } else if (act === "copy-no") {
      await copyText(share?.groupNo || "", t("群号已复制"));
    } else if (act === "copy-link") {
      await copyText(share?.url || "", t("链接已复制"));
    } else if (act === "reset-share") {
      const ok = await Dialog.confirm({
        title: t("重置邀请链接"),
        message: t("旧链接会立刻失效，之前发出去的邀请链接都点不开了；群号不受影响。"),
        confirmText: t("重置"),
        danger: true,
      });
      if (ok) {
        await run(() => api.post(`/api/chat/conversations/${convId}/share`, {}), t("邀请链接已重置"));
      }
    } else if (act === "leave") {
      const ok = await Dialog.confirm({
        title: t("退出群聊"),
        message: t("退出后这条会话会从列表里消失，想回来需要重新被邀请。"),
        confirmText: t("退出"),
        danger: true,
      });
      if (ok) {
        try {
          await api.post(`/api/chat/conversations/${convId}/leave`);
          Toast.success(t("已退出群聊"));
          close();
          onChanged?.({ left: true });
        } catch (err) {
          Toast.error(err?.message || t("操作失败"));
        }
      }
    }
  });

  backdrop.addEventListener("change", (event) => {
    if (event.target.matches("[data-avatar-input]")) {
      void uploadAvatar(event.target.files?.[0]);
    } else if (event.target.matches("[data-file-input]")) {
      void uploadFiles([...(event.target.files || [])]);
      event.target.value = "";
    }
  });
}
