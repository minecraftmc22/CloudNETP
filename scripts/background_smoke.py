# -*- coding: utf-8 -*-
"""自定义背景图冒烟：上传 / 缩放 / 取色前提 / 越权 / 换图删旧图 / 移除。

用法：python scripts/background_smoke.py [port]
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User, UserSetting  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"

ok_count = 0
fail_count = 0


def check(label, cond, extra=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  OK   {label} {extra}")
    else:
        fail_count += 1
        print(f"  FAIL {label} {extra}")


def http(method, path, body=None, cookie="", raw=False, binary=None):
    if binary is not None:
        data = binary
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/octet-stream" if binary is not None else "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if raw:
                return resp.status, payload, ctype
            if "json" in ctype:
                return resp.status, json.loads(payload)
            return resp.status, payload[:120]
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, payload[:160]


def make_png(size=(900, 600), color=(47, 109, 178)):
    """造一张以某个主色为主的假壁纸（加一点噪点，模拟真实照片）。"""
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse((w * 0.55, h * 0.1, w * 0.95, h * 0.5), fill=(250, 240, 225))
    draw.rectangle((0, h * 0.78, w, h), fill=(30, 32, 46))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), size


def main():
    db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    other = db.query(User).filter(User.id != admin.id).order_by(User.id).first()
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    other_cookie = f"{settings.COOKIE_NAME}={create_token(other.id, other.token_version)}"
    admin_id, other_id = admin.id, other.id
    row = db.get(UserSetting, admin_id)
    backup = (row.background_image, row.wallpaper) if row else (None, "sakura")
    db.close()

    print("== 上传 ==")
    raw, size = make_png()
    st, d = http("PUT", "/api/me/background", cookie=cookie, binary=raw)
    check("上传返回 200", st == 200, st if st != 200 else "")
    user = d.get("user", {}) if isinstance(d, dict) else {}
    stg = user.get("settings", {})
    url = stg.get("backgroundImage")
    check("返回 backgroundImage", bool(url), url)
    check("wallpaper 自动切成 custom", stg.get("wallpaper") == "custom", stg.get("wallpaper"))

    print("== 文件 ==")
    name = (url or "").rsplit("/", 1)[-1]
    path = settings.background_dir / name
    check("落盘存在", path.is_file(), str(path))
    check("文件名带用户前缀", name.startswith(f"{admin_id}-"), name)
    if path.is_file():
        with Image.open(path) as img:
            check("转成 webp", img.format == "WEBP", img.format)
            check("尺寸保持原比例", abs(img.width / img.height - size[0] / size[1]) < 0.02, img.size)
            check("长边不超过 2560", max(img.size) <= 2560, img.size)
        check("体积合理(< 1MB)", path.stat().st_size < 1024 * 1024, f"{path.stat().st_size} B")

    print("== 读取 ==")
    st, payload, ctype = http("GET", url, cookie=cookie, raw=True)
    check("本人可读 200", st == 200, st)
    check("Content-Type webp", "webp" in (ctype or ""), ctype)
    check("是 RIFF/WEBP 数据", payload[:4] == b"RIFF" and payload[8:12] == b"WEBP", payload[:12])
    st, _ = http("GET", url, cookie=other_cookie, raw=True)
    check("他人读不到 404", st == 404, st)
    st, _ = http("GET", url, raw=True)
    check("未登录 401", st == 401, st)

    print("== 超大图缩放 ==")
    big, big_size = make_png((5200, 3000), (120, 60, 160))
    st, d2 = http("PUT", "/api/me/background", cookie=cookie, binary=big)
    check("超大图上传 200", st == 200, st)
    name2 = (d2.get("user", {}).get("settings", {}).get("backgroundImage") or "").rsplit("/", 1)[-1]
    path2 = settings.background_dir / name2
    if path2.is_file():
        with Image.open(path2) as img:
            check("缩到长边 2560", max(img.size) == 2560, img.size)
            check("比例不变", abs(img.width / img.height - big_size[0] / big_size[1]) < 0.02, img.size)
    check("旧图已删除", not path.is_file(), str(path))

    print("== 主题色 ==")
    st, d3 = http("PATCH", "/api/me/settings", {"accentColor": "#2f6db2"}, cookie)
    check("改主题色 200", st == 200, st)
    check("主题色已存", d3.get("settings", {}).get("accentColor") == "#2f6db2", d3.get("settings", {}).get("accentColor"))
    st, _ = http("PATCH", "/api/me/settings", {"accentColor": "not-a-color"}, cookie)
    check("非法颜色被拒 422", st == 422, st)

    print("== 非图片 ==")
    st, d4 = http("PUT", "/api/me/background", cookie=cookie, binary=b"this is definitely not an image")
    check("非图片被拒 400", st == 400, st)

    print("== 移除 ==")
    st, d5 = http("DELETE", "/api/me/background", cookie=cookie)
    stg5 = d5.get("user", {}).get("settings", {}) if isinstance(d5, dict) else {}
    check("移除 200", st == 200, st)
    check("backgroundImage 清空", stg5.get("backgroundImage") is None, stg5.get("backgroundImage"))
    check("壁纸回落 sakura", stg5.get("wallpaper") == "sakura", stg5.get("wallpaper"))
    check("文件已删除", not path2.is_file(), str(path2))

    print("== 还原 ==")
    db = db_failover.current_sessionmaker()()
    row = db.get(UserSetting, admin_id)
    if row:
        row.background_image, row.wallpaper = backup
        db.commit()
    db.close()
    check("还原原设置", True)

    print(f"\n通过 {ok_count} 项，失败 {fail_count} 项")
    print("（admin id=%s，other id=%s）" % (admin_id, other_id))
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
