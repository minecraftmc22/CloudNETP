"""启动入口：python run.py

监听地址、端口、对外站点地址、SSL 证书全部由项目根目录的 .env 控制，例如：

    HOST=0.0.0.0            # 0.0.0.0 允许局域网/公网访问，127.0.0.1 只允许本机
    PORT=8123
    SITE_URL=https://pan.example.com   # 分享链接/WebDAV 地址用这个域名
    SSL_CERTFILE=D:/certs/fullchain.pem
    SSL_KEYFILE=D:/certs/privkey.pem
    RELOAD=false            # 开发时 true 可热重载

直接 `python -m uvicorn app.main:app` 只能用默认值，想改配置请用本脚本。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from app.config import settings


def _check_ssl() -> list[str]:
    """证书路径写错时给出明确提示，而不是启动到一半崩掉。"""
    problems: list[str] = []
    for label, raw in (
        ("SSL_CERTFILE", settings.SSL_CERTFILE),
        ("SSL_KEYFILE", settings.SSL_KEYFILE),
    ):
        if raw and not Path(raw).is_file():
            problems.append(f"{label} 指向的文件不存在：{raw}")
    if bool(settings.SSL_CERTFILE) != bool(settings.SSL_KEYFILE):
        problems.append("SSL_CERTFILE / SSL_KEYFILE 必须同时配置")
    return problems


def main(force_reload: bool = False) -> int:
    problems = _check_ssl()
    if problems:
        for line in problems:
            print(f"[配置错误] {line}", file=sys.stderr)
        return 1

    listen = f"{settings.scheme}://{settings.HOST}:{settings.PORT}"
    print(f"CloudNETP {settings.VERSION}", flush=True)
    print(f"  监听      {listen}", flush=True)
    print(f"  对外地址  {settings.public_base}", flush=True)
    print(f"  WebDAV    {settings.public_base}/dav/", flush=True)
    print(f"  TLS       {'已启用（uvicorn 直出）' if settings.ssl_enabled else '未启用（可交给反向代理）'}", flush=True)
    if settings.SECRET_KEY_IS_EPHEMERAL:
        print("  警告      SECRET_KEY 未配置，重启后所有会话失效", flush=True)

    reload = force_reload or settings.RELOAD
    # 让子进程知道当前是 uvicorn 热重载模式：自动更新后靠 WatchFiles 重启，
    # 不要再额外拉起 restart_helper，否则会抢同一个端口。
    if reload:
        os.environ["CLOUDNETP_RELOAD"] = "1"
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=reload,
        reload_dirs=["app", "static"] if reload else None,
        proxy_headers=settings.TRUST_PROXY,
        forwarded_allow_ips="*" if settings.TRUST_PROXY else None,
        ssl_certfile=settings.SSL_CERTFILE or None,
        ssl_keyfile=settings.SSL_KEYFILE or None,
        ssl_keyfile_password=settings.SSL_KEYFILE_PASSWORD or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
