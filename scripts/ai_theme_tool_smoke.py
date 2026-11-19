# -*- coding: utf-8 -*-
"""AI 改主题色工具离线联调（不经过模型，直接调 run_tool_safe）。

  A 没勾「修改界面主题色」权限时，工具拒绝并提示去哪开
  B 单色：落库 accent_color / accent_style，返回里带色值和 settings 补丁
  C 渐变：多色去重、小写、容忍三位缩写与不带 # 的写法
  D 渐变只给一个颜色：自动补同色系的第二个颜色
  E 从背景图取色：真的读背景图采样，默认走渐变
  F 没有背景图却要求取色：如实报错，不编颜色
  G reset：恢复默认主题色（樱紫单色）
  H tool_specs：没授权不下发，授权后才出现

    ./.venv/Scripts/python.exe scripts/ai_theme_tool_smoke.py
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-theme-")).resolve()
DB = TMP / "tool.db"


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


snapshot_db(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["DB_MIRROR_INTERVAL"] = "0"
sys.path.insert(0, str(ROOT))

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


def main() -> int:
    from PIL import Image

    from app import ai_service, profile_service, theme_color
    from app.db import SessionLocal, init_db
    from app.models import User
    from app.user_service import ensure_settings

    init_db()
    db = SessionLocal()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    if admin is None:
        raise SystemExit("库里没有管理员")
    row = ensure_settings(db, admin.id)
    row.background_image = None
    row.accent_color = "#c084fc"
    row.accent_style = {"mode": "solid", "colors": ["#c084fc"]}
    db.commit()

    print("\n[A] 没勾权限")
    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"color": "#60a5fa"}, {"theme": False}
    )
    check("拒绝执行", "失败" in out.text and "主题色" in out.text, out.text[:60])
    db.expire_all()
    check("库里的颜色没被改动", (row.accent_color or "") == "#c084fc", row.accent_color)

    print("\n[B] 单色")
    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"mode": "solid", "color": "#60A5FA"}, {"theme": True}
    )
    db.expire_all()
    check("落库 accent_color", row.accent_color == "#60a5fa", row.accent_color)
    check("落库 accent_style", row.accent_style == {"mode": "solid", "colors": ["#60a5fa"]}, str(row.accent_style))
    check("返回里说明已生效", "#60a5fa" in out.text and "生效" in out.text, out.text[:80])
    check("带回 settings 补丁", out.settings == {"accentColor": "#60a5fa", "accentStyle": {"mode": "solid", "colors": ["#60a5fa"]}}, str(out.settings))

    print("\n[C] 渐变（脏写法")
    out = ai_service.run_tool_safe(
        admin.id,
        "set_theme_color",
        {"mode": "gradient", "colors": ["#ABC", "34d399", "#34d399", "zzz", "#f472b6"]},
        {"theme": True},
    )
    db.expire_all()
    check(
        "去重 + 小写 + 补全 #abc",
        row.accent_style == {"mode": "gradient", "colors": ["#aabbcc", "#34d399", "#f472b6"]},
        str(row.accent_style),
    )
    check("主色跟第一个走", row.accent_color == "#aabbcc", row.accent_color)
    check("返回里画了渐变箭头", "→" in out.text, out.text[:80])

    print("\n[D] 渐变只给一个颜色")
    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"mode": "gradient", "color": "#fbbf24"}, {"theme": True}
    )
    db.expire_all()
    colors = (row.accent_style or {}).get("colors") or []
    check("自动补到两色", len(colors) == 2, str(colors))
    check("第二个是同色系", colors[0] == "#fbbf24" and colors[1] != colors[0], str(colors))

    print("\n[E] 从背景图取色")
    buf = io.BytesIO()
    img = Image.new("RGB", (320, 180), "#1b5e20")
    for x in range(120):
        for y in range(120):
            img.putpixel((x, y), (240, 120, 60))
    img.save(buf, format="PNG")
    name = profile_service.save_background(admin.id, buf.getvalue())
    db.expire_all()
    row.background_image = name
    db.commit()

    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"from_background": True}, {"theme": True}
    )
    db.expire_all()
    style = row.accent_style or {}
    colors = style.get("colors") or []
    check("真的从图里取到颜色", bool(colors) and all(theme_color.normalize_hex(c) == c for c in colors), str(colors))
    check("没说模式时默认走渐变", style.get("mode") == "gradient" and len(colors) >= 2, str(style))
    check("返回里带采样结果", "#" in out.text and "生效" in out.text, out.text[:100])

    print("\n[F] 没有背景图时取色")
    db.expire_all()
    row.background_image = None
    db.commit()
    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"from_background": True}, {"theme": True}
    )
    check("如实报错", "失败" in out.text and "背景图" in out.text, out.text[:70])

    print("\n[G] 恢复默认")
    out = ai_service.run_tool_safe(
        admin.id, "set_theme_color", {"reset": True}, {"theme": True}
    )
    db.expire_all()
    check(
        "回到樱紫单色",
        row.accent_color == "#c084fc" and row.accent_style == {"mode": "solid", "colors": ["#c084fc"]},
        f"{row.accent_color} {row.accent_style}",
    )
    check("返回里有默认色", "#c084fc" in out.text, out.text[:60])

    print("\n[H] tool_specs 下发")
    names = {spec["function"]["name"] for spec in ai_service.tool_specs({"tools": True})}
    check("没授权不下发", "set_theme_color" not in names, str(sorted(names))[:80])
    names = {
        spec["function"]["name"]
        for spec in ai_service.tool_specs({"tools": True, "themeColor": True})
    }
    check("授权后下发", "set_theme_color" in names, "")
    check("权限映射对得上", ai_service.TOOL_PERMISSION.get("set_theme_color") == "theme", "")

    db.close()
    print(f"\n结果：{passed} 通过，{failed} 失败")
    import shutil

    if failed == 0:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("临时目录保留：", TMP)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
