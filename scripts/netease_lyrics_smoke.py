# -*- coding: utf-8 -*-
"""网易云歌词 provider（逐字 / 逐行）的回归。

三个部分：

  A. 离线解析：yrc 逐字时间轴、间奏行、元数据行；tlyric 翻译对齐；
     parse_lrc 兜底路径（无 yrc 时）
  B. provider 分发：LYRICS_PROVIDER / NETEASE_COOKIE 的读取路径、
     无密钥时 lyriclib 的报错、audio_prefs.provider 老库补列
  C. 线上冒烟：真实搜索 + 真实歌词（网易云匿名可拿逐行；有 Cookie 时逐字）

    ./.venv/Scripts/python.exe scripts/netease_lyrics_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "smoke-secret")

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


from app import lyrics_service as ls  # noqa: E402
from app.config import settings  # noqa: E402

# --------------------------------------------------------------------------- #
# A. 离线解析
# --------------------------------------------------------------------------- #
print("A. 离线解析")

YRC_SAMPLE = """[8120,2440](8120,810,0) devised(8930,240,0) test
[12000,3000]
[{"t":16000,"c":[{"tx":"作词 : 测试"},{"tx":" 作曲 : 测试"}]}]
[30000,1500](30000,700,0)完(30700,800,0)结
"""

lines = ls._parse_yrc(YRC_SAMPLE)
check("yrc 行数", len(lines) == 4, str(len(lines)))
w0 = lines[0].get("words") or []
check("逐字字块时间轴", len(w0) == 2 and w0[0]["start"] == 8120 and w0[1]["end"] == 9170, str(w0))
check("行文本是字块拼接", lines[0]["text"] == "devisedtest", repr(lines[0]["text"]))
check("间奏行保留为空文本", lines[1]["text"] == "" and "words" not in lines[1])
check("JSON 元数据行", lines[2]["text"].startswith("作词") and lines[2]["start"] == 16000)
check("中文逐字", lines[3]["words"][1]["text"] == "结")

tmap = ls._tlyric_map("[00:30.00]line3 translation\n[00:08.12]meta translation\n[00:09.00]")
check(
    "tlyric 只收有文本的行",
    tmap == {30000: "line3 translation", 8120: "meta translation"},
    str(tmap),
)
ls._merge_trans(lines, tmap)
check("翻译贴到逐字行", lines[3].get("trans") == "line3 translation")
check("翻译贴到元数据行", lines[0].get("trans") == "meta translation")
check("间奏行没有翻译", "trans" not in lines[1])

# 无 yrc 时退回 parse_lrc：A2 增强行也要能解析出字块
lrc_only = ls.netease_lyrics.__doc__  # 仅确认函数存在（真实路径在 C 段跑）
lines2, unsynced = ls.parse_lrc("[00:05.00]<00:05.00>增<00:05.50>强<00:06.00>行")
check("parse_lrc 词级标签", not unsynced and bool(lines2[0].get("words")), str(bool(lines2 and lines2[0].get("words"))))

# --------------------------------------------------------------------------- #
# B. provider 分发与补列
# --------------------------------------------------------------------------- #
print("B. provider 分发")

check("默认来源是 netease", ls._use_netease() is True, settings.LYRICS_PROVIDER)
old = settings.LYRICS_PROVIDER
old_key = settings.LYRICS_API_KEY
try:
    settings.LYRICS_PROVIDER = "lyriclib"
    settings.LYRICS_API_KEY = ""  # 本机 .env 可能真配了密钥，测试要的是「没配」的路径
    check("切到 lyriclib", ls._use_netease() is False)
    try:
        ls.search_songs("anything")
        check("无密钥时报错", False, "没有抛异常")
    except ls.LyricsApiError as exc:
        check("无密钥时报错", exc.code == "no_key", exc.code)
finally:
    settings.LYRICS_PROVIDER = old
    settings.LYRICS_API_KEY = old_key

# fetch_lyrics 的 provider 入参优先于默认配置（真连网在 C 段做）
check("fetch_lyrics 签名支持 provider", "provider" in ls.fetch_lyrics.__code__.co_varnames)

# 老库补列：建一个没有 provider 列的 audio_prefs，再跑 _sync_columns
import sqlalchemy  # noqa: E402
from sqlalchemy import Column, ForeignKey, Integer, String, Text, inspect, text as sql_text  # noqa: E402

from app.db import _sync_columns  # noqa: E402
from app.models import Base  # noqa: E402

import tempfile  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="netease_smoke_"))
try:
    old_engine = sqlalchemy.create_engine(f"sqlite:///{tmp / 'old.db'}")
    meta = sqlalchemy.MetaData()
    nodes = sqlalchemy.Table(
        "nodes", meta,
        Column("id", Integer, primary_key=True),
        Column("blob_sha256", String(64)),
        Column("name", String(255)),
    )
    prefs = sqlalchemy.Table(
        "audio_prefs", meta,
        Column("node_id", Integer, ForeignKey("nodes.id"), primary_key=True),
        Column("mode", String(8), nullable=False, default="auto"),
        Column("song_id", Integer),
        Column("song_title", String(200)),
        Column("song_artist", String(200)),
        Column("custom_lyrics", Text),
    )
    meta.create_all(old_engine)
    with old_engine.begin() as tx:
        tx.execute(sql_text(
            "INSERT INTO audio_prefs(node_id, mode, song_id, song_title, song_artist) "
            "VALUES (1, 'song', 186016, '晴天', '周杰伦')"
        ))
    _sync_columns(old_engine)
    cols = {c["name"] for c in inspect(old_engine).get_columns("audio_prefs")}
    check("audio_prefs.provider 补列", "provider" in cols, str(sorted(cols)))
    with old_engine.begin() as tx:
        provider = tx.execute(sql_text("SELECT provider FROM audio_prefs WHERE node_id = 1")).scalar()
    check("旧行 provider 为空串", provider == "", repr(provider))
finally:
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
# C. 线上冒烟（需要能访问 music.163.com；失败不中断，单独计数）
# --------------------------------------------------------------------------- #
print("C. 线上冒烟")
live_ok = 0
live_total = 0


def live(label: str, ok: bool, extra: str = "") -> None:
    global live_ok, live_total
    live_total += 1
    if ok:
        live_ok += 1
        check(label, True, extra)
    else:
        check(label, False, extra)


try:
    items = ls.netease_search("晴天 周杰伦", 5)
    live("网易云搜索", len(items) >= 1 and items[0]["provider"] == "netease", f"{len(items)} 条")
    lyr = ls.netease_lyrics(186016)
    has_words = any("words" in line for line in lyr["lines"])
    if settings.NETEASE_COOKIE:
        live("网易云逐字", lyr["granularity"] == "word" and has_words, lyr["granularity"])
        trans = sum(1 for line in lyr["lines"] if line.get("trans"))
        live("翻译行", trans > 0, f"{trans} 行")
    else:
        live("网易云逐行（无 Cookie）", lyr["granularity"] == "line" and len(lyr["lines"]) > 0,
             f"{len(lyr['lines'])} 行")
        live("无 Cookie 无逐字", not has_words)
    data = ls.fetch_lyrics(186016, "netease")
    live("fetch_lyrics 带 provider", data.get("provider") == "netease", str(data.get("provider")))
    found = ls.find_and_fetch("晴天", "周杰伦")
    live("find_and_fetch 返回来源", found.get("provider") == "netease"
         and found.get("matched", {}).get("provider") == "netease")
except ls.LyricsApiError as exc:
    live("网易云可达", False, f"{exc.code}: {exc.message}")

print(f"\n通过 {passed}/{passed + failed}"
      + (f"（含线上 {live_ok}/{live_total}）" if live_total else ""))
raise SystemExit(0 if failed == 0 else 1)
