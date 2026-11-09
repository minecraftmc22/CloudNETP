"""歌词来源配置（后台可改、密钥加密）离线冒烟。

不连真实网络：本地起一个假歌词库，验证
1. 没配置来源时「有歌词库密钥就两个都启用」
2. 后台保存的来源 / 地址 / 密钥 / Cookie 立即生效，且优先于 .env
3. 密钥与 Cookie 在库里是密文，snapshot 只回掩码
4. 留空表示「不改」，清除用 clearKey / clearCookie
5. 歌词库搜索确实带上了后台配置的地址、Bearer 与 Cookie
6. 清掉后台密钥后自动回退 .env

跑法：
    ./.venv/Scripts/python.exe scripts/lyrics_config_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-lyr-cfg-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["LYRICS_API_KEY"] = "env-key-1234"
os.environ["LYRICS_API_COOKIE"] = ""
os.environ["LYRICS_API_BASE"] = "http://env.invalid/api/v1"
os.environ.pop("LYRICS_PROVIDERS", None)
os.environ.pop("LYRICS_PROVIDER", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module  # noqa: E402
from app import lyrics_config as lc  # noqa: E402
from app import lyrics_service as ls  # noqa: E402
from app import settings_store  # noqa: E402
from app.config import settings  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  ({detail})")


# --------------------------------------------------------------------------- #
# 假歌词库
# --------------------------------------------------------------------------- #
class FakeLib(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        FakeLib.calls.append({"path": self.path, "headers": dict(self.headers)})
        payload = {
            "ok": True,
            "data": {
                "items": [
                    {
                        "id": 7,
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_ms": 269000,
                    }
                ]
            },
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLib)
threading.Thread(target=srv.serve_forever, daemon=True).start()
FAKE_BASE = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"

# 建表：settings_store 需要 system_settings
db_module.Base.metadata.create_all(db_module.engine)


def new_db():
    return db_module.SessionLocal()


# --------------------------------------------------------------------------- #
print("\nA. 默认行为：有歌词库密钥就两个都启用")
lc.reset()
settings.LYRICS_PROVIDERS = ""
settings.LYRICS_PROVIDER = ""
check("两个来源都启用", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())
check("默认仍是网易云", ls.default_provider() == "netease", ls.default_provider())

snap = lc.snapshot()
check("来源标记为自动", snap["providersSource"] == "auto", snap["providersSource"])
check("密钥来自 .env", snap["keySource"] == "env", snap["keySource"])
check("密钥只回掩码", snap["keyMask"].endswith("1234") and "env-key" not in json.dumps(snap), snap["keyMask"])

settings.LYRICS_API_KEY = ""
check("没密钥时只剩网易云", ls.enabled_providers() == ["netease"], ls.enabled_providers())
settings.LYRICS_API_KEY = "env-key-1234"

print("\nB. 后台保存：来源 / 地址 / 密钥 / Cookie")
db = new_db()
cfg = lc.save(
    db,
    providers=["netease"],
    api_base=FAKE_BASE,
    api_key="admin-key-9876",
    api_cookie="MUSIC_U=lib-session",
)
check("只启用网易云", ls.enabled_providers() == ["netease"], ls.enabled_providers())
check("来源标记为后台", cfg["providersSource"] == "admin", cfg["providersSource"])
check("地址用后台的", cfg["apiBase"] == FAKE_BASE, cfg["apiBase"])
check("密钥标记为后台", cfg["keySource"] == "admin", cfg["keySource"])
check("Cookie 标记为后台", cfg["cookieSource"] == "admin", cfg["cookieSource"])
check("密钥掩码只留尾部", cfg["keyMask"].endswith("9876"), cfg["keyMask"])
check("status 里没有明文密钥", "admin-key-9876" not in json.dumps(cfg), "leak!")
check("status 里没有明文 Cookie", "lib-session" not in json.dumps(cfg), "leak!")

raw_key = settings_store.get_value(db, lc.STORE_API_KEY)
raw_cookie = settings_store.get_value(db, lc.STORE_API_COOKIE)
check("密钥加密入库", raw_key.startswith("enc:") and "admin-key" not in raw_key, raw_key[:10])
check("Cookie 加密入库", raw_cookie.startswith("enc:") and "lib-session" not in raw_cookie, raw_cookie[:10])
check("解密后一致", lc.api_key() == "admin-key-9876", lc.api_key())
check("Cookie 解密后一致", lc.api_cookie() == "MUSIC_U=lib-session", lc.api_cookie())

print("\nC. 搜索确实用后台配置")
FakeLib.calls.clear()
items = ls.lyriclib_search("晴天", 5, use_cache=False)
call = FakeLib.calls[0]
check("搜到结果", len(items) == 1 and items[0]["title"] == "晴天", items)
check("请求打到后台地址", call["path"].startswith("/api/v1/search.php"), call["path"])
check("带 Bearer 密钥", call["headers"].get("Authorization") == "Bearer admin-key-9876", call["headers"].get("Authorization"))
check("透传 Cookie", "MUSIC_U=lib-session" in call["headers"].get("Cookie", ""), call["headers"].get("Cookie"))

print("\nD. 留空 = 不改，clear* = 清除")
cfg = lc.save(
    db,
    providers=["netease", "lyriclib"],
    api_base="",
    api_key="",
    api_cookie="",
)
check("两个来源都启用", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())
check("空密钥不清掉原值", lc.api_key() == "admin-key-9876", lc.api_key())
check("空 Cookie 不清掉原值", lc.api_cookie() == "MUSIC_U=lib-session", lc.api_cookie())
check("空地址回退 .env", cfg["apiBase"] == settings.LYRICS_API_BASE, cfg["apiBase"])

cfg = lc.save(
    db,
    providers=[],
    api_base=None,
    api_key="",
    api_cookie="",
    clear_key=True,
    clear_cookie=True,
)
check("清密钥后回退 .env", lc.api_key() == "env-key-1234", lc.api_key())
check("密钥来源变 .env", cfg["keySource"] == "env", cfg["keySource"])
check("清 Cookie 后为空", cfg["hasCookie"] is False, cfg["cookieSource"])
check("来源回到自动", cfg["providersSource"] == "auto", cfg["providersSource"])

print("\nE. 重启后从库里读回")
lc.reset()
lc.hydrate()
check("hydrate 读到后台地址", lc.api_base() == settings.LYRICS_API_BASE, lc.api_base())

db2 = new_db()
lc.save(
    db2,
    providers=["lyriclib"],
    api_base=FAKE_BASE,
    api_key="persisted-key-5555",
    api_cookie="",
)
lc.reset()  # 模拟进程重启
lc.hydrate()
check("重启后来源仍是 lyriclib", ls.enabled_providers() == ["lyriclib"], ls.enabled_providers())
check("重启后地址还在", lc.api_base() == FAKE_BASE, lc.api_base())
check("重启后密钥还在", lc.api_key() == "persisted-key-5555", lc.api_key())

# 收尾：把来源恢复成默认，避免影响后续断言
lc.save(db2, providers=[], api_base="", api_key="", api_cookie="", clear_key=True, clear_cookie=True)

print(f"\n{'=' * 56}")
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
for name in FAILED:
    print("  FAILED:", name)
srv.shutdown()
sys.exit(1 if FAILED else 0)
