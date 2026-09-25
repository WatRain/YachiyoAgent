# 第三方组件与许可

月见八千代（YachiyoAgent）本体代码由作者所有。它使用了下面这些第三方组件 ——
**每一个都保留其原有许可**，本文件按各自要求列出署名与许可标识。

分发（打包成安装包给别人的时候）请连同本文件一起带上。

---

## 一、Live2D（角色渲染）

**这部分有额外限制，请认真读。**

### Live2D Cubism Core

- 版权：© Live2D Inc.
- 许可：Live2D Proprietary Software License Agreement（专有许可，**不是**开源）
- 本项目不重新分发 Core 文件：页面在运行时从 Live2D 官方 CDN
  （`https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js`）加载，
  所以你拿到的是 Live2D 官方发布的那份。
- 如果你打算把 Core 打进安装包（离线可用），必须同时附上 Live2D 的许可条款原文
  —— 见 <https://www.live2d.com/en/sdk/license/> 与 SDK 包内的 `LICENSE.md`。
- 使用 Cubism SDK 还需遵守 SDK Release License：个人 / 小规模企业免费，
  但「以 Live2D 作为 AI 或聊天机器人界面的内容」需要事先申请。
  发布前请按官方页面上的流程图自查，或直接问 Live2D。

### 模型文件（`models/` 下的东西）

模型的授权**和代码完全无关**，取决于模型本身：

- **Live2D 官方提供的样例模型（Free Material License Agreement）**：
  以个人 / 小规模企业身份使用时，无论是否商用，都可以使用、修改并分发；
  分发时**必须**标注版权声明（见下面两条，按官方要求二选一）：
  - `This content uses sample data owned and copyrighted by Live2D Inc. The sample data are utilized in accordance with terms and conditions set by Live2D Inc. This content itself is created at the author's sole discretion.`
  - `This content uses sample data owned and copyrighted by Live2D Inc.`
  官方「协作角色」（Collaboration Character）不在此列：**既不能商用，也不能改动、不能分发**。
- **你自己做的模型**：你有权分发（除非用了别人的素材）。
- **网上找的 / 别人给的模型**：通常**没有**再分发权 —— 自己本地用可以，
  不要打进公开版。本项目默认把 `models/` 排除在 git 之外，就是为这个。

在应用里，**设置浮层的「角色」一行**显示当前加载的模型目录名 —— 官方要求
版权声明出现的地方就是这种用户能看到的位置（要更严谨就跟着这句一起写全）。

---

## 二、前端渲染（JavaScript）

| 组件 | 版本 | 许可 | 版权 |
| --- | --- | --- | --- |
| [pixi.js](https://pixijs.com/) | 8.13.1 | MIT | © 2013-2023 Mathew Groves, Chad Engler |
| [untitled-pixi-live2d-engine](https://www.npmjs.com/package/untitled-pixi-live2d-engine) | 1.4.0 | MIT | © 2026 GuangChen2333 |
| [Electron](https://www.electronjs.org/) | 44.x | MIT（内含 Chromium/Node，各自许可见 <https://www.electronjs.org/docs/latest/legal-notices>） | © Electron contributors / OpenJS Foundation |
| [Inter](https://github.com/rsms/inter) | 可变字体 | SIL OFL 1.1 | © 2020 The Inter Project Authors |

两份 MIT 许可原文随文件一起放在 `app/assets/live2d/vendor/`：

- `app/assets/live2d/vendor/LICENSE-pixi.txt`
- `app/assets/live2d/vendor/LICENSE-untitled-pixi-live2d-engine.txt`

### 字体

界面上标题 / 名字的拉丁字形（Inter，见 `desktop/renderer/style.css` 的 `--font-display`）
随包分发在 `desktop/renderer/fonts/`，只收了 latin 与 latin-ext 两个 woff2 子集：

- `desktop/renderer/fonts/inter-latin.woff2`
- `desktop/renderer/fonts/inter-latin-ext.woff2`
- `desktop/renderer/fonts/OFL.txt` —— SIL OFL 1.1 全文，版权行为
  `Copyright 2020 The Inter Project Authors`；该字体未声明 Reserved Font Name，
  许可允许随软件一起分发、嵌入与再分发（保留本声明与许可全文即可）。

中文标题不走这个文件，落到系统里的思源黑体 / Noto Sans SC / 微软雅黑那一串。

---

## 三、Python 后端

| 组件 | 许可 |
| --- | --- |
| [litellm](https://github.com/BerriAI/litellm) | MIT |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT |
| [uvicorn](https://www.uvicorn.org/) | BSD-3-Clause |
| [Starlette](https://www.starlette.io/) | BSD-3-Clause |
| [pydantic](https://docs.pydantic.dev/) | MIT |
| [anyio](https://anyio.readthedocs.io/) | MIT |
| [websockets](https://websockets.readthedocs.io/) | BSD-3-Clause |
| [httpx](https://www.python-httpx.org/) | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | MIT-CMU |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | MIT |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 |
| [tiktoken](https://github.com/openai/tiktoken) | MIT（打包时会一并带上它的 BPE 词表） |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause |
| [ddgs](https://github.com/deedy5/ddgs) | MIT（联网搜索，免密钥） |
| [markdownify](https://github.com/matthewwithanm/python-markdownify) | MIT（网页转 Markdown） |
| [pyperclip](https://github.com/asweigart/pyperclip) | BSD-3-Clause（剪贴板） |
| [lxml](https://lxml.de/) | BSD-3-Clause（markdownify 的解析后端） |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT（同上） |

litellm 在调用各家模型服务时会用到对应的 SDK / HTTP 客户端
（如 `openai`、`aiohttp`、`requests`、`tiktoken` 等），它们分别是
Apache-2.0 / MIT 等许可，随 `requirements.txt` 一起安装。

### 关于 PyInstaller

打包后端用的是 [PyInstaller](https://pyinstaller.org/) 6.x，
其许可为 `GPLv2-or-later with a special exception which allows to use PyInstaller
to build and distribute non-free programs` —— 该例外明确允许用打包产物闭源分发。
启动器（bootloader）的代码会嵌进打包出来的 exe，例外条款就是为这种情况写的。

---

最后更新：迁移到 Electron、并打通打包链路之后。Flet 已从依赖里移除。
