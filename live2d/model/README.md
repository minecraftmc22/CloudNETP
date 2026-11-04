# Live2D 模型目录

网页端的看板娘会读取这个目录，**每次打开 / 每 40 秒会自动重新扫描一次**，
所以往这里增删模型后不需要改代码、也不用重启服务，刷一下页面（或等一会儿）就生效。

## 放法

一个模型一个文件夹，里面放 `*.model3.json` 及其贴图、moc3、动作等：

```
live2d/model/
  Hiyori/
    Hiyori.model3.json
    Hiyori.moc3
    Hiyori.2048/texture_00.png
    motions/xxx.motion3.json
```

也支持直接把 `xxx.model3.json` 丢在 `model/` 根下。

## 支持的格式

| 格式 | 识别文件 | 是否可渲染 |
| --- | --- | --- |
| Cubism 4 / 5 | `*.model3.json` | ✅ |
| Cubism 2.1 | `*.model.json` | ❌（会在 `/api/live2d/models` 的 `unsupported` 里列出来） |

## 其他

- 模型地址对外是 `/live2d/model/<文件夹名>/<文件名>`，由后端 StaticFiles 直接托管。
- 有多个模型时，鼠标移到看板娘上会出现「换一个」按钮；选择记在 localStorage 的 `mascot-model`。
- 目录为空或加载失败时自动退回原来的云朵 SVG，不会白屏。
- 官方示例模型为 Live2D 免费素材，商用前请看清其条款；本仓库自带的 **CDP娘** 模型版权归
  Minecraftmc22 所有，未经授权禁止商用，详见 [CDP娘版权声明](CDP/COPYRIGHT.md)。
