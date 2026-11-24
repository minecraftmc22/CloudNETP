/**
 * 群成员右键菜单（聊天页头像 / 群面板成员列表共用）
 *
 * 权限跟服务端一一对应，前端只是「不给入口」，真正的拦截在 group_service：
 * - 群主：能禁言任何人（含管理员）、能设/取消管理员、能移出任何人
 * - 管理员：只能禁言 / 移出普通成员，不能碰群主和同级管理员
 */
import { api } from "../api.js";
import { t } from "../i18n.js";
import { Dialog } from "./dialog.js";
import { openMenu } from "./menu.js";
import { Toast } from "./toast.js";

/** 常用档位；服务端上限 30 天，这里不放开。写成函数是因为 t() 要按当前语言现算 */
function muteOptions() {
  return [
    { label: t("禁言 10 分钟"), minutes: 10 },
    { label: t("禁言 1 小时"), minutes: 60 },
    { label: t("禁言 1 天"), minutes: 1440 },
    { label: t("禁言 7 天"), minutes: 10080 },
  ];
}

/** 服务端只在「禁言还没过期」时下发 muteUntil，所以有值就是禁言中 */
export function isMuted(member) {
  return Boolean(member?.muteUntil);
}

export function canMuteMember(conv, member) {
  if (!conv || conv.kind !== "group" || !conv.canManage || !member) return false;
  if (member.id === conv.me?.id) return false;
  if (member.id === conv.ownerId) return false;
  // 管理员之间平级：只有群主能禁言管理员
  if (member.isAdmin && !conv.isOwner) return false;
  return true;
}

export function canKickMember(conv, member) {
  if (!conv || conv.kind !== "group" || !conv.canManage || !member) return false;
  if (member.id === conv.me?.id) return false;
  if (member.id === conv.ownerId) return false;
  if (member.isAdmin && !conv.isOwner) return false;
  return true;
}

async function muteMember(conv, member, minutes, onChanged) {
  try {
    await api.post(`/api/chat/conversations/${conv.id}/members/${member.id}/mute`, {
      userId: member.id,
      minutes,
    });
    Toast.success(minutes > 0 ? t("已禁言") : t("已解除禁言"));
    onChanged?.();
  } catch (err) {
    Toast.error(err?.message || t("操作失败"));
  }
}

async function setAdmin(conv, member, on, onChanged) {
  try {
    await api.post(`/api/chat/conversations/${conv.id}/admins`, {
      userId: member.id,
      admin: on,
    });
    Toast.success(on ? t("已设为管理员") : t("已取消管理员"));
    onChanged?.();
  } catch (err) {
    Toast.error(err?.message || t("操作失败"));
  }
}

async function kickMember(conv, member, onChanged) {
  const ok = await Dialog.confirm({
    title: t("移出成员"),
    message: t("「{name}」会收到一条通知，之前的消息仍留在群里。", {
      name: member.alias || member.displayName || "",
    }),
    confirmText: t("移出"),
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/chat/conversations/${conv.id}/members/${member.id}`);
    Toast.success(t("已移出"));
    onChanged?.();
  } catch (err) {
    Toast.error(err?.message || t("操作失败"));
  }
}

async function setNote(conv, member, onChanged) {
  const alias = await Dialog.prompt({
    title: t("设置群内备注"),
    message: t("只对你自己可见，留空则恢复对方的昵称。"),
    label: t("备注"),
    value: member.alias || "",
    placeholder: member.displayName || "",
    confirmText: t("保存"),
  });
  if (alias === null) return;
  try {
    await api.put(`/api/chat/conversations/${conv.id}/members/${member.id}/note`, {
      userId: member.id,
      alias,
    });
    Toast.success(t("备注已更新"));
    onChanged?.();
  } catch (err) {
    Toast.error(err?.message || t("操作失败"));
  }
}

/**
 * 弹出成员菜单。没有任何可操作项时返回 false（调用方可以退回别的菜单）。
 */
export function openMemberMenu({ conv, member, x = 0, y = 0, anchor = null, onChanged } = {}) {
  if (!conv || !member) return false;
  const isMe = member.id === conv.me?.id;
  const items = [];

  if (isMe) {
    // 自己：只能改备注以外的东西，菜单里留个「我」的说明就够了
    return false;
  }

  const muted = isMuted(member);
  const canMute = canMuteMember(conv, member);
  if (canMute) {
    if (muted) {
      items.push({
        label: t("解除禁言"),
        icon: "i-bell",
        onClick: () => void muteMember(conv, member, 0, onChanged),
      });
    } else {
      // 禁言档位直接铺在菜单里：右键 → 点一下就走完，不用再弹一层
      muteOptions().forEach((opt) => {
        items.push({
          label: opt.label,
          icon: "i-mute",
          onClick: () => void muteMember(conv, member, opt.minutes, onChanged),
        });
      });
    }
  }

  items.push({
    label: t("备注"),
    icon: "i-text",
    onClick: () => void setNote(conv, member, onChanged),
  });

  if (conv.isOwner && member.id !== conv.ownerId) {
    items.push({
      label: member.isAdmin ? t("取消管理员") : t("设为管理员"),
      icon: "i-users",
      onClick: () => void setAdmin(conv, member, !member.isAdmin, onChanged),
    });
  }

  const canKick = canKickMember(conv, member);
  if (canKick) {
    items.push({ sep: true });
    items.push({
      label: t("移出群聊"),
      icon: "i-trash",
      danger: true,
      onClick: () => void kickMember(conv, member, onChanged),
    });
  }

  if (!items.some((item) => item && !item.sep)) return false;

  openMenu({
    x,
    y,
    anchor,
    items,
    header: {
      name: member.alias || member.displayName || "",
      meta: member.username ? `@${member.username}` : "",
    },
  });
  return true;
}
