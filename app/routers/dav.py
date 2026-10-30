"""WebDAV 端点：把 nodes 树暴露成标准 WebDAV 目录。

挂载在 /dav，用 HTTP Basic（账号 = 用户名，密码 = 登录密码）认证，
可被 Windows 资源管理器、macOS Finder、RaiDrive、Documents 等直接挂载。

性能与安全的两条底线：
- scrypt 校验约 50ms，而 WebDAV 客户端每发一个请求都会带凭据，
  所以加了「凭据摘要 → 用户」的内存缓存（10 分钟，上限 512 条），
  密码变更会让 token_version 递增，缓存自然失效；
- 认证失败单独限流，避免被当成密码爆破入口。
"""

from __future__ import annotations

import base64
import hashlib
import html
import mimetypes
import re
import time
from collections import OrderedDict, defaultdict, deque
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import dav_auth as da
from .. import dav_service as ds
from .. import node_service as ns
from .. import storage
from ..config import settings
from ..models import Node, User, utcnow
from ..db import get_db
from ..security import client_ip, verify_password
from ..settings_store import get_int
from ..user_service import ensure_settings, log_action

router = APIRouter(tags=["webdav"])

ALLOWED = (
    "OPTIONS, HEAD, GET, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK"
)
METHODS = [m.strip() for m in ALLOWED.split(",")]

_CACHE_TTL = 600
_CACHE_MAX = 512
_cred_cache: OrderedDict[str, tuple[int, int, float]] = OrderedDict()

_FAIL_WINDOW = 300
_FAIL_LIMIT = 10
_failures: dict[str, deque[float]] = defaultdict(deque)


class MultiHeaders:
    """允许出现重复响应头（同名的多个 WWW-Authenticate）。

    Starlette 这版 Response.init_headers 只认 .items()，所以给这一个小壳子。
    """

    __slots__ = ("_pairs",)

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self):
        return self._pairs

    def get(self, key: str, default=None):
        low = key.lower()
        for k, v in self._pairs:
            if k.lower() == low:
                return v
        return default


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
def _cache_key(username: str, password: str) -> str:
    raw = f"{settings.SECRET_KEY}|dav|{username.lower()}|{password}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _basic(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value.strip():
        return None
    try:
        raw = base64.b64decode(value.strip(), validate=False)
    except (ValueError, TypeError):
        return None
    text = raw.decode("utf-8", "replace")
    username, sep, password = text.partition(":")
    if not sep:
        return None
    # Windows 可能把域一起塞进来：DOMAIN\user
    return username.split("\\")[-1].strip(), password


def _unauthorized(message: str = "需要登录") -> HTTPException:
    """401 同时给出 Digest 与 Basic。

    Digest 必须排前面：Windows 的 WebClient 默认 BasicAuthLevel=1，
    对 http:// 上的 Basic challenge 会直接放弃（资源管理器报 0x80070043），
    看到 Digest 才会正常带凭据重发。第三方客户端不支持 Digest 时仍走 Basic。
    """
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        message,
        headers=MultiHeaders(
            [
                ("WWW-Authenticate", da.challenge()),
                ("WWW-Authenticate", 'Basic realm="CloudNETP WebDAV", charset="UTF-8"'),
                ("DAV", "1, 2"),
            ]
        ),
    )


def _guard(username: str, request: Request) -> None:
    key = f"{client_ip(request)}|{username.lower()}"
    now = time.time()
    queue = _failures[key]
    while queue and now - queue[0] > _FAIL_WINDOW:
        queue.popleft()
    if len(queue) >= _FAIL_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "认证失败次数过多，请 5 分钟后再试",
            headers=MultiHeaders(
                [
                    ("WWW-Authenticate", da.challenge()),
                    ("WWW-Authenticate", 'Basic realm="CloudNETP WebDAV", charset="UTF-8"'),
                ]
            ),
        )


def _request_uris(request: Request) -> set[str]:
    """Digest 摘要里的 uri 要和客户端算的一致。

    客户端可能用原始百分号编码（/dav/%E4%B8%AD），也可能是解码后的路径，
    这里把两种形态都放进白名单；uri 只参与摘要计算，不用于定位资源，放宽无风险。
    """
    uris = {request.url.path}
    raw = request.scope.get("raw_path") or b""
    if raw:
        for codec in ("latin-1", "utf-8"):
            try:
                uris.add(raw.decode(codec))
            except UnicodeDecodeError:
                continue
    query = request.url.query
    if query:
        uris |= {f"{uri}?{query}" for uri in list(uris)}
    return uris


def _seed_ha1(db: Session, user: User, password: str) -> None:
    """Basic 登录成功时顺手补上 Digest 用的 HA1，老账号无需专门迁移。"""
    if user.dav_ha1:
        return
    user.dav_ha1 = da.ha1(user.username, password)
    db.commit()


def _digest_user(request: Request, db: Session, header: str) -> User:
    data = da.parse(header) or {}
    raw_name = (data.get("username") or "").split("\\")[-1].strip()
    if not raw_name:
        raise _unauthorized("用户名格式不正确")
    username = raw_name.lower()
    throttle_key = f"{client_ip(request)}|{username}"
    _guard(username, request)

    user = db.scalar(select(User).where(func.lower(User.username) == username))
    # 不存在 / 没初始化 HA1 一律回同一句，避免被用来探测用户名
    if user is None or not user.dav_ha1:
        _failures[throttle_key].append(time.time())
        raise _unauthorized("用户名或密码错误")
    if user.banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该账号已被封禁")

    ok, why = da.verify(header, request.method, _request_uris(request), username, user.dav_ha1)
    if not ok:
        _failures[throttle_key].append(time.time())
        raise _unauthorized(why or "认证失败")
    _failures.pop(throttle_key, None)
    return user


def dav_user(request: Request, db: Session = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    if header[:6].lower() == "digest":
        return _digest_user(request, db, header)

    credentials = _basic(request)
    if credentials is None:
        raise _unauthorized()
    username, password = credentials

    key = _cache_key(username, password)
    now = time.time()
    cached = _cred_cache.get(key)
    if cached and cached[2] > now:
        user = db.get(User, cached[0])
        # token_version 变了说明改过密码或被踢过，缓存作废重验
        if user and not user.banned and user.token_version == cached[1]:
            _cred_cache.move_to_end(key)
            _seed_ha1(db, user, password)
            return user

    throttle_key = f"{client_ip(request)}|{username.lower()}"
    _guard(username, request)

    user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None or not verify_password(password, user.password_hash):
        _failures[throttle_key].append(time.time())
        raise _unauthorized("用户名或密码错误")
    if user.banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该账号已被封禁")

    _failures.pop(throttle_key, None)
    _cred_cache[key] = (user.id, user.token_version, now + _CACHE_TTL)
    _cred_cache.move_to_end(key)
    while len(_cred_cache) > _CACHE_MAX:
        _cred_cache.popitem(last=False)
    _seed_ha1(db, user, password)
    return user


def _base(request: Request) -> str:
    return (request.scope.get("root_path") or "") + "/dav"


def _xml(payload: str, code: int = 200) -> Response:
    return Response(
        content=payload,
        status_code=code,
        media_type='application/xml; charset="utf-8"',
        headers={"DAV": "1, 2"},
    )


def _load(request: Request, db: Session, user: User, segments: list[str]) -> Node | None:
    _, node, _ = ds.resolve(db, user.id, segments)
    return node


def _listing(
    db: Session, user: User, node: Node | None, segments: list[str], base: str
) -> Response:
    """浏览器直接访问目录时给一张可点的清单，方便排障（客户端不会走这里）。"""
    items = ds.children(db, user.id, node.id if node else None)
    rows = []
    for item in items:
        label = html.escape(item.name + ("/" if item.type == "folder" else ""))
        href = html.escape(ds.href_for(base, [*segments, item.name], item.type == "folder"))
        rows.append(f'<li><a href="{href}">{label}</a></li>')
    title = html.escape("/" + "/".join(segments) if segments else "我的文件")
    body = "".join(rows) or '<li class="empty">（空目录）</li>'
    page = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{title} · WebDAV</title>"
        "<style>body{font:14px/1.7 system-ui,'Segoe UI',sans-serif;padding:32px;color:#2b2b33}"
        "h1{font-size:18px;margin:0 0 12px}ul{padding-left:18px;margin:0}"
        "li{margin:2px 0}a{color:#7c3aed;text-decoration:none}a:hover{text-decoration:underline}</style>"
        f"<h1>{title}</h1><ul>{body}</ul>"
        '<p style="margin-top:18px;color:#8a8a99;font-size:12px">'
        "这是 WebDAV 目录，用文件管理器挂载可读写。</p>"
    )
    return Response(content=page, media_type="text/html; charset=utf-8")


# --------------------------------------------------------------------------- #
# 方法实现
# --------------------------------------------------------------------------- #
def _options() -> Response:
    return Response(
        status_code=200,
        headers={"DAV": "1, 2", "MS-Author-Via": "DAV", "Allow": ALLOWED, "Cache-Control": "no-store"},
    )


async def _propfind(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    raw = (request.headers.get("depth") or "1").strip().lower()
    if raw == "infinity":
        depth = ds.MAX_DEPTH
    else:
        try:
            depth = int(raw)
        except ValueError:
            depth = 1
    depth = max(0, min(depth, ds.MAX_DEPTH))

    node = _load(request, db, user, segments)
    if segments and node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")

    entries = ds.walk(db, user.id, node, segments, depth, ds.quota_of(user), _base(request))
    return _xml(ds.multistatus(entries), 207)


async def _proppatch(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    node = _load(request, db, user, segments)
    if segments and node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")

    body = (await request.body()).decode("utf-8", "replace")
    block = body
    start = body.find("<prop")
    if start >= 0:
        block = body[start:]
    names = [m.group(1) for m in re.finditer(r"<\s*(?:[\w.-]+:)?([\w.-]+)\s*/>", block)]
    # displayname 之类没写自闭合的也兜一下
    names += [m.group(1) for m in re.finditer(r"<\s*(?:[\w.-]+:)?([\w.-]+)\s*>\s*</", block)]
    props = "".join(f"<D:{html.escape(n)}/>" for n in dict.fromkeys(names) if ":" not in n) or "<D:displayname/>"
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:"><D:response>'
        f"<D:href>{html.escape(ds.href_for(_base(request), segments, node is None or node.type == 'folder'))}</D:href>"
        f"<D:propstat><D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response></D:multistatus>"
    )
    return _xml(payload, 207)


async def _get(request: Request, db: Session, user: User, segments: list[str], *, head: bool) -> Response:
    node = _load(request, db, user, segments)

    if not segments or (node is not None and node.type == "folder"):
        if head:
            return Response(status_code=200, headers={"Content-Type": "text/html; charset=utf-8"})
        return _listing(db, user, node, segments, _base(request))

    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    if not node.blob_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件内容缺失")

    path = storage.blob_path(node.blob_sha256)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件内容缺失，可能已被清理")

    mime = node.mime or mimetypes.guess_type(node.name)[0] or "application/octet-stream"
    headers = {
        "Content-Type": mime,
        "Content-Length": str(path.stat().st_size),
        "Accept-Ranges": "bytes",
        "ETag": ds.etag(node),
        "Last-Modified": ds.http_date(node.updated_at),
        "Cache-Control": "private, max-age=0",
        "X-Content-Type-Options": "nosniff",
    }
    if head:
        return Response(status_code=200, headers=headers)
    response = storage.file_response(path, node.name, mime, request)
    response.headers["ETag"] = ds.etag(node)
    response.headers["Last-Modified"] = ds.http_date(node.updated_at)
    return response


async def _put(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    if not segments:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "不能上传到根路径")
    name = ds.valid_name(segments[-1])

    try:
        _, parent, _ = ds.resolve(db, user.id, segments[:-1])
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status.HTTP_409_CONFLICT, "上级目录不存在") from None
        raise
    if parent is not None and parent.type != "folder":
        raise HTTPException(status.HTTP_409_CONFLICT, "上级路径不是文件夹")

    _, existing, _ = ds.resolve(db, user.id, segments)
    if existing is not None and existing.type == "folder":
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "同名资源是文件夹")

    old_size = existing.size if existing is not None else 0
    max_size = get_int(db, "max_file_size_bytes") or settings.max_file_size_bytes
    quota = user.quota_bytes
    allowance = quota - user.used_bytes + old_size
    if allowance <= 0:
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE, "存储空间不足，请清理回收站或联系管理员扩容"
        )

    tmp = storage.new_tmp_path()
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as handle:
            async for block in request.stream():
                if not block:
                    continue
                size += len(block)
                if size > max_size:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
                    )
                if size > allowance:
                    raise HTTPException(
                        status.HTTP_507_INSUFFICIENT_STORAGE,
                        "存储空间不足，请清理回收站或联系管理员扩容",
                    )
                digest.update(block)
                handle.write(block)
        sha256 = digest.hexdigest()
        storage.commit_tmp(tmp, sha256)
    finally:
        tmp.unlink(missing_ok=True)

    header_mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not header_mime or header_mime in ("application/octet-stream", "application/x-www-form-urlencoded"):
        header_mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

    node = ds.make_file(
        db,
        user,
        parent.id if parent else None,
        name,
        sha256,
        size,
        header_mime[:127],
        replace=existing,
    )
    ns.recalc_usage(db, user)
    log_action(db, "dav_upload", user.id, {"name": name, "size": size}, request)
    db.commit()

    code = status.HTTP_201_CREATED if existing is None else status.HTTP_204_NO_CONTENT
    stamp = utcnow()
    return Response(
        status_code=code,
        headers={"ETag": f'"{sha256[:20]}-{size}"', "Last-Modified": ds.http_date(stamp)},
    )


async def _delete(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    if not segments:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "不能删除根目录")
    node = _load(request, db, user, segments)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")

    trash_on = ensure_settings(db, user.id).trash_enabled
    if trash_on:
        removed = ns.soft_delete(db, user, node)
        log_action(db, "dav_delete", user.id, {"name": node.name, "items": removed}, request)
    else:
        removed = ns.purge_node(db, user, node)
        log_action(
            db, "dav_delete_permanent", user.id, {"name": node.name, "items": removed}, request
        )
    ns.recalc_usage(db, user)
    db.commit()
    return Response(status_code=204)


async def _mkcol(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    if not segments:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "根目录已存在")
    name = ds.valid_name(segments[-1])
    parent, node, _ = ds.resolve(db, user.id, segments)
    if node is not None:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "同名资源已存在")
    if parent is not None and parent.type != "folder":
        raise HTTPException(status.HTTP_409_CONFLICT, "上级路径不是文件夹")

    folder = ds.make_folder(db, user, parent.id if parent else None, name)
    log_action(db, "dav_mkcol", user.id, {"name": name}, request)
    db.commit()
    href = ds.href_for(_base(request), segments, True)
    return Response(status_code=201, headers={"Location": href})


def _destination(request: Request, user: User, db: Session) -> list[str]:
    raw = (request.headers.get("destination") or "").strip()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 Destination 头")
    path = urlsplit(raw).path if "://" in raw else raw
    path = unquote(path, encoding="utf-8", errors="replace")
    base = _base(request)
    if path.startswith(base):
        path = path[len(base) :]
    segments = ds.split_path(path)
    if not segments:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标路径无效")
    return segments


def _prepare_destination(
    request: Request, db: Session, user: User, source: Node, segments: list[str], dest: list[str]
) -> tuple[int | None, Node | None]:
    """校验目标路径并处理 Overwrite，返回 (目标父 id, 被覆盖的节点)。"""
    if dest == segments:
        return source.parent_id, source
    # resolve 返回 (父, 自身, 名称)，这里要的是「目标的父目录」= 第二个值
    _, parent, _ = ds.resolve(db, user.id, dest[:-1])
    if parent is not None and parent.type != "folder":
        raise HTTPException(status.HTTP_409_CONFLICT, "目标上级不是文件夹")
    # 不能把文件夹搬/拷进自己内部（必须在覆盖之前判断，否则先删后报错）
    if source.type == "folder" and parent is not None:
        if parent.id == source.id or parent.id in ns.ancestor_ids(db, user.id, source.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "不能把文件夹放进它自己里面")
    _, target, _ = ds.resolve(db, user.id, dest)
    if target is not None:
        overwrite = (request.headers.get("overwrite") or "T").strip().upper() != "F"
        if not overwrite:
            raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "目标已存在且禁止覆盖")
        if target.id == source.id:
            return source.parent_id, source
        ns.purge_node(db, user, target)
        ns.recalc_usage(db, user)
    return parent.id if parent else None, target


async def _move(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    if not segments:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "不能移动根目录")
    source = _load(request, db, user, segments)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "源资源不存在")

    dest = _destination(request, user, db)
    if dest == segments:
        return Response(status_code=204)

    dest_parent_id, target = _prepare_destination(request, db, user, source, segments, dest)
    code = status.HTTP_204_NO_CONTENT if target is not None else status.HTTP_201_CREATED

    source.parent_id = dest_parent_id
    source.name = ds.valid_name(dest[-1])
    source.updated_at = utcnow()
    log_action(db, "dav_move", user.id, {"from": "/".join(segments), "to": "/".join(dest)}, request)
    db.commit()
    return Response(status_code=code)


async def _copy(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    source = _load(request, db, user, segments)
    if segments and source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "源资源不存在")
    if source is None:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "不支持复制根目录")

    dest = _destination(request, user, db)
    if dest == segments:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "源和目标是同一个资源")
    dest_parent_id, target = _prepare_destination(request, db, user, source, segments, dest)
    code = status.HTTP_204_NO_CONTENT if target is not None else status.HTTP_201_CREATED

    ds.copy_subtree(db, user, source, dest_parent_id, ds.valid_name(dest[-1]))
    ns.recalc_usage(db, user)
    log_action(db, "dav_copy", user.id, {"from": "/".join(segments), "to": "/".join(dest)}, request)
    db.commit()
    return Response(status_code=code)


async def _lock(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    parent, node, _ = ds.resolve(db, user.id, segments)
    if node is None and parent is None and segments:
        raise HTTPException(status.HTTP_409_CONFLICT, "上级目录不存在")
    await request.body()  # 可能是空的（锁刷新），读完避免连接挂住

    depth = (request.headers.get("depth") or "infinity").strip().lower()
    token = ds.acquire_lock(user.id, segments)
    href = ds.href_for(_base(request), segments, node is None or node.type == "folder")
    response = _xml(ds.lock_xml(token, href, depth), 200)
    response.headers["Lock-Token"] = f"<{token}>"
    response.headers["Timeout"] = f"Second-{ds.LOCK_TTL}"
    return response


async def _unlock(request: Request, db: Session, user: User, segments: list[str]) -> Response:
    token = request.headers.get("lock-token") or ""
    if not ds.release_lock(user.id, segments, token):
        raise HTTPException(status.HTTP_409_CONFLICT, "锁令牌不匹配")
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
async def _dispatch(request: Request, path: str, db: Session) -> Response:
    user = dav_user(request, db)
    segments = ds.split_path(path)
    method = request.method.upper()

    if method == "OPTIONS":
        return _options()
    if method == "PROPFIND":
        return await _propfind(request, db, user, segments)
    if method == "PROPPATCH":
        return await _proppatch(request, db, user, segments)
    if method in ("GET", "HEAD"):
        return await _get(request, db, user, segments, head=method == "HEAD")
    if method == "PUT":
        return await _put(request, db, user, segments)
    if method == "DELETE":
        return await _delete(request, db, user, segments)
    if method == "MKCOL":
        return await _mkcol(request, db, user, segments)
    if method == "MOVE":
        return await _move(request, db, user, segments)
    if method == "COPY":
        return await _copy(request, db, user, segments)
    if method == "LOCK":
        return await _lock(request, db, user, segments)
    if method == "UNLOCK":
        return await _unlock(request, db, user, segments)
    raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "不支持的方法")


@router.api_route("/dav", methods=METHODS, include_in_schema=False)
async def dav_root(request: Request, db: Session = Depends(get_db)) -> Response:
    return await _dispatch(request, "", db)


@router.api_route("/dav/{path:path}", methods=METHODS, include_in_schema=False)
async def dav_path(request: Request, path: str, db: Session = Depends(get_db)) -> Response:
    return await _dispatch(request, path, db)
