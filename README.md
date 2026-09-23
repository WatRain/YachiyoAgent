# 月见八千代智能体

*尚未完工*

一个 Windows 桌面端的陪伴型 AI 应用。**用户自带 provider 和 API Key，开发者不提供也不接触密钥。**

界面是 [Electron](https://www.electronjs.org/)（`desktop/`），后端是本机的一个 [FastAPI](https://fastapi.tiangolo.com/) 服务（`backend/`），
模型调用层用 [LiteLLM](https://docs.litellm.ai/)（支持任意 OpenAI 兼容端点）。
Live2D 角色直接画在 Electron 的渲染进程里（`app/assets/live2d/pet.html` + 打包好的 Cubism 引擎）。

## 快速开始

```powershell
# 1. 建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 装 Python 依赖
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

# 如果国内镜像缺包：
#   ... -m pip install -r requirements.txt --extra-index-url https://pypi.org/simple

# 3. 装前端依赖（第一次要下 Electron，约 100MB）
cd desktop
npm install
cd ..

# 4. 跑起来（会用 .venv 里的 python 起后端）
cd desktop
npm start
```

`npm start` 做三件事：起后端（`python -m backend`，端口随机）→ 读 stdout 里的
`YACHIYO_BACKEND_READY` 拿到端口和 token → 开窗口加载 `http://127.0.0.1:<port>/`。
界面、接口、Live2D 页面都由这一个源提供，所以没有跨源问题。

**首次使用**：程序自己弹出引导 → 选一个 provider（有内置预设）→ 填 Base URL、模型 ID、API Key → 点「测试连接」→ 点「保存」。
没配好不会放你进主界面。

常用 provider 填法：

| Provider | Base URL | 模型 ID 示例 | Key 在哪申请 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | platform.deepseek.com |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | platform.openai.com |
| OpenRouter | `https://openrouter.ai/api/v1` | 视模型而定 | openrouter.ai |
| 本地 Ollama | `http://localhost:11434/v1` | `llama3.1` | 不需要（随便填非空值） |

> 模型 ID 不用写 `openai/` 前缀，程序会自动补。

## 你的 API Key 存在哪

| 存哪 | 说明 |
|---|---|
| **Windows 凭据管理器** | 勾了"保存到系统凭据库"时（默认）。条目名是 `YachiyoAgent/apikey:<provider>`，绑定当前 Windows 用户，别人拷走程序目录也拿不到 |
| **仅内存** | 不勾选时。退出程序即消失 |

> 实现方式：`core/secrets.py` 用 `ctypes` 直调 `advapi32` 的 `CredReadW/CredWriteW/CredDeleteW`。
> 不依赖任何第三方库，也不经过任何插件 IPC —— 调用发生在后端进程内，实测写 8ms / 读 1ms。
> 界面进程拿不到已保存的密钥：读回来的明文只在后端内存里用。

**密钥会发往哪**：只发往你在设置页填的那个地址，**本机直连，不经过任何第三方服务器**。

**日志**：只记录"调用失败""配置损坏"这类元信息，**绝不记录对话内容和密钥**（有专门的脱敏过滤器，见 `core/logging_setup.py`）。

### 早期版本留下的东西

0.1 之前用的是 `flet-secure-storage`。它并没有存进凭据管理器，而是写了一个 DPAPI 加密文件：

```
%APPDATA%\Appveyor Systems Inc\Flet\flutter_secure_storage.dat
```

现在的版本不再读它了。换了新 key 之后，这个文件可以直接删掉（里面是旧 key 的密文）。

## 数据放在哪

打包后 `%APPDATA%\月见八千代\data\`；开发期在项目下的 `.devdata/`（也可以用 `YACHIYO_DATA_DIR` 指定）。

| 文件 | 内容 |
|---|---|
| `config.json` | provider 配置（**不含密钥**） |
| `memories.json` | 长期记忆（结构化，你可以直接打开看和改） |
| `conversation.json` | 上次的对话记录 |
| `logs\app.log` | 运行日志 |

想彻底清空：删掉上面这个目录，再去「控制面板 → 凭据管理器 → Windows 凭据」删掉 `YachiyoAgent` 开头的条目。

## 项目结构

```
├─ prompt.md              八千代的人格设定（静态角色卡）
├─ backend/               ★ 本机 HTTP/WebSocket 后端（界面与接口都从它出去）
│  ├─ app.py              FastAPI 应用：配置、provider、密钥、会话、记忆、/ws/chat
│  ├─ live2d.py           把渲染页与模型资源挂到同一个源上
│  └─ legacy.py           老版本装在别处的数据，启动时搬过来（只补缺、不覆盖）
├─ desktop/               ★ Electron 前端
│  ├─ main.js             主进程：开窗口、起后端、推鼠标位置（视线要）
│  ├─ backend.js          起后端的进程管理（读 READY 行、探活）
│  ├─ preload.js          渲染进程唯一的桥（拿 token、最小化/关闭、记日志）
│  └─ renderer/           界面本身：index.html / style.css / app.js
├─ app/                   只留 Live2D 部分
│  ├─ assets/live2d/      pet.html（渲染页）+ 打包好的引擎
│  └─ live2d/             模型查找、贴图压缩、静态文件的防穿越
├─ core/                  核心逻辑（纯 Python，可单独测试）
│  ├─ chat.py             ★ agent loop：messages 管理、流式、工具调用接口
│  ├─ memory.py           长期记忆：抽取、解析、注入
│  ├─ store.py            本地存储（JSON）
│  ├─ secrets.py          ★ API Key 层（唯一接触明文密钥的模块）
│  ├─ providers.py        ★ provider 预设、地址校验、LiteLLM 参数翻译
│  ├─ config.py           用户配置（原子写、损坏兜底、版本迁移）
│  ├─ llm.py              测试连接、错误人话化
│  ├─ logging_setup.py    日志 + 密钥脱敏
│  └─ paths.py            数据目录（打包兼容）
├─ packaging/             PyInstaller 规格（把后端打成 exe）
└─ tests/                 200+ 个测试，不联网、不需要 API Key
```

**三条设计规矩**：

1. **只有 `core/secrets.py` 能碰明文 key**，其他模块拿不到；界面进程只有"写进去"和"问有没有"两条路
2. **只有 `core/providers.py` 能把用户配置翻译成 SDK 参数**，界面层不出现 `api_base`
3. **界面不含业务逻辑**：它只画画和转发，聊天、记忆、密钥都在后端

## 跑测试

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -v
```

测试全部**不联网、不需要 API Key** —— 模型调用被替换成按剧本演出的假函数。

## 已知限制

- **只做了对话 + 记忆**。工具调用（搜索、提醒）的框架在 `core/chat.py` 里留好了接口（`add_tools()`），`core/tools.py` 里有骨架，但还没接线
- **没有对话列表 / 多会话**：目前只有一个持续对话，退出时自动保存、下次启动恢复
- **记忆抽取每轮多花一次 API 调用**（用便宜模型/低 max_tokens 更省）
- 开发时 `npm start`（见上）；打成安装包：

  ```powershell
  cd desktop
  npm run pack   # 只出解包目录 release\win-unpacked，方便先试
  npm run dist   # 出安装包 release\YachiyoAgent-Setup-<版本>.exe
  ```

  两条都会先把后端用 PyInstaller 打成 `packaging/dist/yachiyo-backend/`（见 `packaging/backend.spec`），
  再让 electron-builder 把它塞进 `resources/backend/`。
