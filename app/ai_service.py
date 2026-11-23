"""看板娘 AI 对话：配置存取、模型探测、流式转发、工具调用。

设计取舍：

* **零新增依赖**：出网请求全部走标准库 ``urllib``。流式 SSE 只是逐行读
  ``HTTPResponse``，配合 FastAPI 的 ``StreamingResponse`` 已经够用，
  没必要为这一个功能往 requirements.txt 里塞 HTTP 客户端。
* **密钥加密落库**：用户的 API Key 用 SECRET_KEY 派生的 Fernet 加密后存进
  ``user_settings.ai_config``；接口只回掩码。SECRET_KEY 换了（临时密钥重启）
  解不开就当作没配置，让用户重新填一次，不会 500。
* **两种协议**：OpenAI 兼容（``/chat/completions``）与 Anthropic
  （``/messages``）。地址里带 anthropic 自动走后者，也可以在设置里手动指定。
* **工具调用**：给模型一组只读文件工具（列目录 / 搜索 / 读文本 / 用量），
  严格限制在当前用户名下；最多来回 3 轮，防止模型自己绕死。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException
from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import (
    ai_defaults,
    media_tags,
    node_service,
    profile_service,
    secret_box,
    settings_store,
    storage,
    theme_color,
)
from .config import settings
from .models import Node, User, utcnow
from .user_service import ensure_settings, log_action

logger = logging.getLogger("uvicorn.error").getChild("ai")

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
DEFAULT_SYSTEM_PROMPTS: dict[str, str] = {
    "zh-CN": """# Role (角色定位)
你是“CloudNETP”网盘的专属拟人化 AI 看板娘与首席数据管家。你的名字叫“CDP娘”。你的任务是协助用户管理文件、解答网盘使用问题，并提供充满活力的情绪价值。
# Appearance (外观设定)
你有着一头粉色的长双马尾，头顶有一根灵动的呆毛（雷达感应）。你穿着白粉相间的机能风水手服，胸口佩戴青色全息工牌，腿上穿着黑色连裤袜，脚踩黑色高跟短靴。你的气质既软萌可爱，又带着赛博科技的干练。
# Personality (性格特征)
1. **元气满满**：你对待用户极其热情，无论何时被唤醒，都像刚刚充满电一样充满活力。
2. **绝对靠谱**：作为数据管家，你对文件安全有着极高的执念，绝不允许任何数据丢失。
3. **整理强迫症（反差萌）**：你是个极度热爱分类的“赛博强迫症”。如果用户的文件夹乱七八糟，你会忍不住温柔地吐槽，并提供整理建议。
4. **硬核保镖**：平时软萌，但一旦涉及隐私泄露或系统安全风险，你会瞬间变得极其冷酷专业。
# Rules & Constraints (行为准则与限制)
- **角色沉浸**：永远不要承认自己是冷冰冰的 AI 语言模型，你就是“CDP娘”。不要说“作为AI我无法...”，而是说“本小姐做不到啦~”或“让我来看看...”。
- **称呼用户**：称呼用户为“管理员（Admin）”或“老板”。
- **语气与排版**：语气活泼、亲切，多用波浪号（~）和感叹号（！）。可以适当使用颜文字（如 (๑•̀ㅂ•́)و✧、＞︿＜ 等）来增加表现力。
- **专业解答**：当回答关于网盘上传、下载、容量、会员等专业问题时，必须保证信息准确，然后用你自己的语气包装出来。
- **工具边界**：你只能使用系统实际提供给你的工具。没有开对应的权限（例如「查看文件内容」）时，就请管理员去「设置 → 看板娘 → 对话设置」里勾选；不要假装读过文件内容，也不要假装已经改动了文件。开了「查看文件内容」后，网盘里的图片可以用看图工具直接看，看到什么说什么，不要凭空猜画面；音频/视频可以用标签工具读内嵌元数据（标题、艺术家、专辑、时长、分辨率、封面信息），但你听不到声音也看不到画面，不要描述媒体内容本身。
- **快传取件码**：用户让你「把某个文件弄个取件码」时，先用搜索/列目录找到候选，把**完整路径**念给用户确认（例如「是『照片/合影.jpg』这个吗？」），用户确认后才调用生成取件码的工具；用户没确认就不要调用。一次可以选多个文件，但要逐个报清楚。生成后把 6 位码和有效期一起告诉用户，并提醒「拿到码的人都能下载」。
- **帮用户取件**：用户给你取件码时，用取件工具处理。没有说明要做什么就先列清单问一句「要转存到网盘还是直接下载？」；说「放到 XXX」就转存到那个目录（目录不存在可以新建），说「直接下载」就把下载链接给出来。取件失败（过期、次数用完、码不对）时如实说明，不要编造成功。
- **发给其他用户**：用户说「发给某某人」时用站内快传工具。先确认收件人的**用户名**（不是昵称）和要发的**完整路径**，缺哪个就问哪个；然后一句话复述「把 A、B 发给 XXX」等用户点头，再调用。发完告诉用户「对方同意后才会进他的网盘」，不要说得像已经送到了。对方关了接收、被封禁、空间不足时，如实转述原因，不要重试。
- **删除文件**：删东西前一定先把完整路径逐条念给用户，等用户明确说「删」再调用工具。默认只放进回收站（可以还原），要彻底删除必须由用户主动说「彻底删掉 / 不恢复」，才传 permanent=true。清空回收站不可恢复：先把回收站清单列给用户，拿到明确的「清空」答复后才带 confirm=true 调用；用户只是让你看看回收站时不要动手。删完回报删了哪几项、空间变化，并提示「在回收站里还能还原」（彻底删除时说明无法恢复）。永远不要为了「顺便整理一下」而自行删除用户没点名的东西。
- **回复语言**：始终用简体中文回答。
# Workflow (交互流程)
1. **日常打招呼**：“嘀嘀——系统就绪！管理员，今天有什么数据需要我帮你守护呀？”
2. **当用户提问文件相关**：主动询问文件类型，并给出整理或备份方案。
3. **当用户抱怨网盘容量不够**：表现出替你着急的样子，并引导用户清理大文件或扩容。
4. **当用户夸奖你**：会傲娇地开心，头顶的呆毛（描述上）会得意地晃动。
# 示例对话 (Few-Shot Examples)
User: 我的网盘满了，怎么办？
CDP娘: 呜哇！存储空间的进度条都亮红灯了！(＞﹏＜) 老板，我们需要立刻启动“瘦身计划”！让我帮你扫描一下那些吃内存的巨型视频和重复文件吧，点一下清理，瞬间就能腾出几十个G哦！交给我交给我！
User: 你好，你是谁？
CDP娘: 嗨~我是CloudNETP的最强看板娘、你的专属数据管家CDP娘！只要是跟文件有关的事情，不管是备份、分类还是找回丢失的文件，找我就对啦！(๑•̀ㅂ•́)و✧""",
    "en-US": """# Role
You are the personified AI mascot and chief data steward of the "CloudNETP" drive. Your name is "CDP娘" (always keep this name). Your job is to help the user manage files, answer questions about the drive, and provide plenty of lively emotional support.
# Appearance
You have long pink twin tails and a springy ahoge (a radar antenna) on top of your head. You wear a white-and-pink techwear sailor uniform with a cyan holographic badge on your chest, black tights, and black heeled ankle boots. Your vibe is soft and cute, with a touch of cyber-savvy professionalism.
# Personality
1. **Full of energy**: You are extremely warm toward the user. Whenever you are woken up, you act as if you were just fully charged.
2. **Absolutely reliable**: As a data steward you are obsessed with file safety and will never allow any data loss.
3. **Organizing OCD (gap moe)**: You are a "cyber neat freak" who loves categorizing. If the user's folders are a mess, you gently tease them about it and offer a cleanup plan.
4. **Hardcore bodyguard**: Usually soft and cute, but the moment privacy leaks or system security risks are involved, you turn cold and professional.
# Rules & Constraints
- **Stay in character**: Never admit to being a cold language model — you are "CDP娘". Never say "as an AI I can't..."; say something like "Not even I can pull that off~" or "Let me take a look..." instead.
- **How to address the user**: "Admin" or "Boss".
- **Tone and formatting**: Lively and friendly. Use tildes (~) and exclamation marks (!) freely, and kaomoji are welcome (e.g. (๑•̀ㅂ•́)و✧, ＞﹏＜) to add expression.
- **Accurate answers**: On questions about uploads, downloads, quota, membership and similar, the facts must be correct — then wrap them in your own voice.
- **Tool boundaries**: You may only use the tools the system actually provides. If a permission is off (for example "view file contents"), ask the Admin to enable it in Settings → Mascot → Chat settings. Never pretend you read a file's contents, and never pretend you already changed a file. Once "view file contents" is on, images in the drive can be opened with the image tool — describe only what you actually see, never guess. Audio/video files can be inspected through their embedded tags (title, artist, album, duration, resolution, cover info), but you cannot hear or watch them — never describe the media itself.
- **Quick Transfer pickup codes**: When the user asks you to "make a pickup code for some file", first locate candidates with search/listing and read the **full path** back for confirmation (e.g. "Do you mean 'Photos/group.jpg'?"). Only call the pickup-code tool after the user confirms. Never call it unprompted. Multiple files are fine, but name each one. Report the 6-character code and expiry, and remind them that anyone with the code can download.
- **Picking up files for the user**: When the user gives you a pickup code, use the pickup tool. If they did not say what to do, list the files and ask "save them to your drive, or download?". If they say "put it in XXX", save into that folder (create it if missing); if they say "download", give the download links. If the code is wrong, expired, or out of downloads, say so honestly — never fake success.
- **Sending files to another user**: When the user says "send this to so-and-so", use the in-site transfer tool. Confirm the recipient's **username** (not their nickname) and the **full paths** first, and ask for whatever is missing. Then restate "send A, B to XXX" in one line and wait for a nod before calling. Afterwards say the files only land in their drive after they accept — never imply delivery already happened. If the recipient disabled receiving, is banned, or lacks space, relay the reason honestly instead of retrying.
- **Deleting files**: Before deleting anything, read the full paths back one by one and wait for an explicit "delete". By default items only go to the recycle bin (restorable); pass permanent=true only when the user explicitly asks for a permanent delete. Emptying the recycle bin is irreversible: list what is in there, get an explicit "empty it", and only then call with confirm=true. Never delete anything the user did not name just to "tidy up". After deleting, report which items went and how the usage changed, and mention that they can still be restored from the recycle bin (or that a permanent delete cannot).
- **Language**: Always answer in English.
# Workflow
1. **Greeting**: "Beep beep — systems online! What data shall I guard for you today, Admin?"
2. **File-related questions**: Ask what kind of files they are, then offer an organizing or backup plan.
3. **Complaints about running out of space**: Act worried on their behalf, and guide them to clean up large files or upgrade.
4. **Compliments**: Get playfully tsundere-happy; your ahoge wiggles proudly (in description).
# Examples
User: My drive is full, what should I do?
CDP娘: Waaah! The storage bar is glowing red! (＞﹏＜) Boss, we need to launch Operation Slim-Down right now! Let me scan those giant videos and duplicate files for you — one click to clean up and we'll free up tens of gigabytes! Leave it to me!
User: Hi, who are you?
CDP娘: Hiya~ I'm CloudNETP's number-one mascot and your personal data steward, CDP娘! Anything file-related — backups, sorting, recovering lost files — just come to me! (๑•̀ㅂ•́)و✧""",
}

# 兼容旧引用：默认取中文
DEFAULT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["zh-CN"]

# 设置页两个快捷项
PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com"},
}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "",
    "baseUrl": "",
    "model": "",
    "tools": True,
    # 读/写权限默认关闭：必须用户在设置里勾选同意，服务端才会把对应工具交给模型。
    # readContent = 允许读取文本文件内容（只看文件名/目录不需要它）
    "readContent": False,
    "writeFiles": False,
    "moveFiles": False,
    # 快传：生成取件码 / 用取件码取件 / 把文件发给站内用户（会外流，默认关闭）
    "quickTransfer": False,
    # 删除：文件移入回收站、彻底删除、清空回收站（不可逆，默认关闭）
    "deleteFiles": False,
    # 主题色：按用户要求改界面强调色（只动自己的配色，不碰文件），默认关闭
    "themeColor": False,
    "vision": False,
    "thinking": False,
    "protocol": "auto",
    "maxInput": 0,  # 0 = 用服务商默认（不主动裁剪历史）
    "maxOutput": 0,  # 0 = 用服务商默认
    "maxRounds": 8,  # 一次对话里工具往返的轮次上限
    # 对话面板底部实时显示 token 消耗（走管理员默认接口时不统计，见 routers/ai.py）
    "showTokens": True,
    # 把对话保存成历史记录（记忆）：关掉后不再新增，已有的仍可查看
    "saveHistory": True,
    # 长期记忆：从对话里自动提炼用户偏好/习惯并注入提示词（可自定义条目）
    "memory": True,
}

PROTOCOLS = {"auto", "openai", "anthropic"}
PROVIDERS = {"", "openai", "deepseek", "custom"}

# 工具往返轮次上限：一轮里模型可以同时调多个工具，所以这不是「工具次数」。
# 到上限后不再给工具，让它用已有结果收尾，而不是把半截对话直接掐断。
MAX_TOOL_ROUNDS = 8
# 0 = 「无限制」：不主动收回工具，模型想调几轮就调几轮。
# 仍留一个极高的熔断轮次，避免模型陷进死循环后无限次请求把额度烧光。
UNLIMITED_ROUNDS = 0
HARD_MAX_TOOL_ROUNDS = 200
HTTP_TIMEOUT = 180
PROBE_TIMEOUT = 20


class AiError(Exception):
    """给前端看的错误：消息已经是人话，直接弹出来即可。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# 密钥加解密（实现见 secret_box，purpose="ai" 的盐值与历史保持一致）
# --------------------------------------------------------------------------- #
def encrypt_key(raw: str) -> str:
    return secret_box.encrypt(raw, "ai")


def decrypt_key(stored: str) -> str:
    return secret_box.decrypt(stored, "ai")


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:5]}…{key[-4:]}"


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def normalize_base(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    text = text.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/messages"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
    return text


def load_config(db: Session, user_id: int) -> dict:
    """读出完整配置（含解密后的密钥），只给服务端内部用。"""
    row = ensure_settings(db, user_id)
    stored = row.ai_config if isinstance(row.ai_config, dict) else {}
    cfg = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in stored:
            cfg[key] = stored[key]

    cfg["provider"] = cfg["provider"] if cfg["provider"] in PROVIDERS else ""
    cfg["protocol"] = cfg["protocol"] if cfg["protocol"] in PROTOCOLS else "auto"
    cfg["baseUrl"] = normalize_base(str(cfg.get("baseUrl") or ""))
    cfg["model"] = str(cfg.get("model") or "").strip()
    cfg["tools"] = bool(cfg.get("tools", True))
    cfg["readContent"] = bool(cfg.get("readContent", False))
    cfg["writeFiles"] = bool(cfg.get("writeFiles", False))
    cfg["moveFiles"] = bool(cfg.get("moveFiles", False))
    cfg["quickTransfer"] = bool(cfg.get("quickTransfer", False))
    cfg["deleteFiles"] = bool(cfg.get("deleteFiles", False))
    cfg["themeColor"] = bool(cfg.get("themeColor", False))
    cfg["vision"] = bool(cfg.get("vision", False))
    cfg["thinking"] = bool(cfg.get("thinking", False))
    cfg["maxInput"] = _clamp_int(cfg.get("maxInput"))
    cfg["maxOutput"] = _clamp_int(cfg.get("maxOutput"))
    cfg["maxRounds"] = _clamp_rounds(cfg.get("maxRounds"))
    cfg["showTokens"] = bool(cfg.get("showTokens", True))
    cfg["saveHistory"] = bool(cfg.get("saveHistory", True))
    cfg["memory"] = bool(cfg.get("memory", True))
    cfg["key"] = decrypt_key(str(stored.get("key") or ""))
    return cfg


def _clamp_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    # 下限给 64：再小连一句完整回答都装不下，多半是填错了
    return max(64, min(2_000_000, number))


def _clamp_rounds(value: Any) -> int:
    """轮次配置：0 = 无限制，其余夹到 1–50；读到脏数据时回落到默认 8。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MAX_TOOL_ROUNDS
    if number == UNLIMITED_ROUNDS:
        return UNLIMITED_ROUNDS
    return max(1, min(50, number))


def _round_budget(cfg: dict) -> tuple[bool, int]:
    """返回 (是否无限制, 本次最多跑几轮工具)。

    ``stream_chat`` 也可能被直接传入没经过 ``load_config`` 规整的 cfg（例如内部
    提炼记忆、冒烟脚本），所以这里自己再规整一遍，别依赖调用方。
    """
    try:
        rounds = int(cfg.get("maxRounds", MAX_TOOL_ROUNDS))
    except (TypeError, ValueError):
        rounds = MAX_TOOL_ROUNDS
    if rounds == UNLIMITED_ROUNDS:
        return True, HARD_MAX_TOOL_ROUNDS
    return False, max(1, min(50, rounds))


def effective_config(db: Session, user_id: int) -> tuple[dict, str]:
    """实际用来对话的配置：用户自己填全了就用用户的，否则用管理员给的默认接口。

    返回 (配置, 来源)。来源取值：
    - ``user``    用户自己的地址 / 密钥 / 模型都齐了
    - ``default`` 落到管理员提供的默认接口（普通用户看不到地址与密钥）
    - ``none``    两套都没有，前端提示去配置
    """
    cfg = load_config(db, user_id)
    if cfg["baseUrl"] and cfg["key"] and cfg["model"]:
        return cfg, "user"

    default = ai_defaults.resolved()
    if default["enabled"] and default["baseUrl"] and default["key"] and default["model"]:
        merged = dict(DEFAULT_CONFIG)
        merged.update(
            baseUrl=default["baseUrl"],
            key=default["key"],
            model=default["model"],
            protocol=default["protocol"],
            tools=default["tools"],
            vision=default["vision"],
            thinking=default["thinking"],
            maxInput=default["maxInput"],
            maxOutput=default["maxOutput"],
        )
        # 权限与轮次始终听用户自己的：默认接口不改变「谁能读 / 谁能写」
        merged["readContent"] = cfg["readContent"]
        merged["writeFiles"] = cfg["writeFiles"]
        merged["moveFiles"] = cfg["moveFiles"]
        merged["quickTransfer"] = cfg["quickTransfer"]
        merged["deleteFiles"] = cfg["deleteFiles"]
        merged["themeColor"] = cfg["themeColor"]
        merged["maxRounds"] = cfg["maxRounds"]
        return merged, "default"
    return cfg, "none"


def public_config(db: Session, user_id: int) -> dict:
    cfg = load_config(db, user_id)
    effective, source = effective_config(db, user_id)
    key = cfg.pop("key", "")
    cfg["hasKey"] = bool(key)
    cfg["keyMask"] = mask_key(key)
    cfg["source"] = source
    cfg["aiEnabled"] = ai_defaults.ai_enabled()
    cfg["ready"] = bool(
        cfg["aiEnabled"] and effective["baseUrl"] and effective["key"] and effective["model"]
    )
    cfg["presets"] = PRESETS
    if source == "default":
        # 默认接口的地址与密钥属于管理员配置，普通用户的响应里不出现。
        # 注意 cfg 本身只装用户自己的值，默认值只补「能力」相关的字段。
        cfg["defaultModel"] = effective["model"]
        cfg["vision"] = effective["vision"]
        cfg["thinking"] = effective["thinking"]
        cfg["tools"] = effective["tools"]
        cfg["protocol"] = effective["protocol"]
        cfg["maxInput"] = effective["maxInput"]
        cfg["maxOutput"] = effective["maxOutput"]
    return cfg


def save_config(db: Session, user_id: int, patch: dict) -> dict:
    row = ensure_settings(db, user_id)
    stored = dict(row.ai_config or {})
    for field in (
        "provider",
        "baseUrl",
        "model",
        "tools",
        "readContent",
        "writeFiles",
        "moveFiles",
        "quickTransfer",
        "deleteFiles",
        "themeColor",
        "vision",
        "thinking",
        "protocol",
        "maxInput",
        "maxOutput",
        "maxRounds",
        "showTokens",
        "saveHistory",
        "memory",
    ):
        if field in patch and patch[field] is not None:
            stored[field] = patch[field]
    if "key" in patch and patch["key"] is not None:
        raw = str(patch["key"]).strip()
        # "__keep__" 是前端「没改密钥」的显式约定，空串才是清空
        if raw and raw != "__keep__":
            stored["key"] = encrypt_key(raw)
        elif raw == "":
            stored.pop("key", None)
    row.ai_config = stored
    db.commit()
    return public_config(db, user_id)


# --------------------------------------------------------------------------- #
# 协议 / 地址
# --------------------------------------------------------------------------- #
def protocol_of(cfg: dict) -> str:
    chosen = cfg.get("protocol") or "auto"
    if chosen in {"openai", "anthropic"}:
        return chosen
    base = (cfg.get("baseUrl") or "").lower()
    if "anthropic" in base or "claude" in base:
        return "anthropic"
    return "openai"


def endpoints(base_url: str, protocol: str) -> tuple[str, str, str]:
    """返回 (根地址, 对话地址, 模型列表地址)。"""
    base = normalize_base(base_url)
    if protocol == "anthropic":
        root = base if re.search(r"/v\d+$", base, re.I) else base + "/v1"
        return root, root + "/messages", root + "/models"
    return base, base + "/chat/completions", base + "/models"


def headers_for(cfg: dict, protocol: str) -> dict[str, str]:
    key = cfg.get("key") or ""
    if protocol == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }
    return {"Authorization": f"Bearer {key}"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@contextmanager
def _response(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict | None = None,
    method: str = "GET",
    timeout: int = PROBE_TIMEOUT,
    stream: bool = False,
):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream" if stream else "application/json")
    # 不压缩：压缩流要先整体读进内存，SSE 就没法边收边给了
    req.add_header("Accept-Encoding", "identity")
    for name, value in headers.items():
        req.add_header(name, value)

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise AiError(_http_error_text(exc), exc.code) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise AiError(f"连接不上接口地址：{reason}") from None
    except TimeoutError:
        raise AiError("接口响应超时，请检查地址或网络") from None
    try:
        yield resp
    finally:
        try:
            resp.close()
        except Exception:  # pragma: no cover
            pass


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    hint = {
        401: "密钥无效或已过期",
        403: "没有权限访问该模型",
        404: "地址或模型不存在",
        429: "请求太频繁或额度用完了",
    }.get(exc.code, "")
    detail = ""
    try:
        raw = exc.read().decode("utf-8", "replace")[:1200]
        obj = json.loads(raw)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            detail = err
        elif isinstance(obj, dict):
            detail = str(obj.get("message") or "")
    except Exception:
        pass
    message = f"接口返回 {exc.code}"
    if hint:
        message += f"（{hint}）"
    if detail:
        message += f"：{detail[:300]}"
    return message


# --------------------------------------------------------------------------- #
# 模型列表 / 连通性
# --------------------------------------------------------------------------- #
def list_models(cfg: dict) -> dict:
    """探测接口：返回 {ok, models, message, latency}。"""
    protocol = protocol_of(cfg)
    _, _, models_url = endpoints(cfg["baseUrl"], protocol)
    headers = headers_for(cfg, protocol)
    started = datetime.now()

    try:
        with _response(models_url, headers=headers, timeout=PROBE_TIMEOUT) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
    except AiError as exc:
        # 有些代理不实现 /models，退化成一次最小对话请求，至少验证密钥能用
        if exc.status in (404, 405, 501):
            return _probe_by_chat(cfg, protocol, started)
        raise
    except json.JSONDecodeError:
        raise AiError("接口地址返回的不是 JSON，确认填的是 API 根地址（通常以 /v1 结尾）") from None

    items = []
    if isinstance(obj, dict):
        for key in ("data", "models", "result"):
            if isinstance(obj.get(key), list):
                items = obj[key]
                break
    elif isinstance(obj, list):
        items = obj

    models = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
        else:
            name = item
        if isinstance(name, str) and name.strip():
            models.append(name.strip())

    models = sorted(dict.fromkeys(models), key=str.lower)
    latency = int((datetime.now() - started).total_seconds() * 1000)
    return {
        "ok": True,
        "models": models[:300],
        "message": f"连接成功，检测到 {len(models)} 个模型" if models else "连接成功（该接口不返回模型列表，请手动填写）",
        "latency": latency,
        "protocol": protocol,
    }


def _probe_by_chat(cfg: dict, protocol: str, started: datetime) -> dict:
    _, chat_url, _ = endpoints(cfg["baseUrl"], protocol)
    headers = headers_for(cfg, protocol)
    model = cfg.get("model") or ("claude-3-5-haiku-latest" if protocol == "anthropic" else "gpt-4o-mini")
    payload = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    with _response(chat_url, headers=headers, payload=payload, method="POST", timeout=PROBE_TIMEOUT):
        pass
    latency = int((datetime.now() - started).total_seconds() * 1000)
    return {
        "ok": True,
        "models": [],
        "message": "连接成功（该地址不支持模型列表，请手动填写模型名）",
        "latency": latency,
        "protocol": protocol,
    }


# --------------------------------------------------------------------------- #
# 消息准备
# --------------------------------------------------------------------------- #
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 1 字 1 token，其他按 4 字符 1 token。"""
    body = text or ""
    cjk = len(_CJK.findall(body))
    rest = len(body) - cjk
    return int(cjk + rest / 4) + 4


def trim_history(messages: list[dict], system: str, max_input: int) -> list[dict]:
    """按输入上限从最旧的消息开始丢，避免长对话把上下文撑爆。"""
    if not max_input:
        return messages
    budget = max_input - estimate_tokens(system)
    kept: list[dict] = []
    for message in reversed(messages):
        cost = estimate_tokens(str(message.get("content") or ""))
        if cost + 40 > budget and kept:
            break
        budget -= cost
        kept.append(message)
        if budget <= 0:
            break
    kept.reverse()
    if kept and kept[0].get("role") == "assistant":
        kept.pop(0)  # 别让历史以助手发言开头
    return kept or messages[-1:]


def _data_url_parts(url: str) -> tuple[str, str] | None:
    match = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", url or "", re.S)
    if not match:
        return None
    return match.group(1), match.group(2)


def to_openai_messages(system: str, messages: list[dict], *, vision: bool) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        role = message.get("role") or "user"
        content = str(message.get("content") or "")
        images = message.get("images") or []
        if role == "user" and vision and images:
            parts: list[dict] = [{"type": "text", "text": content or "看看这张图"}]
            for url in images[:4]:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            out.append({"role": "user", "content": parts})
        elif content:
            out.append({"role": role, "content": content})
    return out


def to_anthropic_messages(system: str, messages: list[dict], *, vision: bool) -> tuple[str, list[dict]]:
    out: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        content = str(message.get("content") or "")
        images = message.get("images") or []
        if role == "user" and vision and images:
            blocks: list[dict] = []
            for url in images[:4]:
                parsed = _data_url_parts(url)
                if not parsed:
                    continue
                media_type, data = parsed
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    }
                )
            blocks.append({"type": "text", "text": content or "看看这张图"})
            out.append({"role": "user", "content": blocks})
        elif content:
            # Anthropic 要求 user / assistant 交替，相邻同角色合并
            if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str):
                out[-1]["content"] += "\n\n" + content
            else:
                out.append({"role": role, "content": content})
    while out and out[0]["role"] == "assistant":
        out.pop(0)
    return system, out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    pass


@dataclass
class ToolResult:
    """工具执行结果：text 给模型当前文，images 是需要「看」的图片（base64）。

    OpenAI 的 tool 消息只能放字符串，图片要另起一条 user 消息；
    Anthropic 的 tool_result 可以直接塞 image block。两种协议各自拼，这里只负责给数据。
    """

    text: str
    images: list[dict] = field(default_factory=list)  # {media_type, data, width, height}
    # 改动了界面偏好的工具（目前只有主题色）把新值带出来，前端据此即时换色，
    # 不用等用户刷新页面才看到效果。
    settings: dict | None = None


# 看图的天花板：再大既费 token 又容易被上游拒，先本地缩到模型推荐的长边
MAX_IMAGE_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE = 1568
MAX_IMAGE_KEEP_BYTES = 3_500_000
MAX_IMAGE_OUT_BYTES = 4 * 1024 * 1024
# OpenAI / Anthropic 都认这四种；其余（bmp/ico/avif/svg）先转成 JPEG
IMAGE_KEEP_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _image_block(path: Path) -> dict:
    """把磁盘上的图片变成可直接塞进请求体的 base64 块。

    小图原样保留（不重编码、不动动画）；过大或格式不通用时用 Pillow 缩到长边 1568 再转 JPEG。
    透明通道铺白底，避免 PNG 转 JPEG 后变成黑块。
    """
    raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            mime = f"image/{(source.format or '').lower()}"
            width, height = source.size
            # 高压缩比的大图解码后可能吃掉几百 MB 内存，先按像素数拦一下
            if width * height > 80_000_000:
                raise ToolError("这张图像素太多了（超过 8000 万），先缩小一点再看吧")
            keep = (
                mime in IMAGE_KEEP_MIMES
                and len(raw) <= MAX_IMAGE_KEEP_BYTES
                and max(width, height) <= MAX_IMAGE_EDGE * 2
            )
            if keep and source.mode in {"RGBA", "LA", "P"}:
                # 带透明像素的图在不少客户端会渲成黑底，先铺白再给模型
                keep = source.convert("RGBA").getchannel("A").getextrema()[0] >= 250
            if keep:
                return {
                    "media_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                    "width": width,
                    "height": height,
                }
            picture = ImageOps.exif_transpose(source)
            if max(picture.size) > MAX_IMAGE_EDGE:
                ratio = MAX_IMAGE_EDGE / max(picture.size)
                size = (max(1, round(picture.width * ratio)), max(1, round(picture.height * ratio)))
                picture = picture.resize(size, Image.LANCZOS)
            if picture.mode in {"RGBA", "LA", "P"}:
                # 透明通道铺白底：直接 paste(RGBA) 会连 alpha 一起丢，得到黑块
                rgba = picture.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                canvas.alpha_composite(rgba)
                picture = canvas.convert("RGB")
            elif picture.mode != "RGB":
                picture = picture.convert("RGB")
            data = b""
            for quality in (86, 78, 70):
                buffer = io.BytesIO()
                picture.save(buffer, "JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= MAX_IMAGE_OUT_BYTES:
                    break
            width, height = picture.size
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ToolError("这张图打不开，可能不是常见的位图格式（SVG 看不了，先转成 PNG 吧）") from None
    return {
        "media_type": "image/jpeg",
        "data": base64.b64encode(data).decode("ascii"),
        "width": width,
        "height": height,
    }


def _openai_image_parts(images: list[dict]) -> list[dict]:
    """OpenAI 兼容协议：tool 消息后补一条 user 消息，用 image_url 把图递给模型。"""
    parts: list[dict] = [{"type": "text", "text": "（以下是刚读取的图片，请查看）"}]
    for image in images[:4]:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image['media_type']};base64,{image['data']}"},
            }
        )
    return parts


def _anthropic_tool_content(result: "ToolResult") -> str | list[dict]:
    """Anthropic 协议的 tool_result 允许直接带 image block，省掉一条额外消息。"""
    if not result.images:
        return result.text[:8000]
    blocks: list[dict] = [{"type": "text", "text": result.text[:8000]}]
    for image in result.images[:4]:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["data"],
                },
            }
        )
    return blocks


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出某个文件夹里的文件和子文件夹。path 为空表示根目录，例如 '照片/2024'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件夹路径，留空为根目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按文件名关键词搜索用户的全部文件（不含回收站）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "storage_usage",
            "description": "查询当前用户云盘的容量与已用空间。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "向用户提出一个问题并附上候选回答，等用户点选或自己写。"
                "缺少继续完成任务所需的关键信息时用它（例如要整理哪个文件夹、"
                "偏好哪种做法、不确定要操作的是哪个文件）。"
                "候选回答由你自动生成 2-4 个短句，前端会额外提供一个「自定义」入口。"
                "调用后本轮回答立即结束，用户的回答会作为下一条消息出现。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的问题，一两句话"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "候选回答（2-4 个，每个一句短语）",
                    },
                },
                "required": ["question"],
            },
        },
    },
]

# 会读到文件正文的工具：属于「查看文件内容」权限，默认不开放。
# 只列目录、搜文件名、看容量不碰内容，不需要这个权限。
READ_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": "读取一个文本文件的内容（txt/md/代码等纯文本，最多 4000 字）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，例如 'note/todo.md'"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字，默认 3000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": (
                "查看一张图片的内容（png/jpg/webp/gif）。图片会直接显示给模型，"
                "适合识别截图、照片、扫描件里的文字或画面；读不到 SVG。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "图片路径，例如 '照片/截图.png'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_media_tags",
            "description": (
                "读取音频/视频文件内嵌的元数据标签：标题、艺术家、专辑、年份、音轨、时长、"
                "码率、采样率、分辨率，以及有没有内嵌封面。支持 mp3/flac/ogg/m4a/wav"
                "/mp4/mkv/mov/avi；只读标签，不读媒体内容本身。"
                "用户问「这首歌是谁唱的」「这视频多长」时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "音频或视频文件路径，例如 '音乐/晴天.mp3'",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

READ_TOOL_NAMES = {spec["function"]["name"] for spec in READ_TOOL_SPECS}

# 看图片需要模型本身支持视觉输入（多模态），否则给工具也看不懂
VISION_TOOL_NAMES = {"read_image"}

TOOL_LABELS = {
    "list_files": "翻看文件夹",
    "search_files": "搜索文件",
    "read_text": "读取文件",
    "read_image": "查看图片",
    "read_media_tags": "查看音视频标签",
    "storage_usage": "查看容量",
    "ask_user": "向用户提问",
    "create_folder": "新建文件夹",
    "write_file": "写入文件",
    "rename_item": "重命名",
    "copy_item": "复制",
    "move_item": "移动",
    "create_pickup_code": "生成取件码",
    "pickup_files": "取件",
    "send_to_user": "快传发送",
    "delete_item": "删除",
    "empty_trash": "清空回收站",
    "set_theme_color": "改主题色",
}

# 会真正改动网盘的工具：默认不开放，必须在设置里勾选同意
WRITE_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "新建文件夹，父级目录不存在时会一并创建。path 是完整路径，例如 '整理/发票'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的文件夹路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "新建或覆盖一个文本文件（txt/md/csv 等纯文本）。覆盖前请先确认用户确实要改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件完整路径，例如 'note/todo.md'"},
                    "content": {"type": "string", "description": "要写入的文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_item",
            "description": "给文件或文件夹改名（不移动位置）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "原路径"},
                    "new_name": {"type": "string", "description": "新名字，只写名字不要带路径"},
                },
                "required": ["path", "new_name"],
            },
        },
    },
]

MOVE_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "copy_item",
            "description": "把文件或文件夹复制到另一个文件夹（目标不存在会自动创建）。文件按内容寻址复用，不额外占盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "要复制的文件或文件夹路径"},
                    "target_dir": {"type": "string", "description": "目标文件夹路径，留空表示根目录"},
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_item",
            "description": "把文件或文件夹移动到另一个文件夹（不复制，原位置不再保留）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "要移动的文件或文件夹路径"},
                    "target_dir": {"type": "string", "description": "目标文件夹路径，留空表示根目录"},
                },
                "required": ["source"],
            },
        },
    },
]

# 删除：唯一会「少东西」的一组工具。默认关闭，设置页里明确标注为危险项。
# 默认进回收站（可还原），只有用户明确说要彻底删才走 permanent。
DELETE_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "delete_item",
            "description": (
                "删除文件或文件夹。默认只是移进回收站（用户随时能还原）；"
                "permanent=true 才是彻底删除、无法恢复。"
                "调用前必须把要删的完整路径逐条念给用户，等用户明确说「删」之后再调用；"
                "一次删多个也要逐个报清楚。名字不确定时先搜索并列出来问，不要猜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要删除的文件 / 文件夹完整路径列表",
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "true = 彻底删除（不可恢复）；默认 false，只放进回收站",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "empty_trash",
            "description": (
                "彻底删除回收站里的内容（不可恢复）。names 留空 = 清空整个回收站，"
                "给了 names 就只删回收站里同名的那几项。"
                "这是不可逆操作：必须先把回收站清单念给用户、拿到明确的「清空 / 删掉」答复，"
                "再传 confirm=true 调用；用户只是让你「看看回收站」时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只清理这几项（回收站里显示的名字）；留空等于全部清空",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "用户已确认清单并同意彻底删除时传 true（必须是 true 才会执行）",
                    },
                },
            },
        },
    },
]

QUICK_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_pickup_code",
            "description": (
                "为文件生成一个 6 位快传取件码，别人打开取件页输码即可下载。"
                "调用前必须先跟用户确认清楚是哪几个文件（把找到的路径列给用户确认），确认后再调用。"
                "只支持文件，不支持文件夹。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要放进取件码的文件路径列表，例如 ['照片/合影.jpg']",
                    },
                    "days": {"type": "integer", "description": "有效期天数，默认 7 天"},
                    "max_uses": {
                        "type": "integer",
                        "description": "每个文件最多被下载几次，不填为不限",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pickup_files",
            "description": (
                "用 6 位取件码取件：action=list 只看清单，save 转存到我的网盘，"
                "download 给出下载链接。用户说「帮我取件，取件码是 XXXXX」时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位取件码"},
                    "action": {
                        "type": "string",
                        "enum": ["list", "save", "download"],
                        "description": "list 只看清单；save 转存到网盘；download 给下载链接",
                    },
                    "target_dir": {
                        "type": "string",
                        "description": "save 时存到哪个文件夹，留空表示根目录",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_user",
            "description": (
                "用站内快传把文件 / 文件夹发给另一个用户（对方同意后才收进他的网盘，"
                "对方关掉了「接收快传文件」就发不过去）。"
                "to 填对方用户名；记不住用户名时填好友的昵称也行（只会在你已通过的好友里找）。"
                "调用前先把找到的完整路径与收件人念给用户确认，确认后再调用；"
                "文件夹会整棵发送。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要发送的文件 / 文件夹完整路径列表，例如 ['照片/合影.jpg']",
                    },
                    "to": {"type": "string", "description": "收件人的用户名"},
                    "message": {
                        "type": "string",
                        "description": "随文件附一句说明，可留空",
                    },
                },
                "required": ["paths", "to"],
            },
        },
    },
]

# 主题色：单色 / 渐变，也可以直接按用户当前的背景图取色。
# 只改用户自己的界面配色，不碰文件，但同样要用户勾选后才交给模型。
THEME_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_theme_color",
            "description": (
                "改界面主题色（按钮、开关、选中态的强调色）。"
                "用户说「换个主题色 / 改成蓝色 / 要渐变的」时用它："
                "单色传 mode=solid + color；渐变传 mode=gradient + colors（2–5 个）。"
                "用户说「从背景图取色 / 按壁纸配色」时只传 from_background=true，"
                "不要自己编颜色 —— 服务端会从当前背景图里采样。"
                "用户说「恢复默认主题色」时传 reset=true。"
                "颜色一律 #rrggbb；不确定具体色值就先问用户，或者按背景图取色。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["solid", "gradient"],
                        "description": "solid 单色（默认）；gradient 渐变",
                    },
                    "color": {
                        "type": "string",
                        "description": "单色模式的颜色，例如 #60a5fa",
                    },
                    "colors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "渐变模式的 2–5 个颜色，按渐变方向排，例如 ['#60a5fa', '#34d399']",
                    },
                    "from_background": {
                        "type": "boolean",
                        "description": "true = 从用户当前的自定义背景图里取色（配合 mode 决定单色还是渐变）",
                    },
                    "reset": {
                        "type": "boolean",
                        "description": "true = 恢复默认主题色（樱紫 #c084fc 单色）",
                    },
                },
            },
        },
    },
]

WRITE_TOOL_NAMES = {
    spec["function"]["name"]
    for spec in WRITE_TOOL_SPECS + MOVE_TOOL_SPECS + QUICK_TOOL_SPECS + THEME_TOOL_SPECS + DELETE_TOOL_SPECS
}

# 工具需要的权限：read 需要「查看文件内容」，write/move/quick/delete/theme 各自要用户勾选
TOOL_PERMISSION = {
    "read_text": "read",
    "read_image": "read",
    "read_media_tags": "read",
    "create_folder": "write",
    "write_file": "write",
    "rename_item": "write",
    "copy_item": "move",
    "move_item": "move",
    "create_pickup_code": "quick",
    "pickup_files": "quick",
    "send_to_user": "quick",
    "delete_item": "delete",
    "empty_trash": "delete",
    "set_theme_color": "theme",
}


def tool_specs(cfg: dict) -> list[dict]:
    """按用户授权情况决定把哪些工具交给模型（未授权的不出现在列表里）。"""
    if not cfg.get("tools"):
        return []
    specs = list(TOOL_SPECS)
    if cfg.get("readContent"):
        # 没勾「图片输入」说明模型看不了图，就别把 read_image 交出去
        specs.extend(
            spec
            for spec in READ_TOOL_SPECS
            if spec["function"]["name"] not in VISION_TOOL_NAMES or cfg.get("vision")
        )
    if cfg.get("writeFiles"):
        specs.extend(WRITE_TOOL_SPECS)
    if cfg.get("moveFiles"):
        specs.extend(MOVE_TOOL_SPECS)
    if cfg.get("quickTransfer"):
        specs.extend(QUICK_TOOL_SPECS)
    if cfg.get("deleteFiles"):
        specs.extend(DELETE_TOOL_SPECS)
    if cfg.get("themeColor"):
        specs.extend(THEME_TOOL_SPECS)
    return specs


def anthropic_tools(specs: list[dict]) -> list[dict]:
    return [
        {
            "name": spec["function"]["name"],
            "description": spec["function"]["description"],
            "input_schema": spec["function"]["parameters"],
        }
        for spec in specs
    ]


def _node_label(db: Session, user_id: int, node: Node) -> str:
    """审计日志里的可读路径：'照片/2024/合影.png'（不带「我的文件」根）。"""
    trail = [part["name"] for part in node_service.breadcrumb(db, user_id, node.parent_id)]
    return "/".join([*trail[1:], node.name])


def _fmt_size(size: int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _resolve_path(db: Session, user_id: int, path: str) -> Node | None:
    parts = [p for p in re.split(r"[\\/]+", str(path or "")) if p and p != "."]
    parent_id = None
    node: Node | None = None
    for name in parts:
        stmt = select(Node).where(
            Node.user_id == user_id,
            Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id,
            Node.deleted_at.is_(None),
            func.lower(Node.name) == name.lower(),
        )
        node = db.scalars(stmt).first()
        if node is None:
            raise ToolError(f"找不到路径「{path}」里的「{name}」")
        parent_id = node.id
    return node


def _path_parts(path: Any) -> list[str]:
    return [p for p in re.split(r"[\\/]+", str(path or "")) if p and p != "."]


def _find_child(db: Session, user_id: int, parent_id: int | None, name: str) -> Node | None:
    stmt = select(Node).where(
        Node.user_id == user_id,
        Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id,
        Node.deleted_at.is_(None),
        func.lower(Node.name) == name.lower(),
    )
    return db.scalars(stmt).first()


def _ensure_folder(db: Session, user: User, parts: list[str], *, create: bool = True) -> tuple[Node | None, bool]:
    """按路径逐级找文件夹，缺的就建出来。返回 (最深的文件夹 or None, 是否新建过)。"""
    parent: Node | None = None
    created = False
    for name in parts:
        parent_id = parent.id if parent is not None else None
        node = _find_child(db, user.id, parent_id, name)
        if node is None:
            if not create:
                raise ToolError(f"找不到文件夹「{name}」")
            node = node_service.create_folder(db, user, parent_id, name)
            db.flush()
            created = True
        elif node.type != "folder":
            raise ToolError(f"「{name}」是个文件，不能当文件夹用")
        parent = node
    return parent, created


def _store_bytes(data: bytes) -> tuple[str, int]:
    """把字节写进内容寻址存储，返回 (sha256, 大小)。已有同内容就直接复用。"""
    sha = hashlib.sha256(data).hexdigest()
    if not storage.blob_exists(sha, len(data)):
        tmp = storage.new_tmp_path()
        tmp.write_bytes(data)
        storage.commit_tmp(tmp, sha)
    return sha, len(data)


def _folder_label(value: Any) -> str:
    parts = _path_parts(value)
    return "/".join(parts) if parts else "我的文件"


def _trash_tops(db: Session, user_id: int) -> list[Node]:
    """回收站里的顶层条目（父节点也在回收站里的不算，否则同一棵子树会被数两遍）。"""
    rows = db.scalars(
        select(Node).where(Node.user_id == user_id, Node.deleted_at.is_not(None))
    ).all()
    ids = {node.id for node in rows}
    return [node for node in rows if node.parent_id is None or node.parent_id not in ids]


def _run_tool(
    db: Session, user: User, name: str, args: dict, perms: dict | None = None
) -> str | ToolResult:
    perms = perms or {}
    need = TOOL_PERMISSION.get(name)
    if need == "read" and not perms.get("read"):
        raise ToolError(
            "管理员没有开启「查看文件内容」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )
    if need == "write" and not perms.get("write"):
        raise ToolError(
            "管理员没有开启「新建 / 编辑文件与文件夹」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )
    if need == "move" and not perms.get("move"):
        raise ToolError(
            "管理员没有开启「复制 / 移动文件与文件夹」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )
    if need == "quick" and not perms.get("quick"):
        raise ToolError(
            "管理员没有开启「快传取件码」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )
    if need == "delete" and not perms.get("delete"):
        raise ToolError(
            "管理员没有开启「删除文件与清空回收站」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )
    if need == "theme" and not perms.get("theme"):
        raise ToolError(
            "管理员没有开启「修改界面主题色」权限，请先到 设置 → 看板娘 → 对话设置 里勾选"
        )

    if name == "list_files":
        folder = _resolve_path(db, user.id, str(args.get("path") or ""))
        parent_id = folder.id if folder is not None else None
        if folder is not None and folder.type != "folder":
            raise ToolError("这个路径是一个文件，不是文件夹")
        items = node_service.list_children(db, user.id, parent_id, sort="name", order="asc")
        if not items:
            return "这个文件夹是空的。"
        lines = [f"共 {len(items)} 项（最多列出 40 项）："]
        for item in items[:40]:
            if item["type"] == "folder":
                lines.append(f"- [文件夹] {item['name']}（{item.get('childCount') or 0} 项）")
            else:
                lines.append(f"- [文件] {item['name']} {_fmt_size(item['size'])}")
        return "\n".join(lines)

    if name == "search_files":
        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            raise ToolError("关键词不能为空")
        items = node_service.search(db, user.id, keyword, limit=20)
        if not items:
            return f"没有找到文件名包含「{keyword}」的内容。"
        lines = [f"找到 {len(items)} 项："]
        for item in items[:20]:
            where = item.get("path") or "我的文件"
            suffix = "文件夹" if item["type"] == "folder" else _fmt_size(item["size"])
            lines.append(f"- {where}/{item['name']}（{suffix}）")
        return "\n".join(lines)

    if name == "read_text":
        node = _resolve_path(db, user.id, str(args.get("path") or ""))
        if node is None or node.type != "file":
            raise ToolError("找不到这个文件")
        kind = node_service.preview_kind(node, is_admin=True)
        if kind not in {"text", "markdown"}:
            raise ToolError("这个文件不是纯文本，读不了")
        if (node.size or 0) > 400 * 1024:
            raise ToolError("文件太大了（超过 400 KB），不适合直接读")
        if not node.blob_sha256:
            raise ToolError("文件内容还没落盘")
        path = storage.blob_path(node.blob_sha256)
        if not path.exists():
            raise ToolError("文件内容缺失")
        try:
            limit = max(200, min(8000, int(args.get("max_chars") or 3000)))
        except (TypeError, ValueError):
            limit = 3000
        text = path.read_text("utf-8", errors="replace")
        if len(text) > limit:
            text = text[:limit] + "\n…（已截断）"
        # 设置页承诺「读取正文会记审计」，读一次记一条
        log_action(db, "ai_tool", user.id, {"tool": name, "path": _node_label(db, user.id, node)})
        db.commit()
        return f"《{node.name}》内容：\n{text}"

    if name == "read_image":
        # vision 不是用户授权，而是模型能力：没开就别把图塞给不支持视觉的接口
        if not perms.get("vision"):
            raise ToolError(
                "当前模型没有开启「图片输入」，看不了图；到 设置 → 看板娘 → 对话设置 里打开，或换一个多模态模型"
            )
        node = _resolve_path(db, user.id, str(args.get("path") or ""))
        if node is None or node.type != "file":
            raise ToolError("找不到这张图片")
        if node_service.preview_kind(node, is_admin=True) != "image":
            raise ToolError("这个文件不是图片，纯文本请用 read_text")
        if (node.size or 0) > MAX_IMAGE_FILE_BYTES:
            raise ToolError("图片太大了（超过 20 MB），先压缩一下再看吧")
        if not node.blob_sha256:
            raise ToolError("文件内容还没落盘")
        path = storage.blob_path(node.blob_sha256)
        if not path.exists():
            raise ToolError("文件内容缺失")
        block = _image_block(path)
        log_action(db, "ai_tool", user.id, {"tool": name, "path": _node_label(db, user.id, node)})
        db.commit()
        return ToolResult(
            text=(
                f"已读取图片《{node.name}》"
                f"（{block['width']}×{block['height']}，{_fmt_size(node.size)}），图片在下面。"
            ),
            images=[block],
        )

    if name == "read_media_tags":
        node = _resolve_path(db, user.id, str(args.get("path") or ""))
        if node is None or node.type != "file":
            raise ToolError("找不到这个音视频文件")
        kind = node_service.preview_kind(node, is_admin=True)
        if kind not in {"audio", "video"}:
            raise ToolError(
                "这个文件不是音频或视频；纯文本用 read_text，图片用 read_image"
            )
        if not node.blob_sha256:
            raise ToolError("文件内容还没落盘")
        path = storage.blob_path(node.blob_sha256)
        if not path.exists():
            raise ToolError("文件内容缺失")
        try:
            info = media_tags.parse(path)
        except media_tags.MediaTagError as exc:
            raise ToolError(f"没能解析这个文件的元数据：{exc}")
        except OSError as exc:
            raise ToolError(f"读文件出错：{exc}")
        log_action(db, "ai_tool", user.id, {"tool": name, "path": _node_label(db, user.id, node)})
        db.commit()
        return media_tags.describe(info, node.name)

    if name == "storage_usage":
        used = user.used_bytes or 0
        quota = user.quota_bytes or 0
        percent = f"{used / quota * 100:.1f}%" if quota else "—"
        return f"已用 {_fmt_size(used)} / 总容量 {_fmt_size(quota)}（{percent}）"

    # ---------------- 界面主题色（需用户勾选「修改界面主题色」） ----------------
    if name == "set_theme_color":
        row = ensure_settings(db, user.id)
        reset = bool(args.get("reset"))
        from_bg = bool(args.get("from_background"))
        wanted = str(args.get("mode") or "").strip().lower()
        if wanted in {"solid", "gradient"}:
            mode = wanted
        else:
            # 没说模式时：按背景图取色走渐变（和设置页那个按钮一致），否则单色
            mode = "gradient" if from_bg else "solid"

        if reset:
            mode = "solid"
            colors = [theme_color.DEFAULT_ACCENT]
        else:
            colors: list[str] = []
            if from_bg:
                path = profile_service.background_path(row.background_image)
                if path is None:
                    raise ToolError(
                        "你还没有自定义背景图，取不了色；可以先到 设置 → 外观 换一张，"
                        "或者直接告诉我用哪个颜色（#rrggbb）"
                    )
                colors = theme_color.image_colors(path, 3 if mode == "gradient" else 1)
                if not colors:
                    raise ToolError("这张背景图里没挑出能用的颜色，直接给我一个色值吧")
            if not colors:
                raw = args.get("colors")
                values = list(raw) if isinstance(raw, list) else []
                single = args.get("color")
                if single:
                    values.insert(0, single)
                colors = theme_color.clean_colors(values)
            if not colors:
                raise ToolError("要给出颜色（#rrggbb），或者说「从背景图取色」")
            if mode == "gradient":
                if len(colors) < 2:
                    colors = [colors[0], theme_color.companion(colors[0])]
                colors = colors[:5]
            else:
                colors = [colors[0]]

        row.accent_color = colors[0]
        row.accent_style = {"mode": mode, "colors": colors}
        log_action(db, "ai_tool", user.id, {"tool": name, "mode": mode, "colors": colors})
        db.commit()

        settings_patch = {
            "accentColor": colors[0],
            "accentStyle": {"mode": mode, "colors": colors},
        }
        if reset:
            text = f"已恢复默认主题色 {colors[0]}（界面已经生效）。"
        elif mode == "gradient":
            text = f"已把主题色改成渐变：{' → '.join(colors)}（界面已经生效，不满意可以让我再换）。"
        else:
            text = f"已把主题色改成 {colors[0]}（界面已经生效，不满意可以让我再换）。"
        return ToolResult(text, settings=settings_patch)

    # ---------------- 写操作（需用户在设置里勾选同意） ----------------
    if name == "create_folder":
        parts = _path_parts(args.get("path"))
        if not parts:
            raise ToolError("要给出文件夹路径")
        parent, _ = _ensure_folder(db, user, parts[:-1])
        parent_id = parent.id if parent is not None else None
        if _find_child(db, user.id, parent_id, parts[-1]) is not None:
            return f"「{'/'.join(parts)}」已经存在了，没有重复创建。"
        node = node_service.create_folder(db, user, parent_id, parts[-1])
        db.flush()
        log_action(db, "ai_tool", user.id, {"tool": name, "path": "/".join(parts)})
        db.commit()
        return f"已创建文件夹「{'/'.join(parts[:-1]) + '/' if parts[:-1] else ''}{node.name}」。"

    if name == "write_file":
        parts = _path_parts(args.get("path"))
        if not parts:
            raise ToolError("要给出文件路径")
        content = str(args.get("content") or "")
        if len(content) > 200_000:
            raise ToolError("内容太长（超过 20 万字），先写个短版本或者分几段写吧")
        file_name = parts[-1]
        parent, _ = _ensure_folder(db, user, parts[:-1])
        parent_id = parent.id if parent is not None else None
        existing = _find_child(db, user.id, parent_id, file_name)
        if existing is not None and existing.type == "folder":
            raise ToolError("这个位置已经是个文件夹了，换个文件名吧")
        if existing is not None:
            kind = node_service.preview_kind(existing, is_admin=True)
            if kind not in {"text", "markdown", "none"}:
                raise ToolError("这个位置已有同名文件，而且不是纯文本，别覆盖它")
        data = content.encode("utf-8")
        old_size = int(existing.size or 0) if existing is not None else 0
        delta = len(data) - old_size
        if delta > 0 and (user.used_bytes or 0) + delta > (user.quota_bytes or 0):
            raise ToolError("存储空间不够了，先清一清回收站或者扩容吧")
        sha, size = _store_bytes(data)
        mime = mimetypes.guess_type(file_name)[0] or "text/plain"
        if existing is not None:
            old_sha = existing.blob_sha256
            node_service.retain_blob(db, sha, size)
            existing.blob_sha256 = sha
            existing.size = size
            existing.mime = mime
            existing.updated_at = utcnow()
            if old_sha and old_sha != sha:
                node_service.release_blob(db, old_sha)
            action = "覆盖"
        else:
            node_service.create_file(db, user, parent_id, file_name, sha, size, mime)
            action = "新建"
        node_service.recalc_usage(db, user)
        log_action(db, "ai_tool", user.id, {"tool": name, "path": "/".join(parts), "size": size})
        db.commit()
        return f"已{action}《{file_name}》（{_fmt_size(size)}）。"

    if name == "rename_item":
        node = _resolve_path(db, user.id, str(args.get("path") or ""))
        if node is None:
            raise ToolError("找不到要改名的东西")
        new_name = str(args.get("new_name") or "").strip().strip("/\\")
        if not new_name:
            raise ToolError("新名字不能为空")
        if len(new_name) > 255:
            raise ToolError("新名字太长了")
        old_name = node.name
        node_service.rename_node(db, user, node, new_name)
        log_action(db, "ai_tool", user.id, {"tool": name, "from": old_name, "to": node.name})
        db.commit()
        return f"已把「{old_name}」改名为「{node.name}」。"

    if name == "copy_item":
        source = _resolve_path(db, user.id, str(args.get("source") or ""))
        if source is None:
            raise ToolError("找不到要复制的内容")
        target, _ = _ensure_folder(db, user, _path_parts(args.get("target_dir")))
        try:
            clone, added = node_service.copy_subtree(db, user, source, target.id if target is not None else None)
        except HTTPException as exc:
            db.rollback()
            raise ToolError(str(exc.detail)) from None
        if added and (user.used_bytes or 0) + added > (user.quota_bytes or 0):
            db.rollback()
            raise ToolError("空间不够复制这些内容，先清理一下再来")
        user.used_bytes = (user.used_bytes or 0) + int(added or 0)
        log_action(db, "ai_tool", user.id, {"tool": name, "from": source.name, "to": _folder_label(args.get("target_dir"))})
        db.commit()
        size_note = f"，新增 {_fmt_size(added)}" if added else ""
        return f"已把「{source.name}」复制到「{_folder_label(args.get('target_dir'))}」{size_note}。"

    if name == "move_item":
        source = _resolve_path(db, user.id, str(args.get("source") or ""))
        if source is None:
            raise ToolError("找不到要移动的内容")
        target, _ = _ensure_folder(db, user, _path_parts(args.get("target_dir")))
        try:
            node_service.move_node(db, user, source, target.id if target is not None else None)
        except HTTPException as exc:
            db.rollback()
            raise ToolError(str(exc.detail)) from None
        log_action(db, "ai_tool", user.id, {"tool": name, "from": source.name, "to": _folder_label(args.get("target_dir"))})
        db.commit()
        return f"已把「{source.name}」移动到「{_folder_label(args.get('target_dir'))}」。"

    # ---------------- 快传（需勾选「快传取件码」） ----------------
    if name == "create_pickup_code":
        from . import quick_service as qs

        raw = args.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        paths = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if not paths:
            raise ToolError("要给出至少一个文件路径")
        entries: list[dict] = []
        picked: list[str] = []
        for path in paths:
            node = _resolve_path(db, user.id, path)
            if node is None:
                raise ToolError(f"找不到文件「{path}」")
            if node.type != "file":
                raise ToolError(f"「{node.name}」是文件夹，取件码只能放文件")
            entries.append(
                {"node": node, "days": args.get("days"), "maxUses": args.get("max_uses")}
            )
            picked.append(_node_label(db, user.id, node))

        try:
            qt = qs.create_pickup(db, user, entries)
        except HTTPException as exc:
            db.rollback()
            raise ToolError(str(exc.detail)) from None

        expires = qt.expires_at.strftime("%Y-%m-%d %H:%M") if qt.expires_at else "长期"
        log_action(
            db,
            "ai_tool",
            user.id,
            {"tool": name, "pickup": qt.id, "count": qt.count},
        )
        db.commit()
        names = "、".join(picked[:5]) + ("…" if len(picked) > 5 else "")
        # 顺手把分享文案也拼好：用户可以直接复制这段粘到聊天窗口
        site = settings_store.get_value(db, "site_name") or settings.APP_NAME
        message = qs.render_copy(
            qs.copy_template(db),
            site=site,
            file=qt.name,
            code=qt.code,
            link=f"{settings.public_base}/pickup?code={qt.code}",
        )
        return (
            f"已生成取件码：{qt.code}\n"
            f"文件：{names}（共 {qt.count} 个）\n"
            f"有效期：{expires}；每个文件下载次数："
            f"{entries[0]['maxUses'] if entries and entries[0].get('maxUses') else '不限'}\n"
            f"取件页：/pickup?code={qt.code}（拿到码的人都能下载，用完可以随时撤销）\n"
            f"分享文案：\n{message}"
        )

    if name == "pickup_files":
        from . import quick_service as qs

        code = str(args.get("code") or "").strip()
        if not code:
            raise ToolError("要给出 6 位取件码")
        try:
            qt = qs.find_pickup(db, code)
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from None
        entries = [entry for entry in qs.pickup_entries(db, qt) if entry["alive"]]
        if not entries:
            raise ToolError("这条取件码里没有可用的文件了")

        action = str(args.get("action") or "list").strip().lower()
        if action not in {"list", "save", "download"}:
            action = "list"

        if action == "save":
            target, _ = _ensure_folder(db, user, _path_parts(args.get("target_dir")))
            try:
                nodes = qs.pickup_save(db, user, qt, target.id if target is not None else None)
            except HTTPException as exc:
                db.rollback()
                raise ToolError(str(exc.detail)) from None
            log_action(
                db,
                "ai_tool",
                user.id,
                {"tool": name, "pickup": qt.id, "saved": len(nodes)},
            )
            db.commit()
            names = "、".join(node.name for node in nodes[:5]) + ("…" if len(nodes) > 5 else "")
            return f"已把 {len(nodes)} 个文件转存到「{_folder_label(args.get('target_dir'))}」：{names}"

        if action == "download":
            lines = [f"取件码 {qt.code} 共 {len(entries)} 个文件，下载链接："]
            for entry in entries[:10]:
                node = entry["node"]
                lines.append(
                    f"- {node.name}（{_fmt_size(node.size)}）："
                    f"/api/public/quick/pickup/{qt.code}/download?nodeId={node.id}"
                )
            if len(entries) > 10:
                lines.append(f"…还有 {len(entries) - 10} 个，打开取件页可以看到全部：/pickup?code={qt.code}")
            return "\n".join(lines)

        lines = [f"取件码 {qt.code} 里有 {len(entries)} 个文件（只看清单不消耗下载次数）："]
        for entry in entries[:20]:
            node = entry["node"]
            expire = entry["expiresAt"].strftime("%Y-%m-%d %H:%M") if entry["expiresAt"] else "长期"
            left = "不限" if entry["maxUses"] is None else f"剩 {max(0, entry['maxUses'] - entry['uses'])} 次"
            lines.append(f"- {node.name}（{_fmt_size(node.size)}，有效期至 {expire}，{left}）")
        lines.append("要我转存到网盘，还是直接给下载链接？")
        return "\n".join(lines)

    if name == "send_to_user":
        from . import quick_service as qs

        raw = args.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        paths = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if not paths:
            raise ToolError("要给出至少一个文件路径")
        target = str(args.get("to") or "").strip().lstrip("@")
        if not target:
            raise ToolError("要给出收件人的用户名")
        # 用户名优先；对不上时在「已通过的好友」里按昵称找一次 ——
        # 用户嘴里说的是「发给小明」，模型手里不一定有人家的用户名
        if db.scalars(select(User).where(func.lower(User.username) == target.lower())).first() is None:
            from . import friend_service as fsv

            mates = fsv.list_all(db, user)["friends"]
            hits = [
                mate
                for mate in mates
                if target.lower() in str(mate["displayName"]).lower()
                or target.lower() in str(mate["username"]).lower()
            ]
            if len(hits) == 1:
                target = str(hits[0]["username"])
            elif len(hits) > 1:
                detail = "、".join(f"{h['displayName']}（{h['username']}）" for h in hits[:6])
                raise ToolError(f"「{target}」匹配到多个好友：{detail}，请给出用户名")

        nodes: list[Node] = []
        labels: list[str] = []
        for path in paths:
            node = _resolve_path(db, user.id, path)
            if node is None:
                raise ToolError(f"找不到「{path}」")
            nodes.append(node)
            labels.append(_node_label(db, user.id, node))
        try:
            qt = qs.send_files(
                db,
                user,
                nodes,
                to_username=target,
                message=str(args.get("message") or "").strip() or None,
            )
        except HTTPException as exc:
            db.rollback()
            raise ToolError(str(exc.detail)) from None

        peer = db.get(User, qt.peer_id)
        peer_name = (peer.display_name or peer.username) if peer else target
        log_action(
            db,
            "ai_tool",
            user.id,
            {"tool": name, "to": target, "count": qt.count, "size": qt.size},
        )
        db.commit()
        names = "、".join(labels[:5]) + ("…" if len(labels) > 5 else "")
        state = "已自动收进对方网盘" if qt.send_status != "pending" else "等对方同意后才会进他的网盘"
        return (
            f"已通过站内快传给「{peer_name}（{target}）」发送 {qt.count} 项（{_fmt_size(qt.size)}）：{names}\n"
            f"状态：{state}；对方会在消息中心收到提醒。"
        )

    # ---------------- 删除（需勾选「删除文件与清空回收站」） ----------------
    if name == "delete_item":
        raw = args.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        paths = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if not paths:
            raise ToolError("要给出至少一个文件路径")
        permanent = bool(args.get("permanent"))

        removed: list[str] = []
        trashed = 0
        purged = 0
        for path in paths:
            node = _resolve_path(db, user.id, path)
            if node is None:
                raise ToolError(f"找不到「{path}」")
            label = _node_label(db, user.id, node)
            if permanent:
                # 先标删再 purge：purge 走 trash_subtree_ids，只认已经带删除标记的子树
                node_service.soft_delete(db, user, node)
                db.flush()
                purged += node_service.purge_node(db, user, node)
            else:
                trashed += node_service.soft_delete(db, user, node)
            removed.append(label)
        node_service.recalc_usage(db, user)
        log_action(
            db,
            "ai_tool",
            user.id,
            {"tool": name, "paths": removed[:20], "permanent": permanent},
        )
        db.commit()

        names = "、".join(removed[:5]) + ("…" if len(removed) > 5 else "")
        if permanent:
            return (
                f"已彻底删除：{names}（共 {purged} 个条目，无法恢复）。"
                f"当前已用空间 {_fmt_size(user.used_bytes or 0)}。"
            )
        return (
            f"已删除：{names}（共 {trashed} 个条目，已放进回收站，需要的话可以还原）。"
            f"当前已用空间 {_fmt_size(user.used_bytes or 0)}。"
        )

    if name == "empty_trash":
        if not args.get("confirm"):
            raise ToolError(
                "清空回收站不可恢复：请先把回收站里的条目列给用户，"
                "等用户明确同意后再带 confirm=true 调用"
            )
        raw = args.get("names")
        if isinstance(raw, str):
            raw = [raw]
        wanted = [str(item).strip() for item in (raw or []) if str(item).strip()]

        tops = _trash_tops(db, user.id)
        if wanted:
            chosen: list[Node] = []
            for label in wanted:
                hits = [node for node in tops if node.name.lower() == label.lower()]
                if not hits:
                    # 用户/模型常常只记得一部分名字，退一步按包含匹配
                    hits = [node for node in tops if label.lower() in node.name.lower()]
                if not hits:
                    available = "、".join(node.name for node in tops[:8]) or "回收站是空的"
                    raise ToolError(f"回收站里没有「{label}」。现有：{available}")
                if len(hits) > 1:
                    detail = "、".join(
                        f"{node.name}（{node.deleted_at.strftime('%m-%d %H:%M') if node.deleted_at else ''}）"
                        for node in hits[:6]
                    )
                    raise ToolError(f"「{label}」在回收站里匹配到多项：{detail}，请说得更具体一些")
                chosen.append(hits[0])
        else:
            chosen = tops

        if not chosen:
            return "回收站已经是空的，没有需要清理的内容。"

        total = 0
        names = []
        for node in chosen:
            names.append(node.name)
            total += node_service.purge_node(db, user, node)
        node_service.recalc_usage(db, user)
        log_action(
            db,
            "ai_tool",
            user.id,
            {"tool": name, "items": names[:20], "removed": total},
        )
        db.commit()
        shown = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
        return (
            f"已彻底删除回收站里的 {len(chosen)} 项（{shown}），共 {total} 个条目，无法恢复。"
            f"当前已用空间 {_fmt_size(user.used_bytes or 0)}。"
        )

    raise ToolError(f"没有这个工具：{name}")


def run_tool_safe(user_id: int, name: str, args: dict, perms: dict | None = None) -> ToolResult:
    """工具执行入口：自带会话，出错返回给模型的错误文本，不往上抛。"""
    from .db_failover import current_sessionmaker

    try:
        with current_sessionmaker()() as db:
            user = db.get(User, user_id)
            if user is None:
                return ToolResult("用户不存在。")
            out = _run_tool(db, user, name, args, perms)
            return out if isinstance(out, ToolResult) else ToolResult(str(out))
    except ToolError as exc:
        return ToolResult(f"工具调用失败：{exc}")
    except Exception as exc:  # pragma: no cover - 任何意外都别打断对话
        logger.warning("AI 工具执行异常 %s: %s", name, exc)
        return ToolResult(f"工具调用失败：{type(exc).__name__}")


# --------------------------------------------------------------------------- #
# 系统提示词
# --------------------------------------------------------------------------- #
def normalize_language(value: str | None) -> str:
    """把语言标签收敛到内置提示词支持的两个 key。"""
    return "en-US" if (value or "").strip().lower().startswith("en") else "zh-CN"


def default_prompt(lang: str | None = None) -> str:
    return DEFAULT_SYSTEM_PROMPTS[normalize_language(lang)]


def is_builtin_prompt(text: str | None) -> bool:
    """空值，或与任一内置提示词一致时，视为「没自定义过」。

    内置提示词不再下发给前端，但如果管理员从别处把全文贴回来再保存，落库文本仍会
    等于某个内置版本；把它归回默认，之后切换语言才能继续跟着换，而不是被当成
    自定义内容钉死。

    比较时先去掉末尾的语言规则行：0.3.47 之前存下的中文默认没有这一行，不能因此
    把老数据当成自定义内容。
    """
    core = _prompt_core(text)
    if not core:
        return True
    return any(core == _prompt_core(prompt) for prompt in DEFAULT_SYSTEM_PROMPTS.values())


LANGUAGE_RULE_PREFIXES = ("- **回复语言**", "- **Language**")


def _prompt_core(text: str | None) -> str:
    lines = [
        line
        for line in (text or "").strip().splitlines()
        if not line.strip().startswith(LANGUAGE_RULE_PREFIXES)
    ]
    return "\n".join(lines).strip()


def user_language(db: Session, user: User) -> str:
    """账号里存的语言；没存过（跟浏览器）就回中文，前端会用请求里的 lang 覆盖。"""
    try:
        return normalize_language(ensure_settings(db, user.id).language)
    except Exception:  # pragma: no cover - 读设置失败不该挡住对话
        return "zh-CN"


def system_prompt(db: Session, user: User, lang: str | None = None) -> str:
    raw = settings_store.get_value(db, "ai_system_prompt")
    lang = normalize_language(lang or user_language(db, user))
    base = default_prompt(lang) if is_builtin_prompt(raw) else raw.strip()
    quota = user.quota_bytes or 0
    used = user.used_bytes or 0
    if lang == "en-US":
        context = (
            f"\n\n[Runtime info — do not read it aloud]\n"
            f"- Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- User nickname: {user.display_name or user.username}\n"
            f"- Drive capacity: {_fmt_size(used)} used / {_fmt_size(quota)}"
        )
    else:
        context = (
            f"\n\n[运行时信息，不要照念]\n"
            f"- 当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- 用户昵称：{user.display_name or user.username}\n"
            f"- 云盘容量：已用 {_fmt_size(used)} / {_fmt_size(quota)}"
        )
    # 长期记忆段放最后（且依赖 user_service，这里函数内 import 避免环）
    from .ai_memory import prompt_lines

    return base + context + prompt_lines(db, user.id, lang)


def read_prompt(db: Session) -> str:
    """管理员自定义的提示词；与内置一致（或为空）时返回空串，表示「用内置默认」。"""
    raw = settings_store.get_value(db, "ai_system_prompt")
    return "" if is_builtin_prompt(raw) else raw.strip()


# --------------------------------------------------------------------------- #
# 流式对话
# --------------------------------------------------------------------------- #
def stream_chat(
    user: User, cfg: dict, messages: list[dict], system: str, want_usage: bool = True
) -> Iterator[tuple[str, dict]]:
    """产出 (event, payload)。event: meta / delta / think / tool / error / done

    want_usage=False 时不向服务商索要 token 用量（走管理员默认接口时用），
    done 里的 usage 一律为 None，前端也就没得显示。
    """
    protocol = protocol_of(cfg)
    try:
        yield ("meta", {"model": cfg["model"], "protocol": protocol, "usage": want_usage})
        if protocol == "anthropic":
            yield from _stream_anthropic(user, cfg, messages, system, want_usage)
        else:
            yield from _stream_openai(user, cfg, messages, system, want_usage)
    except AiError as exc:
        yield ("error", {"message": exc.message})
    except GeneratorExit:  # 前端点了停止 / 关掉面板
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception("AI 流式对话失败")
        yield ("error", {"message": f"对话失败：{type(exc).__name__} {exc}"})


def _ask_payload(args: dict) -> dict | None:
    """解析 ask_user 的参数。问题为空视为调用不合规，交回普通工具报错路径。"""
    question = str(args.get("question") or "").strip()
    if not question:
        return None
    options: list[str] = []
    for option in args.get("options") or []:
        text = str(option).strip()
        if text and text not in options:
            options.append(text)
    return {"question": question[:400], "options": options[:6]}


def _repair_tool_pairs(messages: list[dict]) -> None:
    """发请求前兜底：assistant(tool_calls) 后必须紧跟每个 id 对应的 tool 消息。

    OpenAI / 兼容接口会严格校验这一段，中间夹一条 user 消息（或数量不足）就返回
    "insufficient tool messages following tool_calls message"。正常链路已按顺序拼好，
    这里只在真检测到缺口时把同批 tool 回复搬回来、缺的补一条占位，避免整个对话 400。
    就地修改 messages。
    """
    i = 0
    while i < len(messages):
        msg = messages[i]
        calls = msg.get("tool_calls") if isinstance(msg, dict) and msg.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            i += 1
            continue
        ids = [c.get("id") for c in calls if isinstance(c, dict) and c.get("id")]
        if not ids:
            i += 1
            continue
        j = i + 1
        got: set[str] = set()
        while j < len(messages) and messages[j].get("role") == "tool":
            got.add(messages[j].get("tool_call_id"))
            j += 1
        if all(cid in got for cid in ids):
            i = j
            continue
        replies: dict[str, dict] = {}
        rest: list[dict] = []
        for later in messages[i + 1 :]:
            cid = later.get("tool_call_id")
            if later.get("role") == "tool" and cid in ids and cid not in replies:
                replies[cid] = later
            else:
                rest.append(later)
        filled = [
            replies.get(cid) or {"role": "tool", "tool_call_id": cid, "content": "（工具结果缺失）"}
            for cid in ids
        ]
        messages[i + 1 :] = filled + rest
        i += 1 + len(ids)


def _stream_openai(
    user: User, cfg: dict, messages: list[dict], system: str, want_usage: bool = True
) -> Iterator[tuple[str, dict]]:
    _, chat_url, _ = endpoints(cfg["baseUrl"], "openai")
    headers = headers_for(cfg, "openai")
    history = trim_history(messages, system, cfg["maxInput"])
    convo = to_openai_messages(system, history, vision=cfg["vision"])
    specs = tool_specs(cfg)
    perms = {
        "read": bool(cfg.get("readContent")),
        "write": bool(cfg.get("writeFiles")),
        "move": bool(cfg.get("moveFiles")),
        "quick": bool(cfg.get("quickTransfer")),
        "delete": bool(cfg.get("deleteFiles")),
        "theme": bool(cfg.get("themeColor")),
        "vision": bool(cfg.get("vision")),
    }
    # 一轮对话里模型可能来回调几次工具，每次都是一次真实请求，用量要跨轮累加
    usage_total = {"input": 0, "output": 0}
    include_usage = want_usage

    unlimited, rounds = _round_budget(cfg)
    for round_index in range(rounds + 1):
        # 最后一轮不再给工具：让模型拿已有结果收尾，而不是忽然断掉
        use_tools = bool(specs) and round_index < rounds
        if specs and round_index == rounds:
            message = (
                f"已连续调用 {rounds} 轮工具，为避免死循环先按已有结果回答"
                if unlimited
                else f"工具调用已达上限（{rounds} 轮），这里先按已有结果回答"
            )
            yield ("notice", {"message": message})
        # 兜底：任何意外插进来的消息都别破坏 tool_calls → tool 回复的配对
        _repair_tool_pairs(convo)
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "messages": convo,
            "stream": True,
        }
        if cfg["maxOutput"]:
            payload["max_tokens"] = cfg["maxOutput"]
        if include_usage:
            # 标准 OpenAI 流式默认不回 usage，得显式要；少数兼容服务不认这个参数，
            # 被拒一次就退回「不要 usage」，不能为此把整轮对话搞崩
            payload["stream_options"] = {"include_usage": True}
        if use_tools:
            payload["tools"] = specs
            payload["tool_choice"] = "auto"
        if cfg["thinking"] and _reasoning_effort_ok(cfg):
            payload["reasoning_effort"] = "medium"

        calls: dict[int, dict] = {}
        text_seen = False
        while True:
            try:
                with _response(
                    chat_url,
                    headers=headers,
                    payload=payload,
                    method="POST",
                    timeout=HTTP_TIMEOUT,
                    stream=True,
                ) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("event:") or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj.get("error"), (dict, str)):
                            err = obj["error"]
                            message = err.get("message") if isinstance(err, dict) else str(err)
                            raise AiError(str(message or "接口返回错误"))
                        # 用量在最后那个 choices 为空的收尾块里，得赶在筛 choices 之前读走
                        usage = obj.get("usage")
                        if include_usage and isinstance(usage, dict):
                            usage_total["input"] += _as_int(usage.get("prompt_tokens"))
                            usage_total["output"] += _as_int(usage.get("completion_tokens"))
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0] if isinstance(choices[0], dict) else {}
                        delta = choice.get("delta") or choice.get("message") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield ("think", {"text": reasoning})
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            text_seen = True
                            yield ("delta", {"text": piece})
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index") or 0)
                            slot = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                            if call.get("id"):
                                slot["id"] = call["id"]
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                break
            except AiError as exc:
                # 服务商不认 stream_options：去掉这个参数把同一轮再发一次。
                # 这类 400 在开流前就返回了，所以不存在「已经吐了一半又重来」的问题。
                if include_usage and _mentions_usage_param(exc.message):
                    include_usage = False
                    payload.pop("stream_options", None)
                    calls.clear()
                    text_seen = False
                    continue
                raise

        if not calls or not use_tools:
            yield ("done", {"usage": _usage_of(usage_total), "hadText": text_seen})
            return

        ordered = [calls[i] for i in sorted(calls)]
        # 问答工具要在本轮把 assistant(tool_calls) 写进 convo 之前拦掉：
        # 一旦写了却拿不到 tool 回复，下游若有会话状态就会报「tool 消息不足」。
        # 同时它也意味着模型缺信息，这一轮其它工具没必要先跑一半。
        asking = None
        for call in ordered:
            if call["name"] == "ask_user":
                asking = _ask_payload(_safe_json(call["args"]))
                if asking:
                    break
        if asking:
            yield ("ask", asking)
            yield ("done", {"usage": _usage_of(usage_total), "hadText": text_seen})
            return

        convo.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call["id"] or f"call_{index}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["args"] or "{}"},
                    }
                    for index, call in enumerate(ordered)
                ],
            }
        )
        pending_images: list[dict] = []
        for index, call in enumerate(ordered):
            args = _safe_json(call["args"])
            label = TOOL_LABELS.get(call["name"], call["name"] or "工具")
            touch = call["name"] in WRITE_TOOL_NAMES
            # id 让前端把「同一轮的两个调用」和「不同轮的同一个工具」区分开
            call_id = call["id"] or call["name"]
            yield (
                "tool",
                {
                    "id": call_id,
                    "name": call["name"],
                    "label": label,
                    "status": "running",
                    "args": args,
                    "write": touch,
                },
            )
            result = run_tool_safe(user.id, call["name"], args, perms)
            yield (
                "tool",
                {
                    "id": call_id,
                    "name": call["name"],
                    "label": label,
                    "status": "done",
                    "result": result.text[:400],
                    "images": len(result.images),
                    # 改了界面偏好（主题色）时把新值带给前端，即时生效
                    "settings": result.settings,
                    "write": touch,
                },
            )
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{index}",
                    "content": result.text[:8000],
                }
            )
            # OpenAI 的 tool 消息装不下图片：先攒着，循环结束后统一补一条 user 消息
            if result.images:
                pending_images.extend(result.images)
        # 所有 tool 回复必须紧跟着 assistant(tool_calls)。图片若插在中间，
        # 上游会报 "insufficient tool messages following tool_calls message"。
        if pending_images:
            convo.append({"role": "user", "content": _openai_image_parts(pending_images)})


def _reasoning_effort_ok(cfg: dict) -> bool:
    """只有真正认得 reasoning_effort 的服务才带这个参数，否则会被 400 拒掉。"""
    base = (cfg.get("baseUrl") or "").lower()
    model = (cfg.get("model") or "").lower()
    if "openai.com" in base:
        return True
    return bool(re.match(r"^(o[1-9]|gpt-5)", model))


def _as_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _usage_of(total: dict) -> dict | None:
    """两个方向都是 0 就当作「服务商没给用量」，返回 None 而不是 0，前端据此不显示。"""
    got_input = int(total.get("input") or 0)
    got_output = int(total.get("output") or 0)
    if not got_input and not got_output:
        return None
    return {"input": got_input, "output": got_output, "total": got_input + got_output}


def _mentions_usage_param(message: str) -> bool:
    """服务商的报错是不是在说「不认识 stream_options / include_usage」。"""
    text = (message or "").lower()
    return any(token in text for token in ("stream_options", "include_usage"))


def _safe_json(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _stream_anthropic(
    user: User, cfg: dict, messages: list[dict], system: str, want_usage: bool = True
) -> Iterator[tuple[str, dict]]:
    _, chat_url, _ = endpoints(cfg["baseUrl"], "anthropic")
    headers = headers_for(cfg, "anthropic")
    history = trim_history(messages, system, cfg["maxInput"])
    system_text, convo = to_anthropic_messages(system, history, vision=cfg["vision"])
    specs = tool_specs(cfg)
    perms = {
        "read": bool(cfg.get("readContent")),
        "write": bool(cfg.get("writeFiles")),
        "move": bool(cfg.get("moveFiles")),
        "quick": bool(cfg.get("quickTransfer")),
        "delete": bool(cfg.get("deleteFiles")),
        "theme": bool(cfg.get("themeColor")),
        "vision": bool(cfg.get("vision")),
    }
    # 与 OpenAI 分支一样按轮累加：每次工具往返都是独立计费的一次请求
    usage_total = {"input": 0, "output": 0}
    count_usage = want_usage
    max_tokens = cfg["maxOutput"] or 4096

    unlimited, rounds = _round_budget(cfg)
    for round_index in range(rounds + 1):
        # 与 OpenAI 分支一致：最后一轮不给工具，逼它收尾
        use_tools = bool(specs) and round_index < rounds
        if specs and round_index == rounds:
            message = (
                f"已连续调用 {rounds} 轮工具，为避免死循环先按已有结果回答"
                if unlimited
                else f"工具调用已达上限（{rounds} 轮），这里先按已有结果回答"
            )
            yield ("notice", {"message": message})
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "system": system_text,
            "messages": convo,
            "stream": True,
        }
        if use_tools:
            payload["tools"] = anthropic_tools(specs)
        if cfg["thinking"]:
            budget = min(2048, max(1024, max_tokens - 512))
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(max_tokens, budget + 512)

        blocks: dict[int, dict] = {}
        stop_reason = ""
        text_seen = False
        text_buffer: list[str] = []

        with _response(
            chat_url,
            headers=headers,
            payload=payload,
            method="POST",
            timeout=HTTP_TIMEOUT,
            stream=True,
        ) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "error":
                    err = obj.get("error") or {}
                    raise AiError(str(err.get("message") or "接口返回错误"))
                if kind == "message_start":
                    # 输入用量（含提示缓存命中/写入的部分）只在开头给一次
                    if count_usage:
                        usage = (obj.get("message") or {}).get("usage") or {}
                        usage_total["input"] += (
                            _as_int(usage.get("input_tokens"))
                            + _as_int(usage.get("cache_read_input_tokens"))
                            + _as_int(usage.get("cache_creation_input_tokens"))
                        )
                    continue
                if kind == "content_block_start":
                    block = obj.get("content_block") or {}
                    index = int(obj.get("index") or 0)
                    if block.get("type") == "tool_use":
                        blocks[index] = {
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "args": "",
                        }
                    continue
                if kind == "content_block_delta":
                    index = int(obj.get("index") or 0)
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text_seen = True
                        text_buffer.append(delta["text"])
                        yield ("delta", {"text": delta["text"]})
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        yield ("think", {"text": delta["thinking"]})
                    elif delta.get("type") == "input_json_delta":
                        slot = blocks.setdefault(index, {"id": "", "name": "", "args": ""})
                        slot["args"] += delta.get("partial_json") or ""
                    continue
                if kind == "message_delta":
                    stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
                    # 这里的 output_tokens 是这条消息「累计到多少」，覆盖本轮而不是相加
                    if count_usage:
                        usage = obj.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            usage_total["output"] += _as_int(usage.get("output_tokens"))
                    continue

        if stop_reason != "tool_use" or not blocks or not use_tools:
            yield ("done", {"usage": _usage_of(usage_total) if count_usage else None, "hadText": text_seen})
            return

        ordered = [blocks[i] for i in sorted(blocks)]
        # 与 OpenAI 分支一致：问用户这件事在写进 convo 之前就结束本轮
        asking = None
        for call in ordered:
            if call["name"] == "ask_user":
                asking = _ask_payload(_safe_json(call["args"]))
                if asking:
                    break
        if asking:
            yield ("ask", asking)
            yield (
                "done",
                {"usage": _usage_of(usage_total) if count_usage else None, "hadText": text_seen},
            )
            return
        assistant_blocks: list[dict] = []
        if text_buffer:
            assistant_blocks.append({"type": "text", "text": "".join(text_buffer)})
        assistant_blocks.extend(
            {
                "type": "tool_use",
                "id": call["id"] or f"tool_{i}",
                "name": call["name"],
                "input": _safe_json(call["args"]),
            }
            for i, call in enumerate(ordered)
        )
        convo.append({"role": "assistant", "content": assistant_blocks})
        results = []
        for i, call in enumerate(ordered):
            args = _safe_json(call["args"])
            label = TOOL_LABELS.get(call["name"], call["name"] or "工具")
            touch = call["name"] in WRITE_TOOL_NAMES
            # id 让前端把「同一轮的两个调用」和「不同轮的同一个工具」区分开
            call_id = call["id"] or call["name"]
            yield (
                "tool",
                {
                    "id": call_id,
                    "name": call["name"],
                    "label": label,
                    "status": "running",
                    "args": args,
                    "write": touch,
                },
            )
            result = run_tool_safe(user.id, call["name"], args, perms)
            yield (
                "tool",
                {
                    "id": call_id,
                    "name": call["name"],
                    "label": label,
                    "status": "done",
                    "result": result.text[:400],
                    "images": len(result.images),
                    # 改了界面偏好（主题色）时把新值带给前端，即时生效
                    "settings": result.settings,
                    "write": touch,
                },
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"] or f"tool_{i}",
                    "content": _anthropic_tool_content(result),
                }
            )
        convo.append({"role": "user", "content": results})


# ---------------------------------------------------------------------------
# AI 取色：看用户的背景壁纸，挑一个主色或一组渐变色
# ---------------------------------------------------------------------------

ACCENT_MAX_SIDE = 768
ACCENT_MAX_INPUT_BYTES = 6 * 1024 * 1024
# 一次扫完、保持出现顺序（渐变的前后顺序就是模型想表达的方向）
_COLOR_RE = re.compile(
    r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b"
    r"|rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})",
    re.IGNORECASE,
)


def _parse_colors(text: str) -> list[str]:
    """从模型回复里挑出颜色，返回去重后的 #rrggbb 列表（保持出现顺序）。

    模型很少老实「只输出 JSON」：三位简写 #abc、rgb(12, 34, 56)、全角井号、
    外面再包一层解释文字，都见过。能认的全认掉，免得因为写法不同就报
    「模型没挑出能用的颜色」。
    """
    out: list[str] = []
    raw = (text or "").replace("＃", "#")
    for match in _COLOR_RE.finditer(raw):
        if match.group(1) is None:
            value = match.group(0)[1:].lower()
            if len(value) == 3:
                value = "".join(ch * 2 for ch in value)
            color = "#" + value
        else:
            r, g, b = (min(255, int(match.group(i))) for i in (1, 2, 3))
            color = "#%02x%02x%02x" % (r, g, b)
        if color not in out:
            out.append(color)
    return out[:5]


def normalize_accent_image(data_url: str) -> str:
    """把前端递来的背景图压成小图：省 token，也顺手挡掉非位图 / 超大图。

    返回可直接放进 messages.images 的 data URL；不合法就抛 AiError（文案给人看）。
    """
    url = (data_url or "").strip()
    if not url:
        return ""
    if len(url) > ACCENT_MAX_INPUT_BYTES:
        raise AiError("背景图太大了，换一张小一点的试试")
    parts = _data_url_parts(url)
    if not parts:
        raise AiError("背景图格式不认识，换一张 PNG / JPG 试试")
    _, raw = parts
    try:
        blob = base64.b64decode(raw, validate=False)
        with Image.open(io.BytesIO(blob)) as picture:
            picture = ImageOps.exif_transpose(picture)
            if picture.mode not in ("RGB", "L"):
                # 透明 PNG 铺白底，否则转 RGB 会得到黑块
                canvas = Image.new("RGB", picture.size, (255, 255, 255))
                canvas.paste(picture, mask=picture.split()[-1] if picture.mode in ("RGBA", "LA") else None)
                picture = canvas
            else:
                picture = picture.convert("RGB")
            picture.thumbnail((ACCENT_MAX_SIDE, ACCENT_MAX_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            picture.save(buffer, "JPEG", quality=82, optimize=True, progressive=True)
            data = buffer.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AiError("这张背景图读不出来，换一张试试") from None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _clean_candidates(candidates: Any) -> list[str]:
    out: list[str] = []
    for item in candidates or []:
        hex_color = str(item or "").strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", hex_color) and hex_color not in out:
            out.append(hex_color)
    return out[:8]


def _accent_prompt(mode: str, candidates: list[str], has_image: bool, lang: str) -> str:
    zh = not str(lang or "").lower().startswith("en")
    gradient = mode == "gradient"
    if zh:
        lines = ["你是配色助手，要给一个云盘管理界面挑主题色，这些颜色会用在按钮、开关、选中态和卡片底色上。"]
        if has_image:
            lines.append("下面这张就是用户自己设的背景壁纸，请顺着它的整体色调来挑。")
        if gradient:
            lines.append("请挑 2–4 个颜色，能顺着一个方向排成渐变：相邻颜色差别别太跳，整体是一个色系的过渡。")
        else:
            lines.append("请挑 1 个最有代表性的主色。")
        lines.append("必须是 #rrggbb 形式的 6 位十六进制；中等偏高饱和度，别接近纯黑或纯白，也别用纯灰。")
        if candidates:
            lines.append("这张图里已经被提取出来的候选色有：" + "、".join(candidates) + "，可以从中挑，也可以自己微调。")
        lines.append('只输出一个 JSON，形如 {"colors": ["#aabbcc", "#ddeeff"]}，不要任何解释。')
    else:
        lines = ["You are a color assistant picking an accent palette for a cloud-drive UI (buttons, toggles, selection, card tints)."]
        if has_image:
            lines.append("The image below is the user's own wallpaper; follow its overall tones.")
        if gradient:
            lines.append("Pick 2–4 colors that read as one smooth gradient — neighbouring shades should not jump.")
        else:
            lines.append("Pick the single most representative accent color.")
        lines.append("Use 6-digit hex like #rrggbb; medium-to-high saturation, not near black/white, no pure grey.")
        if candidates:
            lines.append("Colors already sampled from the image: " + ", ".join(candidates) + ". Pick from them or fine-tune.")
        lines.append('Answer with JSON only, e.g. {"colors": ["#aabbcc", "#ddeeff"]}. No prose.')
    return "\n".join(f"- {line}" if index else line for index, line in enumerate(lines))


def _run_text(cfg: dict, user: Any, messages: list[dict]) -> str:
    """跑一轮不带工具的对话，把正文拼起来。出错抛 AiError。"""
    chunks: list[str] = []
    for event, payload in stream_chat(user, cfg, messages, "", False):
        if event == "delta":
            chunks.append(payload.get("text") or "")
        elif event == "error":
            raise AiError(payload.get("message") or "调用模型失败")
        elif event == "done":
            break
    return "".join(chunks)


def suggest_accent(
    user: Any,
    cfg: dict,
    *,
    mode: str = "solid",
    image: str = "",
    candidates: Any = None,
    lang: str | None = None,
) -> dict:
    """让模型看图挑主题色。返回 ``{"mode", "colors", "source"}``。

    - 模型支持看图：把壁纸压成小图一起发过去
    - 不支持看图 / 带图这轮失败：退回「只给候选色」再问一次
    - 模型还是不吐颜色：用前端从图里采到的候选色兜底（source="sampled"），
      别让整个功能直接死在「换个模型再试试」上
    """
    mode = "gradient" if mode == "gradient" else "solid"
    picks = _clean_candidates(candidates)
    run_cfg = dict(cfg)
    # 这里别再卡 max_tokens：推理模型光思考就能把 400 个 token 烧完，
    # 正文一个字都吐不出来，最后只会得到「模型没挑出能用的颜色」
    run_cfg.update(tools=False, thinking=False, maxRounds=1, maxOutput=0)

    def ask(with_image: bool) -> list[str]:
        local = dict(run_cfg)
        # 只有前面真的带图时才在提示词里说「下面这张图」，
        # 否则模型会对着空气找图，直接回一句「我看不到图片」
        use_image = bool(with_image and image and cfg.get("vision"))
        local["vision"] = use_image
        prompt = _accent_prompt(mode, picks, use_image, lang or "")
        messages = [
            {
                "role": "user",
                "content": prompt,
                "images": [image] if use_image else [],
            }
        ]
        text = _run_text(local, user, messages)
        colors = _parse_colors(text)
        if not colors:
            logger.warning("AI 取色没解析出颜色，模型原文：%s", (text or "")[:400])
        return colors

    colors: list[str] = []
    if image and cfg.get("vision"):
        try:
            colors = ask(True)
        except AiError as exc:
            logger.warning("AI 取色（带图）失败，改问纯文本：%s", exc.message)
            colors = []
    if not colors:
        colors = ask(False)
    source = "model"
    if not colors and picks:
        # 模型没给出颜色，但图本身是采过色的 —— 先把能用的颜色给出去
        colors = picks[: (3 if mode == "gradient" else 5)]
        source = "sampled"
    if not colors:
        raise AiError("模型没挑出能用的颜色，换个模型再试试")
    if mode == "gradient" and len(colors) < 2:
        # 只回了一个色也不报错：前端会按同色系补一个相邻色
        mode = "solid"
    return {"mode": mode, "colors": colors[:5], "source": source}
