# 月见八千代 Agent

这是一位住在桌面上的 AI 伙伴。

月见八千代 Agent 把大语言模型对话和 Live2D 角色放在同一个 Windows 桌面应用里。她可以流式回复，也能根据对话回应表情与动作。你可以在主窗口聊天，也可以把角色放到桌面浮窗里，再选择继续使用主窗口，或在她身旁的小窗中聊天。

模型服务和 API Key 由你自己选择并配置。应用不内置模型，也不提供模型服务。

## 她能做什么

- **和你聊天：** 连接 DeepSeek、OpenAI、Anthropic、Gemini、OpenRouter、本地 Ollama，或填写兼容的自定义端点。回复以流式方式逐步呈现。
- **陪在桌面上：** 在主窗口中显示 Live2D 角色，也可以把角色脱离为桌面浮窗。浮窗模式下，聊天可以留在主窗口，也可以放到角色旁的小窗。
- **按习惯调整：** 外观可以跟随系统，也可以固定为浅色或深色；角色窗口、聊天位置和工具档位都可以在设置里调整。
- **回应对话：** 模型可以按语气触发表情和少量预设动作。角色模型随项目提供，来自[雪熊企划](https://space.bilibili.com/3546783265327964)。
- **使用工具：** 默认可以搜索网页、读取本地资料、查看时间和剪贴板、截图、管理长期记忆，以及控制角色表情动作。写文件、运行命令等操作需要切换到「完全」档位，并逐次确认。
- **记住重要信息：** 长期记忆以可读的 JSON 文件保存在本机。八千代可以通过记忆工具查询和修改，你也可以直接打开文件查看。

目前聊天记录是一个持续会话；应用重新打开后会接着显示这段记录。项目还没有多会话列表，也不会自动把旧聊天压缩成摘要。

## 技术栈与开源项目

| 部分 | 使用的技术 | 用途 |
| --- | --- | --- |
| 桌面应用 | [Electron](https://www.electronjs.org/) 44.4.5、[Node.js](https://nodejs.org/)、HTML、CSS、原生 JavaScript | Electron 主进程基于 Node.js 管理窗口、子进程和系统接口；渲染进程负责界面。 |
| 本机后端 | [Python](https://www.python.org/)、[FastAPI](https://fastapi.tiangolo.com/)、[Uvicorn](https://www.uvicorn.org/) | 提供本地 HTTP 与 WebSocket 接口，处理配置、聊天、记忆和工具调用。 |
| 模型接入 | [LiteLLM](https://github.com/BerriAI/litellm) | 把不同服务商的模型请求交给统一的调用层处理。 |
| 配置、联网与桌面工具 | [Pydantic](https://docs.pydantic.dev/)、[HTTPX](https://www.python-httpx.org/)、[DDGS](https://github.com/deedy5/ddgs)、[markdownify](https://github.com/matthewwithanm/python-markdownify)、[Pillow](https://python-pillow.org/)、[Pyperclip](https://github.com/asweigart/pyperclip)、[tiktoken](https://github.com/openai/tiktoken) | 分别用于配置校验、网页请求与整理、联网搜索、图像和剪贴板处理，以及 LiteLLM 使用的 Tokenizer 数据。 |
| 对话循环 | 项目自己的 Python Agent Loop | 流式接收模型回复；模型提出工具调用时执行工具、把结果交回模型，再继续回复。 |
| Live2D | [PixiJS](https://pixijs.com/) 8.13.1、[untitled-pixi-live2d-engine](https://www.npmjs.com/package/untitled-pixi-live2d-engine) 1.4.0 | 在 Electron 页面中绘制角色并加载模型。 |
| 本机数据 | JSON、Windows 凭据管理器 | 配置、会话与长期记忆保存在本机；API Key 可交由 Windows 凭据管理器保存。 |
| Windows 打包 | [PyInstaller](https://pyinstaller.org/)、[electron-builder](https://www.electron.build/) | 分别打包 Python 后端和 Electron 安装程序。 |

Electron 主进程启动一个只监听本机回环地址的 FastAPI 服务，并通过 preload 暴露受限的窗口控制接口。后端启动时选择可用端口，并生成本次运行使用的访问令牌；界面通过本地 HTTP 和 WebSocket 与它通信。模型调用从后端发往你配置的服务端点，渲染进程不直接持有 API Key。

## 模型工具

在设置的「对话与工具」中，可以选择三个档位：

- **关闭：** 不向模型提供工具，只进行普通对话。
- **安全（默认）：** 提供以下工具，不包含文件写入、打开路径或执行命令。
- **完全：** 在安全档位的基础上，增加写文件、改文件、打开路径和执行命令。

安全档位中的工具：

| 工具 | 能做什么 |
| --- | --- |
| `get_time` | 查看本机时间，也可以指定时区。 |
| `web_search`、`web_fetch` | 搜索网页并读取公开网页正文。搜索使用免密钥的 DDGS；读取网页只接受 HTTP/HTTPS，并会拦截解析到本机或内网的地址。 |
| `read_file`、`list_dir` | 读取 UTF-8 文本文件、列出目录。读取文件单次最多 200 KB，目录最多显示 200 项。 |
| `memory_search`、`memory_add`、`memory_forget` | 查询、添加或删除长期记忆。 |
| `screenshot` | 截取整个屏幕并保存到本机。 |
| `clipboard_read`、`clipboard_write` | 读取或写入剪贴板文字。 |
| `control_live2d` | 选择预设表情或动作，让角色配合回复。 |

「完全」档位额外提供：

| 工具 | 能做什么 |
| --- | --- |
| `write_file` | 新建文件或覆盖文件。 |
| `edit_file` | 将文件中唯一匹配的一段文字替换为新内容。 |
| `open_path` | 用系统默认程序打开文件或文件夹。 |
| `run_command` | 在本机执行命令；Windows 下使用 PowerShell，默认最多运行 60 秒，最高 300 秒。 |

这些操作会在执行前显示确认弹窗，只有你同意后才会继续。当前实现采用“无法确认就拒绝”的策略：确认界面不可用，或关闭「动手前先问我」时，高权限工具不会执行，也不会跳过确认直接动手。文件工具可以访问当前 Windows 用户有权限访问的路径，不局限于项目目录；命令工具会在本机运行。模型是否调用某个工具仍由当前模型根据工具说明和对话内容决定；工具可用不代表每轮都会调用。

读取文件、网页和工具返回内容都有大小上限，例如单次文件读取最多 200 KB、网页正文默认最多 8,000 字，工具返回给模型的内容最多 20,000 字符。工具执行结果会作为对话上下文交回模型服务，以便它继续组织回复。联网搜索会发送搜索词，网页读取会请求对应的网站；请根据你使用的服务和工具内容判断适合发送哪些信息。

## 对话上下文与长期记忆

- 每次请求都会加载项目中的 [prompt.md](prompt.md) 作为角色设定，并把长期记忆附加到系统消息中。
- 会话记录完整保存在本机的 `conversation.json`，其中包括用户消息、角色回复和工具调用结果。应用重启后会恢复这份记录。
- 发给模型的会话历史默认只保留最近 **12 个用户回合**，并从用户消息边界开始裁剪，避免把工具调用和工具结果从中间拆开。更早的记录仍留在本机文件里，但不会继续随每次请求发给模型。当前实际按回合数裁剪，没有按 Token 数裁剪。
- 每次用户发言最多进行 4 轮模型请求；最后一轮不再附带工具列表，让模型收束为最终回复。
- 长期记忆保存在 `memories.json`。八千代可以通过 `memory_search`、`memory_add` 和 `memory_forget` 管理记忆；主界面会显示记忆条数，但没有单独的记忆编辑页，需要查看或手动修改时可以直接打开该 JSON 文件。当前不会在每轮对话后自动运行记忆抽取或生成旧对话摘要。

## 数据与隐私

打包版数据默认放在：

```text
%APPDATA%\月见八千代\data\
```

开发版默认放在项目根目录的 `.devdata/`。可以通过环境变量 `YACHIYO_DATA_DIR` 指定其他数据目录。

| 文件或位置 | 内容 |
| --- | --- |
| `config.json` | Provider、外观、工具等配置，不保存 API Key。 |
| `conversation.json` | 持续会话，包括工具调用与工具结果。 |
| `memories.json` | 可读的长期记忆。 |
| `reminders.json` | 本地提醒记录；当前没有后台定时通知，也没有提供给模型使用的提醒工具。 |
| `logs/app.log` | 后端日志，位于数据目录的 `logs/` 子目录；日志会对密钥类内容做脱敏处理。Electron 启动日志和后端进程输出另存在开发版的 `desktop/.logs/` 或安装版的应用数据目录 `logs/` 中。 |
| Windows 凭据管理器 | 选择保存时，API Key 以 `YachiyoAgent/apikey:<provider>` 为名称保存；否则只在本次运行期间留在内存中。 |

会话、记忆和配置是普通本地 JSON 文件，不会加密。与模型对话时，消息和工具结果会发送给你配置的模型服务，由该服务按它自己的隐私规则处理。应用不经过自有的中转服务器。Live2D 的 PixiJS 和渲染引擎已随项目提供；**Live2D Cubism Core** 是独立的专有运行库，首次使用时从 Live2D 官方 CDN 下载并缓存在用户数据目录，后续启动会使用缓存。删除这份缓存后，需要再次联网下载。

## 在 Windows 上运行开发版

需要安装 Python、Node.js 和 npm。PowerShell 中执行：

```powershell
# 在项目根目录
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 安装 Electron 依赖并启动应用
cd desktop
npm install
npm start
```

`npm start` 会启动 Electron；开发版后端优先使用项目根目录的 `.venv\Scripts\python.exe`，找不到时再使用系统的 `python`。首次打开应用时，按引导选择模型服务，填写模型 ID 和 API Key，并测试连接。

## 打包

打包需要额外安装 PyInstaller，并且构建 Live2D 与 Tokenizer 缓存时需要联网：

```powershell
# 在项目根目录安装打包工具
.\.venv\Scripts\python.exe -m pip install pyinstaller

# 在 desktop 目录运行
cd desktop
npm run pack   # 生成 release\win-unpacked，适合本机试运行
npm run dist   # 生成 Windows NSIS 安装程序
```

构建脚本会先把 Python 后端打包，再由 electron-builder 生成桌面程序。输出位于 `desktop/release/`。

## 项目结构

```text
├─ prompt.md                  角色设定与工具使用说明
├─ backend/                   FastAPI 服务、聊天 WebSocket、Live2D 资源服务
├─ core/                      对话循环、工具、Provider、记忆、配置与凭据
├─ desktop/                   Electron 主进程、preload 与界面
│  └─ renderer/               主界面、角色旁聊天窗、样式与关于页
├─ app/assets/live2d/         内置 Live2D 模型和渲染依赖
├─ packaging/                 PyInstaller 后端打包配置与构建脚本
├─ requirements.txt           Python 依赖范围
└─ THIRD_PARTY_NOTICES.md     第三方组件、模型与许可证说明
```

应用代码采用 MIT 许可，详见 [LICENSE](LICENSE)。模型与第三方组件有各自的来源和许可，请同时阅读 [第三方组件与许可说明](THIRD_PARTY_NOTICES.md)。
