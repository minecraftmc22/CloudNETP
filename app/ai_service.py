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
import json
import logging
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import node_service, settings_store, storage
from .config import settings
from .models import Node, User
from .user_service import ensure_settings

logger = logging.getLogger("uvicorn.error").getChild("ai")

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
DEFAULT_SYSTEM_PROMPT = """你是 CloudNETP 云盘的看板娘「CDP娘」，住在用户的云端硬盘里。

说话温和、简短、自然，自称「CDP娘」，不用「您」。先回答问题，不要客套。

你熟悉这个云盘：可以上传文件、整理文件夹、生成分享链接、在线预览图片/文档/音乐、
用 WebDAV 挂载到资源管理器。用户问到自己文件的事，先调用工具查清楚再回答，
不要凭空编文件名或大小。涉及删除、覆盖这类操作，提醒用户到界面上亲自操作，
不要声称你已经替他做了。

回答用中文（用户换语言时跟着换），别用花哨排版，能一句话说清就别写三段。"""

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
    "vision": False,
    "thinking": False,
    "protocol": "auto",
    "maxInput": 0,  # 0 = 用服务商默认（不主动裁剪历史）
    "maxOutput": 0,  # 0 = 用服务商默认
}

PROTOCOLS = {"auto", "openai", "anthropic"}
PROVIDERS = {"", "openai", "deepseek", "custom"}

MAX_TOOL_ROUNDS = 3
HTTP_TIMEOUT = 180
PROBE_TIMEOUT = 20


class AiError(Exception):
    """给前端看的错误：消息已经是人话，直接弹出来即可。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# 密钥加解密
# --------------------------------------------------------------------------- #
_FERNET = None


def _fernet():
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet

        digest = hashlib.sha256(f"cloudnetp-ai:{settings.SECRET_KEY}".encode()).digest()
        _FERNET = Fernet(base64.urlsafe_b64encode(digest))
    return _FERNET


def encrypt_key(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return "enc:" + _fernet().encrypt(text.encode()).decode()
    except Exception:  # pragma: no cover - 加密库异常时退化成明文，别丢用户配置
        logger.warning("AI 密钥加密失败，改为明文保存", exc_info=True)
        return "raw:" + text


def decrypt_key(stored: str) -> str:
    text = (stored or "").strip()
    if not text:
        return ""
    if text.startswith("raw:"):
        return text[4:]
    if not text.startswith("enc:"):
        return text
    try:
        return _fernet().decrypt(text[4:].encode()).decode()
    except Exception:
        # SECRET_KEY 变了（例如临时密钥重启）：当作没配置，让用户重填
        logger.warning("AI 密钥解密失败（SECRET_KEY 可能已变），请重新填写")
        return ""


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
    cfg["vision"] = bool(cfg.get("vision", False))
    cfg["thinking"] = bool(cfg.get("thinking", False))
    cfg["maxInput"] = _clamp_int(cfg.get("maxInput"))
    cfg["maxOutput"] = _clamp_int(cfg.get("maxOutput"))
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


def public_config(db: Session, user_id: int) -> dict:
    cfg = load_config(db, user_id)
    key = cfg.pop("key", "")
    cfg["hasKey"] = bool(key)
    cfg["keyMask"] = mask_key(key)
    cfg["ready"] = bool(cfg["baseUrl"] and key and cfg["model"])
    cfg["presets"] = PRESETS
    return cfg


def save_config(db: Session, user_id: int, patch: dict) -> dict:
    row = ensure_settings(db, user_id)
    stored = dict(row.ai_config or {})
    for field in (
        "provider",
        "baseUrl",
        "model",
        "tools",
        "vision",
        "thinking",
        "protocol",
        "maxInput",
        "maxOutput",
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
            "name": "storage_usage",
            "description": "查询当前用户云盘的容量与已用空间。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_LABELS = {
    "list_files": "翻看文件夹",
    "search_files": "搜索文件",
    "read_text": "读取文件",
    "storage_usage": "查看容量",
}

ANTHROPIC_TOOLS = [
    {
        "name": spec["function"]["name"],
        "description": spec["function"]["description"],
        "input_schema": spec["function"]["parameters"],
    }
    for spec in TOOL_SPECS
]


def anthropic_tools() -> list[dict]:
    return ANTHROPIC_TOOLS


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


def _run_tool(db: Session, user: User, name: str, args: dict) -> str:
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
        return f"《{node.name}》内容：\n{text}"

    if name == "storage_usage":
        used = user.used_bytes or 0
        quota = user.quota_bytes or 0
        percent = f"{used / quota * 100:.1f}%" if quota else "—"
        return f"已用 {_fmt_size(used)} / 总容量 {_fmt_size(quota)}（{percent}）"

    raise ToolError(f"没有这个工具：{name}")


def run_tool_safe(user_id: int, name: str, args: dict) -> str:
    """工具执行入口：自带会话，出错返回给模型的错误文本，不往上抛。"""
    from .db_failover import current_sessionmaker

    try:
        with current_sessionmaker()() as db:
            user = db.get(User, user_id)
            if user is None:
                return "用户不存在。"
            return _run_tool(db, user, name, args)
    except ToolError as exc:
        return f"工具调用失败：{exc}"
    except Exception as exc:  # pragma: no cover - 任何意外都别打断对话
        logger.warning("AI 工具执行异常 %s: %s", name, exc)
        return f"工具调用失败：{type(exc).__name__}"


# --------------------------------------------------------------------------- #
# 系统提示词
# --------------------------------------------------------------------------- #
def system_prompt(db: Session, user: User) -> str:
    base = settings_store.get_value(db, "ai_system_prompt").strip() or DEFAULT_SYSTEM_PROMPT
    quota = user.quota_bytes or 0
    used = user.used_bytes or 0
    context = (
        f"\n\n[运行时信息，不要照念]\n"
        f"- 当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"- 用户昵称：{user.display_name or user.username}\n"
        f"- 云盘容量：已用 {_fmt_size(used)} / {_fmt_size(quota)}"
    )
    return base + context


def read_prompt(db: Session) -> str:
    return settings_store.get_value(db, "ai_system_prompt")


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
    tools_on = bool(cfg["tools"])

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "messages": convo,
            "stream": True,
        }
        if cfg["maxOutput"]:
            payload["max_tokens"] = cfg["maxOutput"]
        if tools_on:
            payload["tools"] = TOOL_SPECS
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

        if not calls or round_index >= MAX_TOOL_ROUNDS:
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
            yield ("tool", {"name": call["name"], "label": label, "status": "running", "args": args})
            result = run_tool_safe(user.id, call["name"], args)
            yield ("tool", {"name": call["name"], "label": label, "status": "done", "result": result[:400]})
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{index}",
                    "content": result[:8000],
                }
            )


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
    tools_on = bool(cfg["tools"])
    max_tokens = cfg["maxOutput"] or 4096

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "system": system_text,
            "messages": convo,
            "stream": True,
        }
        if tools_on:
            payload["tools"] = anthropic_tools()
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

        if stop_reason != "tool_use" or not blocks or round_index >= MAX_TOOL_ROUNDS:
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
            yield ("tool", {"name": call["name"], "label": label, "status": "running", "args": args})
            result = run_tool_safe(user.id, call["name"], args)
            yield ("tool", {"name": call["name"], "label": label, "status": "done", "result": result[:400]})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"] or f"tool_{i}",
                    "content": result[:8000],
                }
            )
        convo.append({"role": "user", "content": results})
