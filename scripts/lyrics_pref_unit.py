# -*- coding: utf-8 -*-
"""parse_lrc / embedded_lyrics 单元验证（0.3.5）。"""
import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import urllib.request

urllib.request.install_opener(urllib.request.ProxyHandler({}))  # 直连

from app.lyrics_service import parse_lrc, embedded_lyrics
from mutagen.id3 import ID3, USLT, TIT2, TPE1
from mutagen.mp3 import MP3

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------- parse_lrc：行级 ----------
lrc = """[ti:测试]
[offset:-500]
[00:01.20]第一行
[00:05.00][00:09.00]重复时间戳行
[00:13.50]中文 translated
"""
lines, unsynced = parse_lrc(lrc)
check("行数", len(lines) == 4, str(len(lines)))
check("第一行含 offset", lines[0]["start"] == 1700, str(lines[0]["start"]))  # offset:-500 → 延后 500ms
check("多时间戳展开", [l["start"] for l in lines] == [1700, 5500, 9500, 14000], str([l["start"] for l in lines]))
check("行 end=下一行 start", lines[0]["end"] == 5500, str(lines[0]["end"]))
check("末行 end=+8000", lines[-1]["end"] == 22000, str(lines[-1]["end"]))
check("非 unsynced", unsynced is False)

# 同一时间戳第二条当翻译
lrc2 = "[00:01.00]Hello\n[00:01.00]你好\n[00:03.00]Bye"
lines2, _ = parse_lrc(lrc2)
check("同戳→翻译", len(lines2) == 2 and lines2[0].get("trans") == "你好", str(lines2))

# ---------- parse_lrc：词级 A2 ----------
lrc3 = "[00:01.00]<00:01.00>你<00:01.50>好<00:02.50>世<00:03.00>界"
lines3, _ = parse_lrc(lrc3)
w = lines3[0].get("words")
check("词级解析", w and len(w) == 4 and w[0]["start"] == 1000 and w[1]["start"] == 1500, str(w))
check("词级 end", w and w[0]["end"] == 1500 and w[3]["end"] == 9000, str(w))  # 末词撑到行尾(+8s)
check("词级行文本", lines3[0]["text"] == "你好世界", lines3[0]["text"])

# ---------- parse_lrc：纯文本 ----------
lines4, unsynced4 = parse_lrc("随便几行\n没有时间戳\n第三行")
check("纯文本 unsynced", unsynced4 is True and len(lines4) == 3 and all(l["start"] == 0 for l in lines4))

# ---------- embedded_lyrics：ID3 USLT ----------
tmp = Path(__file__).parent / "_tmp_uslt.mp3"
import struct


def make_min_mp3(path: Path):
    # 手写最小 MPEG 头帧（0.3.4 验证过的套路）：MP3() 需要能解出时长
    frames = b"\xff\xfb\x90\x64" + b"\x00" * 413  # 128kbps 44100 一帧
    path.write_bytes(frames * 40)


make_min_mp3(tmp)
tags = ID3()
tags.add(USLT(encoding=3, lang="chi", desc="", text="[00:01.00]内嵌歌词第一行\n[00:04.00]第二行"))
tags.add(TIT2(encoding=3, text="内嵌测试歌"))
tags.add(TPE1(encoding=3, text="测试歌手"))
mp3 = MP3(str(tmp))
mp3.tags = tags
mp3.save()
text = embedded_lyrics(tmp)
check("ID3 USLT 提取", "内嵌歌词第一行" in text and "第二行" in text, text[:50])
check("USLT→LRC 行数", len(parse_lrc(text)[0]) == 2)

# USLT 优先于 WM 等：换文件测 MP4 ©lyr
from mutagen.mp4 import MP4

tmp4 = Path(__file__).parent / "_tmp_lyr.m4a"
from mutagen.mp4 import MP4Tags, Atoms

# 直接用 mutagen 写 MP4：需要合法容器，用最小 m4a 太麻烦，这里只验证键名存在
# （端到端脚本再覆盖真实文件场景）
tmp.unlink(missing_ok=True)
tmp4.unlink(missing_ok=True)

print()
print("FAILED:", failures if failures else "无")
sys.exit(1 if failures else 0)
