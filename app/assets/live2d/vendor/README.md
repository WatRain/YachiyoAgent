# 内置的前端渲染依赖

这里**不是**本项目的代码，是第三方库，按各自许可证随程序分发。
两个都是 MIT，不影响本项目选择什么许可证。

重新生成：`python tools/vendor_live2d.py`

| 文件 | 说明 | 大小 | sha256 |
|---|---|---|---|
| `pixi.min.mjs` | PixiJS 8.13.1（ESM 构建）<br>来源：https://cdn.jsdelivr.net/npm/pixi.js@8.13.1/dist/pixi.min.mjs | 703842 B | `9ddaaa814931b317056dc36d6d1868b0c7657ed4ffcf8438e581b6d1471269ff` |
| `pixi-live2d-engine.cubism.es.js` | untitled-pixi-live2d-engine 1.4.0（Cubism 3/4/5，ESM 构建）<br>来源：https://cdn.jsdelivr.net/npm/untitled-pixi-live2d-engine@1.4.0/dist/cubism.es.js | 548402 B | `4afe8c561d06b6f5c3bd5b4eeb67362cf54e3c12b5e84433e16429dde3159091` |
| `LICENSE-pixi.txt` | PixiJS 8.13.1 许可证全文<br>来源：https://cdn.jsdelivr.net/npm/pixi.js@8.13.1/LICENSE | 1092 B | `5ce7447bc57f7349ffc48338782fbcabe613696e00712b20d66bc58e780f9473` |
| `LICENSE-untitled-pixi-live2d-engine.txt` | untitled-pixi-live2d-engine 1.4.0 许可证全文<br>来源：https://cdn.jsdelivr.net/npm/untitled-pixi-live2d-engine@1.4.0/LICENSE | 1070 B | `159c77232f8fe25574e83ea55db995e49d090ddf6a744df7390740f6b5a8c5e9` |

## 不在这里的依赖

**Cubism Core**（`live2dcubismcore.min.js`）由页面在运行时从 Live2D 官方 CDN 加载，
不由我们分发：

    https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js

## 为什么不用 live2d-widget

模型原先配的是 `stevenjoezhang/live2d-widget`，它是 **GPL-3.0-or-later**。
一旦随程序分发，整个应用就必须是 GPL-3（必须公开源码）。
换成 MIT 的 PIXI + Live2D 引擎，本项目就能自由选择许可证。

## 为什么不用 pixi-live2d-display 0.4.0

它是 2022 年的东西，只带 **Cubism 4** 框架；我们的模型 moc3 是**版本 5**
（文件头 `MOC3\x05`），加载它会让渲染进程直接崩，而且 HTML 侧没有任何报错。
换成支持 Cubism 2/3/4/5 的 untitled-pixi-live2d-engine。
