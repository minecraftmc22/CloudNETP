# -*- coding: utf-8 -*-
"""主题色样式（单色 / 渐变）的清洗规则冒烟。

只测纯函数，不依赖数据库与浏览器：

  A 合法输入原样保留（含大小写归一）
  B 非法色值 / 非数组 / 超长数组被过滤
  C 渐变不足两色降级为单色
  D 最多 5 个颜色、重复颜色去掉
  E 序列化：老用户没有 accent_style 时输出空对象（前端按单色兜底）

    ./.venv/Scripts/python.exe scripts/accent_style_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-accent-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(TMP / 'accent.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(TMP / "storage"))

from app.routers.auth import _clean_accent_style, _hex_color  # noqa: E402

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
    print("[A] 合法输入")
    check("#RRGGBB 归一成小写", _hex_color("#C084FC") == "#c084fc")
    check(
        "单色原样保留",
        _clean_accent_style({"mode": "solid", "colors": ["#c084fc"]})
        == {"mode": "solid", "colors": ["#c084fc"]},
    )
    check(
        "渐变原样保留",
        _clean_accent_style({"mode": "gradient", "colors": ["#c084fc", "#f472b6"]})
        == {"mode": "gradient", "colors": ["#c084fc", "#f472b6"]},
    )

    print("[B] 非法输入")
    for bad in ("c084fc", "#c084f", "#c084fcc", "#gggggg", "", None, 12345):
        check(f"{bad!r} 被拒", _hex_color(bad) == "", repr(_hex_color(bad)))
    check(
        "非法色值被剔除",
        _clean_accent_style({"mode": "gradient", "colors": ["#c084fc", "red", "#f472b6", "", None]})
        == {"mode": "gradient", "colors": ["#c084fc", "#f472b6"]},
    )
    check("非 dict 返回空对象", _clean_accent_style("oops") == {})
    check("colors 不是数组时当没有", _clean_accent_style({"mode": "solid", "colors": "#c084fc"}) == {"mode": "solid", "colors": []})
    check("未知 mode 按单色", _clean_accent_style({"mode": "rainbow", "colors": ["#c084fc"]})["mode"] == "solid")

    print("[C] 渐变不足两色")
    check(
        "渐变只有一色 → 降级单色",
        _clean_accent_style({"mode": "gradient", "colors": ["#c084fc"]})["mode"] == "solid",
    )
    check(
        "渐变没有颜色 → 降级单色",
        _clean_accent_style({"mode": "gradient", "colors": []})["mode"] == "solid",
    )

    print("[D] 数量与去重")
    many = ["#c084fc", "#f472b6", "#60a5fa", "#34d399", "#fbbf24", "#fb7185", "#111111"]
    cleaned = _clean_accent_style({"mode": "gradient", "colors": many})
    check("最多保留 5 个", len(cleaned["colors"]) == 5, str(len(cleaned["colors"])))
    dup = _clean_accent_style({"mode": "gradient", "colors": ["#c084fc", "#C084FC", "#f472b6"]})
    check("重复颜色去掉", dup["colors"] == ["#c084fc", "#f472b6"], str(dup["colors"]))
    long_list = _clean_accent_style({"mode": "gradient", "colors": [f"#{i:02x}0000" for i in range(1, 40)]})
    check("超长数组不拖慢且截到 5", len(long_list["colors"]) == 5)

    print("[E] 模型与入参")
    from app.models import UserSetting  # noqa: WPS433

    check("模型有 accent_style 列", "accent_style" in UserSetting.__table__.columns)
    from app.schemas import SettingsIn  # noqa: WPS433

    parsed = SettingsIn.model_validate(
        {"accentStyle": {"mode": "gradient", "colors": ["#c084fc", "#f472b6"]}}
    )
    check("入参接受 accentStyle", parsed.accent_style == {"mode": "gradient", "colors": ["#c084fc", "#f472b6"]})

    print()
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
