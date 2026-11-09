"""歌词多来源（网易云 / 歌词库）离线冒烟。

不连真实网络：起两个本地假上游，把 lyrics_service 里的 URL 常量指过去，
验证「同时启用两个来源 + 各自 Cookie」的完整分发逻辑。

跑法：
    ./.venv/Scripts/python.exe scripts/lyrics_provider_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-lyr-prov-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["LYRICS_API_KEY"] = "test-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import lyrics_service as ls  # noqa: E402
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
# 假上游：记录每次请求的路径与请求头，按路径回不同数据
# --------------------------------------------------------------------------- #
class Recorder(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        Recorder.calls.append({"path": self.path, "headers": dict(self.headers)})
        payload = self.reply(self.path)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def reply(path: str) -> dict:
        raise NotImplementedError


class NeteaseFake(Recorder):
    """网易云：/api/search/get/web 与 /api/song/lyric/v1"""

    @staticmethod
    def reply(path: str) -> dict:
        if "/search/" in path:
            return {
                "code": 200,
                "result": {
                    "songs": [
                        {
                            "id": 111,
                            "name": f"网易云结果 {len(Recorder.calls)}",
                            "artists": [{"name": "NE 歌手"}],
                            "album": {"name": "NE 专辑", "picUrl": ""},
                            "duration": 210000,
                        }
                    ]
                },
            }
        return {
            "code": 200,
            "lrc": {"lyric": "[00:01.00]网易云第一行\n[00:02.00]网易云第二行"},
        }


class LyriclibFake(Recorder):
    """歌词库：/api/v1/search.php 与 /api/v1/lyrics.php"""

    @staticmethod
    def reply(path: str) -> dict:
        if "/search.php" in path:
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "id": 222,
                            "title": "歌词库结果",
                            "artist": "LIB 歌手",
                            "album": "LIB 专辑",
                            "duration_ms": 180000,
                        }
                    ]
                },
            }
        return {
            "ok": True,
            "data": {"lines": [{"start": 0, "text": "歌词库第一行"}], "granularity": "line"},
        }


def serve(handler) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


ne_srv, ne_base = serve(NeteaseFake)
lib_srv, lib_base = serve(LyriclibFake)
ls._NETEASE_SEARCH_URL = f"{ne_base}/api/search/get/web"
ls._NETEASE_LYRIC_URL = f"{ne_base}/api/song/lyric/v1"
settings.LYRICS_API_BASE = f"{lib_base}/api/v1"

# 记录原始配置，段与段之间重置
_ORIG = (
    settings.LYRICS_PROVIDERS,
    settings.LYRICS_PROVIDER,
    settings.LYRICS_API_KEY,
    settings.LYRICS_API_COOKIE,
    settings.NETEASE_COOKIE,
)


def reset(**kwargs) -> None:
    """重置配置（默认两个来源都开、密钥齐全、无 Cookie）。"""
    settings.LYRICS_PROVIDERS = kwargs.get("providers", "netease,lyriclib")
    settings.LYRICS_PROVIDER = kwargs.get("provider", "netease")
    settings.LYRICS_API_KEY = kwargs.get("key", "test-key")
    settings.LYRICS_API_COOKIE = kwargs.get("lib_cookie", "")
    settings.NETEASE_COOKIE = kwargs.get("ne_cookie", "")
    Recorder.calls.clear()
    ls._search_cache.clear()
    ls._lyrics_cache.clear()


def paths() -> list[str]:
    return [c["path"] for c in Recorder.calls]


# --------------------------------------------------------------------------- #
print("\nA. 配置解析")
reset(providers="netease,lyriclib")
check("逗号分隔两个来源", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())

reset(providers="163, mc22")
check("别名与空格", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())

reset(providers="all")
check("all = 全部来源", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())

reset(providers="乱写的来源")
check("写错时退回默认来源", ls.enabled_providers() == ["netease"], ls.enabled_providers())

reset(providers="", provider="lyriclib")
check("留空退回 LYRICS_PROVIDER", ls.enabled_providers() == ["lyriclib"], ls.enabled_providers())

reset(providers="netease,lyriclib,netease")
check("重复项只留一次", ls.enabled_providers() == ["netease", "lyriclib"], ls.enabled_providers())

print("\nB. 可用性与默认来源")
reset(providers="netease,lyriclib", key="")
check("网易云免密钥即可用", ls.provider_ready("netease") is True)
check("歌词库缺密钥不可用", ls.provider_ready("lyriclib") is False)
check("默认来源跳过没凭据的", ls.default_provider() == "netease", ls.default_provider())
info = ls.providers_info()
check("providers_info 两个来源", len(info) == 2, info)
check("标记默认项", [i["id"] for i in info if i["default"]] == ["netease"], info)
check("不可用带原因", info[1]["ready"] is False and bool(info[1]["reason"]), info[1])
check("展示名是中文", [i["label"] for i in info] == ["网易云", "歌词库"], info)

reset(providers="lyriclib,netease", key="test-key")
check("顺序即优先级", ls.default_provider() == "lyriclib", ls.default_provider())

reset(providers="lyriclib,netease", key="")
check("按顺序但第一个没凭据 → 下一个", ls.default_provider() == "netease", ls.default_provider())

print("\nC. 搜索按来源分发")
reset()
items = ls.search_songs("晴天", provider="netease")
check("netease 走网易云", any("/search/get/web" in p for p in paths()), paths())
check("netease 条目带来源", items and items[0]["provider"] == "netease", items[:1])

reset()
items = ls.search_songs("晴天", provider="lyriclib")
check("lyriclib 走歌词库", any("/search.php" in p for p in paths()), paths())
check("lyriclib 条目带来源", items and items[0]["provider"] == "lyriclib", items[:1])
check(
    "请求带 Bearer 密钥",
    all(c["headers"].get("Authorization") == "Bearer test-key" for c in Recorder.calls),
    [c["headers"].get("Authorization") for c in Recorder.calls],
)

reset()
ls.search_songs("同一句", provider="netease")
ls.search_songs("同一句", provider="lyriclib")
lyr = ls.search_songs("同一句", provider="lyriclib")
check("换来源不会串缓存", lyr and lyr[0]["id"] == 222, lyr[:1])
check("网易云缓存独立", any("/search/get/web" in p for p in paths()), paths())

reset(providers="netease")
try:
    ls.search_songs("晴天", provider="lyriclib")
    check("未启用来源报错", False, "没有抛异常")
except ls.LyricsApiError as exc:
    check("未启用来源报错", exc.code == "provider_disabled", exc.code)
    check("报错里有可用来源", "网易云" in exc.message, exc.message)

reset()
try:
    ls.search_songs("晴天", provider="qqmusic")
    check("未知来源报错", False, "没有抛异常")
except ls.LyricsApiError as exc:
    check("未知来源报错", exc.code == "bad_provider", exc.code)

reset(key="")
try:
    ls.search_songs("晴天", provider="lyriclib")
    check("歌词库缺密钥报错", False, "没有抛异常")
except ls.LyricsApiError as exc:
    check("歌词库缺密钥报错", exc.code == "no_key", exc.code)

print("\nD. 取词（同 id 不同来源）")
reset()
ne = ls.fetch_lyrics(1, "netease")
lib = ls.fetch_lyrics(1, "lyriclib")
check("netease 取到逐行", ne["provider"] == "netease" and ne["lines"][0]["text"].startswith("网易云"), ne)
check("lyriclib 取到", lib["provider"] == "lyriclib" and lib["lines"][0]["text"].startswith("歌词库"), lib)
check("同 id 不串缓存", len(ls._lyrics_cache) == 2, list(ls._lyrics_cache))

reset(providers="netease", provider="netease")
lib = ls.fetch_lyrics(222, "lyriclib")
check("停用的来源仍可取历史选择", lib["provider"] == "lyriclib", lib.get("provider"))

print("\nE. Cookie 透传")
reset(lib_cookie="lib_session=abc", ne_cookie="MUSIC_U=xyz")
ls.search_songs("周杰伦", provider="lyriclib")
lib_calls = [c for c in Recorder.calls if "/search.php" in c["path"]]
check("歌词库 Cookie 已带", lib_calls and "lib_session=abc" in (lib_calls[0]["headers"].get("Cookie") or ""), lib_calls[:1])
Recorder.calls.clear()
ls.fetch_lyrics(9, "netease")
ne_calls = [c for c in Recorder.calls if "/song/lyric" in c["path"]]
check("网易云 Cookie 已带", ne_calls and "MUSIC_U=xyz" in (ne_calls[0]["headers"].get("Cookie") or ""), ne_calls[:1])

print("\nF. 自动匹配按顺序尝试")
reset(providers="netease,lyriclib")
data = ls.find_and_fetch("晴天", "周杰伦")
check("优先第一个来源", data["provider"] == "netease", data.get("provider"))
check("带 matched 信息", (data.get("matched") or {}).get("provider") == "netease", data.get("matched"))

# 让网易云返回空结果，应当落到歌词库
_orig_reply = NeteaseFake.reply  # 类上取到的是普通函数


def _empty_netease(path: str) -> dict:
    if "/search/" in path:
        return {"code": 200, "result": {"songs": []}}
    return _orig_reply(NeteaseFake, path)


NeteaseFake.reply = staticmethod(_empty_netease)
try:
    reset(providers="netease,lyriclib")
    data = ls.find_and_fetch("晴天", "周杰伦")
    check("第一个来源没结果 → 落到第二个", data["provider"] == "lyriclib", data.get("provider"))
finally:
    NeteaseFake.reply = staticmethod(_orig_reply)

# 两个来源都报错 → 抛出可展示的错误
_orig_lib_reply = LyriclibFake.reply


def _boom(path: str) -> dict:
    raise BrokenPipeError("fake upstream down")


NeteaseFake.reply = staticmethod(_boom)
LyriclibFake.reply = staticmethod(_boom)
try:
    reset(providers="netease,lyriclib")
    try:
        ls.find_and_fetch("晴天", "周杰伦")
        check("两个来源都挂时抛错", False, "没有抛异常")
    except ls.LyricsApiError as exc:
        check("两个来源都挂时抛错", exc.code in {"upstream", "network"}, exc.code)
    except Exception as exc:  # 假上游直接断连时会变成非业务异常，这里算失败
        check("两个来源都挂时抛错", False, f"{type(exc).__name__}: {exc}")
finally:
    NeteaseFake.reply = staticmethod(_orig_reply)
    LyriclibFake.reply = staticmethod(_orig_lib_reply)

print("\nG. 接口签名")
from app.routers import nodes as nodes_router  # noqa: E402

sig = nodes_router.lyrics_proxy.__code__.co_varnames
check("/api/lyrics 接受 provider", "provider" in sig, sig)
check("/api/lyrics/search 接受 provider", "provider" in nodes_router.lyrics_search.__code__.co_varnames)
check("有 providers 接口", hasattr(nodes_router, "lyrics_providers"))

# --------------------------------------------------------------------------- #
settings.LYRICS_PROVIDERS, settings.LYRICS_PROVIDER, settings.LYRICS_API_KEY, settings.LYRICS_API_COOKIE, settings.NETEASE_COOKIE = _ORIG
ne_srv.shutdown()
lib_srv.shutdown()

print(f"\n结果：{PASSED} 项通过，{len(FAILED)} 项失败")
if FAILED:
    print("失败项：" + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
