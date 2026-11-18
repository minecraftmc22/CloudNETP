# -*- coding: utf-8 -*-
"""AI 取色的单元测试（不打真实接口，直接把 _run_text 换成假模型）。

  ./.venv/Scripts/python.exe scripts/ai_accent_unit.py

覆盖：
  1. 颜色解析：三位简写 / rgb() / 全角井号 / 去重
  2. 支持看图：图片真的塞进 messages，提示词才敢说「下面这张图」
  3. 不支持看图：绝不带图、也不说「下面这张图」，问纯文本那轮照常
  4. 模型死活不吐颜色：退回壁纸里采到的候选色（source=sampled），不再一报到底
  5. 连候选色都没有：才抛「模型没挑出能用的颜色」
  6. max_tokens 不再卡 400（推理模型会把预算烧在思考上）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ai_service as ai  # noqa: E402

PASS = FAIL = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


CFG = {
    "baseUrl": "https://example.invalid/v1",
    "key": "sk-test",
    "model": "test-model",
    "protocol": "openai",
    "vision": True,
    "maxOutput": 0,
    "thinking": False,
}
IMG = "data:image/jpeg;base64,AAAA"


def fake(text_fn, seen: list | None = None):
    def _run(cfg, user, messages):
        if seen is not None:
            seen.append({"cfg": cfg, "messages": messages})
        if isinstance(text_fn, Exception):
            raise text_fn
        if callable(text_fn):
            return text_fn(messages)
        return text_fn

    return _run


def main() -> int:
    print("\n[1] 颜色解析")
    check(
        "三位简写 + rgb() + 全角井号",
        ai._parse_colors("挑 ＃ABC 和 rgb(255, 0, 128) 还有 #3b82f6") == ["#aabbcc", "#ff0080", "#3b82f6"],
        str(ai._parse_colors("挑 ＃ABC 和 rgb(255, 0, 128) 还有 #3b82f6")),
    )
    check("去重", ai._parse_colors("#3b82f6 #3B82F6") == ["#3b82f6"], str(ai._parse_colors("#3b82f6 #3B82F6")))
    check("夹在解释文字里也能挑出来", ai._parse_colors('好的：{"colors": ["#a1b2c3"]}') == ["#a1b2c3"])
    check("没颜色就返回空", ai._parse_colors("我看不到图片，请重新上传") == [])

    print("\n[2] 支持看图：图片要带上，提示词才提图")
    seen: list = []
    ai._run_text = fake(lambda m: '{"colors": ["#3b82f6", "#a78bfa"]}', seen)
    out = ai.suggest_accent(None, CFG, mode="gradient", image=IMG, candidates=["#223344"], lang="zh-CN")
    first = seen[0]["messages"][0]
    check("带了图", len(first["images"]) == 1, str(len(first["images"])))
    check("提示词提到壁纸", "下面这张" in first["content"], first["content"][:40])
    check("返回 source=model", out["source"] == "model", str(out))
    check("两个颜色都在", out["colors"] == ["#3b82f6", "#a78bfa"], str(out["colors"]))
    check("这次没卡 max_tokens（=0 交给服务商默认）", seen[0]["cfg"]["maxOutput"] == 0, str(seen[0]["cfg"]["maxOutput"]))

    print("\n[3] 不支持看图：别带图、别在提示词里提图")
    seen = []
    ai._run_text = fake(lambda m: '{"colors": ["#22c55e"]}', seen)
    no_vision = dict(CFG, vision=False)
    out = ai.suggest_accent(None, no_vision, mode="solid", image=IMG, candidates=["#223344"], lang="zh-CN")
    first = seen[0]["messages"][0]
    check("没有图", first["images"] == [], str(first["images"]))
    check("提示词不提图", "下面这张" not in first["content"], first["content"][:60])
    check("照常拿到颜色", out["colors"] == ["#22c55e"], str(out))

    print("\n[4] 模型死活不给颜色：用采到的候选色兜底")
    ai._run_text = fake("我看不到图片，你可以把颜色发给我")
    out = ai.suggest_accent(
        None,
        CFG,
        mode="gradient",
        image=IMG,
        candidates=["#7c3aed", "#a855f7", "#c084fc", "#e879f9"],
        lang="zh-CN",
    )
    check("source 标成 sampled", out["source"] == "sampled", str(out))
    check("渐变给 3 个候选色", len(out["colors"]) == 3, str(out["colors"]))
    check("模式仍是渐变", out["mode"] == "gradient", str(out["mode"]))

    print("\n[5] 连候选色都没有：才报错")
    ai._run_text = fake("我不知道")
    try:
        ai.suggest_accent(None, CFG, mode="solid", image=IMG, candidates=[], lang="zh-CN")
        check("应该抛 AiError", False, "没抛")
    except ai.AiError as exc:
        check("报的是那句熟悉的提示", "没挑出能用的颜色" in exc.message, exc.message)

    print("\n[6] 带图那轮挂了：退回纯文本那轮")
    calls: list = []

    def flaky(cfg, user, messages):
        calls.append(1 if messages[0]["images"] else 0)
        if messages[0]["images"]:
            raise ai.AiError("这个模型不支持图片")
        return '{"colors": ["#f97316"]}'

    ai._run_text = flaky
    out = ai.suggest_accent(None, CFG, mode="solid", image=IMG, candidates=["#111111"], lang="zh-CN")
    check("先带图后纯文本各问一次", calls == [1, 0], str(calls))
    check("第二轮的颜色生效", out["colors"] == ["#f97316"] and out["source"] == "model", str(out))

    print(f"\n通过 {PASS} / {PASS + FAIL}")
    return 1 if FAIL else 0


sys.exit(main())
