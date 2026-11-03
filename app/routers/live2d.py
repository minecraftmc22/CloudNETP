"""Live2D 看板娘：扫描模型目录，前端据此自动加载 / 自动换新。

目录约定（默认 <项目根>/live2d/model，可用 .env 的 LIVE2D_DIR 改根目录）：

    live2d/model/
      Hiyori/Hiyori.model3.json      ← 一个模型一个文件夹（推荐）
      Shizuku/shizuku.model.json     ← Cubism 2 模型，能列出来但前端不渲染
      mark.model3.json               ← 也可以直接丢在 model/ 下

只做「列出有什么」，不做任何写入；文件夹里增删模型后前端下次轮询即可发现。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Response

from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live2d", tags=["live2d"])

CUBISM4_SUFFIX = ".model3.json"
CUBISM2_SUFFIX = ".model.json"


def _model_entry(root: Path, base: Path, settings_file: Path, kind: str) -> dict:
    """把一份 model*.json 描述成前端要的条目。"""
    rel = settings_file.relative_to(root).as_posix()
    folder = base.relative_to(root).as_posix() if base != root else ""
    try:
        mtime = int(settings_file.stat().st_mtime)
    except OSError:
        mtime = 0
    return {
        "id": rel[: -len(kind)],
        "name": base.name if base != root else settings_file.name[: -len(kind)],
        "folder": folder,
        "type": "cubism4" if kind == CUBISM4_SUFFIX else "cubism2",
        "supported": kind == CUBISM4_SUFFIX,
        "url": "/live2d/model/" + quote(rel, safe="/"),
        "updatedAt": mtime,
    }


def scan_models() -> list[dict]:
    """扫描 model/ 一级子目录与根目录下的 *.model3.json / *.model.json。"""
    root = settings.live2d_model_dir
    if not root.is_dir():
        return []

    found: list[dict] = []
    dirs = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith((".", "_", "~")):
            continue
        if entry.is_dir():
            dirs.append(entry)
        elif entry.is_file() and entry.name.endswith((CUBISM4_SUFFIX, CUBISM2_SUFFIX)):
            dirs.append(entry)

    for entry in dirs:
        if entry.is_file():
            kind = CUBISM4_SUFFIX if entry.name.endswith(CUBISM4_SUFFIX) else CUBISM2_SUFFIX
            found.append(_model_entry(root, root, entry, kind))
            continue

        # 目录：优先 Cubism 4，没有再退回 Cubism 2；同类型取文件名排序第一个
        c4 = sorted(entry.glob(f"*{CUBISM4_SUFFIX}"))
        c2 = sorted(entry.glob(f"*{CUBISM2_SUFFIX}"))
        if c4:
            found.append(_model_entry(root, entry, c4[0], CUBISM4_SUFFIX))
        elif c2:
            found.append(_model_entry(root, entry, c2[0], CUBISM2_SUFFIX))

    return found


@router.get("/models")
def list_models(response: Response) -> dict:
    """列出可用模型。前端轮询这个接口实现「往文件夹里丢模型即刷新」。"""
    models = scan_models()
    response.headers["Cache-Control"] = "no-store"
    supported = [m for m in models if m["supported"]]
    unsupported = [m for m in models if not m["supported"]]
    if unsupported:
        logger.info(
            "Live2D：%s 个 Cubism 2 模型未被支持（%s）",
            len(unsupported),
            "、".join(m["name"] for m in unsupported[:5]),
        )
    return {
        "dir": "/live2d/model",
        "count": len(supported),
        "models": supported,
        "unsupported": [{"id": m["id"], "name": m["name"], "type": m["type"]} for m in unsupported],
        "scannedAt": int(time.time()),
    }
