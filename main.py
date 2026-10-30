"""开发入口（`python main.py`）。

监听地址、端口、SSL 与 run.py 完全同一套逻辑：全部读根目录的 .env，
唯一的区别是本入口默认开启热重载（改 app/ 与 static/ 下的文件会自动重启）。

⚠️ 直接用 `uvicorn main:app --reload` 启动时，uvicorn 会用**自己的**默认值
127.0.0.1:8000，.env 里的 HOST/PORT 完全不生效 —— uvicorn 不读 .env，
只有 run.py / main.py 会把 .env 的值传给 uvicorn。需要自定义地址时用：

    python main.py                                        # 读 .env + 热重载
    python run.py                                         # 读 .env，热重载看 RELOAD
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload  # 手动补 host/port

另外注意：环境变量优先级高于 .env，所以 IDE 运行配置里若写了 PORT，
会盖掉 .env 里的 PORT（.run/CloudNETP (dev).run.xml 已刻意不再设置）。

说明：PyCharm 的「FastAPI」类型运行配置会静态分析文件里的 FastAPI 实例。
这里只做重导出，所以用类型注解显式声明 app 的类型帮助识别；
若仍提示找不到应用，把运行配置的 Application file 指到 app/main.py、Application name 填 app 即可。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import settings
from app.main import app as _fastapi_app

# 显式类型标注：让静态分析能认出这是一个 FastAPI 应用
app: FastAPI = _fastapi_app

__all__ = ["app", "settings"]


def main() -> None:
    """复用 run.py 的启动逻辑，只把热重载强制打开。"""
    from run import main as serve

    raise SystemExit(serve(force_reload=True))


if __name__ == "__main__":
    main()
