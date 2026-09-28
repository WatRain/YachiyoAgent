# 第三方组件与许可说明

月见八千代 Agent 的自有代码按仓库根目录 [LICENSE](LICENSE) 中的 MIT License 授权。这个许可不覆盖第三方代码、Live2D 模型或其他素材；它们仍按各自的许可和权利人要求使用。

本项目为**完全开源免费**的个人项目，我**不会**从其中获得任何盈利。如有侵权，请通过我的[Github 主页](https://github.com/WatRain)联系我，我将积极配合处理。

下表列出项目当前使用或随程序分发的主要组件。Python 依赖在 [requirements.txt](requirements.txt) 中使用版本范围而非完整锁定文件，因此具体构建中解析到的版本可能不同。

## Live2D、角色原作与模型素材

### 角色原作

- 月见八千代（日文名：月見ヤチヨ）是官方作品[《超时空辉夜姬！》](https://www.cho-kaguyahime.com/)中的角色。官方页面列出她是虚拟空间「ツクヨミ」的管理人兼顶级直播主；作品官网页脚标注版权为 `©コロリド・ツインエンジンパートナーズ`。
- 本项目是独立的非官方应用（二创作品）。仓库根目录的 MIT License 只覆盖本项目自有代码，不覆盖《超时空辉夜姬！》的角色、名称、设定、音乐、画面或其他原作素材。

### Live2D Cubism Core

- 性质：Live2D 的专有运行库，不是开源软件。
- 本项目不在仓库或安装包中分发 Core。首次使用 Live2D 时，程序从 Live2D 官方 CDN 下载，并缓存到当前用户的应用数据目录；之后从本机缓存加载。删除缓存后需要再次联网获取。
- 官方文件地址：<https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js>
- Cubism Core 是 Cubism SDK 中负责模型顶点等计算的原生运行库，官方说明见 [Cubism Core](https://docs.live2d.com/cubism-sdk-manual/cubism-core/)。其使用受 [Live2D Proprietary Software 使用授权协议](https://www.live2d.com/zh-CHS/sdk/download/web/)约束。
- 使用 Cubism SDK 制作并正式发布内容时，应先查看 [SDK 发行许可（出版许可协议）](https://www.live2d.com/zh-CHS/sdk/license/)；官方说明开发测试阶段无需该许可，个人和小规模事业者在特定条件下可免除许可及费用，但扩展性应用等情形可能不适用。本项目不替用户判断具体发行行为是否符合豁免条件。

### 内置 Live2D 模型

- 来源：雪熊企划，见[哔哩哔哩主页](https://space.bilibili.com/3546783265327964)。
- 文件位置：`app/assets/live2d/models/yachiyo/`。该模型随项目提供，与项目自有代码分开授权；根目录的 MIT License 不会把模型文件变成 MIT 许可。

## 随项目提供的前端组件与字体

| 组件 | 版本 | 许可 | 用途与许可文件 |
| --- | --- | --- | --- |
| [Electron](https://www.electronjs.org/) | 44.4.5（锁定于 `desktop/package-lock.json`） | MIT；Electron 同时包含 Chromium、Node.js 等组件 | 桌面窗口与运行环境。完整第三方声明见 [Electron Legal Notices](https://www.electronjs.org/docs/latest/legal-notices)。 |
| [PixiJS](https://pixijs.com/) | 8.13.1 | MIT | Live2D 页面渲染；许可全文位于 `app/assets/live2d/vendor/LICENSE-pixi.txt`。 |
| [untitled-pixi-live2d-engine](https://www.npmjs.com/package/untitled-pixi-live2d-engine) | 1.4.0 | MIT | 加载 Cubism 3/4/5 模型；许可全文位于 `app/assets/live2d/vendor/LICENSE-untitled-pixi-live2d-engine.txt`。 |
| [Inter](https://github.com/rsms/inter) | Latin 与 Latin-ext 字体子集 | SIL Open Font License 1.1 | 界面拉丁字形；许可全文位于 `desktop/renderer/fonts/OFL.txt`，版权声明为 Copyright 2020 The Inter Project Authors。 |

PixiJS 与 Live2D 渲染引擎的来源、文件校验值和版本记录见 `app/assets/live2d/vendor/vendor.json`。

## Python 运行依赖

| 组件 | requirements.txt 中的版本范围 | 许可 | 用途 |
| --- | --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi) | `>=0.115` | MIT | 本机 HTTP 与 WebSocket 接口。 |
| [Uvicorn](https://github.com/encode/uvicorn) | `>=0.30` | BSD-3-Clause | 运行本机 ASGI 服务。 |
| [LiteLLM](https://github.com/BerriAI/litellm) | `>=1.100` | MIT | 调用用户配置的模型服务。 |
| [Pydantic](https://github.com/pydantic/pydantic) | `>=2.10,<3` | MIT | 配置数据校验与迁移。 |
| [Pillow](https://github.com/python-pillow/Pillow) | `>=10` | MIT-CMU | 处理 Live2D 贴图和截图。 |
| [ddgs](https://github.com/deedy5/ddgs) | `>=9.0` | MIT | 提供免密钥网页搜索。 |
| [markdownify](https://github.com/matthewwithanm/python-markdownify) | `>=1.0` | MIT | 将网页 HTML 正文整理为 Markdown。 |
| [HTTPX](https://github.com/encode/httpx) | `>=0.27` | BSD-3-Clause | 获取网页及 HTTP 请求。 |
| [Pyperclip](https://github.com/asweigart/pyperclip) | `>=1.9` | BSD-3-Clause | 读取和写入剪贴板文字。 |

这些运行库还会带入传递依赖，例如 Starlette（BSD-3-Clause）、AnyIO（MIT）、websockets（BSD-3-Clause）、Pydantic Core（MIT）、Click（BSD-3-Clause）、Beautiful Soup（MIT）、certifi（MPL-2.0）和 tiktoken（MIT）。LiteLLM 也会按其依赖配置使用模型服务 SDK 与 HTTP 客户端。具体安装集合随依赖解析结果变化；重新分发安装程序时，应一并保留解析到的组件各自要求的许可声明。

## 打包与开发工具

| 组件 | 版本 | 许可 | 说明 |
| --- | --- | --- | --- |
| [electron-builder](https://github.com/electron-userland/electron-builder) | 26.15.3（锁定于 `desktop/package-lock.json`） | MIT | 生成 Windows 桌面安装程序；仅用于构建。 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 由构建环境提供 | GPL-2.0-or-later，含 PyInstaller 特别例外 | 将 Python 后端打包为可执行文件；不把 PyInstaller 本身作为应用运行依赖分发。该例外允许按其条款分发打包产物，详情见项目的 [COPYING.txt](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt)。 |
| [pytest](https://github.com/pytest-dev/pytest) | `>=8` | MIT | 测试依赖，不包含在应用安装包中。 |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | `>=0.24` | Apache-2.0 | 测试依赖，不包含在应用安装包中。 |

## 保留与再分发

- 上表标注的 SPDX 许可标识用于快速查阅；第三方组件的许可全文以其上游发行包为准。再分发时，请保留随组件提供的版权声明和许可文本。
- Electron 对 Chromium、Node.js 等包含组件的声明请一并查看 Electron 的 Legal Notices。
- 模型文件、Cubism Core 与其他角色素材不因代码采用 MIT License 而取得相同授权。
