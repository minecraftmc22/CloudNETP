"""音频元数据解析（mutagen）与歌词 API 代理。

歌词来源可以同时启用多个（LYRICS_PROVIDERS，逗号分隔），按顺序即优先级：
- netease：直连网易云接口，免密钥即可搜索 + 逐行歌词（lrc）；
  配置 NETEASE_COOKIE（网页版 Cookie 里的 MUSIC_U=...）后可拿逐字歌词（yrc）。
- lyriclib：自建歌词库开发者 API（LYRICS_API_BASE / LYRICS_API_KEY /
  可选 LYRICS_API_COOKIE），即 0.2.x 的行为。

前端「搜索指定」里的分类只列已启用的来源；自动匹配按配置顺序依次尝试。
密钥/Cookie 只放服务端，前端只访问本站代理。

本模块对上游的全部请求都直连（不走系统代理）：本机代理对 TLS 上游会超时。
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("uvicorn.error")

SEARCH_TTL = 6 * 3600  # 搜索缓存 6 小时
LYRICS_TTL = 24 * 3600  # 歌词缓存 24 小时（歌词几乎不变）
_CACHE_MAX = 300

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock = threading.Lock()
_search_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
# 键 = (provider, song_id)：同一个数字 id 在两个来源里是两首不同的歌
_lyrics_cache: OrderedDict[tuple[str, int], tuple[float, dict]] = OrderedDict()


class LyricsApiError(RuntimeError):
    """歌词接口业务错误，message 为可直接展示的中文说明。"""

    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# 音频元数据（mutagen）
# --------------------------------------------------------------------------- #
# 扩展名 → mutagen 精确加载器。mutagen.File() 靠启发式评分猜类型，
# 带 ID3 的 WAV/AIFF 会被误判成 MP3 而抛错，所以已知扩展名先走精确加载。
def _explicit_loaders() -> dict[str, callable]:
    from mutagen.asf import ASF
    from mutagen.aiff import AIFF
    from mutagen.dsf import DSF
    from mutagen.flac import FLAC
    from mutagen.monkeysaudio import MonkeysAudio
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wavpack import WavPack
    from mutagen.wave import WAVE

    return {
        ".mp3": MP3, ".flac": FLAC, ".m4a": MP4, ".mp4": MP4, ".m4b": MP4,
        ".ogg": OggVorbis, ".oga": OggVorbis, ".opus": OggOpus,
        ".wma": ASF, ".wav": WAVE, ".aiff": AIFF, ".aif": AIFF,
        ".dsf": DSF, ".wv": WavPack, ".ape": MonkeysAudio,
    }


def _load_audio(path: Path) -> tuple[object | None, object | None]:
    """按扩展名精确加载 → mutagen.File() 启发式 → tags-only 兜底。

    返回 (fileobj, tags_only)：fileobj 是完整 FileType（含 info）；
    音轨信息解析失败（如 MP4 时基异常）时退化为只读标签对象，元数据照常展示。
    """
    from mutagen import File as MutagenFile

    loader = _explicit_loaders().get(path.suffix.lower())
    if loader is not None:
        try:
            mf = loader(str(path))
            if mf is not None:
                return mf, None
        except Exception as exc:
            log.warning("音频元数据精确加载失败 %s（%s）：%s", path.name, path.suffix, exc)
    try:
        mf = MutagenFile(str(path))
        if mf is not None:
            return mf, None
    except Exception as exc:
        log.warning("音频元数据解析失败 %s：%s", path.name, exc)

    # 最后的兜底：跳过音轨/时长，只读标签容器
    ext = path.suffix.lower()
    candidates = []
    if ext in (".mp3", ".wav", ".aiff", ".aif", ".dsf"):
        from mutagen.id3 import ID3
        candidates.append(lambda p: ID3(str(p)))
    if ext in (".m4a", ".mp4", ".m4b"):
        def _mp4_tags(p: str):
            # MP4Tags 不能独立构造：需先建 atom 树（音轨信息解析失败不影响标签）
            from mutagen.mp4 import Atoms, MP4Tags

            with open(p, "rb") as fh:
                atoms = Atoms(fh)
                fh.seek(0)
                return MP4Tags(atoms, fh)
        candidates.append(_mp4_tags)
    for loader in candidates:
        try:
            return None, loader(str(path))
        except Exception:
            continue
    return None, None


def audio_meta(path: Path, fallback_title: str) -> dict:
    """读取音频文件元数据与内嵌封面（mutagen 全容器）。

    支持的标签容器：ID3v2（MP3/WAV/AIFF/DSF）、MP4（M4A）、
    Vorbis 注释（FLAC/Ogg/Opus）、ASF（WMA）、APEv2（APE/WavPack）。
    返回 {title, artist, album, duration, cover, coverMime}，解析失败时字段留空
    并以文件名兜底，绝不抛异常——元数据缺失也要能正常播放。
    """
    meta: dict = {
        "title": fallback_title,
        "artist": "",
        "album": "",
        "duration": 0.0,
        "cover": None,
        "coverMime": "",
    }
    mf, tags_only = _load_audio(path)
    if mf is None and tags_only is None:
        return meta

    if mf is not None:
        try:
            meta["duration"] = round(float(mf.info.length), 3)
        except Exception:
            pass
        tags = mf.tags
    else:
        tags = tags_only
    if tags:

        def _first(key: str) -> str:
            try:
                vals = tags.get(key)
            except Exception:
                return ""
            if not vals:
                return ""
            value = vals[0]
            return str(value).strip() if value is not None else ""

        # 键名按容器区分（按命中概率排序）：
        #   ID3（MP3/WAV/AIFF/DSF）: TIT2/TPE1/TALB
        #   MP4（M4A）: ©nam/©ART/©alb
        #   Vorbis 注释（FLAC/Ogg/Opus）: 小写 title/artist/album
        #   ASF（WMA）: Title/Author/WM/AlbumTitle
        #   APEv2（APE/WavPack）: Title/Artist/Album
        _KEYS = {
            "title": ("TIT2", "©nam", "title", "Title", "WM/AlbumTitle"),
            "artist": ("TPE1", "©ART", "artist", "Author", "Artist"),
            "album": ("TALB", "©alb", "album", "Album", "WM/AlbumTitle"),
        }
        for field, keys in _KEYS.items():
            for key in keys:
                if value := _first(key):
                    meta[field] = value
                    break

    cover, mime = _extract_cover(mf, tags)
    if cover:
        meta["cover"] = cover
        meta["coverMime"] = mime
    return meta


def _extract_cover(mf, tags) -> tuple[bytes | None, str]:
    """按容器类型取内嵌封面（全部容器的封面都是独立存图，mime 统一归一化）。

    MP3/WAV/AIFF/DSF: ID3 APIC · FLAC/Ogg: Picture · MP4: covr ·
    WMA: WM/Picture · APE/WavPack: Cover Art (front)
    """
    pictures = getattr(mf, "pictures", None)  # FLAC（Ogg 走 METADATA_BLOCK_PICTURE）
    if pictures:
        pic = pictures[0]
        return pic.data, _norm_image_mime(pic.mime, pic.data)

    if tags is None:
        return None, ""
    try:
        apics = tags.getall("APIC") if hasattr(tags, "getall") else []  # ID3v2
    except Exception:
        apics = []
    if apics:
        pic = apics[0]
        # ID3v2.2/2.3 的 mime 常写成 "PNG"/"JPG"（没有 image/ 前缀）
        return bytes(pic.data), _norm_image_mime(pic.mime, pic.data)

    try:
        covrs = tags.get("covr")  # MP4 / M4A
    except Exception:
        covrs = None
    if covrs:
        data = bytes(covrs[0])
        return data, _sniff_image_mime(data)

    try:
        wm_pics = tags.get("WM/Picture")  # WMA / ASF
    except Exception:
        wm_pics = None
    if wm_pics:
        data = _asf_picture(bytes(wm_pics[0]))
        if data:
            return data, _sniff_image_mime(data)

    try:
        ape_pics = tags.get("Cover Art (front)")  # APE / WavPack
    except Exception:
        ape_pics = None
    if ape_pics:
        raw = bytes(ape_pics[0])
        # APE 封面 = 文件名 + b"\x00" + 图片数据
        if b"\x00" in raw:
            data = raw.split(b"\x00", 1)[1]
            if data:
                return data, _sniff_image_mime(data)

    try:
        raw = tags.get("METADATA_BLOCK_PICTURE")  # Ogg/Vorbis（base64 FLAC Picture）
    except Exception:
        raw = None
    if raw:
        try:
            from mutagen.flac import Picture

            pic = Picture(base64.b64decode(str(raw[0])))
            return pic.data, _norm_image_mime(pic.mime, pic.data)
        except Exception:
            pass
    return None, ""


def _asf_picture(raw: bytes) -> bytes:
    """解析 ASF WM/Picture：picture_type(1B) + mime\0 + 描述\0 + 图片数据。"""
    try:
        pos = raw.index(b"\x00") + 1  # 跳过 mime
        pos = raw.index(b"\x00", pos) + 1  # 跳过描述
        return raw[pos:]
    except ValueError:
        return b""


def _norm_image_mime(mime: str, data: bytes) -> str:
    """把各容器里的封面 mime 归一化；可疑/缺失时按文件头嗅探。"""
    mime = (mime or "").strip().lower()
    if mime in {"png", "jpg", "jpeg", "jpe", "gif", "bmp"}:
        mime = f"image/{'jpeg' if mime in {'jpg', 'jpeg', 'jpe'} else mime}"
    if not mime.startswith("image/"):
        return _sniff_image_mime(data)
    return mime


def _sniff_image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# --------------------------------------------------------------------------- #
# 歌词 API 代理
# --------------------------------------------------------------------------- #
def _get(url: str, key: str, timeout: float = 12.0, cookie: str = "") -> dict:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "CloudNETP/lyrics"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        err = body.get("error") or {}
        raise LyricsApiError(
            err.get("message") or f"歌词接口返回 HTTP {exc.code}",
            code=err.get("code") or "http_error",
            status=exc.code,
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, http.client.HTTPException) as exc:
        # OSError / HTTPException 覆盖对端直接掐连接（RemoteDisconnected）等
        # 非 URLError 的失败，否则会漏成 500 而不是「服务暂时不可用」
        raise LyricsApiError("歌词服务暂时不可用", code="network") from exc


def _cache_get(cache: OrderedDict, key, ttl: float):
    with _lock:
        hit = cache.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > ttl:
            del cache[key]
            return None
        cache.move_to_end(key)
        return value


def _cache_put(cache: OrderedDict, key, value) -> None:
    with _lock:
        cache[key] = (time.time(), value)
        cache.move_to_end(key)
        while len(cache) > _CACHE_MAX:
            cache.popitem(last=False)


# --------------------------------------------------------------------------- #
# 歌词来源（可以同时启用多个，前端按「分类」切换）
# --------------------------------------------------------------------------- #
# 已知来源：id → 展示名。这里的顺序也是「一个都没配时的默认顺序」
PROVIDERS: dict[str, str] = {"netease": "网易云", "lyriclib": "歌词库"}
DEFAULT_PROVIDER = "netease"

# 兼容 .env 里可能写成人话/别名的几种写法
_PROVIDER_ALIASES = {
    "netease": "netease",
    "163": "netease",
    "music163": "netease",
    "music.163": "netease",
    "网易云": "netease",
    "网易": "netease",
    "lyriclib": "lyriclib",
    "mc22": "lyriclib",
    "minecraftmc22": "lyriclib",
    "歌词库": "lyriclib",
    "local": "lyriclib",
}


def normalize_provider(value: str) -> str:
    """把配置/入参里的来源写法归一成 id；不认识返回空串。"""
    return _PROVIDER_ALIASES.get((value or "").strip().lower(), "")


def provider_label(pid: str) -> str:
    return PROVIDERS.get(pid, pid)


def provider_ready(pid: str) -> bool:
    """该来源当前是否可用（凭据齐不齐）。

    网易云免密钥，永远可用；歌词库要 LYRICS_API_KEY。
    """
    from .config import settings

    if pid == "netease":
        return True
    if pid == "lyriclib":
        return bool((settings.LYRICS_API_KEY or "").strip())
    return False


def _provider_env() -> list[str]:
    """读配置：LYRICS_PROVIDERS 优先，退回单值 LYRICS_PROVIDER。"""
    from .config import settings

    raw = (getattr(settings, "LYRICS_PROVIDERS", "") or "").strip()
    if not raw:
        raw = (settings.LYRICS_PROVIDER or DEFAULT_PROVIDER).strip()
    if raw.strip().lower() in {"*", "all", "any"}:
        return list(PROVIDERS)
    out: list[str] = []
    for part in re.split(r"[,;\s|]+", raw):
        pid = normalize_provider(part)
        if pid and pid not in out:
            out.append(pid)
    return out


def enabled_providers() -> list[str]:
    """已启用的来源，顺序即自动匹配 / 默认来源的优先级。

    配置写错了（例如写了不认识的名字）时退回默认来源，
    不让界面出现「一个来源都没有」的死角。
    """
    return _provider_env() or [DEFAULT_PROVIDER]


def default_provider() -> str:
    """默认来源：已启用里第一个凭据齐全的，都没有就用第一个。"""
    enabled = enabled_providers()
    for pid in enabled:
        if provider_ready(pid):
            return pid
    return enabled[0]


def providers_info() -> list[dict]:
    """给前端渲染「分类」用：只列已启用的，并标出默认项和不可用原因。"""
    default = default_provider()
    out: list[dict] = []
    for pid in enabled_providers():
        ready = provider_ready(pid)
        out.append(
            {
                "id": pid,
                "label": provider_label(pid),
                "ready": ready,
                "default": pid == default,
                "reason": "" if ready else "未配置歌词接口密钥（LYRICS_API_KEY）",
            }
        )
    return out


def _check_provider(pid: str, *, for_search: bool) -> str:
    """校验来源入参，返回归一后的 id。

    - 搜索：必须是在 LYRICS_PROVIDERS 里启用的来源（用户点的是「分类」）
    - 取词：只要是已知且凭据齐全的来源就放行——歌单里存的是历史选择，
      后来把某个来源从配置里去掉，也不该让老歌单瞬间变哑
    """
    enabled = enabled_providers()
    raw = (pid or "").strip()
    if raw:
        norm = normalize_provider(raw)
        if not norm:
            raise LyricsApiError(f"未知的歌词来源：{pid}", code="bad_provider", status=400)
    else:
        # 没指定就用默认来源，但凭据检查一样要走：默认来源没配密钥时
        # 应该直接报「缺少凭据」，而不是带着空 key 去请求上游
        norm = default_provider()
    if for_search and norm not in enabled:
        names = "、".join(provider_label(p) for p in enabled)
        raise LyricsApiError(
            f"歌词来源「{provider_label(norm)}」未启用，当前可用：{names}（在 .env 的 LYRICS_PROVIDERS 里启用）",
            code="provider_disabled",
            status=400,
        )
    if not provider_ready(norm):
        raise LyricsApiError(
            f"歌词来源「{provider_label(norm)}」缺少凭据（LYRICS_API_KEY）",
            code="no_key",
        )
    return norm


def _use_netease() -> bool:
    """默认来源是否为网易云（老接口，保留给旧调用点/测试）。"""
    return default_provider() == "netease"


def search_songs(query: str, limit: int = 8, provider: str = "") -> list[dict]:
    """按指定来源搜索歌曲。

    返回 items（id/title/artist/album/duration_ms/cover_url/provider）。
    provider 留空 = 默认来源（enabled_providers 里第一个可用的）。
    """
    from .config import settings

    prov = _check_provider(provider, for_search=True)
    if prov == "netease":
        return netease_search(query, limit)

    query = query.strip()
    if not query:
        return []
    cache_key = f"{prov}|{limit}|{query}"
    if (hit := _cache_get(_search_cache, cache_key, SEARCH_TTL)) is not None:
        return hit
    url = (
        f"{settings.LYRICS_API_BASE}/search.php?"
        + urllib.parse.urlencode({"q": query, "limit": limit})
    )
    body = _get(url, settings.LYRICS_API_KEY, cookie=settings.LYRICS_API_COOKIE)
    if not body.get("ok"):
        raise LyricsApiError("歌词搜索失败", code="upstream")
    items = body.get("data", {}).get("items") or []
    for item in items:
        item.setdefault("provider", "lyriclib")
    _cache_put(_search_cache, cache_key, items)
    return items


def fetch_lyrics(song_id: int, provider: str = "") -> dict:
    """按 (来源, 歌曲 id) 取歌词，返回 {lines, granularity?, ...}。

    provider 为空时用当前默认来源；歌单里已保存的偏好会带上
    当时的来源，避免两个来源的数字 id 撞车。
    """
    from .config import settings

    prov = _check_provider(provider, for_search=False)
    cache_key = (prov, song_id)
    if (hit := _cache_get(_lyrics_cache, cache_key, LYRICS_TTL)) is not None:
        return hit
    if prov == "netease":
        # Cookie 优先用后台上扫码登录保存的，其次 .env（都在服务端，不下发浏览器）
        from . import netease_auth

        data = netease_lyrics(song_id, netease_auth.current_cookie())
        data["provider"] = "netease"
    else:
        url = (
            f"{settings.LYRICS_API_BASE}/lyrics.php?"
            + urllib.parse.urlencode({"song": song_id, "lang": "both", "gran": "auto"})
        )
        body = _get(url, settings.LYRICS_API_KEY, cookie=settings.LYRICS_API_COOKIE)
        if not body.get("ok"):
            raise LyricsApiError("歌词获取失败", code="upstream")
        data = body.get("data") or {}
        data["provider"] = "lyriclib"
    _cache_put(_lyrics_cache, cache_key, data)
    return data


def find_and_fetch(title: str, artist: str = "") -> dict:
    """按「标题 歌手」搜索并取最匹配歌曲的歌词。

    同时启用多个来源时按配置顺序依次尝试，先用先得（网易云在前就优先网易云）。
    匹配策略：先「标题 + 歌手」，没结果退回仅标题；命中后取第一条。
    返回 {provider, granularity?, lines: [...], matched: {...}}。
    """
    q_parts = [part for part in (title.strip(), artist.strip()) if part]
    if not q_parts:
        raise LyricsApiError("缺少歌名", code="missing_param")

    providers = [p for p in enabled_providers() if provider_ready(p)] or [default_provider()]
    errors: list[str] = []
    for prov in providers:
        try:
            items = search_songs(" ".join(q_parts), provider=prov)
            if not items and artist.strip() and title.strip():
                items = search_songs(title.strip(), provider=prov)
        except LyricsApiError as exc:
            # 单个来源挂了不该让整次匹配失败：记下来继续试下一个
            errors.append(exc.message or provider_label(prov))
            continue
        if not items:
            continue
        best = items[0]
        data = fetch_lyrics(best["id"], best.get("provider") or prov)
        data["matched"] = {
            "id": best["id"],
            "provider": best.get("provider", prov),
            **{k: best.get(k, "") for k in ("title", "artist", "album")},
        }
        return data

    if errors and len(errors) == len(providers):
        raise LyricsApiError(errors[0], code="upstream")
    raise LyricsApiError("没有找到这首歌", code="song_not_found", status=404)


# --------------------------------------------------------------------------- #
# 网易云 provider：免密钥搜索 + 逐字（yrc）/逐行（lrc）歌词
# --------------------------------------------------------------------------- #
_NETEASE_SEARCH_URL = "https://music.163.com/api/search/get/web"
_NETEASE_LYRIC_URL = "https://interface.music.163.com/api/song/lyric/v1"
# 网页版同款 UA / Referer / Cookie：缺了会被当成脚本请求，拿不到翻译等完整数据
_NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Cookie": "os=pc; appver=2.10.13",
}
_YRC_HEAD_RE = re.compile(r"^\[(\d+),(\d+)\]")
_YRC_TOKEN_RE = re.compile(r"\((\d+),(\d+)(?:,(\d+))?\)")
# 元数据行两种写法都收：键带引号的 "t":16000 与不带引号的 t:16000
_YRC_T_RE = re.compile(r'["\']?t["\']?\s*[:=]\s*(\d+)')
_YRC_TX_RE = re.compile(r'["\']?tx["\']?\s*[:=]\s*"((?:[^"\\]|\\.)*)"')


def _netease_get(url: str, extra_cookie: str = "") -> dict:
    headers = dict(_NETEASE_HEADERS)
    if extra_cookie.strip():
        # NETEASE_COOKIE 允许填完整的网页版 Cookie，也允许只填 MUSIC_U=...
        headers["Cookie"] = headers["Cookie"] + "; " + extra_cookie.strip().strip(";")
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=12.0) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise LyricsApiError(
            f"网易云接口返回 HTTP {exc.code}",
            code="upstream",
            status=exc.code,
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, http.client.HTTPException) as exc:
        raise LyricsApiError("网易云服务暂时不可用", code="network") from exc


def netease_search(query: str, limit: int = 8) -> list[dict]:
    """网易云单曲搜索（type=1），返回归一化条目，免密钥可用。"""
    query = query.strip()
    if not query:
        return []
    cache_key = f"netease|{limit}|{query}"
    if (hit := _cache_get(_search_cache, cache_key, SEARCH_TTL)) is not None:
        return hit
    url = _NETEASE_SEARCH_URL + "?" + urllib.parse.urlencode({"s": query, "type": 1, "limit": limit})
    body = _netease_get(url)
    if body.get("code") != 200:
        raise LyricsApiError(f"网易云搜索失败（code {body.get('code')}）", code="upstream")
    items: list[dict] = []
    for song in (body.get("result") or {}).get("songs") or []:
        album = song.get("album") or {}
        items.append(
            {
                "id": int(song["id"]),
                "title": (song.get("name") or "").strip(),
                "artist": "/".join(a.get("name", "") for a in song.get("artists") or []).strip(),
                "album": (album.get("name") or "").strip(),
                "duration_ms": int(song.get("duration") or 0),
                "cover_url": album.get("picUrl") or "",
                "provider": "netease",
            }
        )
    _cache_put(_search_cache, cache_key, items)
    return items


def _parse_yrc_json_line(raw: str) -> dict | None:
    """yrc 里的元数据行（`[{t:8120,c:[{tx:"作词 : …"}]}]`）→ 无字块的普通行。

    这种行不是标准 JSON（键没有引号），用正则取时间与文本即可。
    """
    m = _YRC_T_RE.search(raw)
    texts = _YRC_TX_RE.findall(raw)
    if m is None or not texts:
        return None
    start = int(m.group(1))
    text = "".join(texts).strip()
    if not text:
        return None
    return {"start": start, "end": start + 4000, "text": text}


def _parse_yrc(text: str) -> list[dict]:
    """网易云逐字歌词（yrc）→ 行数组。

    行格式：`[起始ms,时长ms](起始ms,时长ms,0)字(起始ms,时长ms,0)字…`；
    间奏行只有头部、没有字块，保留为空文本行（前端会跳过渲染）。
    """
    lines: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head = _YRC_HEAD_RE.match(line)
        if head is None:
            row = _parse_yrc_json_line(line)
            if row is not None:
                lines.append(row)
            continue
        start, dur = int(head.group(1)), int(head.group(2))
        tokens = list(_YRC_TOKEN_RE.finditer(line))
        words: list[dict] = []
        for i, tk in enumerate(tokens):
            seg = line[tk.end(): tokens[i + 1].start() if i + 1 < len(tokens) else len(line)]
            seg = seg.strip()
            if not seg:
                continue  # 间奏占位块（空文本）
            w_start, w_dur = int(tk.group(1)), int(tk.group(2))
            words.append({"start": w_start, "end": w_start + w_dur, "text": seg})
        row = {
            "start": start,
            "end": max(start + dur, start + 1),
            "text": "".join(w["text"] for w in words),
        }
        if words:
            row["words"] = words
        lines.append(row)
    lines.sort(key=lambda r: r["start"])
    return lines


def _tlyric_map(text: str) -> dict[int, str]:
    """LRC 形式的翻译行（tlyric）→ {起始ms: 翻译文本}。"""
    out: dict[int, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = [_lrc_ms(m) for m in _TIME_RE.finditer(line)]
        if not stamps:
            continue
        content = _TIME_RE.sub("", line).strip()
        if content:
            out.setdefault(stamps[0], content)
    return out


def _merge_trans(lines: list[dict], tmap: dict[int, str]) -> None:
    """把翻译按起始时间贴回行数组（不覆盖已有翻译）。

    yrc 行与 tlyric 行的起始时间严格相等；lrc 百分秒与 yrc 毫秒之间
    可能有 10ms 级舍入差，做一次 ±10/±20 的就近匹配。
    """
    for row in lines:
        if row.get("trans"):
            continue
        trans = tmap.get(row["start"])
        if not trans:
            for off in (-10, 10, -20, 20):
                trans = tmap.get(row["start"] + off)
                if trans:
                    break
        if trans:
            row["trans"] = trans


def netease_lyrics(song_id: int, cookie: str = "") -> dict:
    """取网易云歌词：有逐字（yrc，需登录 Cookie）用逐字，否则逐行（lrc）。

    返回 {lines, granularity: "word"|"line"}；两种都没有时按 404 报错。
    """
    url = _NETEASE_LYRIC_URL + "?" + urllib.parse.urlencode(
        {"id": song_id, "cv": 4747474, "lv": 0, "kv": 0, "tv": 0, "rv": 0}
    )
    body = _netease_get(url, cookie)
    if body.get("code") != 200:
        raise LyricsApiError(
            f"网易云歌词获取失败（code {body.get('code')}）", code="upstream"
        )

    yrc = ((body.get("yrc") or {}).get("lyric") or "").strip()
    lrc = ((body.get("lrc") or {}).get("lyric") or "").strip()
    tmap = _tlyric_map(((body.get("tlyric") or {}).get("lyric") or ""))

    if yrc:
        lines = _parse_yrc(yrc)
        _merge_trans(lines, tmap)
        return {"lines": lines, "granularity": "word"}
    if lrc:
        lines, unsynced = parse_lrc(lrc)
        _merge_trans(lines, tmap)
        result: dict = {"lines": lines, "granularity": "line"}
        if unsynced:
            result["unsynced"] = True
        return result
    raise LyricsApiError("这首歌在网易云没有歌词", code="song_not_found", status=404)


# --------------------------------------------------------------------------- #
# 内嵌歌词与 LRC 解析
# --------------------------------------------------------------------------- #
_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|hash|total|sign|length|encoding):(.*)\]$", re.IGNORECASE)
_WORD_RE = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")


def embedded_lyrics(path: Path) -> str:
    """提取音频文件内嵌歌词文本（通常是 LRC）。

    按容器取：ID3 USLT（MP3/WAV/AIFF/DSF）· MP4 ©lyr ·
    Vorbis LYRICS/UNSYNCEDLYRICS · ASF WM/Lyrics · APEv2 Lyrics。
    没有内嵌歌词返回空串，绝不抛异常。
    """
    mf, tags_only = _load_audio(path)
    try:
        tags = mf.tags if mf is not None and mf.tags else tags_only
    except Exception:
        tags = tags_only
    if not tags:
        return ""

    try:
        uslts = tags.getall("USLT") if hasattr(tags, "getall") else []
    except Exception:
        uslts = []
    for frame in uslts:  # ID3v2 非同步歌词
        text = getattr(frame, "text", None)
        if text and str(text).strip():
            return str(text)

    def _get(key: str) -> str:
        try:
            vals = tags.get(key)
        except Exception:
            return ""
        if not vals:
            return ""
        value = vals[0]
        return str(value).strip() if value is not None else ""

    for key in ("©lyr", "LYRICS", "UNSYNCEDLYRICS", "UNSYNCED LYRICS", "LYRIC", "WM/Lyrics", "Lyrics"):
        val = _get(key)
        if val:
            return val
    return ""


def _lrc_ms(match: re.Match) -> int:
    minute = int(match.group(1))
    sec = int(match.group(2))
    frac = match.group(3) or ""
    ms = minute * 60_000 + sec * 1000
    if frac:
        ms += int(frac) * 10 ** (3 - len(frac))  # 2 位百分秒 / 3 位毫秒 / 1 位十分秒
    return ms


def parse_lrc(text: str) -> tuple[list[dict], bool]:
    """LRC 文本（含增强 A2 词级 <mm:ss.xx> 标签）→ 行数组。

    返回 (lines, unsynced)：有时间戳走同步渲染；整篇没有时间戳就按
    静态文本返回（start/end 都为 0，前端不高亮不跟随）。
    """
    offset = 0
    timed: list[tuple[int, str, list | None]] = []
    plain: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if (meta := _META_RE.match(line)):
            if meta.group(1).lower() == "offset":
                try:
                    offset = int(meta.group(2).strip())
                except ValueError:
                    pass
            continue
        stamps = [_lrc_ms(m) for m in _TIME_RE.finditer(line)]
        content = _TIME_RE.sub("", line).strip()
        if not stamps:
            if content:
                plain.append(content)
            continue

        # 增强型 A2：行内 <mm:ss.xx> 词级时间戳
        words: list[dict] | None = None
        wtags = list(_WORD_RE.finditer(content))
        if len(wtags) >= 2:
            words = []
            for i, w in enumerate(wtags):
                seg = content[w.end(): wtags[i + 1].start() if i + 1 < len(wtags) else len(content)]
                if seg:
                    words.append({"start": _lrc_ms(w), "end": 0, "text": seg})
            if len(words) < 2:
                words = None
            content = _WORD_RE.sub("", content).strip()
        for st in stamps:
            timed.append((max(0, st - offset), content, words))

    if not timed:
        # 全是没时间戳的裸文本：静态展示
        return [{"start": 0, "end": 0, "text": t} for t in plain], True

    timed.sort(key=lambda r: r[0])
    merged: list[dict] = []
    for st, content, words in timed:
        if not content and not words:
            continue
        prev = merged[-1] if merged else None
        if prev and prev["start"] == st:
            if content == prev["text"]:
                continue  # 完全重复的时间戳行
            if content and not prev["trans"]:
                prev["trans"] = content  # 第二条通常是翻译
                continue
            if not content and not words:
                continue
            # 已有翻译还来同时间戳的新内容 → 对唱另一人，保留为独立行
        merged.append({"start": st, "text": content, "trans": "", "words": words})

    lines: list[dict] = []
    for i, row in enumerate(merged):
        end = merged[i + 1]["start"] if i + 1 < len(merged) else row["start"] + 8000
        line: dict = {"start": row["start"], "end": max(end, row["start"] + 1), "text": row["text"]}
        if row["trans"]:
            line["trans"] = row["trans"]
        if row["words"]:
            words = row["words"]
            for j, w in enumerate(words):
                w["end"] = words[j + 1]["start"] if j + 1 < len(words) else max(end, w["start"] + 500)
            line["words"] = words
        lines.append(line)
    return lines, False


def _pref_song(pref) -> dict | None:
    if pref is None or not pref.song_id:
        return None
    return {
        "id": pref.song_id,
        "title": pref.song_title,
        "artist": pref.song_artist,
        "provider": pref.provider or "lyriclib",
    }


def resolve_lyrics(db, node, blob_path: Path, fallback_title: str) -> dict:
    """按每首歌的偏好解析最终歌词。

    优先级（auto 模式）：音频自带 → 在线搜索 → 无歌词；
    song/custom/none 是用户显式选择，直接生效。
    返回 {source, matched, lines, unsynced?, error?}，不抛异常。
    """
    from .models import AudioPref

    pref = db.get(AudioPref, node.id)
    mode = pref.mode if pref is not None else "auto"

    if mode == "none":
        return {"source": "none", "matched": None, "lines": []}

    if mode == "custom":
        text = (pref.custom_lyrics if pref else "") or ""
        if not text.strip():
            return {"source": "none", "matched": None, "lines": []}
        lines, unsynced = parse_lrc(text)
        return {"source": "custom", "matched": None, "lines": lines, "unsynced": unsynced}

    if mode == "song":
        if pref is None or not pref.song_id:
            return {"source": "none", "matched": None, "lines": []}
        try:
            # 老库存的偏好没有 provider 字段，全部是 lyriclib 时代的数字 id，
            # 不能拿去网易云查（两个来源的 id 不互通）
            data = fetch_lyrics(pref.song_id, pref.provider or "lyriclib")
        except LyricsApiError as exc:
            return {
                "source": "song", "matched": _pref_song(pref),
                "lines": [], "error": exc.message or "歌词获取失败",
            }
        return {
            "source": "song", "matched": _pref_song(pref),
            "lines": data.get("lines") or [],
            "provider": data.get("provider"), "granularity": data.get("granularity"),
        }

    # ---- auto：音频自带 → 在线搜索 → 无歌词 ----
    try:
        text = embedded_lyrics(blob_path)
    except Exception as exc:
        log.warning("内嵌歌词读取失败 %s：%s", blob_path.name, exc)
        text = ""
    if text.strip():
        lines, unsynced = parse_lrc(text)
        if lines:
            return {"source": "embedded", "matched": None, "lines": lines, "unsynced": unsynced}

    meta = audio_meta(blob_path, fallback_title)
    try:
        data = find_and_fetch(meta["title"], meta["artist"])
        return {
            "source": "online", "matched": data.get("matched"),
            "lines": data.get("lines") or [],
            "provider": data.get("provider"), "granularity": data.get("granularity"),
        }
    except LyricsApiError as exc:
        return {"source": "none", "matched": None, "lines": [], "error": exc.message or "没有找到这首歌的歌词"}
