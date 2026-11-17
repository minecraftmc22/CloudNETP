"""音视频元数据（标签）解析：给看板娘 AI 读文件的只读工具用。

**不引新依赖**：标签读取复用项目已有的 mutagen（``lyrics_service`` 也在用），
mutagen 不认识的容器（MKV / WebM / AVI）和它拿不到的信息（视频分辨率、
WAV 的 LIST/INFO）由本模块按格式直接读少量字节补上。

视频动辄几百 MB，所以自有解析全部走 ``seek`` 局部读取，绝不把文件整读进内存；
ID3 / Vorbis comment 这类标签区也有体积上限（``TAG_BUDGET``），
防止有人往标签里塞几十 MB 的封面把进程拖死。

支持范围（不认识就抛 :class:`MediaTagError`，由调用方转成友好提示）：

========================  ============================================
MP3                       ID3v2.2/2.3/2.4（含 APIC 封面）+ ID3v1；
                          首个帧头给码率/采样率，CBR 估算时长，Xing/Info 精确帧数
FLAC                      STREAMINFO（采样率/位深/时长）+ VORBIS_COMMENT + PICTURE
OGG（Vorbis / Opus）       识别头 + 注释头；末尾页 granule position 算时长
MP4 / M4A / MOV           moov/mvhd 时长、tkhd 宽高、udta/meta/ilst 标签
WAV                       fmt 采样参数 + LIST/INFO 标签
AVI                       avih 宽高与帧数 + LIST/INFO 标签
MKV / WebM                Info 时长、Tracks 宽高、Tags SimpleTag
========================  ============================================

标签键统一成小写英文（title / artist / album / date / track …），
方便模型理解；呈现给用户的文本由 :func:`describe` 生成中文。
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# 标签区读取上限：超过就只读前面这一截，不再往下翻
TAG_BUDGET = 8 * 1024 * 1024
# 单个标签值最多保留多少字（歌词、长评论会被截断）
TEXT_MAX_CHARS = 400
# 最多展示多少条标签
MAX_TAGS = 60
# 结构化容器（moov / Tracks / Tags）整段读入的上限
BOX_BUDGET = 32 * 1024 * 1024

# 规范键 → 中文名（顺序就是展示顺序）
LABELS: list[tuple[str, str]] = [
    ("title", "标题"),
    ("artist", "艺术家"),
    ("albumartist", "专辑艺术家"),
    ("album", "专辑"),
    ("date", "年份 / 日期"),
    ("genre", "流派"),
    ("track", "音轨"),
    ("disc", "碟号"),
    ("composer", "作曲"),
    ("conductor", "指挥"),
    ("lyricist", "作词"),
    ("publisher", "发行"),
    ("copyright", "版权"),
    ("encodedby", "编码者"),
    ("encoder", "编码软件"),
    ("bpm", "BPM"),
    ("key", "调性"),
    ("isrc", "ISRC"),
    ("language", "语言"),
    ("comment", "注释"),
    ("lyrics", "歌词"),
    ("description", "描述"),
]


class MediaTagError(Exception):
    """容器不认识、文件损坏或读不到任何标签。"""


@dataclass
class MediaInfo:
    """解析结果：container 是人看的格式名，tech 是技术参数（已是可读字符串）。"""

    container: str = ""
    duration: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    tech: dict[str, str] = field(default_factory=dict)
    cover: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, value: Any) -> None:
        text = _clean(value)
        if not text or key in self.tags or len(self.tags) >= MAX_TAGS:
            return
        self.tags[key] = text


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str:
    """标签值归一：去 NUL、压空白、掐掉超长歌词。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s{2,}", " ", text).strip(" \t")
    if len(text) > TEXT_MAX_CHARS:
        text = text[:TEXT_MAX_CHARS] + "…"
    return text


def _read_at(f, offset: int, size: int) -> bytes:
    f.seek(offset)
    return f.read(size)


def _u32be(b: bytes, off: int = 0) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _u64be(b: bytes, off: int = 0) -> int:
    return struct.unpack_from(">Q", b, off)[0]


def _u32le(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _syncsafe(b: bytes, off: int = 0) -> int:
    """ID3v2 的 7 bit 长度：4 字节里每字节只用低 7 位。"""
    if off + 4 > len(b):
        return 0
    return ((b[off] & 0x7F) << 21) | ((b[off + 1] & 0x7F) << 14) | ((b[off + 2] & 0x7F) << 7) | (b[off + 3] & 0x7F)


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --------------------------------------------------------------------------- #
# MP3 / ID3
# --------------------------------------------------------------------------- #
_ID3_FRAMES = {
    # v2.3 / v2.4
    "TIT2": "title",
    "TPE1": "artist",
    "TPE2": "albumartist",
    "TALB": "album",
    "TDRC": "date",
    "TYER": "date",
    "TDRL": "date",
    "TDOR": "date",
    "TRCK": "track",
    "TPOS": "disc",
    "TCON": "genre",
    "TCOM": "composer",
    "TPE3": "conductor",
    "TEXT": "lyricist",
    "TPUB": "publisher",
    "TCOP": "copyright",
    "TENC": "encodedby",
    "TSSE": "encoder",
    "TBPM": "bpm",
    "TKEY": "key",
    "TSRC": "isrc",
    "TLAN": "language",
    "TIT1": "description",
    # v2.2（三字符 ID）
    "TT2": "title",
    "TP1": "artist",
    "TP2": "albumartist",
    "TAL": "album",
    "TYE": "date",
    "TRK": "track",
    "TPA": "disc",
    "TCO": "genre",
    "TCM": "composer",
    "TP3": "conductor",
    "TXT": "lyricist",
    "TPB": "publisher",
    "TCR": "copyright",
    "TEN": "encodedby",
    "TSS": "encoder",
    "TBP": "bpm",
    "TKE": "key",
    "TRC": "isrc",
    "TLA": "language",
}

_ID3_MIME_BY_EXT = {"JPG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif"}


def _id3_text(payload: bytes) -> str:
    """按首字节声明的编码解出文本帧（多值用 NUL 分隔的，转成 ' / '）。"""
    if not payload:
        return ""
    enc, body = payload[0], payload[1:]
    try:
        if enc == 0:
            text = body.decode("latin-1", "replace")
        elif enc == 1:
            text = body.decode("utf-16", "replace")
        elif enc == 2:
            text = body.decode("utf-16-be", "replace")
        else:
            text = body.decode("utf-8", "replace")
    except (UnicodeDecodeError, LookupError):
        text = body.decode("utf-8", "replace")
    parts = [p.strip() for p in text.split("\x00") if p.strip()]
    return " / ".join(parts)


def _parse_id3v2(f, info: MediaInfo, budget: int) -> int:
    """解析文件开头的 ID3v2 标签，返回标签总长度（用于定位第一个音频帧）。"""
    head = _read_at(f, 0, 10)
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    version = head[3]
    flags = head[5]
    tag_size = _syncsafe(head, 6)
    if tag_size <= 0:
        return 0
    total = 10 + tag_size + (10 if flags & 0x10 else 0)  # footer
    body = _read_at(f, 10, min(tag_size, budget))
    info.container = f"MP3（ID3v2.{version}）" if version in (2, 3, 4) else "MP3（ID3）"

    # v2.3/2.4 带扩展头时前面多 4 字节，跳过
    pos = 4 if version >= 3 and flags & 0x40 else 0

    id_len, size_len = (3, 3) if version == 2 else (4, 4)
    # v2.3/2.4 的帧头是 id + size + 2 字节 flags，v2.2 只有 id + size
    frame_header = id_len + size_len + (0 if version == 2 else 2)
    while pos + frame_header <= len(body):
        frame_id = body[pos : pos + id_len].decode("latin-1")
        if not re.fullmatch(r"[A-Z0-9]{3,4}", frame_id):
            break  # 到填充区了
        raw = body[pos + id_len : pos + id_len + size_len]
        size = _syncsafe(raw) if version >= 4 else int.from_bytes(raw, "big")
        if size <= 0 or pos + frame_header + size > len(body):
            break
        payload = body[pos + frame_header : pos + frame_header + size]
        pos += frame_header + size

        key = _ID3_FRAMES.get(frame_id)
        if key and payload:
            info.add(key, _id3_text(payload))
            continue
        if frame_id in ("COMM", "COM"):
            # enc(1) + lang(3) + 简介(NUL 结尾) + 正文
            if len(payload) > 4:
                enc = payload[0]
                rest = payload[4:]
                sep = b"\x00\x00" if enc in (1, 2) else b"\x00"
                cut = rest.find(sep)
                text = rest[cut + len(sep) :] if cut >= 0 else rest
                info.add("comment", _id3_text(bytes([enc]) + text))
            continue
        if frame_id in ("USLT", "ULT"):
            if len(payload) > 4:
                enc = payload[0]
                rest = payload[4:]
                sep = b"\x00\x00" if enc in (1, 2) else b"\x00"
                cut = rest.find(sep)
                text = rest[cut + len(sep) :] if cut >= 0 else rest
                info.add("lyrics", _id3_text(bytes([enc]) + text))
            continue
        if frame_id in ("APIC", "PIC") and info.cover is None:
            info.cover = _id3_picture(frame_id, payload)
    return total


def _id3_picture(frame_id: str, payload: bytes) -> dict[str, Any] | None:
    """封面帧只取「什么格式、多大」，图片本身不交给模型。"""
    if len(payload) < 6:
        return None
    enc = payload[0]
    if frame_id == "PIC":
        fmt = payload[1:4].decode("latin-1", "replace").upper()
        mime = _ID3_MIME_BY_EXT.get(fmt, "image/unknown")
        rest_start = 4
    else:
        end = payload.find(b"\x00", 1)
        if end < 0:
            return None
        mime = payload[1:end].decode("latin-1", "replace").lower() or "image/unknown"
        rest_start = end + 1
    # picture type(1) + 描述(NUL 结尾) + 数据
    desc_end = payload.find(b"\x00\x00" if enc in (1, 2) else b"\x00", rest_start + 1)
    data_start = (desc_end + (2 if enc in (1, 2) else 1)) if desc_end >= 0 else rest_start + 1
    size = max(0, len(payload) - data_start)
    if size <= 0:
        return None
    return {"mime": mime, "size": size}


def _parse_id3v1(f, info: MediaInfo, file_size: int) -> bool:
    if file_size < 128:
        return False
    tail = _read_at(f, file_size - 128, 128)
    if tail[:3] != b"TAG":
        return False
    def field(start: int, end: int) -> str:
        return tail[start:end].split(b"\x00")[0].decode("latin-1", "replace").strip()
    info.add("title", field(3, 33))
    info.add("artist", field(33, 63))
    info.add("album", field(63, 93))
    info.add("date", field(93, 97))
    comment = field(97, 127)
    if tail[125] == 0 and tail[126] != 0:
        info.add("track", str(tail[126]))
        comment = field(97, 125)
    info.add("comment", comment)
    genre_id = tail[127]
    if genre_id < len(_ID3V1_GENRES):
        info.add("genre", _ID3V1_GENRES[genre_id])
    return True


_ID3V1_GENRES = [
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge", "Hip-Hop",
    "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae", "Rock",
    "Techno", "Industrial", "Alternative", "Ska", "Death Metal", "Pranks", "Soundtrack",
    "Euro-Techno", "Ambient", "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance",
    "Classical", "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "AlternRock", "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop",
    "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-Industrial", "Electronic",
    "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy", "Cult", "Gangsta",
    "Top 40", "Christian Rap", "Pop/Funk", "Jungle", "Native American", "Cabaret",
    "New Wave", "Psychadelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal",
    "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock",
]

# 码率表（kbps）：键是 (MPEG 版本, 层)。MPEG2/2.5 共用一张表。
_BITRATE_TABLE = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}

_SAMPLE_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


def _mp3_frame(b: bytes) -> dict[str, Any] | None:
    """解一个 MPEG 音频帧头；不是同步字就返回 None。"""
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return None
    ver_bits = (b[1] >> 3) & 0x03
    layer_bits = (b[1] >> 1) & 0x03
    if ver_bits == 1 or layer_bits == 0:
        return None
    mpeg = {0: 25, 2: 2, 3: 1}[ver_bits]  # 25 = MPEG2.5
    layer = 4 - layer_bits
    br_idx = (b[2] >> 4) & 0x0F
    sr_idx = (b[2] >> 2) & 0x03
    padding = (b[2] >> 1) & 0x01
    channel_mode = (b[3] >> 6) & 0x03
    table_version = 1 if mpeg == 1 else 2
    bitrates = _BITRATE_TABLE.get((table_version, layer))
    if not bitrates or br_idx in (0, 15) or sr_idx == 3:
        return None
    sample_rate = _SAMPLE_RATES[mpeg][sr_idx]
    bitrate = bitrates[br_idx]
    if layer == 1:
        samples = 384
    elif layer == 2:
        samples = 1152
    else:
        samples = 1152 if mpeg == 1 else 576
    # 边信息长度：给 Xing 头定位用
    if mpeg == 1:
        side_info = 17 if channel_mode == 3 else 32
    else:
        side_info = 9 if channel_mode == 3 else 17
    return {
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "samples": samples,
        "channels": 1 if channel_mode == 3 else 2,
        "side_info": side_info,
        "crc": (b[1] & 0x01) == 0,
    }


def _mp3_audio_info(f, info: MediaInfo, audio_start: int, file_size: int) -> None:
    """从第一个有效帧拿码率/采样率，顺带找 Xing/Info 精确帧数。"""
    window = _read_at(f, audio_start, 64 * 1024)
    pos = 0
    frame = None
    while pos + 4 <= len(window):
        if window[pos] == 0xFF and (window[pos + 1] & 0xE0) == 0xE0:
            frame = _mp3_frame(window[pos : pos + 4])
            if frame:
                break
        pos += 1
    if frame is None:
        return
    info.tech["码率"] = f"{frame['bitrate']} kbps"
    info.tech["采样率"] = f"{frame['sample_rate']} Hz"
    info.tech["声道"] = str(frame["channels"])

    # Xing / Info 头：帧头 + CRC(可选) + 边信息之后，标志位 & 1 表示带总帧数
    xing_at = pos + 4 + (2 if frame["crc"] else 0) + frame["side_info"]
    vbr_frames = None
    if window[xing_at : xing_at + 4] in (b"Xing", b"Info"):
        flags = _u32be(window, xing_at + 4)
        if flags & 0x01 and xing_at + 12 <= len(window):
            vbr_frames = _u32be(window, xing_at + 8)
            info.notes.append("VBR" if window[xing_at : xing_at + 4] == b"Xing" else "CBR（Info 头）")
    if vbr_frames:
        info.duration = vbr_frames * frame["samples"] / frame["sample_rate"]
        return
    # 没有 Xing：按 CBR 估算（VBR 文件会偏，所以措辞是「约」）
    audio_bytes = max(0, file_size - audio_start)
    if frame["bitrate"]:
        info.duration = audio_bytes * 8 / (frame["bitrate"] * 1000)


def _parse_mp3(f, info: MediaInfo, file_size: int) -> None:
    tag_len = _parse_id3v2(f, info, TAG_BUDGET)
    if tag_len <= 0:
        info.container = "MP3"
    _mp3_audio_info(f, info, tag_len, file_size)
    _parse_id3v1(f, info, file_size)
    if not info.tags and not info.tech:
        raise MediaTagError("不是 MP3 文件（既没有 ID3 标签，也同步不到 MPEG 音频帧）")


# --------------------------------------------------------------------------- #
# FLAC
# --------------------------------------------------------------------------- #
_FLAC_VORBIS_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUMARTIST": "albumartist",
    "ALBUM": "album",
    "DATE": "date",
    "YEAR": "date",
    "GENRE": "genre",
    "TRACKNUMBER": "track",
    "TRACKTOTAL": "tracktotal",
    "DISCNUMBER": "disc",
    "COMPOSER": "composer",
    "PERFORMER": "artist",
    "CONDUCTOR": "conductor",
    "LYRICIST": "lyricist",
    "LABEL": "publisher",
    "ORGANIZATION": "publisher",
    "COPYRIGHT": "copyright",
    "ENCODED-BY": "encodedby",
    "ENCODER": "encoder",
    "BPM": "bpm",
    "ISRC": "isrc",
    "LANGUAGE": "language",
    "COMMENT": "comment",
    "DESCRIPTION": "description",
    "LYRICS": "lyrics",
    "UNSYNCEDLYRICS": "lyrics",
}


def _parse_vorbis_comment(buf: bytes, info: MediaInfo) -> None:
    """Vorbis comment 主体（不含 vendor/前缀魔法）。"""
    if len(buf) < 8:
        return
    pos = 0
    vendor_len = _u32le(buf, pos)
    pos += 4 + vendor_len
    if pos + 4 > len(buf):
        return
    count = _u32le(buf, pos)
    pos += 4
    for _ in range(min(count, 256)):
        if pos + 4 > len(buf):
            break
        length = _u32le(buf, pos)
        pos += 4
        if length <= 0 or pos + length > len(buf):
            break
        entry = buf[pos : pos + length].decode("utf-8", "replace")
        pos += length
        if "=" not in entry:
            continue
        raw_key, value = entry.split("=", 1)
        key = _FLAC_VORBIS_MAP.get(raw_key.strip().upper())
        if key:
            info.add(key, value)
    # TRACKTOTAL 单独出现时补成 "3/12" 这种形式
    total = info.tags.pop("tracktotal", "")
    if total and info.tags.get("track") and "/" not in info.tags["track"]:
        info.tags["track"] = f"{info.tags['track']}/{total}"


def _parse_flac(f, info: MediaInfo, file_size: int) -> None:
    info.container = "FLAC"
    head = _read_at(f, 0, 4)
    if head == b"fLaC":
        offset = 4
    elif head[:3] == b"ID3":  # 少数文件把 ID3 挂在 FLAC 前面
        offset = _parse_id3v2(f, info, TAG_BUDGET) or 4
    else:
        raise MediaTagError("不是 FLAC 文件（没有 fLaC 标记）")
    while offset + 4 <= file_size:
        header = _read_at(f, offset, 4)
        if len(header) < 4:
            break
        is_last = bool(header[0] & 0x80)
        block_type = header[0] & 0x7F
        length = int.from_bytes(header[1:4], "big")
        body_at = offset + 4
        if block_type == 0 and length >= 34:  # STREAMINFO
            body = _read_at(f, body_at, min(length, 64))
            if len(body) >= 18:
                packed = _u64be(body, 10)
                sample_rate = packed >> 44
                channels = ((packed >> 41) & 0x07) + 1
                bits = ((packed >> 36) & 0x1F) + 1
                total_samples = packed & 0xFFFFFFFFF
                if sample_rate:
                    info.tech["采样率"] = f"{sample_rate} Hz"
                    info.tech["声道"] = str(channels)
                    info.tech["位深"] = f"{bits} bit"
                    if total_samples:
                        info.duration = total_samples / sample_rate
        elif block_type == 4:  # VORBIS_COMMENT
            body = _read_at(f, body_at, min(length, TAG_BUDGET))
            _parse_vorbis_comment(body, info)
        elif block_type == 6:  # PICTURE
            body = _read_at(f, body_at, min(length, 64 * 1024))
            cover = _flac_picture(body)
            if cover and info.cover is None:
                info.cover = cover
        offset = body_at + length
        if is_last:
            break


def _flac_picture(buf: bytes) -> dict[str, Any] | None:
    if len(buf) < 8:
        return None
    try:
        pos = 4  # picture type u32
        mime_len = _u32be(buf, pos)
        pos += 4
        mime = buf[pos : pos + mime_len].decode("latin-1", "replace") or "image/unknown"
        pos += mime_len
        desc_len = _u32be(buf, pos)
        pos += 4 + desc_len
        pos += 16  # width/height/depth/colors
        data_len = _u32be(buf, pos)
        return {"mime": mime, "size": data_len}
    except (struct.error, IndexError):
        return None


# --------------------------------------------------------------------------- #
# OGG（Vorbis / Opus）
# --------------------------------------------------------------------------- #
def _ogg_packets(f, file_size: int, want: int = 3, budget: int = 1024 * 1024) -> list[bytes]:
    """顺着 Ogg 页拼出前几个逻辑包（识别头 / 注释头）。"""
    packets: list[bytes] = []
    current = bytearray()
    offset = 0
    read_bytes = 0
    while offset + 27 <= file_size and len(packets) < want and read_bytes < budget:
        header = _read_at(f, offset, 27)
        if len(header) < 27 or header[:4] != b"OggS":
            break
        seg_count = header[26]
        table = _read_at(f, offset + 27, seg_count)
        if len(table) < seg_count:
            break
        payload_len = sum(table)
        payload = _read_at(f, offset + 27 + seg_count, payload_len)
        read_bytes += 27 + seg_count + payload_len
        pos = 0
        for lace in table:
            current += payload[pos : pos + lace]
            pos += lace
            if lace < 255:  # 一个包结束
                packets.append(bytes(current))
                current = bytearray()
                if len(packets) >= want:
                    break
        offset += 27 + seg_count + payload_len
    return packets


def _ogg_duration(f, file_size: int, sample_rate: int) -> float | None:
    """最后一页的 granule position / 采样率 = 时长。"""
    if file_size <= 0 or not sample_rate:
        return None
    tail_size = min(file_size, 96 * 1024)
    tail = _read_at(f, file_size - tail_size, tail_size)
    idx = tail.rfind(b"OggS")
    if idx < 0 or idx + 14 > len(tail):
        return None
    granule = struct.unpack_from("<q", tail, idx + 6)[0]
    if granule <= 0:
        return None
    return granule / sample_rate


def _parse_ogg(f, info: MediaInfo, file_size: int) -> None:
    packets = _ogg_packets(f, file_size)
    if not packets:
        raise MediaTagError("Ogg 页解析失败")
    first = packets[0]
    sample_rate = 0
    if first[:7] == b"\x01vorbis" and len(first) >= 16:
        info.container = "Ogg Vorbis"
        channels = first[11]
        sample_rate = _u32le(first, 12)
        nominal = _u32le(first, 20) if len(first) >= 24 else 0
        if nominal:
            info.tech["码率"] = f"约 {round(nominal / 1000)} kbps"
    elif first[:8] == b"OpusHead" and len(first) >= 19:
        info.container = "Ogg Opus"
        channels = first[9]
        sample_rate = 48000  # Opus 内部固定 48 kHz
        info.tech["原始采样率"] = f"{_u32le(first, 12)} Hz"
    else:
        raise MediaTagError("不是 Vorbis / Opus 流")
    info.tech["采样率"] = f"{sample_rate} Hz"
    info.tech["声道"] = str(channels)

    for packet in packets[1:]:
        if packet[:7] == b"\x03vorbis":
            _parse_vorbis_comment(packet[7:], info)
            break
        if packet[:8] == b"OpusTags":
            _parse_vorbis_comment(packet[8:], info)
            break
    info.duration = _ogg_duration(f, file_size, sample_rate)


# --------------------------------------------------------------------------- #
# MP4 / M4A / MOV
# --------------------------------------------------------------------------- #
_MP4_TEXT_KEYS = {
    "©nam": "title",
    "©ART": "artist",
    "aART": "albumartist",
    "©alb": "album",
    "©day": "date",
    "©gen": "genre",
    "©wrt": "composer",
    "©cmt": "comment",
    "©lyr": "lyrics",
    "desc": "description",
    "©too": "encoder",
    "©cpy": "copyright",
    "©enc": "encodedby",
    "tmpo": "bpm",
    "©pub": "publisher",
}


def _iter_boxes(buf: bytes, start: int = 0, end: int | None = None, depth: int = 0) -> Iterator[tuple[str, int, int]]:
    """遍历内存里的 box 序列，产出 (类型, 载荷起点, 载荷终点)。"""
    end = len(buf) if end is None else min(end, len(buf))
    pos = start
    guard = 0
    while pos + 8 <= end and guard < 4096:
        guard += 1
        size = _u32be(buf, pos)
        kind = buf[pos + 4 : pos + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = _u64be(buf, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield kind, pos + header, pos + size
        pos += size


def _mp4_meta_tags(buf: bytes, info: MediaInfo) -> None:
    """ilst 里的标签：每个条目下挂一个 'data' box。"""
    for key, ps, pe in _iter_boxes(buf):
        payload = b""
        for sub, s_ps, s_pe in _iter_boxes(buf, ps, pe):
            if sub == "data" and s_pe - s_ps >= 8:
                payload = buf[s_ps + 8 : s_pe]
                break
        if key == "covr":
            mime = "image/png" if payload[:4] == b"\x89PNG" else "image/jpeg"
            if payload:
                info.cover = info.cover or {"mime": mime, "size": len(payload)}
            continue
        if key in ("trkn", "disk"):
            if len(payload) >= 4:
                number = int.from_bytes(payload[2:4], "big")
                total = int.from_bytes(payload[4:6], "big") if len(payload) >= 6 else 0
                value = f"{number}/{total}" if total else str(number)
                info.add("track" if key == "trkn" else "disc", value)
            continue
        name = _MP4_TEXT_KEYS.get(key)
        if name and payload:
            info.add(name, payload.decode("utf-8", "replace"))


def _parse_mp4(f, info: MediaInfo, file_size: int) -> None:
    info.container = "MP4"
    offset = 0
    moov: tuple[int, int] | None = None
    ftyp = ""
    guard = 0
    while offset + 8 <= file_size and guard < 512:
        guard += 1
        head = _read_at(f, offset, 16)
        if len(head) < 8:
            break
        size = _u32be(head, 0)
        kind = head[4:8].decode("latin-1", "replace")
        header = 8
        if size == 1 and len(head) >= 16:
            size = _u64be(head, 8)
            header = 16
        elif size == 0:
            size = file_size - offset
        if size < header:
            break
        if kind == "ftyp":
            ftyp = _read_at(f, offset + header, 4).decode("latin-1", "replace")
        elif kind == "moov":
            moov = (offset + header, offset + size)
            break  # moov 之后的都是媒体数据，不用再翻
        offset += size

    if moov is None:
        raise MediaTagError("没找到 moov 元数据块（文件可能没写完）")
    # moov 可能很大（含封面/章节），但通常几百 KB
    msize = moov[1] - moov[0]
    if msize > BOX_BUDGET:
        raise MediaTagError("moov 块过大，放弃解析")
    buf = _read_at(f, moov[0], msize)
    info.container = {
        "M4A ": "M4A",
        "M4B ": "M4B",
        "M4V ": "M4V",
        "qt  ": "QuickTime MOV",
    }.get(ftyp, "MP4")

    size_seen = False
    for kind, ps, pe in _iter_boxes(buf):
        if kind == "mvhd":
            payload = buf[ps:pe]
            if len(payload) >= 20:
                version = payload[0]
                if version == 1 and len(payload) >= 32:
                    timescale = _u32be(payload, 20)
                    duration = _u64be(payload, 24)
                else:
                    timescale = _u32be(payload, 12)
                    duration = _u32be(payload, 16)
                if timescale and duration and duration != 0xFFFFFFFF:
                    info.duration = duration / timescale
        elif kind == "trak" and not size_seen:
            for sub, s_ps, s_pe in _iter_boxes(buf, ps, pe):
                if sub != "tkhd" or s_pe - s_ps < 8:
                    continue
                payload = buf[s_ps:s_pe]
                width = _u32be(payload, len(payload) - 8) / 65536
                height = _u32be(payload, len(payload) - 4) / 65536
                if width >= 16 and height >= 16:
                    info.tech["分辨率"] = f"{int(round(width))}×{int(round(height))}"
                    size_seen = True
                    break
        elif kind == "udta":
            for sub, s_ps, s_pe in _iter_boxes(buf, ps, pe):
                if sub == "meta":
                    # meta 的载荷前面多 4 字节 version/flags
                    payload = buf[s_ps:s_pe]
                    for inner, i_ps, i_pe in _iter_boxes(payload, 4, len(payload)):
                        if inner == "ilst":
                            _mp4_meta_tags(payload[i_ps:i_pe], info)
                elif sub in ("©nam", "©ART", "©alb", "©day", "©cmt", "©gen"):
                    payload = buf[s_ps:s_pe]
                    if len(payload) > 4:
                        name = _MP4_TEXT_KEYS.get(sub)
                        if name:
                            info.add(name, payload[4:].decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# RIFF：WAV / AVI
# --------------------------------------------------------------------------- #
_RIFF_INFO_MAP = {
    "INAM": "title",
    "IART": "artist",
    "IPRD": "album",
    "ICMT": "comment",
    "ICRD": "date",
    "IGNR": "genre",
    "ITRK": "track",
    "ICOP": "copyright",
    "ISFT": "encoder",
    "IENG": "encodedby",
    "IWRI": "composer",
}


def _parse_riff(f, info: MediaInfo, file_size: int, form: str) -> None:
    if _read_at(f, 0, 4) != b"RIFF":
        raise MediaTagError("不是 RIFF 容器")
    if form == "AVI ":
        info.container = "AVI"
    elif form == "WAVE":
        info.container = "WAV"
    else:
        raise MediaTagError(f"不支持的 RIFF 类型：{form.strip() or '未知'}")
    offset = 12
    byte_rate = 0
    data_size = 0
    guard = 0
    while offset + 8 <= file_size and guard < 512:
        guard += 1
        head = _read_at(f, offset, 8)
        if len(head) < 8:
            break
        kind = head[:4].decode("latin-1", "replace")
        size = int.from_bytes(head[4:8], "little")
        body_at = offset + 8
        if kind == "fmt ":
            body = _read_at(f, body_at, min(size, 64))
            if len(body) >= 16:
                channels = _u16le(body, 2)
                sample_rate = _u32le(body, 4)
                byte_rate = _u32le(body, 8)
                bits = _u16le(body, 14)
                info.tech["采样率"] = f"{sample_rate} Hz"
                info.tech["声道"] = str(channels)
                if bits:
                    info.tech["位深"] = f"{bits} bit"
                if byte_rate:
                    info.tech["码率"] = f"{round(byte_rate * 8 / 1000)} kbps"
        elif kind == "data":
            data_size = size
        elif kind == "LIST":
            head4 = _read_at(f, body_at, 4)
            if head4 == b"INFO":
                _parse_riff_info(_read_at(f, body_at + 4, min(size - 4, TAG_BUDGET)), info)
            elif head4 == b"hdrl":
                # AVI 的 avih 藏在 LIST hdrl 里，往下走一层
                _parse_riff_hdrl(_read_at(f, body_at + 4, min(size - 4, 1 << 20)), info)
        elif kind == "avih":
            _riff_avih(_read_at(f, body_at, min(size, 64)), info)
        offset = body_at + size + (size & 1)  # RIFF 块按偶数字节对齐

    if form in ("WAVE", "AVI ") and not info.duration and byte_rate and data_size:
        info.duration = data_size / byte_rate


def _riff_avih(body: bytes, info: MediaInfo) -> None:
    """主 AVISTREAMHEADER 之外的 avih：宽高 + 每帧微秒 × 总帧数。"""
    if len(body) < 40:
        return
    micro_per_frame = _u32le(body, 0)
    frames = _u32le(body, 16)
    width = _u32le(body, 32)
    height = _u32le(body, 36)
    if width and height:
        info.tech["分辨率"] = f"{width}×{height}"
    if micro_per_frame and frames:
        info.duration = micro_per_frame * frames / 1_000_000


def _parse_riff_hdrl(buf: bytes, info: MediaInfo) -> None:
    """LIST hdrl 里的块：只要 avih，别的（strh/strf）跳过。"""
    pos = 0
    guard = 0
    while pos + 8 <= len(buf) and guard < 64:
        guard += 1
        key = buf[pos : pos + 4].decode("latin-1", "replace")
        size = int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if pos + 8 + size > len(buf):
            break
        if key == "avih":
            _riff_avih(buf[pos + 8 : pos + 8 + size], info)
        pos += 8 + size + (size & 1)


def _u16le(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _parse_riff_info(buf: bytes, info: MediaInfo) -> None:
    pos = 0
    guard = 0
    while pos + 8 <= len(buf) and guard < 128:
        guard += 1
        key = buf[pos : pos + 4].decode("latin-1", "replace")
        size = int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if size < 0 or pos + 8 + size > len(buf):
            break
        name = _RIFF_INFO_MAP.get(key)
        if name:
            info.add(name, buf[pos + 8 : pos + 8 + size])
        pos += 8 + size + (size & 1)


# --------------------------------------------------------------------------- #
# MKV / WebM（EBML）
# --------------------------------------------------------------------------- #
_EBML_IDS = {
    0x1A45DFA3: "EBML",
    0x18538067: "Segment",
    0x1549A966: "Info",
    0x2AD7B1: "TimecodeScale",
    0x4489: "Duration",
    0x1654AE6B: "Tracks",
    0xAE: "TrackEntry",
    0xE0: "Video",
    0xB0: "PixelWidth",
    0xBA: "PixelHeight",
    0x1254C467: "Tags",
    0x7373: "Tag",
    0x63C0: "Targets",
    0x67C8: "SimpleTag",
    0x45A3: "TagName",
    0x4487: "TagString",
    0x4484: "TagDefault",
}

_MKV_TAG_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUM": "album",
    "ALBUM_ARTIST": "albumartist",
    "DATE_RELEASED": "date",
    "DATE": "date",
    "GENRE": "genre",
    "COMMENT": "comment",
    "DESCRIPTION": "description",
    "COMPOSER": "composer",
    "ENCODER": "encoder",
    "COPYRIGHT": "copyright",
    "PART_NUMBER": "track",
    "TOTAL_PARTS": "tracktotal",
    "DISC": "disc",
    "LYRICS": "lyrics",
    "BPM": "bpm",
}


def _ebml_vint(buf: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int, bool]:
    """解 EBML 变长整数，返回 (值, 新位置, 是否未知长度)。

    EBML ID 要保留标记位（0x1A45DFA3 就是原样），长度值去掉标记位；
    「全 1」的长度表示流式写入时未知大小（Cluster 常见）。
    """
    if pos >= len(buf):
        raise MediaTagError("EBML 数据截断")
    first = buf[pos]
    if first == 0:
        raise MediaTagError("EBML 长度非法")
    length = 1
    mask = 0x80
    while not (first & mask):
        length += 1
        mask >>= 1
        if length > 8:
            raise MediaTagError("EBML 长度非法")
    if pos + length > len(buf):
        raise MediaTagError("EBML 数据截断")
    value = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    # 未知长度：去掉标记位后全为 1（例如 8 字节的 0x01FFFFFFFFFFFFFF）
    unknown = (not keep_marker) and value == (1 << (7 * length)) - 1
    return value, pos + length, unknown


def _ebml_uint(buf: bytes) -> int:
    return int.from_bytes(buf, "big") if buf else 0


def _ebml_children(buf: bytes, start: int, end: int) -> Iterator[tuple[int, int, int]]:
    """产出 (ID, 载荷起点, 载荷终点)；未知长度按到 end 处理。"""
    pos = start
    guard = 0
    end = min(end, len(buf))
    while pos < end and guard < 4096:
        guard += 1
        try:
            elem_id, pos, _ = _ebml_vint(buf, pos, keep_marker=True)
            size, pos, unknown = _ebml_vint(buf, pos, keep_marker=False)
        except MediaTagError:
            return
        data_end = end if unknown else min(pos + size, end)
        yield elem_id, pos, data_end
        pos = data_end


def _ebml_element(buf: bytes, pos: int = 0) -> tuple[int, int, int, bool]:
    """解一个元素头，返回 (ID, 大小, 载荷起点, 是否未知长度)。"""
    elem_id, pos, _ = _ebml_vint(buf, pos, keep_marker=True)
    size, pos, unknown = _ebml_vint(buf, pos, keep_marker=False)
    return elem_id, size, pos, unknown


def _parse_mkv(f, info: MediaInfo, file_size: int) -> None:
    info.container = "Matroska（MKV）"
    head = _read_at(f, 0, 64)
    try:
        elem_id, size, payload_at, _ = _ebml_element(head, 0)
    except MediaTagError as exc:
        raise MediaTagError("EBML 头解析失败") from exc
    if elem_id != 0x1A45DFA3:
        raise MediaTagError("不是 EBML / Matroska 文件")
    # DocType 是 0x4282，WebM 与 MKV 结构一致，只是名字不同
    doc = head[payload_at : payload_at + min(size, 64 - payload_at)]
    for sub, s_ps, s_pe in _ebml_children(doc, 0, len(doc)):
        if sub == 0x4282 and doc[s_ps:s_pe].lower().startswith(b"webm"):
            info.container = "WebM"

    pos = payload_at + size
    if pos + 16 > file_size:
        raise MediaTagError("没有 Segment（文件可能没写完）")
    seg_head = _read_at(f, pos, 32)
    try:
        seg_id, seg_size, seg_at, seg_unknown = _ebml_element(seg_head, 0)
    except MediaTagError as exc:
        raise MediaTagError("Segment 解析失败") from exc
    if seg_id != 0x18538067:
        raise MediaTagError("没找到 Segment 块")
    seg_end = file_size if seg_unknown else min(pos + seg_at + seg_size, file_size)

    # 顺次扫一级子元素：只整段读 Info / Tracks / Tags，Cluster 直接 seek 跳过。
    # 上限给到 4096 是因为一部长片的 Cluster 动辄上千个，Tags 又常在文件尾部。
    cursor = pos + seg_at
    guard = 0
    while cursor < seg_end and guard < 4096:
        guard += 1
        raw = _read_at(f, cursor, 32)
        try:
            elem_id, size, data_at, unknown = _ebml_element(raw, 0)
        except MediaTagError:
            break
        name = _EBML_IDS.get(elem_id)
        if name in ("Info", "Tracks", "Tags") and not unknown and size <= BOX_BUDGET:
            body = _read_at(f, cursor + data_at, size)
            if name == "Info":
                _mkv_info(body, info)
            elif name == "Tracks":
                _mkv_tracks(body, info)
            else:
                _mkv_tags(body, info)
        if unknown:
            break  # 未知长度的 Cluster，往后定位不可靠，见好就收
        cursor += data_at + size
    if not info.duration:
        info.notes.append("没读到时长信息")


def _mkv_info(buf: bytes, info: MediaInfo) -> None:
    scale = 1_000_000  # 默认 1 ms
    duration = None
    for elem_id, ps, pe in _ebml_children(buf, 0, len(buf)):
        payload = buf[ps:pe]
        if elem_id == 0x2AD7B1:
            scale = _ebml_uint(payload) or scale
        elif elem_id == 0x4489:
            if len(payload) == 4:
                duration = struct.unpack(">f", payload)[0]
            elif len(payload) == 8:
                duration = struct.unpack(">d", payload)[0]
    if duration and duration > 0:
        info.duration = duration * scale / 1_000_000_000


def _mkv_tracks(buf: bytes, info: MediaInfo) -> None:
    found = False
    for elem_id, ps, pe in _ebml_children(buf, 0, len(buf)):
        if elem_id != 0xAE or found:
            continue
        width = height = 0
        for sub, s_ps, s_pe in _ebml_children(buf, ps, pe):
            if sub != 0xE0:
                continue
            for v_id, v_ps, v_pe in _ebml_children(buf, s_ps, s_pe):
                if v_id == 0xB0:
                    width = _ebml_uint(buf[v_ps:v_pe])
                elif v_id == 0xBA:
                    height = _ebml_uint(buf[v_ps:v_pe])
        if width and height:
            info.tech["分辨率"] = f"{width}×{height}"
            found = True


def _mkv_tags(buf: bytes, info: MediaInfo) -> None:
    def walk(start: int, end: int, depth: int = 0) -> None:
        if depth > 4:
            return
        for elem_id, ps, pe in _ebml_children(buf, start, end):
            if elem_id == 0x67C8:  # SimpleTag
                name = value = ""
                for sub, s_ps, s_pe in _ebml_children(buf, ps, pe):
                    if sub == 0x45A3:
                        name = buf[s_ps:s_pe].decode("utf-8", "replace").strip()
                    elif sub == 0x4487:
                        value = buf[s_ps:s_pe].decode("utf-8", "replace")
                key = _MKV_TAG_MAP.get(name.upper())
                if key:
                    info.add(key, value)
            elif elem_id in (0x7373, 0x63C0):
                walk(ps, pe, depth + 1)

    walk(0, len(buf))
    total = info.tags.pop("tracktotal", "")
    if total and info.tags.get("track") and "/" not in info.tags["track"]:
        info.tags["track"] = f"{info.tags['track']}/{total}"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
_EXT_HINTS = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".m4a": "mp4",
    ".m4b": "mp4",
    ".m4v": "mp4",
    ".mp4": "mp4",
    ".mov": "mp4",
    ".wav": "riff",
    ".avi": "riff",
    ".mkv": "mkv",
    ".webm": "mkv",
}


def _magic_kind(head: bytes) -> str:
    """只看文件头：扩展名可以随便改，内容不会骗人。"""
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "riff"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "mkv"
    return ""


def _sniff_kind(path: Path, head: bytes) -> str:
    """按文件头判断容器；文件头没特征时退回扩展名。"""
    return _magic_kind(head) or _EXT_HINTS.get(path.suffix.lower(), "")


def sniff(path: Path) -> str:
    """只按文件头判断，返回容器类型（不认识则为空串）。

    给调用方做「这个文件到底是不是音视频」的初筛用——被改成 .mp3 的网页、
    下载了一半的错误页，都不该被当成媒体去解析。
    """
    with path.open("rb") as f:
        return _magic_kind(f.read(16))


def _parse_own(f, info: MediaInfo, file_size: int, kind: str) -> None:
    """零依赖的自有解析器：也当 mutagen 不认识格式时的兜底。"""
    if kind == "mp3":
        _parse_mp3(f, info, file_size)
    elif kind == "flac":
        _parse_flac(f, info, file_size)
    elif kind == "ogg":
        _parse_ogg(f, info, file_size)
    elif kind == "mp4":
        _parse_mp4(f, info, file_size)
    elif kind == "riff":
        form = _read_at(f, 8, 4).decode("latin-1", "replace")
        _parse_riff(f, info, file_size, form)
    elif kind == "mkv":
        _parse_mkv(f, info, file_size)
    else:
        raise MediaTagError("不认识的容器格式")


# --------------------------------------------------------------------------- #
# mutagen：标签主解析（项目本来就依赖它，歌词模块在用）
# --------------------------------------------------------------------------- #
# mutagen 的类名 → 展示用容器名
_CONTAINER_NAMES = {
    "MP3": "MP3",
    "FLAC": "FLAC",
    "MP4": "MP4",
    "OggVorbis": "Ogg Vorbis",
    "OggOpus": "Ogg Opus",
    "OggTheora": "Ogg",
    "WAVE": "WAV",
    "AIFF": "AIFF",
    "ASF": "WMA",
    "MonkeysAudio": "APE",
    "WavPack": "WavPack",
    "DSF": "DSF",
}

# 各容器标签键 → 规范键（ID3 帧 ID / MP4 atom / Vorbis 注释 / ASF·APE 字段）
_TAG_KEY_MAP: dict[str, str] = {**_ID3_FRAMES, **_MP4_TEXT_KEYS, **_FLAC_VORBIS_MAP}
_TAG_KEY_MAP.update(
    {
        "TITLE": "title",
        "ARTIST": "artist",
        "AUTHOR": "artist",
        "ALBUM": "album",
        "ALBUMARTIST": "albumartist",
        "ALBUM ARTIST": "albumartist",
        "YEAR": "date",
        "DATE": "date",
        "GENRE": "genre",
        "TRACK": "track",
        "TRACKNUMBER": "track",
        "TRACKTOTAL": "tracktotal",
        "DISC": "disc",
        "DISCNUMBER": "disc",
        "DISCTOTAL": "disctotal",
        "COMPOSER": "composer",
        "CONDUCTOR": "conductor",
        "LYRICIST": "lyricist",
        "COMMENT": "comment",
        "DESCRIPTION": "description",
        "COPYRIGHT": "copyright",
        "ENCODER": "encoder",
        "ENCODEDBY": "encodedby",
        "PUBLISHER": "publisher",
        "LYRICS": "lyrics",
        "UNSYNCEDLYRICS": "lyrics",
        "BPM": "bpm",
        "ISRC": "isrc",
        "LANGUAGE": "language",
        "WM/ALBUMTITLE": "album",
        "WM/GENRE": "genre",
        "WM/YEAR": "date",
        "WM/TRACKNUMBER": "track",
        "WM/COMPOSER": "composer",
    }
)


def _tag_key(raw: str) -> str:
    key = str(raw)
    if key in _TAG_KEY_MAP:
        return _TAG_KEY_MAP[key]
    if key.upper() in _TAG_KEY_MAP:
        return _TAG_KEY_MAP[key.upper()]
    # TXXX:xxx / WXXX:xxx 这类带描述的帧：只看帧 ID
    if ":" in key:
        return _TAG_KEY_MAP.get(key.split(":", 1)[0], "")
    return ""


def _mutagen_value(tags, key: str, canonical: str) -> str:
    try:
        raw = tags.get(key)
    except Exception:
        return ""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        if not raw:
            return ""
        first = raw[0]
        if canonical in ("track", "disc") and isinstance(first, tuple):
            number = first[0] if first else 0
            total = first[1] if len(first) > 1 else 0
            return f"{number}/{total}" if total else str(number)
        raw = first
    if hasattr(raw, "text"):  # ID3 帧：COMM/USLT 的正文在 text 里
        text = raw.text
        if isinstance(text, (list, tuple)):
            parts = [str(item).strip() for item in text if str(item).strip()]
            return " / ".join(parts)
        return str(text)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _sniff_cover_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/unknown"


def _norm_cover_mime(mime: str, data: bytes) -> str:
    mime = (mime or "").strip().lower()
    if mime in {"png", "jpg", "jpeg", "jpe", "gif", "bmp"}:
        mime = f"image/{'jpeg' if mime in {'jpg', 'jpeg', 'jpe'} else mime}"
    return mime if mime.startswith("image/") else _sniff_cover_mime(data)


def _mutagen_cover(mf, tags) -> dict[str, Any] | None:
    """封面只取格式与大小，图片数据本身不交给模型。"""
    pictures = getattr(mf, "pictures", None)  # FLAC
    if pictures:
        pic = pictures[0]
        data = bytes(pic.data)
        return {"mime": _norm_cover_mime(pic.mime, data), "size": len(data)}
    if tags is None:
        return None
    try:
        apics = tags.getall("APIC")  # ID3v2
    except Exception:
        apics = []
    if apics:
        data = bytes(apics[0].data)
        return {"mime": _norm_cover_mime(apics[0].mime, data), "size": len(data)}
    try:
        covrs = tags.get("covr")  # MP4
    except Exception:
        covrs = None
    if covrs:
        data = bytes(covrs[0])
        return {"mime": _sniff_cover_mime(data), "size": len(data)}
    try:
        raw = tags.get("METADATA_BLOCK_PICTURE")  # Ogg（base64 的 FLAC Picture）
    except Exception:
        raw = None
    if raw:
        try:
            from mutagen.flac import Picture

            pic = Picture(base64.b64decode(str(raw[0])))
            data = bytes(pic.data)
            return {"mime": _norm_cover_mime(pic.mime, data), "size": len(data)}
        except Exception:
            return None
    return None


def _mutagen_loader(path: Path, kind: str):
    """按扩展名/容器精确挑加载器；mutagen.File() 的启发式会误判带 ID3 的 WAV。"""
    from mutagen.aiff import AIFF
    from mutagen.asf import ASF
    from mutagen.dsf import DSF
    from mutagen.flac import FLAC
    from mutagen.monkeysaudio import MonkeysAudio
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
    from mutagen.wavpack import WavPack

    by_ext = {
        ".mp3": MP3, ".flac": FLAC, ".m4a": MP4, ".m4b": MP4, ".mp4": MP4,
        ".m4v": MP4, ".mov": MP4, ".ogg": OggVorbis, ".oga": OggVorbis,
        ".opus": OggOpus, ".wma": ASF, ".wav": WAVE, ".aiff": AIFF,
        ".aif": AIFF, ".dsf": DSF, ".wv": WavPack, ".ape": MonkeysAudio,
    }
    loader = by_ext.get(path.suffix.lower())
    if loader is not None:
        return loader
    return {"mp3": MP3, "flac": FLAC, "mp4": MP4, "ogg": OggVorbis, "riff": WAVE}.get(kind)


def _parse_with_mutagen(path: Path, info: MediaInfo, kind: str) -> bool:
    """mutagen 主解析：标签、时长、音频参数、封面。不认识的容器返回 False。"""
    from mutagen import File as MutagenFile

    mf = None
    loader = _mutagen_loader(path, kind)
    if loader is not None:
        try:
            mf = loader(str(path))
        except Exception:
            mf = None
    if mf is None:
        try:
            mf = MutagenFile(str(path))
        except Exception:
            mf = None
    if mf is None:
        return False

    info.container = _CONTAINER_NAMES.get(type(mf).__name__, type(mf).__name__)
    ext = path.suffix.lower()
    if kind == "mp4":
        if ext in (".m4a", ".m4b", ".m4v"):
            info.container = ext[1:].upper()
        elif ext in (".mov", ".qt"):
            info.container = "QuickTime MOV"

    audio = getattr(mf, "info", None)
    length = getattr(audio, "length", 0) or 0
    if length and length > 0:
        info.duration = float(length)
    if audio is not None:
        bitrate = getattr(audio, "bitrate", 0) or 0
        kbps = round(bitrate / 1000) if bitrate else 0
        if kbps > 0:  # FLAC 的平均码率可能低到 0.x kbps，没必要写出来
            info.tech["码率"] = f"{kbps} kbps"
        sample_rate = getattr(audio, "sample_rate", 0) or 0
        if sample_rate:
            info.tech["采样率"] = f"{sample_rate} Hz"
        channels = getattr(audio, "channels", 0) or 0
        if channels:
            info.tech["声道"] = str(channels)
        bits = getattr(audio, "bits_per_sample", 0) or 0
        if bits:
            info.tech["位深"] = f"{bits} bit"

    tags = getattr(mf, "tags", None)
    if tags is not None:
        try:
            keys = list(tags.keys())
        except Exception:
            keys = []
        for raw_key in keys:
            canonical = _tag_key(raw_key)
            if not canonical or canonical in info.tags:
                continue
            value = _mutagen_value(tags, raw_key, canonical)
            if value:
                info.add(canonical, value)
        # 音轨号单独出现时补上总数
        total = info.tags.pop("tracktotal", "")
        if total and info.tags.get("track") and "/" not in info.tags["track"]:
            info.tags["track"] = f"{info.tags['track']}/{total}"
        total = info.tags.pop("disctotal", "")
        if total and info.tags.get("disc") and "/" not in info.tags["disc"]:
            info.tags["disc"] = f"{info.tags['disc']}/{total}"

    cover = _mutagen_cover(mf, tags)
    if cover:
        info.cover = cover
    return True


def _supplement_from_container(path: Path, info: MediaInfo, kind: str) -> None:
    """mutagen 不给分辨率、不认 WAV 的 LIST/INFO —— 自有解析器补一手。

    mutagen 已经给出的一律不覆盖，只填空缺（``MediaInfo.add`` 本身也不覆盖）。
    """
    own = MediaInfo()
    try:
        with path.open("rb") as f:
            _parse_own(f, own, path.stat().st_size, kind)
    except (MediaTagError, OSError, ValueError):
        return
    for key, value in own.tech.items():
        info.tech.setdefault(key, value)
    for key, value in own.tags.items():
        info.add(key, value)
    if not info.duration and own.duration:
        info.duration = own.duration
    if not info.cover and own.cover:
        info.cover = own.cover


def parse(path: Path) -> MediaInfo:
    """读一个音频/视频文件的元数据；`path` 必须存在。"""
    file_size = path.stat().st_size
    if file_size <= 0:
        raise MediaTagError("文件是空的")
    with path.open("rb") as f:
        head = f.read(16)
    kind = _sniff_kind(path, head)
    if not kind:
        raise MediaTagError("不认识的容器格式")

    info = MediaInfo()
    if _parse_with_mutagen(path, info, kind):
        _supplement_from_container(path, info, kind)
    else:
        with path.open("rb") as f:
            _parse_own(f, info, file_size, kind)
    if not info.container:
        raise MediaTagError("没能识别容器格式")
    if not info.tags:
        info.notes.append("文件里没有标题 / 艺术家这类标签")
    return info


def describe(info: MediaInfo, filename: str = "") -> str:
    """生成给模型看的中文摘要（也直接当工具结果返回给前端折叠面板）。"""
    lines: list[str] = []
    title = f"《{filename}》" if filename else ""
    lines.append(f"{title}元数据（{info.container}）" if title else f"元数据（{info.container}）")
    if info.duration:
        lines.append(f"时长：{_fmt_duration(info.duration)}（{info.duration:.1f} 秒）")
    for key, value in info.tech.items():
        lines.append(f"{key}：{value}")
    for key, label in LABELS:
        if info.tags.get(key):
            lines.append(f"{label}：{info.tags[key]}")
    # 未知键兜底（比如容器特有的字段）
    known = {key for key, _ in LABELS}
    for key, value in info.tags.items():
        if key not in known:
            lines.append(f"{key}：{value}")
    if info.cover:
        size = int(info.cover.get("size") or 0)
        shown = f"约 {size} 字节" if size < 1024 else f"约 {round(size / 1024)} KB"
        lines.append(f"封面：内嵌 {info.cover['mime']}，{shown}（图片本身没有读取）")
    if info.notes:
        lines.append("说明：" + "；".join(info.notes))
    return "\n".join(lines)
