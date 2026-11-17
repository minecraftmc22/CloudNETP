# -*- coding: utf-8 -*-
"""音视频元数据标签冒烟：拿手工构造的容器样本逐格式验解析，再走一遍 AI 工具链路。

样本都是「结构正确、负载随便填」的最小文件——解析器只读标签区和头部，
不需要真正的音频码流，所以不引入 ffmpeg。

    ./.venv/Scripts/python.exe scripts/media_tags_smoke.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-media-tags-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(_TMP / "storage"))

from app import media_tags, node_service  # noqa: E402
from app import ai_service as ai  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import User  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}  {extra}")


# --------------------------------------------------------------------------- #
# 样本构造
# --------------------------------------------------------------------------- #
def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3_frame(fid: str, payload: bytes) -> bytes:
    return fid.encode("latin-1") + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _utf8(text: str) -> bytes:
    return b"\x03" + text.encode("utf-8")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2048 + b"\xff\xd9"


def build_id3_mp3() -> bytes:
    frames = b"".join(
        [
            _id3_frame("TIT2", _utf8("晴天")),
            _id3_frame("TPE1", _utf8("周杰伦")),
            _id3_frame("TPE2", _utf8("周杰伦")),
            _id3_frame("TALB", _utf8("叶惠美")),
            _id3_frame("TYER", _utf8("2003")),
            _id3_frame("TRCK", _utf8("3/11")),
            _id3_frame("TCON", _utf8("Pop")),
            _id3_frame("TCOM", _utf8("周杰伦")),
            _id3_frame("TPUB", _utf8("杰威尔音乐")),
            _id3_frame("COMM", b"\x03eng\x00" + "主打歌".encode()),
            _id3_frame("APIC", b"\x03image/jpeg\x00\x03\x00" + JPEG),
        ]
    )
    tag = b"ID3\x03\x00\x00" + _syncsafe(len(frames)) + frames
    # FF FB 90 00：MPEG1 Layer3 / 128 kbps / 44.1 kHz / 立体声
    audio = b"\xff\xfb\x90\x00" + b"\x00" * (160_000 - 4)
    return tag + audio


def build_id3v1_mp3() -> bytes:
    audio = b"\xff\xfb\x90\x00" + b"\x00" * 40_000
    tail = (
        b"TAG"
        + b"Old Title".ljust(30, b"\x00")
        + b"Old Artist".ljust(30, b"\x00")
        + b"Old Album".ljust(30, b"\x00")
        + b"1998"
        + b"v1 comment".ljust(28, b"\x00")
        + b"\x00\x07"  # v1.1：注释里藏音轨号
        + bytes([17])  # Rock
    )
    return audio + tail


def _flac_block(btype: int, body: bytes, last: bool = False) -> bytes:
    return bytes([(0x80 if last else 0) | btype]) + len(body).to_bytes(3, "big") + body


def _vorbis_comment(pairs: list[tuple[str, str]]) -> bytes:
    vendor = b"cloudnetp-smoke"
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(pairs))
    for key, value in pairs:
        entry = f"{key}={value}".encode("utf-8")
        out += struct.pack("<I", len(entry)) + entry
    return out


def _flac_picture(mime: str, data: bytes) -> bytes:
    mime_b = mime.encode()
    return (
        struct.pack(">I", 3)
        + struct.pack(">I", len(mime_b))
        + mime_b
        + struct.pack(">I", 0)  # 描述
        + struct.pack(">IIII", 600, 600, 24, 0)
        + struct.pack(">I", len(data))
        + data
    )


def build_flac() -> bytes:
    # 采样率 44100 / 2 声道 / 16 bit / 总采样数 44100*95
    packed = (44100 << 44) | ((2 - 1) << 41) | ((16 - 1) << 36) | (44100 * 95)
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + struct.pack(">Q", packed) + b"\x00" * 16
    blocks = _flac_block(0, streaminfo)
    blocks += _flac_block(4, _vorbis_comment([
        ("TITLE", "夜曲"),
        ("ARTIST", "周杰伦"),
        ("ALBUM", "十一月的萧邦"),
        ("DATE", "2005-11-01"),
        ("TRACKNUMBER", "1"),
        ("TRACKTOTAL", "12"),
        ("GENRE", "Pop"),
        ("COMMENT", "flac 注释"),
    ]))
    blocks += _flac_block(6, _flac_picture("image/png", b"\x89PNG" + b"\x00" * 4096), last=True)
    return b"fLaC" + blocks + b"\x00" * 128


def _ogg_page(payload: bytes, seq: int, granule: int = 0, bos: bool = False) -> bytes:
    lacing = []
    rest = len(payload)
    while rest >= 255:
        lacing.append(255)
        rest -= 255
    lacing.append(rest)
    header = (
        b"OggS"
        + bytes([0, 0x02 if bos else 0x04])
        + struct.pack("<q", granule)
        + struct.pack("<I", 2024)  # serial
        + struct.pack("<I", seq)
        + b"\x00" * 4  # crc（解析器不校验）
        + bytes([len(lacing)])
        + bytes(lacing)
    )
    return header + payload


def build_ogg() -> bytes:
    ident = (
        b"\x01vorbis"
        + struct.pack("<I", 0)  # 版本
        + bytes([2])  # 声道
        + struct.pack("<I", 44100)
        + struct.pack("<I", 0)  # 最大码率
        + struct.pack("<I", 160000)  # 名义码率
        + struct.pack("<I", 0)
        + bytes([0xB8])  # 块大小
        + bytes([0x01])  # framing
    )
    comment = b"\x03vorbis" + _vorbis_comment([
        ("TITLE", "卡农"),
        ("ARTIST", "Pachelbel"),
        ("ALBUM", "Classic"),
        ("DATE", "1680"),
    ])
    setup = b"\x05vorbis" + b"\x00" * 32
    pages = [
        _ogg_page(ident, 0, 0, bos=True),
        _ogg_page(comment, 1),
        _ogg_page(setup, 2),
        _ogg_page(b"\x00" * 64, 3, granule=44100 * 210),
    ]
    return b"".join(pages)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def build_m4a() -> bytes:
    ftyp = _box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">I", 1000) + struct.pack(">I", 233_000) + b"\x00" * 80)
    # tkhd 最后 8 字节是宽高的 16.16 定点（纯音频为 0，这里只验时长/标签）
    tkhd = _box(
        b"tkhd",
        b"\x00\x00\x00\x07"
        + b"\x00" * 8  # creation / modification
        + b"\x00" * 4  # track id
        + b"\x00" * 4  # 保留
        + b"\x00" * 4  # duration
        + b"\x00" * 8  # 保留
        + b"\x00" * 8  # layer / alternate / volume / 保留
        + b"\x00" * 36  # matrix
        + struct.pack(">I", 0)  # width
        + struct.pack(">I", 0),
    )

    def data(value: bytes, flag: int = 1) -> bytes:
        return _box(b"data", struct.pack(">I", flag) + b"\x00" * 4 + value)

    ilst = _box(
        b"ilst",
        _box(b"\xa9nam", data("稻香".encode()))
        + _box(b"\xa9ART", data("周杰伦".encode()))
        + _box(b"aART", data("周杰伦".encode()))
        + _box(b"\xa9alb", data("魔杰座".encode()))
        + _box(b"\xa9day", data(b"2008"))
        + _box(b"\xa9gen", data("Pop".encode()))
        + _box(b"trkn", data(b"\x00\x00\x00\x01\x00\x0a\x00\x00", 0))
        + _box(b"\xa9wrt", data("周杰伦".encode()))
        + _box(b"covr", data(JPEG, 13)),
    )
    hdlr = _box(b"hdlr", b"\x00" * 8 + b"mdirappl" + b"\x00" * 9)
    meta = _box(b"meta", b"\x00\x00\x00\x00" + hdlr + ilst)
    udta = _box(b"udta", meta)
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd) + udta)
    return ftyp + moov + _box(b"mdat", b"\x00" * 4096)


def build_wav() -> bytes:
    seconds, rate, channels, bits = 6, 44100, 2, 16
    byte_rate = rate * channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, rate, byte_rate, channels * bits // 8, bits)
    data = b"\x00" * (byte_rate * seconds)
    info_body = b"INFO"
    for key, value in (("INAM", "测试音频"), ("IART", "测试者"), ("IPRD", "测试专辑"), ("ICRD", "2026")):
        raw = value.encode("utf-8") + b"\x00"
        info_body += key.encode() + struct.pack("<I", len(raw)) + raw + (b"\x00" if len(raw) % 2 else b"")
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"LIST" + struct.pack("<I", len(info_body)) + info_body
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def build_avi() -> bytes:
    avih = struct.pack(
        "<IIIIIIIIII",
        40000,  # 每帧微秒 → 25 fps
        0,
        0,
        0,
        250,  # 总帧数 → 10 秒
        0,
        1,
        0,
        1920,  # 宽
        1080,  # 高
    ) + b"\x00" * 16
    hdrl = b"hdrl" + b"avih" + struct.pack("<I", len(avih)) + avih
    chunks = b"LIST" + struct.pack("<I", len(hdrl)) + hdrl
    mdat = b"\x00" * 1024
    chunks += b"LIST" + struct.pack("<I", 4 + 18 + 12) + b"INFO" + b"INAM" + struct.pack("<I", 4) + b"\x00\x00\x00\x00" + b"ICMT" + struct.pack("<I", 6) + b"hello\x00"
    chunks += b"mdat" + struct.pack("<I", len(mdat)) + mdat
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"AVI " + chunks


def _vsize(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ValueError("太大")


def _el(elem_id: bytes, payload: bytes) -> bytes:
    return elem_id + _vsize(len(payload)) + payload


def build_mkv() -> bytes:
    doc = _el(b"\x1a\x45\xdf\xa3", _el(b"\x42\x82", b"matroska"))
    info = _el(
        b"\x15\x49\xa9\x66",
        _el(b"\x2a\xd7\xb1", (1_000_000).to_bytes(3, "big"))  # 1 ms
        + _el(b"\x44\x89", struct.pack(">d", 95_000.0)),  # 95 秒
    )
    tracks = _el(
        b"\x16\x54\xae\x6b",
        _el(
            b"\xae",
            _el(b"\xe0", _el(b"\xb0", (1280).to_bytes(2, "big")) + _el(b"\xba", (720).to_bytes(2, "big"))),
        ),
    )
    tags = _el(
        b"\x12\x54\xc4\x67",
        _el(
            b"\x73\x73",
            _el(b"\x67\xc8", _el(b"\x45\xa3", b"TITLE") + _el(b"\x44\x87", "旅行的意义".encode()))
            + _el(b"\x67\xc8", _el(b"\x45\xa3", b"ARTIST") + _el(b"\x44\x87", "陈绮贞".encode()))
            + _el(b"\x67\xc8", _el(b"\x45\xa3", b"DATE_RELEASED") + _el(b"\x44\x87", b"2004")),
        ),
    )
    segment = _el(b"\x18\x53\x80\x67", info + tracks + tags + _el(b"\x1f\x43\xb6\x75", b"\x00" * 4096))
    return doc + segment


# --------------------------------------------------------------------------- #
# 解析断言
# --------------------------------------------------------------------------- #
def parse_bytes(data: bytes, name: str) -> media_tags.MediaInfo:
    path = _TMP / name
    path.write_bytes(data)
    return media_tags.parse(path)


print("== 各容器解析 ==")

info = parse_bytes(build_id3_mp3(), "song.mp3")
check("MP3 容器识别", info.container.startswith("MP3（ID3v2.3"), info.container)
check("MP3 标题", info.tags.get("title") == "晴天", info.tags.get("title"))
check("MP3 艺术家", info.tags.get("artist") == "周杰伦", info.tags.get("artist"))
check("MP3 专辑", info.tags.get("album") == "叶惠美")
check("MP3 年份", info.tags.get("date") == "2003")
check("MP3 音轨", info.tags.get("track") == "3/11", info.tags.get("track"))
check("MP3 作曲", info.tags.get("composer") == "周杰伦")
check("MP3 注释", info.tags.get("comment") == "主打歌", info.tags.get("comment"))
check("MP3 封面", (info.cover or {}).get("mime") == "image/jpeg" and info.cover["size"] > 2000, str(info.cover))
check("MP3 码率", info.tech.get("码率") == "128 kbps", info.tech.get("码率"))
check("MP3 采样率", info.tech.get("采样率") == "44100 Hz")
check("MP3 时长（CBR 估算）", info.duration and 9.5 < info.duration < 10.5, str(info.duration))

info = parse_bytes(build_id3v1_mp3(), "old.mp3")
check("ID3v1 标题", info.tags.get("title") == "Old Title", info.tags.get("title"))
check("ID3v1 艺术家", info.tags.get("artist") == "Old Artist")
check("ID3v1 音轨", info.tags.get("track") == "7", info.tags.get("track"))
check("ID3v1 流派", info.tags.get("genre") == "Rock", info.tags.get("genre"))

info = parse_bytes(build_flac(), "song.flac")
check("FLAC 容器", info.container == "FLAC")
check("FLAC 标题", info.tags.get("title") == "夜曲", info.tags.get("title"))
check("FLAC 日期", info.tags.get("date") == "2005-11-01")
check("FLAC 音轨带总数", info.tags.get("track") == "1/12", info.tags.get("track"))
check("FLAC 位深", info.tech.get("位深") == "16 bit", str(info.tech))
check("FLAC 时长", info.duration and abs(info.duration - 95) < 0.01, str(info.duration))
check("FLAC 封面", (info.cover or {}).get("mime") == "image/png", str(info.cover))

info = parse_bytes(build_ogg(), "song.ogg")
check("OGG 容器", info.container == "Ogg Vorbis", info.container)
check("OGG 标题", info.tags.get("title") == "卡农", info.tags.get("title"))
check("OGG 采样率", info.tech.get("采样率") == "44100 Hz", str(info.tech))
check("OGG 时长（末页 granule）", info.duration and abs(info.duration - 210) < 0.5, str(info.duration))

info = parse_bytes(build_m4a(), "song.m4a")
check("M4A 容器", info.container == "M4A", info.container)
check("M4A 标题", info.tags.get("title") == "稻香", info.tags.get("title"))
check("M4A 艺术家", info.tags.get("artist") == "周杰伦")
check("M4A 专辑", info.tags.get("album") == "魔杰座")
check("M4A 音轨", info.tags.get("track") == "1/10", info.tags.get("track"))
check("M4A 时长", info.duration and abs(info.duration - 233) < 0.01, str(info.duration))
check("M4A 封面", (info.cover or {}).get("mime") == "image/jpeg", str(info.cover))

info = parse_bytes(build_wav(), "voice.wav")
check("WAV 容器", info.container == "WAV")
check("WAV 时长", info.duration and abs(info.duration - 6) < 0.01, str(info.duration))
check("WAV 声道/位深", info.tech.get("声道") == "2" and info.tech.get("位深") == "16 bit", str(info.tech))
check("WAV LIST/INFO 标题", info.tags.get("title") == "测试音频", info.tags.get("title"))
check("WAV LIST/INFO 艺术家", info.tags.get("artist") == "测试者")

info = parse_bytes(build_avi(), "clip.avi")
check("AVI 容器", info.container == "AVI")
check("AVI 分辨率", info.tech.get("分辨率") == "1920×1080", str(info.tech))
check("AVI 时长（avih）", info.duration and abs(info.duration - 10) < 0.01, str(info.duration))

info = parse_bytes(build_mkv(), "clip.mkv")
check("MKV 容器", info.container.startswith("Matroska"), info.container)
check("MKV 时长", info.duration and abs(info.duration - 95) < 0.01, str(info.duration))
check("MKV 分辨率", info.tech.get("分辨率") == "1280×720", str(info.tech))
check("MKV 标签", info.tags.get("title") == "旅行的意义" and info.tags.get("artist") == "陈绮贞", str(info.tags))

# 解析失败与空文件的友好报错
for name, data in (("bad.bin", b"\x00" * 32), ("empty.mp3", b"")):
    path = _TMP / name
    path.write_bytes(data)
    try:
        media_tags.parse(path)
        check(f"{name} 该报错", False)
    except media_tags.MediaTagError as exc:
        check(f"{name} 报 MediaTagError", bool(str(exc)))

# 文本摘要
text = media_tags.describe(info, "clip.mkv")
check("摘要含容器", "Matroska" in text)
check("摘要含时长", "时长：1:35" in text, text)
check("摘要含标题行", "标题：旅行的意义" in text)
check("摘要含分辨率", "分辨率：1280×720" in text)

# 封面很长时不把 base64 塞进文本
mp3_text = media_tags.describe(parse_bytes(build_id3_mp3(), "song2.mp3"), "song2.mp3")
check("摘要里封面只写大小", "封面：内嵌 image/jpeg" in mp3_text, mp3_text)
check("摘要不含图片数据", "\xff\xd8" not in mp3_text)

_EXT_HINTS = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".wav",
              ".mp4", ".mkv", ".webm", ".avi", ".mov")

# 两条路径（mutagen / 自有解析）必须给出同样的标签，防止逻辑各自漂移
own = media_tags.MediaInfo()
flac_path = _TMP / "song.flac"
with flac_path.open("rb") as fh:
    media_tags._parse_own(fh, own, flac_path.stat().st_size, "flac")
check("FLAC 自有解析标题与主路径一致",
      own.tags.get("title") == media_tags.parse(flac_path).tags.get("title"),
      f"{own.tags.get('title')}")

print("== 工具注册 ==")
cfg_base = {"tools": True, "readContent": True, "vision": False, "writeFiles": False, "moveFiles": False, "quickTransfer": False}
names = [spec["function"]["name"] for spec in ai.tool_specs(cfg_base)]
check("开「查看文件内容」即下发", "read_media_tags" in names, str(names))
check("不看图的模型也有该工具", "read_image" not in names and "read_media_tags" in names, str(names))
cfg_off = dict(cfg_base, readContent=False)
check("关掉查看内容就不下发", "read_media_tags" not in [s["function"]["name"] for s in ai.tool_specs(cfg_off)])
check("权限映射为 read", ai.TOOL_PERMISSION.get("read_media_tags") == "read")
check("工具标签", ai.TOOL_LABELS.get("read_media_tags") == "查看音视频标签")
check("工具总数 +1", len(ai.READ_TOOL_SPECS) == 3, str(len(ai.READ_TOOL_SPECS)))

print("== 工具链路（_run_tool） ==")
ensure_schema(engine)
with SessionLocal() as db:
    user = User(username="media", password_hash="x", display_name="Media", quota_bytes=1 << 30)
    db.add(user)
    db.commit()
    uid = int(user.id)

    sha, size = ai._store_bytes(build_id3_mp3())
    node_service.create_file(db, db.get(User, uid), None, "晴天.mp3", sha, size, "audio/mpeg")
    sha2, size2 = ai._store_bytes(build_mkv())
    node_service.create_file(db, db.get(User, uid), None, "旅行.mkv", sha2, size2, "video/x-matroska")
    sha3, size3 = ai._store_bytes(b"hello")
    node_service.create_file(db, db.get(User, uid), None, "note.txt", sha3, size3, "text/plain")
    node_service.recalc_usage(db, db.get(User, uid))
    db.commit()

    owner = db.get(User, uid)
    out = ai._run_tool(db, owner, "read_media_tags", {"path": "晴天.mp3"}, {"read": True})
    check("工具返回 MP3 摘要", "标题：晴天" in out and "艺术家：周杰伦" in out, out[:120])
    out = ai._run_tool(db, owner, "read_media_tags", {"path": "旅行.mkv"}, {"read": True})
    check("工具返回 MKV 摘要", "分辨率：1280×720" in out and "时长：1:35" in out, out[:160])

    try:
        ai._run_tool(db, owner, "read_media_tags", {"path": "note.txt"}, {"read": True})
        check("非音视频被拒", False)
    except ai.ToolError as exc:
        check("非音视频被拒", "不是音频或视频" in str(exc), str(exc))
    try:
        ai._run_tool(db, owner, "read_media_tags", {"path": "nope.mp3"}, {"read": True})
        check("找不到文件被拒", False)
    except ai.ToolError as exc:
        check("找不到文件被拒", "找不到" in str(exc), str(exc))
    try:
        ai._run_tool(db, owner, "read_media_tags", {"path": "晴天.mp3"}, {})
        check("没授权时报权限", False)
    except ai.ToolError as exc:
        check("没授权时报权限", "查看文件内容" in str(exc), str(exc))

    # 审计应该记了三条成功调用
    from sqlalchemy import func, select

    from app.models import AuditLog

    n = db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai_tool")
    )
    check("成功调用记了审计", int(n or 0) >= 2, str(n))

# 有真实样本时顺手验一遍（MEDIA_SAMPLE_DIR 指向一个目录即可；没有样本就跳过，
# 不联网、也不去仓库里塞二进制）
sample_dir = os.environ.get("MEDIA_SAMPLE_DIR", "").strip()
if sample_dir and Path(sample_dir).is_dir():
    print("== 真实样本 ==")
    for path in sorted(Path(sample_dir).iterdir()):
        if path.suffix.lower() not in _EXT_HINTS:
            continue
        if not media_tags.sniff(path):
            print(f"  --  {path.name} 文件头不是媒体，跳过（可能是伪装/下载失败的文件）")
            continue
        try:
            info = media_tags.parse(path)
            check(f"{path.name} 容器", bool(info.container), info.container)
            check(f"{path.name} 时长", bool(info.duration), str(info.duration))
            check(f"{path.name} 有可读信息", bool(info.tags or info.tech), str(info.tech))
        except media_tags.MediaTagError as exc:
            check(f"{path.name} 解析", False, str(exc))

print()
print(f"通过 {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)