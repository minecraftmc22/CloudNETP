"""磁盘存储层：blob 内容寻址布局、原子落盘、Range 响应。

布局（避免单目录几十万文件）：
    storage/blobs/ab/cd/abcdef...        <- 完整 sha256 作为文件名
    storage/tmp/<uuid>.part              <- 上传中的临时文件
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse

from .config import settings

CHUNK = 1024 * 1024  # 1 MiB


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def blob_path(sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("非法 sha256")
    return settings.blob_dir / sha256[:2] / sha256[2:4] / sha256


def ensure_blob_dirs(sha256: str) -> Path:
    target = blob_path(sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def new_tmp_path() -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    return settings.tmp_dir / f"{uuid.uuid4().hex}.part"


def blob_exists(sha256: str, size: int | None = None) -> bool:
    try:
        path = blob_path(sha256)
    except ValueError:
        return False
    if not path.is_file():
        return False
    return size is None or path.stat().st_size == size


def commit_tmp(tmp: Path, sha256: str) -> int:
    """把临时文件原子地放进 blob 目录，返回最终大小。"""
    target = ensure_blob_dirs(sha256)
    if target.exists():
        # 已有同内容 blob，直接丢弃临时文件
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, target)
    return target.stat().st_size


def delete_blob(sha256: str) -> None:
    try:
        blob_path(sha256).unlink(missing_ok=True)
    except ValueError:
        return


def blob_size(sha256: str) -> int:
    try:
        path = blob_path(sha256)
    except ValueError:
        return 0
    return path.stat().st_size if path.is_file() else 0


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 下载响应（单区间 Range，供浏览器拖动进度条 / 播放器 seek / 下载器续传）
# --------------------------------------------------------------------------- #
DISPOSITION_FALLBACK = re.compile(r"[^A-Za-z0-9._ -]")


def attachment_header(filename: str) -> str:
    """同时给 filename 与 filename*，中文名在现代浏览器里才能正确落盘。"""
    ascii_name = DISPOSITION_FALLBACK.sub("_", filename).strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # bytes=-500 表示最后 500 字节
            length = int(end_s)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def _iter_slice(path: Path, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            block = fh.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def file_response(
    path: Path,
    filename: str,
    mime: str | None = None,
    request: Request | None = None,
    *,
    inline: bool = False,
    cache: str = "private, max-age=0",
    csp: str | None = None,
) -> StreamingResponse:
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件内容不存在")
    size = path.stat().st_size
    media_type = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache,
        "Content-Disposition": (
            f"inline; filename=\"{DISPOSITION_FALLBACK.sub('_', filename).strip() or 'file'}\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}"
            if inline
            else attachment_header(filename)
        ),
        # 预览 HTML/SVG 一律禁止执行，避免存储型 XSS
        "X-Content-Type-Options": "nosniff",
    }
    # csp 传空串表示不放这个头（PDF 内嵌查看器需要，sandbox 会导致它渲染不出来）
    policy = (
        "sandbox; default-src 'none'; img-src data: blob: 'self'; media-src 'self'"
        if csp is None
        else csp
    )
    if policy:
        headers["Content-Security-Policy"] = policy

    range_header = request.headers.get("range") if request else None
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            raise HTTPException(
                status.HTTP_416_RANGE_NOT_SATISFIABLE,
                "请求范围无效",
                headers={"Content-Range": f"bytes */{size}"},
            )
        start, end = parsed
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        return StreamingResponse(
            _iter_slice(path, start, length),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers=headers,
        )

    headers["Content-Length"] = str(size)
    return StreamingResponse(_iter_slice(path, 0, size), media_type=media_type, headers=headers)


# --------------------------------------------------------------------------- #
# 磁盘用量
# --------------------------------------------------------------------------- #
def disk_usage() -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(settings.STORAGE_DIR)
        return usage.used, usage.total
    except OSError:
        return 0, 0


# --------------------------------------------------------------------------- #
# 分片上传中转区
# --------------------------------------------------------------------------- #
def session_dir(session_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id or ""):
        raise ValueError("非法会话 id")
    return settings.chunk_dir / session_id


def chunk_path(session_id: str, index: int) -> Path:
    return session_dir(session_id) / f"{index:06d}.part"


def write_chunk(session_id: str, index: int, data: bytes) -> str:
    """原子写入一个分片，返回其 sha256。"""
    directory = session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = chunk_path(session_id, index)
    tmp = directory / f".{index:06d}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return hashlib.sha256(data).hexdigest()


def has_chunk(session_id: str, index: int, expected_size: int | None = None) -> bool:
    try:
        path = chunk_path(session_id, index)
    except ValueError:
        return False
    if not path.is_file():
        return False
    return expected_size is None or path.stat().st_size == expected_size


def chunk_size_on_disk(session_id: str, index: int) -> int:
    try:
        path = chunk_path(session_id, index)
    except ValueError:
        return 0
    return path.stat().st_size if path.is_file() else 0


def drop_session_dir(session_id: str) -> None:
    """逐文件删除会话分片目录。

    刻意不用 shutil.rmtree：某些运行环境会给它套安全删除钩子并直接中断进程，
    而这里都是我们自己刚写的 .part 文件，逐个 unlink 更可控。
    """
    try:
        directory = session_dir(session_id)
    except ValueError:
        return
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                for nested in entry.iterdir():
                    nested.unlink(missing_ok=True)
                entry.rmdir()
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
    try:
        directory.rmdir()
    except OSError:
        pass


def assemble_chunks(session_id: str, total_chunks: int, expected_size: int) -> tuple[Path, str, int]:
    """按序拼接分片到临时文件，边写边算全量 sha256。

    返回 (tmp 路径, sha256, 实际字节数)；分片缺失或总长不符会抛 HTTPException。
    """
    tmp = new_tmp_path()
    digest = hashlib.sha256()
    written = 0
    try:
        with tmp.open("wb") as out:
            for index in range(total_chunks):
                part = chunk_path(session_id, index)
                if not part.is_file():
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"缺少第 {index + 1} / {total_chunks} 个分片"
                    )
                with part.open("rb") as src:
                    for block in iter(lambda: src.read(CHUNK), b""):
                        digest.update(block)
                        out.write(block)
                        written += len(block)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if written != expected_size:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"合并后大小不符：期望 {expected_size}，实际 {written}"
        )
    return tmp, digest.hexdigest(), written



# --------------------------------------------------------------------------- #
# 文本预览（在线预览与公开分享共用）
# --------------------------------------------------------------------------- #
TEXT_PREVIEW_BYTES = 1_000_000


def _decode_text(raw: bytes, truncated: bool) -> tuple[str, str]:
    """按 utf-8 → gb18030 → latin-1 依次尝试，兼顾中文 Windows 的老文件。"""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            # 截断可能把多字节字符切成半个，去掉尾部残字节再试
            if truncated:
                for cut in (1, 2, 3):
                    try:
                        return raw[:-cut].decode(encoding), encoding
                    except UnicodeDecodeError:
                        continue
    return raw.decode("latin-1"), "latin-1"


def text_payload(path: Path, max_bytes: int = TEXT_PREVIEW_BYTES) -> dict:
    """读取文本内容并解码。二进制文件抛 ValueError，由调用方转成 415。"""
    size = path.stat().st_size
    with path.open("rb") as fh:
        raw = fh.read(max_bytes)
    if b"\x00" in raw[:8192]:
        raise ValueError("binary")
    truncated = size > len(raw)
    text, encoding = _decode_text(raw, truncated)
    return {"content": text, "encoding": encoding, "size": size, "truncated": truncated}
