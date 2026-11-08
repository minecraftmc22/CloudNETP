# -*- coding: utf-8 -*-
"""生成 README 用的界面截图。

不碰真实库：临时目录里建一套演示数据（用户 / 目录树 / 图片 / Markdown / 分享 / 审计日志），
起独立实例后用 Playwright 截图，最后压到固定宽度写进 docs/screenshots/。

    ./.venv/Scripts/python.exe scripts/make_readme_shots.py [--keep]

--keep 保留临时目录（调试用），默认跑完就删。
"""

from __future__ import annotations

import io
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
KEEP = "--keep" in sys.argv

# 截图宽度（CSS 像素）：桌面 1440、手机 390，抓 2 倍图后按宽度压回来
DESK_W, DESK_H = 1440, 1000
MOB_W, MOB_H = 390, 844


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 演示图：PIL 现画的风景（天空渐变 + 日晕 + 层叠山脊 + 花瓣 / 星星）
# 不往仓库塞任何第三方素材，脚本跑完图也跟着临时目录一起删掉
# --------------------------------------------------------------------------- #
def _ridge(draw, width: int, height: int, base: float, amp: float, phase: float,
           color: tuple[int, int, int]) -> None:
    import math

    points = [(0, height)]
    for x in range(0, width + 12, 10):
        t = x / width
        y = base * height - amp * height * (
            0.55 * math.sin(t * 5.1 + phase)
            + 0.30 * math.sin(t * 11.7 + phase * 2.3)
            + 0.15 * math.sin(t * 23.3 + phase * 4.1)
        )
        points.append((x, y))
    points.append((width, height))
    draw.polygon(points, fill=color)


def _petal(draw, cx: float, cy: float, radius: float, angle: float,
           color: tuple[int, int, int, int]) -> None:
    import math

    points = []
    for i in range(14):
        theta = 2 * math.pi * i / 14
        x = radius * math.cos(theta)
        y = radius * 0.52 * math.sin(theta)
        points.append((cx + x * math.cos(angle) - y * math.sin(angle),
                       cy + x * math.sin(angle) + y * math.cos(angle)))
    draw.polygon(points, fill=color)


PRESETS: dict[str, dict] = {
    # 樱雾：黄昏粉紫天空 + 低垂日轮 + 三层山脊 + 飘落花瓣
    "sakura": dict(
        sky=((58, 32, 80), (255, 216, 232)),
        sun=(0.68, 0.60, 0.30, (255, 240, 244), 165),
        ridges=[(0.62, 0.045, 0.0, (198, 134, 178)),
                (0.71, 0.055, 1.7, (142, 86, 134)),
                (0.82, 0.070, 3.4, (72, 38, 74))],
        petals=90, stars=0, seed=7,
    ),
    # 夜樱：深蓝夜空 + 月亮 + 星星
    "night": dict(
        sky=((5, 7, 18), (38, 44, 92)),
        sun=(0.24, 0.22, 0.11, (240, 244, 255), 150),
        ridges=[(0.58, 0.040, 0.6, (32, 36, 68)),
                (0.70, 0.060, 2.2, (18, 22, 44)),
                (0.84, 0.080, 4.0, (7, 9, 20))],
        petals=0, stars=460, seed=13,
    ),
    # 晨雾：暖橘到灰紫的日出
    "dawn": dict(
        sky=((255, 214, 170), (108, 84, 128)),
        sun=(0.30, 0.66, 0.26, (255, 244, 214), 175),
        ridges=[(0.66, 0.038, 1.1, (206, 160, 150)),
                (0.76, 0.058, 2.9, (140, 102, 118)),
                (0.88, 0.075, 4.6, (66, 48, 66))],
        petals=40, stars=0, seed=21,
    ),
    # 林间：青绿到墨绿
    "forest": dict(
        sky=((188, 226, 208), (12, 34, 34)),
        sun=(0.62, 0.34, 0.22, (232, 250, 226), 130),
        ridges=[(0.58, 0.050, 0.3, (96, 148, 124)),
                (0.72, 0.070, 2.0, (38, 84, 74)),
                (0.86, 0.090, 3.8, (10, 30, 30))],
        petals=0, stars=0, seed=33,
    ),
    # 纸感：浅灰水墨，当对比色
    "paper": dict(
        sky=((246, 246, 250), (176, 180, 196)),
        sun=(0.34, 0.40, 0.24, (255, 255, 255), 120),
        ridges=[(0.60, 0.035, 0.8, (186, 190, 204)),
                (0.74, 0.055, 2.6, (134, 140, 160)),
                (0.88, 0.075, 4.4, (78, 84, 104))],
        petals=0, stars=0, seed=41,
    ),
    # 花瓣特写：整片粉色 + 大量花瓣，浅景深感
    "petal": dict(
        sky=((255, 240, 246), (226, 158, 192)),
        sun=(0.36, 0.30, 0.34, (255, 252, 253), 150),
        ridges=[],
        petals=220, stars=0, seed=55,
    ),
}


def landscape(kind: str, width: int = 1600, height: int = 1000) -> bytes:
    import math
    import random

    from PIL import Image, ImageDraw, ImageFilter

    cfg = PRESETS[kind]
    rng = random.Random(cfg["seed"])

    grad = Image.linear_gradient("L").resize((width, height))
    base = Image.composite(
        Image.new("RGB", (width, height), cfg["sky"][1]),
        Image.new("RGB", (width, height), cfg["sky"][0]),
        grad,
    ).convert("RGBA")

    # 日轮 / 月亮：一圈同心圆做柔光
    if cfg.get("sun"):
        cx, cy, radius, color, alpha = cfg["sun"]
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        layers = 26
        for i in range(layers, 0, -1):
            r = radius * width * (i / layers) * 1.6
            a = int(alpha * (1 - i / layers) ** 2.2)
            if a <= 0:
                continue
            gdraw.ellipse((cx * width - r, cy * height - r, cx * width + r, cy * height + r),
                          fill=(*color, a))
        base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(width / 90)))

    # 星星
    if cfg.get("stars"):
        stars = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(stars)
        for _ in range(cfg["stars"]):
            x = rng.uniform(0, width)
            y = rng.uniform(0, height * 0.62)
            r = rng.choice([0.7, 0.9, 1.1, 1.4, 1.9])
            a = int(120 + 120 * rng.random())
            sdraw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 250, 240, a))
        base = Image.alpha_composite(base, stars.filter(ImageFilter.GaussianBlur(0.6)))

    # 山脊
    ridges = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ridges)
    for base_y, amp, phase, color in cfg["ridges"]:
        _ridge(rdraw, width, height, base_y, amp, phase, color)
    base = Image.alpha_composite(base, ridges)

    # 花瓣：越靠下越大，做出景深
    if cfg.get("petals"):
        petals = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(petals)
        for _ in range(cfg["petals"]):
            x = rng.uniform(-40, width + 40)
            y = rng.uniform(-40, height + 40)
            depth = y / height
            size = 4 + 13 * depth * rng.random()
            alpha = int(70 + 90 * depth)
            tint = rng.choice([(255, 226, 238), (250, 206, 224), (255, 238, 244), (244, 190, 214)])
            _petal(pdraw, x, y, size, rng.uniform(0, math.pi), (*tint, min(190, alpha)))
        base = Image.alpha_composite(base, petals.filter(ImageFilter.GaussianBlur(2.2)))

    base = base.convert("RGB")

    # 暗角 + 细颗粒，压住大面积渐变的色带
    vignette = Image.new("L", (width, height), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse((-width * 0.25, -height * 0.35, width * 1.25, height * 1.35), fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(width / 12))
    base = Image.composite(base, Image.eval(base, lambda v: int(v * 0.78)), vignette)

    noise = Image.effect_noise((width, height), 10).convert("L")
    base = Image.composite(base.point(lambda v: min(255, v + 5)), base, noise)

    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=90, progressive=True, optimize=True)
    return buf.getvalue()


def markdown_doc() -> bytes:
    text = """# CloudNETP 使用说明

> 一份自己写的云盘，数据放在自己的服务器上。

## 快速上手

1. 把文件或整个文件夹拖进窗口，同名目录会自动合并
2. 大文件按 5 MB 切片上传，断网后从已传的块继续
3. 相同内容只存一份，第二次上传直接秒传

## 支持的预览

| 类型 | 说明 |
| --- | --- |
| 图片 | 缩放、拖动、方向键切换 |
| 音视频 | 断点续传、字幕、章节、进度缩略图 |
| 文档 | Word / Excel / PPT / ODF / PDF / Markdown |

```bash
./start.sh          # Linux / macOS
.\\start.ps1         # Windows PowerShell
```

## 目录约定

- `01_学习资料` 笔记与课件
- `02_摄影` 原图与成片
- `03_影音` 音乐、视频
- `04_文档` 部署笔记、接口速查
"""
    return text.encode("utf-8")


# --------------------------------------------------------------------------- #
# 种子数据
# --------------------------------------------------------------------------- #
def seed(db, settings, security, settings_store) -> dict:
    from app.models import AuditLog, Blob, Feedback, Node, Share, User
    from app.user_service import ensure_settings

    now = datetime.utcnow()
    admin = User(
        username="sakura", password_hash=security.hash_password("Cloud@2026"),
        display_name="樱花", email="sakura@example.com", email_verified=True,
        role=1, quota_bytes=100 * 1024**3, token_version=1,
        created_at=now - timedelta(days=126), last_login_at=now - timedelta(minutes=3),
    )
    guest_a = User(
        username="asuka", password_hash=security.hash_password("Cloud@2026"),
        display_name="明日香", role=0, quota_bytes=20 * 1024**3,
        created_at=now - timedelta(days=37), last_login_at=now - timedelta(days=1),
    )
    guest_b = User(
        username="rei", password_hash=security.hash_password("Cloud@2026"),
        display_name="绫波", role=0, quota_bytes=20 * 1024**3,
        created_at=now - timedelta(days=3), last_login_at=now - timedelta(hours=6),
    )
    db.add_all([admin, guest_a, guest_b])
    db.commit()

    st = ensure_settings(db, admin.id)
    st.theme_mode = 1  # 暗色
    st.wallpaper = "sakura"
    st.accent_color = "#f9a8d4"
    st.language = "zh-CN"
    st.view_mode = "grid"
    st.share_show_name = True
    st.surface_opacity = {"nav": 78, "titlebar": 74, "panel": 82, "dialog": 94}
    for user in (guest_a, guest_b):
        s = ensure_settings(db, user.id)
        s.theme_mode = 1
        s.language = "zh-CN"

    def add_blob(payload: bytes) -> tuple[str, int]:
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        path = settings.blob_dir / sha[:2] / sha[2:4] / sha
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        row = db.get(Blob, sha)
        if row is None:
            db.add(Blob(sha256=sha, size=len(payload), ref_count=1, created_at=now))
        else:
            row.ref_count += 1
        return sha, len(payload)

    total_used = [0]

    def make_file(parent_id, name: str, payload: bytes, mime: str, days_ago: float = 1.0) -> Node:
        sha, size = add_blob(payload)
        stamp = now - timedelta(days=days_ago)
        node = Node(
            user_id=admin.id, parent_id=parent_id, type="file", name=name,
            blob_sha256=sha, size=size, mime=mime, created_at=stamp, updated_at=stamp,
        )
        db.add(node)
        db.flush()
        total_used[0] += size
        return node

    def make_folder(parent_id, name: str, days_ago: float = 2.0) -> Node:
        stamp = now - timedelta(days=days_ago)
        node = Node(
            user_id=admin.id, parent_id=parent_id, type="folder", name=name,
            size=0, mime=None, created_at=stamp, updated_at=stamp,
        )
        db.add(node)
        db.flush()
        return node

    sakura = landscape("sakura")
    petal = landscape("petal")
    dawn = landscape("dawn")
    forest = landscape("forest")
    night = landscape("night")
    paper = landscape("paper")

    # 根目录
    study = make_folder(None, "01_学习资料", 42)
    photo = make_folder(None, "02_摄影", 30)
    media = make_folder(None, "03_影音", 21)
    docs = make_folder(None, "04_文档", 15)
    design = make_folder(None, "05_设计稿", 8)
    setup = make_folder(None, "06_安装包", 5)

    make_file(None, "使用说明.md", markdown_doc(), "text/markdown", 0.2)
    make_file(None, "樱雾壁纸.jpg", sakura, "image/jpeg", 0.6)
    make_file(None, "换机清单.csv",
              "项目,状态,备注\n浏览器书签,已迁移,\nSSH 密钥,已迁移,id_ed25519\n笔记库,待导出,约 2.1 GB\n".encode("utf-8"),
              "text/csv", 3)
    make_file(None, "待办.md",
              "# 待办\n\n- [x] 迁移笔记库\n- [ ] 整理 06_安装包\n- [ ] 给分享链接加提取码\n".encode("utf-8"),
              "text/markdown", 1.2)
    make_file(None, "桌面壁纸-夜.jpg", night, "image/jpeg", 2.0)
    make_file(None, "读书清单.csv",
              "书名,状态\n《设计数据密集型应用》,在读\n《计算机程序的构造和解释》,搁置\n".encode("utf-8"),
              "text/csv", 7.5)

    # 01_学习资料
    make_file(study.id, "Python 笔记.md",
              "# Python 笔记\n\n## 解包\n\n```python\na, *rest = [1, 2, 3]\n```\n\n## 上下文管理器\n\n`with` 走 `__enter__` / `__exit__`，异常也会经过 `__exit__`。\n".encode("utf-8"),
              "text/markdown", 12)
    make_file(study.id, "算法题解.md",
              "# 算法题解\n\n- 双指针：有序数组两数之和\n- 滑动窗口：最长无重复子串\n- 单调栈：下一个更大元素\n".encode("utf-8"),
              "text/markdown", 9)
    make_file(study.id, "学习计划.csv",
              "周次,主题,进度\nW1,HTTP 与缓存,100%\nW2,数据库索引,80%\nW3,并发与锁,40%\n".encode("utf-8"),
              "text/csv", 6)
    front = make_folder(study.id, "前端", 7)
    make_file(front.id, "ES Module 备忘.md",
              "# ES Module 备忘\n\n- 顶层 `await` 只在 module 里可用\n- 导入是静态提升的，不能写在 `if` 里\n- `type=\"module\"` 的脚本天然 defer\n".encode("utf-8"),
              "text/markdown", 5)
    make_file(front.id, "样式变量表.csv",
              "变量,用途\n--radius-m,卡片圆角\n--op-panel,面板不透明度\n--accent,主题色\n".encode("utf-8"),
              "text/csv", 5)

    # 02_摄影
    spring = make_folder(photo.id, "2026 樱花", 4)
    make_file(spring.id, "樱花-01.jpg", petal, "image/jpeg", 4)
    make_file(spring.id, "樱花-02.jpg", dawn, "image/jpeg", 4)
    make_file(spring.id, "樱花-03.jpg", forest, "image/jpeg", 4)
    city = make_folder(photo.id, "城市夜景", 11)
    make_file(city.id, "夜景-01.jpg", night, "image/jpeg", 11)
    make_file(city.id, "雾夜-01.jpg", paper, "image/jpeg", 11)

    # 03_影音
    make_file(media.id, "歌单.csv",
              "曲名,歌手\nLover Without A Heart,CG5\nOn My Feet,JokerZHEN\n".encode("utf-8"),
              "text/csv", 8)

    # 04_文档
    make_file(docs.id, "部署笔记.md",
              "# 部署笔记\n\n1. `cp .env.example .env`\n2. 填 `DATABASE_URL` 与 `SECRET_KEY`\n3. `./start.sh`\n\n反向代理记得转发 `X-Forwarded-For`，登录限流才准。\n".encode("utf-8"),
              "text/markdown", 10)
    make_file(docs.id, "接口速查.csv",
              "方法,路径,说明\nGET,/api/nodes,列目录\nPOST,/api/uploads,建上传会话\nGET,/api/shares,我的分享\n".encode("utf-8"),
              "text/csv", 13)

    # 05_设计稿
    icons = make_folder(design.id, "图标", 2)
    make_file(icons.id, "icon-512.png", (ROOT / "static" / "assets" / "icon-512.png").read_bytes(), "image/png", 2)

    # 06_安装包留空，展示空目录也能正常显示
    _ = setup

    # 另外两个用户各放一点东西：占用排行与「本周新增」才有内容
    guest_size = [0, 0]

    def guest_file(user, name: str, payload: bytes, mime: str, days_ago: float, slot: int) -> None:
        sha, size = add_blob(payload)
        stamp = now - timedelta(days=days_ago)
        db.add(Node(user_id=user.id, parent_id=None, type="file", name=name, blob_sha256=sha,
                    size=size, mime=mime, created_at=stamp, updated_at=stamp))
        guest_size[slot] += size

    guest_file(guest_a, "夜色.jpg", night, "image/jpeg", 1.4, 0)
    guest_file(guest_a, "手账.md", "# 手账\n\n- 周五交作业\n- 周末拍樱花\n".encode("utf-8"), "text/markdown", 2.2, 0)
    guest_file(guest_b, "速写.jpg", paper, "image/jpeg", 0.8, 1)

    admin.used_bytes = total_used[0]
    guest_a.used_bytes = guest_size[0]
    guest_b.used_bytes = guest_size[1]
    db.commit()

    # 审计日志：今天 / 本周 / 更早都铺一点，后台概览的数字才不会全是 0
    logs = [
        (admin, "login", "127.0.0.1", 0.01),
        (admin, "node_upload", "樱雾壁纸.jpg（1.4 MB）", 0.03),
        (admin, "share_create", "樱雾壁纸.jpg · 有效期 12 天", 0.06),
        (guest_a, "login", "127.0.0.1", 0.2),
        (guest_a, "node_upload", "歌单.csv（128 B）", 0.25),
        (admin, "node_rename", "Python笔记 → Python 笔记", 0.5),
        (admin, "node_move", "樱花-03.jpg → 2026 樱花", 1.2),
        (guest_b, "login", "127.0.0.1", 1.5),
        (admin, "node_delete", "旧备份.zip", 2.4),
        (admin, "node_restore", "学习计划.csv", 3.1),
        (guest_a, "share_download", "樱花-01.jpg", 4.6),
        (admin, "dav_upload", "/dav/04_文档/部署笔记.md", 6.2),
        (admin, "node_upload", "部署笔记.md（2.1 KB）", 9.0),
        (guest_b, "register", "127.0.0.1", 11.0),
        (admin, "login", "127.0.0.1", 16.0),
        (admin, "admin_settings_update", "site_name / 默认配额", 18.5),
    ]
    for user, action, detail, days in logs:
        db.add(AuditLog(user_id=user.id, action=action, detail=detail,
                        ip="192.168.1.24", created_at=now - timedelta(days=days)))

    db.add_all([
        Feedback(user_id=guest_a.id, kind="suggestion", title="希望支持批量改后缀",
                 content="一次选中十个文件改后缀要点十次，能批量就好了。", status=0,
                 created_at=now - timedelta(days=2)),
        Feedback(user_id=guest_b.id, kind="bug", title="iOS 上播放器进度条拖不动",
                 content="Safari 里拖进度条会跳回开头，安卓 Chrome 正常。", contact="rei@example.com",
                 status=1, handled_by=admin.id, handled_at=now - timedelta(days=4),
                 created_at=now - timedelta(days=5)),
    ])

    # 分享：单文件 + 合集
    wallpaper = db.query(Node).filter(Node.name == "樱雾壁纸.jpg").one()
    share = Share(
        user_id=admin.id, node_id=wallpaper.id, token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(days=12), max_downloads=200, download_count=37,
        created_at=now - timedelta(days=2),
    )
    collection = Share(
        user_id=admin.id, node_id=photo.id, node_ids=f"[{photo.id}]",
        token=secrets.token_urlsafe(32), expires_at=now + timedelta(days=7),
        download_count=12, created_at=now - timedelta(days=5),
    )
    db.add_all([share, collection])
    db.commit()

    settings_store.set_value(db, "site_name", "CloudNETP")
    settings_store.set_value(db, "site_announcement", "")
    settings_store.set_value(db, "contact_email", "sakura@example.com")
    db.commit()

    return {"share_token": share.token, "user": admin, "wallpaper": wallpaper.name}


# --------------------------------------------------------------------------- #
def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(100):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-readme-")).resolve()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    os.environ.update({
        "DATABASE_URL": f"sqlite:///{(tmp / 'demo.db').as_posix()}",
        "STORAGE_DIR": str(tmp / "storage"),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "RELOAD": "false",
        "DB_PROBE_INTERVAL": "0",
        "DB_FALLBACK_ENABLED": "false",
        "ALLOW_REGISTER": "0",
        "SECRET_KEY": secrets.token_hex(32),
    })
    sys.path.insert(0, str(ROOT))

    from app import security, settings_store
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema

    ensure_schema(engine)
    with SessionLocal() as db:
        info = seed(db, settings, security, settings_store)
        cookie = security.create_token(info["user"].id, info["user"].token_version)
        share_token = info["share_token"]
        db.refresh(info["user"])
        print(f"种子数据就绪：{settings.DATABASE_URL}")

    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=os.environ.copy(),
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    shots: list[tuple[str, Path]] = []
    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        raw_dir = tmp / "raw"
        raw_dir.mkdir()
        OUT.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(headless=True)

            def new_page(width, height, mobile=False, scale=2):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=scale,
                    is_mobile=mobile,
                    has_touch=mobile,
                    locale="zh-CN",
                    color_scheme="dark",
                )
                ctx.add_cookies([{"name": settings.COOKIE_NAME, "value": cookie, "url": base}])
                page = ctx.new_page()
                page.add_init_script("try { localStorage.setItem('cloudnetp.lang', 'zh-CN'); } catch (e) {}")
                return ctx, page

            def shot(page, name: str) -> None:
                path = raw_dir / f"{name}.png"
                page.wait_for_timeout(350)
                page.screenshot(path=str(path))
                shots.append((name, path))
                print(f"  截到 {name}.png")

            # 1. 登录页（无 Cookie）
            guest = browser.new_context(
                viewport={"width": DESK_W, "height": DESK_H}, device_scale_factor=2,
                locale="zh-CN", color_scheme="dark",
            )
            gpage = guest.new_page()
            gpage.goto(base, wait_until="domcontentloaded")
            gpage.wait_for_selector(".auth-card", timeout=20000)
            gpage.wait_for_timeout(1400)
            shot(gpage, "login")

            # 6. 分享页（同一个访客上下文）
            share_page = guest.new_page()
            share_page.goto(f"{base}/s/{share_token}", wait_until="domcontentloaded")
            share_page.wait_for_selector(".share-file__name", timeout=20000)
            share_page.wait_for_timeout(1500)
            shot(share_page, "share")
            guest.close()

            ctx, page = new_page(DESK_W, DESK_H)

            # 2. 文件管理
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".file-grid .tile", timeout=20000)
            page.wait_for_timeout(1500)
            shot(page, "files")

            # 3. 图片预览
            page.dblclick('.tile:has-text("樱雾壁纸.jpg")')
            page.wait_for_selector(".viewer__frame", timeout=20000)
            page.wait_for_timeout(1600)
            shot(page, "viewer-image")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

            # 4. Markdown 预览
            page.dblclick('.tile:has-text("使用说明.md")')
            page.wait_for_selector(".md-body", timeout=20000)
            page.wait_for_timeout(1200)
            shot(page, "viewer-markdown")
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # 5. 设置页
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector(".settings", timeout=20000)
            page.wait_for_timeout(1400)
            shot(page, "settings")

            # 7. 管理后台
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector(".admin-tabs", timeout=20000)
            page.wait_for_timeout(1600)
            shot(page, "admin")
            ctx.close()

            # 8. 手机端
            mctx, mpage = new_page(MOB_W, MOB_H, mobile=True, scale=3)
            mpage.goto(f"{base}/#/files", wait_until="domcontentloaded")
            mpage.wait_for_selector(".tile", timeout=20000)
            mpage.wait_for_timeout(1500)
            shot(mpage, "mobile")
            mctx.close()
            browser.close()

        # 压到固定宽度，顺手把体积降下来
        from PIL import Image

        widths = {"mobile": MOB_W * 2}
        for name, path in shots:
            target_w = widths.get(name, 1600)
            img = Image.open(path).convert("RGB")
            if img.width > target_w:
                img = img.resize((target_w, round(img.height * target_w / img.width)), Image.LANCZOS)
            out = OUT / f"{name}.jpg"
            img.save(out, format="JPEG", quality=92, optimize=True, progressive=True)
            print(f"  {out.relative_to(ROOT)}  {img.width}×{img.height}  {out.stat().st_size // 1024} KB")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()
        if KEEP:
            print(f"临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
