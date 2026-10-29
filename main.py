"""开发入口。

三种启动方式任选：
  1) PyCharm 运行配置「CloudNETP (dev)」（已在 .run/ 生成，直接可用）
  2) 命令行：python main.py
  3) 命令行：uvicorn main:app --reload --reload-dir app

说明：PyCharm 的「FastAPI」类型运行配置会静态分析文件里的 FastAPI 实例。
这里只做重导出，所以用类型注解显式声明 app 的类型帮助识别；
若仍提示找不到应用，把运行配置的 Application file 指到 app/main.py、Application name 填 app 即可。
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from app.config import settings
from app.main import app as _fastapi_app

# 显式类型标注：让静态分析能认出这是一个 FastAPI 应用
app: FastAPI = _fastapi_app

__all__ = ["app", "settings"]


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=["app", "static"],
        log_level="info",
    )


if __name__ == "__main__":
    main()
