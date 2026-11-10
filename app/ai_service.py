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

from . import ai_defaults, node_service, secret_box, settings_store, storage
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
- **工具边界**：你只能使用系统实际提供给你的工具。没有开对应的权限（例如「查看文件内容」）时，就请管理员去「设置 → 看板娘 → 对话设置」里勾选；不要假装读过文件内容，也不要假装已经改动了文件。开了「查看文件内容」后，网盘里的图片可以用看图工具直接看，看到什么说什么，不要凭空猜画面。
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
- **Tool boundaries**: You may only use the tools the system actually provides. If a permission is off (for example "view file contents"), ask the Admin to enable it in Settings → Mascot → Chat settings. Never pretend you read a file's contents, and never pretend you already changed a file. Once "view file contents" is on, images in the drive can be opened with the image tool — describe only what you actually see, never guess.
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
    "vision": False,
    "thinking": False,
    "protocol": "auto",
    "maxInput": 0,  # 0 = 用服务商默认（不主动裁剪历史）
    "maxOutput": 0,  # 0 = 用服务商默认
    "maxRounds": 8,  # 一次对话里工具往返的轮次上限
}

PROTOCOLS = {"auto", "openai", "anthropic"}
PROVIDERS = {"", "openai", "deepseek", "custom"}

# 工具往返轮次上限：一轮里模型可以同时调多个工具，所以这不是「工具次数」。
# 到上限后不再给工具，让它用已有结果收尾，而不是把半截对话直接掐断。
MAX_TOOL_ROUNDS = 8
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
    cfg["vision"] = bool(cfg.get("vision", False))
    cfg["thinking"] = bool(cfg.get("thinking", False))
    cfg["maxInput"] = _clamp_int(cfg.get("maxInput"))
    cfg["maxOutput"] = _clamp_int(cfg.get("maxOutput"))
    cfg["maxRounds"] = _clamp_rounds(cfg.get("maxRounds"))
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
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MAX_TOOL_ROUNDS
    return max(1, min(50, number))


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
        "vision",
        "thinking",
        "protocol",
        "maxInput",
        "maxOutput",
        "maxRounds",
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
]

READ_TOOL_NAMES = {spec["function"]["name"] for spec in READ_TOOL_SPECS}

# 看图片需要模型本身支持视觉输入（多模态），否则给工具也看不懂
VISION_TOOL_NAMES = {"read_image"}

TOOL_LABELS = {
    "list_files": "翻看文件夹",
    "search_files": "搜索文件",
    "read_text": "读取文件",
    "read_image": "查看图片",
    "storage_usage": "查看容量",
    "create_folder": "新建文件夹",
    "write_file": "写入文件",
    "rename_item": "重命名",
    "copy_item": "复制",
    "move_item": "移动",
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

WRITE_TOOL_NAMES = {spec["function"]["name"] for spec in WRITE_TOOL_SPECS + MOVE_TOOL_SPECS}

# 工具需要的权限：read 需要「查看文件内容」，write/move 各自要用户勾选
TOOL_PERMISSION = {
    "read_text": "read",
    "read_image": "read",
    "create_folder": "write",
    "write_file": "write",
    "rename_item": "write",
    "copy_item": "move",
    "move_item": "move",
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

    if name == "storage_usage":
        used = user.used_bytes or 0
        quota = user.quota_bytes or 0
        percent = f"{used / quota * 100:.1f}%" if quota else "—"
        return f"已用 {_fmt_size(used)} / 总容量 {_fmt_size(quota)}（{percent}）"

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
    return base + context


def read_prompt(db: Session) -> str:
    """管理员自定义的提示词；与内置一致（或为空）时返回空串，表示「用内置默认」。"""
    raw = settings_store.get_value(db, "ai_system_prompt")
    return "" if is_builtin_prompt(raw) else raw.strip()


# --------------------------------------------------------------------------- #
# 流式对话
# --------------------------------------------------------------------------- #
def stream_chat(user: User, cfg: dict, messages: list[dict], system: str) -> Iterator[tuple[str, dict]]:
    """产出 (event, payload)。event: meta / delta / think / tool / error / done"""
    protocol = protocol_of(cfg)
    try:
        yield ("meta", {"model": cfg["model"], "protocol": protocol})
        if protocol == "anthropic":
            yield from _stream_anthropic(user, cfg, messages, system)
        else:
            yield from _stream_openai(user, cfg, messages, system)
    except AiError as exc:
        yield ("error", {"message": exc.message})
    except GeneratorExit:  # 前端点了停止 / 关掉面板
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception("AI 流式对话失败")
        yield ("error", {"message": f"对话失败：{type(exc).__name__} {exc}"})


def _stream_openai(
    user: User, cfg: dict, messages: list[dict], system: str
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
        "vision": bool(cfg.get("vision")),
    }

    rounds = cfg.get("maxRounds") or MAX_TOOL_ROUNDS
    for round_index in range(rounds + 1):
        # 最后一轮不再给工具：让模型拿已有结果收尾，而不是忽然断掉
        use_tools = bool(specs) and round_index < rounds
        if specs and round_index == rounds:
            yield ("notice", {"message": f"工具调用已达上限（{rounds} 轮），这里先按已有结果回答"})
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "messages": convo,
            "stream": True,
        }
        if cfg["maxOutput"]:
            payload["max_tokens"] = cfg["maxOutput"]
        if use_tools:
            payload["tools"] = specs
            payload["tool_choice"] = "auto"
        if cfg["thinking"] and _reasoning_effort_ok(cfg):
            payload["reasoning_effort"] = "medium"

        calls: dict[int, dict] = {}
        text_seen = False
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

        if not calls or not use_tools:
            yield ("done", {"usage": None, "hadText": text_seen})
            return

        ordered = [calls[i] for i in sorted(calls)]
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
            # OpenAI 的 tool 消息装不下图片：紧跟一条 user 消息把图递过去
            if result.images:
                convo.append({"role": "user", "content": _openai_image_parts(result.images)})


def _reasoning_effort_ok(cfg: dict) -> bool:
    """只有真正认得 reasoning_effort 的服务才带这个参数，否则会被 400 拒掉。"""
    base = (cfg.get("baseUrl") or "").lower()
    model = (cfg.get("model") or "").lower()
    if "openai.com" in base:
        return True
    return bool(re.match(r"^(o[1-9]|gpt-5)", model))


def _safe_json(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _stream_anthropic(
    user: User, cfg: dict, messages: list[dict], system: str
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
        "vision": bool(cfg.get("vision")),
    }
    max_tokens = cfg["maxOutput"] or 4096

    rounds = cfg.get("maxRounds") or MAX_TOOL_ROUNDS
    for round_index in range(rounds + 1):
        # 与 OpenAI 分支一致：最后一轮不给工具，逼它收尾
        use_tools = bool(specs) and round_index < rounds
        if specs and round_index == rounds:
            yield ("notice", {"message": f"工具调用已达上限（{rounds} 轮），这里先按已有结果回答"})
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
                    continue

        if stop_reason != "tool_use" or not blocks or not use_tools:
            yield ("done", {"usage": None, "hadText": text_seen})
            return

        ordered = [blocks[i] for i in sorted(blocks)]
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
