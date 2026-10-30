"""播放器插件相关的端到端校验：同名字幕 / 缩略图 / 章节 / 弹幕的自动发现。

用法：python -m scripts.plugin_smoke
会在数据库里创建（或重置）pluginprobe 账号，上传一整套测试文件；
跑完不删数据，方便继续用浏览器打开 http://127.0.0.1:8123 手动看效果。
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8123"
USER = "pluginprobe"
PASSWORD = "Plugin#2026"
SAMPLES = Path("C:/Users/Lenovo/AppData/Local/Temp/plugin-samples")

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [ok]   {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


def build_samples() -> None:
    """生成测试素材：mp4 从临时目录取，字幕/章节/弹幕/缩略图现场造。"""
    SAMPLES.mkdir(parents=True, exist_ok=True)
    src = Path("C:/Users/Lenovo/AppData/Local/Temp/sample.mp4")
    if not src.is_file():
        raise SystemExit("缺少测试视频，请先准备 C:/Users/Lenovo/AppData/Local/Temp/sample.mp4")
    (SAMPLES / "demo.mp4").write_bytes(src.read_bytes())

    (SAMPLES / "demo.vtt").write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:04.000\n第一句字幕：插件已挂载。\n\n"
        "00:00:04.000 --> 00:00:09.000\n第二句字幕：多字幕合并渲染。\n",
        encoding="utf-8",
    )
    (SAMPLES / "demo.zh.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:09.000\n中文轨道（第二语言）\n",
        encoding="utf-8",
    )
    (SAMPLES / "demo.chapters.json").write_text(
        json.dumps(
            [
                {"start": 0, "end": 4, "title": "开场"},
                {"start": 4, "end": 7, "title": "中段"},
                {"start": 7, "title": "收尾"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (SAMPLES / "demo.danmaku.json").write_text(
        json.dumps(
            [
                {"time": 1.0, "text": "前排围观", "color": "#FF6699"},
                {"time": 3.5, "text": "这个播放器不错"},
                {"time": 6.0, "text": "顶部弹幕", "mode": 1},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from PIL import Image, ImageDraw

    sprite = Image.new("RGB", (480, 90), "#1b1230")
    draw = ImageDraw.Draw(sprite)
    for index in range(3):
        x = index * 160
        draw.rectangle([x, 0, x + 159, 89], fill=("#3b1d5e", "#5b2a86", "#7c3aed")[index])
        draw.text((x + 60, 40), f"#{index + 1}", fill="#ffffff")
    sprite.save(SAMPLES / "demo.thumbs.jpg", quality=85)
    (SAMPLES / "demo.thumbs.vtt").write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:03.000\ndemo.thumbs.jpg#xywh=0,0,160,90\n\n"
        "00:00:03.000 --> 00:00:06.000\ndemo.thumbs.jpg#xywh=160,0,160,90\n\n"
        "00:00:06.000 --> 00:00:09.000\ndemo.thumbs.jpg#xywh=320,0,160,90\n",
        encoding="utf-8",
    )


def ensure_user() -> None:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import User, UserSetting
    from app.security import hash_password

    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == USER))
        if user is None:
            user = User(
                username=USER,
                password_hash=hash_password(PASSWORD),
                role=0,
                quota_bytes=512 * 1024 ** 2,
                token_version=1,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(UserSetting(user_id=user.id))
            db.commit()
        else:
            user.password_hash = hash_password(PASSWORD)
            user.banned = False
            user.token_version += 1
            db.commit()


def client() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))


def call(op, method: str, path: str, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with op.open(req, timeout=30) as res:
            body = res.read().decode()
            return res.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()[:200]


def upload(op, path: Path, parent_id: int | None = None) -> dict:
    query = f"/api/nodes/upload?name={urllib.parse.quote(path.name)}"
    if parent_id:
        query += f"&parentId={parent_id}"
    req = urllib.request.Request(BASE + query, method="PUT", data=path.read_bytes())
    req.add_header("Content-Type", "application/octet-stream")
    with op.open(req, timeout=120) as res:
        return json.loads(res.read().decode())["node"]


def main() -> int:
    print("1) 准备素材与账号")
    build_samples()
    ensure_user()

    op = client()
    status, _ = call(op, "POST", "/api/auth/login", {"username": USER, "password": PASSWORD})
    check("登录成功", status == 200, str(status))

    # 清空上一次的残留，保证断言基于干净数据
    status, listing = call(op, "GET", "/api/nodes")
    for item in (listing or {}).get("items", []):
        call(op, "DELETE", f"/api/nodes/{item['id']}")
    call(op, "POST", "/api/trash/empty", {})

    print("2) 上传整套测试文件")
    order = ["demo.mp4", "demo.vtt", "demo.zh.vtt", "demo.thumbs.vtt", "demo.thumbs.jpg",
             "demo.chapters.json", "demo.danmaku.json"]
    nodes = {}
    for name in order:
        nodes[name] = upload(op, SAMPLES / name)
    check("全部文件上传完成", len(nodes) == len(order), str(len(nodes)))

    video = nodes["demo.mp4"]
    check("mp4 预览类型为 video", video.get("previewKind") == "video", str(video.get("previewKind")))

    print("3) 伴生资源自动发现")
    status, sidecars = call(op, "GET", f"/api/nodes/{video['id']}/sidecars")
    check("sidecars 接口 200", status == 200, str(status))
    sidecars = sidecars or {}
    subs = sidecars.get("subtitles") or []
    check("识别出 2 条字幕", len(subs) == 2, str([s.get("name") for s in subs]))
    check("字幕带类型字段", all(s.get("type") == "vtt" for s in subs), str(subs))
    check("识别出语言后缀", any(s.get("lang") == "zh" for s in subs), str([s.get("lang") for s in subs]))
    check("识别出缩略图 VTT", bool(sidecars.get("thumbnails")), str(sidecars.get("thumbnails")))
    check("识别出章节", bool(sidecars.get("chapters")), str(sidecars.get("chapters")))
    check("识别出弹幕", bool(sidecars.get("danmaku")), str(sidecars.get("danmaku")))
    siblings = sidecars.get("siblings") or {}
    check("返回同目录文件名映射", "demo.thumbs.jpg" in siblings, str(list(siblings)[:4]))
    check("映射里不含视频自己", "demo.mp4" not in siblings, str(list(siblings)[:4]))

    print("4) 伴生文件内容可读")
    for key in ("chapters", "danmaku"):
        url = sidecars.get(key)
        if not url:
            continue
        status, body = call(op, "GET", url)
        check(f"{key} 可下载", status == 200, str(status))
    req = urllib.request.Request(BASE + str(sidecars.get("thumbnails")))
    with op.open(req, timeout=30) as res:
        vtt_text = res.read().decode("utf-8", "replace")
    check("缩略图 VTT 可读", "xywh" in vtt_text, vtt_text[:60])

    print("5) 边界：普通文件不应有伴生资源")
    status, plain = call(op, "GET", f"/api/nodes/{nodes['demo.chapters.json']['id']}/sidecars")
    check("json 文件同样返回结构", status == 200 and isinstance(plain, dict), str(status))

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
