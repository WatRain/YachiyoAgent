# 月见八千代 · 陪伴型智能体

一个 Windows 桌面端的陪伴型 AI 应用。**用户自带 provider 和 API Key，开发者不提供也不接触密钥。**

前端用 [Flet](https://github.com/flet-dev/flet)，模型调用层用 [LiteLLM](https://docs.litellm.ai/)（支持任意 OpenAI 兼容端点）。

## 快速开始

```powershell
# 1. 建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 装依赖
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

# 如果国内镜像缺包：
#   ... -m pip install -r requirements.txt --extra-index-url https://pypi.org/simple

# 3. 跑起来
flet run app\main.py
```

**首次使用**：切到「设置」页 → 选一个 provider → 填 Base URL、模型 ID、API Key → 点「测试连接」→ 点「保存」。

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
| **Windows 凭据管理器** | 勾了"保存到系统凭据库"时（默认）。绑定当前 Windows 用户，别人拷走程序目录也拿不到 |
| **仅内存** | 不勾选时。退出程序即消失 |

**密钥会发往哪**：只发往你在设置页填的那个地址，**本机直连，不经过任何第三方服务器**。

**日志**：只记录"调用失败""配置损坏"这类元信息，**绝不记录对话内容和密钥**（有专门的脱敏过滤器，见 `core/logging_setup.py`）。

## 数据放在哪

打包后 `%APPDATA%\<company>\<product>\data\`；开发期（`flet run`）在 `.flet/storage/data/`。

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
├─ app/                   界面层（只知道怎么画，不懂对话逻辑）
│  ├─ main.py             入口：组装界面、管配置、保存对话
│  └─ views/
│     ├─ chat.py          聊天界面（流式、停止、节流）
│     └─ settings.py      设置页（provider、密钥、测试连接）
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
└─ tests/                 94 个测试，不联网、不需要 API Key
```

**三条设计规矩**：

1. **只有 `core/secrets.py` 能碰明文 key**，其他模块拿不到
2. **只有 `core/providers.py` 能把用户配置翻译成 SDK 参数**，界面层不出现 `api_base`
3. **界面层的耗时操作都是 `async`**（Flet 1.0 里同步处理器会冻住窗口）

## 跑测试

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -v
```

测试全部**不联网、不需要 API Key** —— 模型调用被替换成按剧本演出的假函数。

## 已知限制

- **只做了对话 + 记忆**。工具调用（搜索、提醒）的框架在 `core/chat.py` 里留好了接口（`add_tools()`），`core/tools.py` 里有骨架，但还没接线
- **没有对话列表 / 多会话**：目前只有一个持续对话，退出时自动保存、下次启动恢复
- **记忆抽取每轮多花一次 API 调用**（用便宜模型/低 max_tokens 更省）
- 开发时用 `flet run` 运行；打包成 exe 见 `docs/WIN_RELEASE.md`

## 文档

`docs/` 目录**不随仓库上传**（在 `.gitignore` 里），包含：

- `GUIDE.md` —— 架构、密钥管理规范、Provider 系统、async 原理
- `AGENT_LOOP.md` —— agent loop 的实现方法（含逐行注释的完整代码）
- `STEP_BY_STEP.md` —— 19 步动手路线
- `WIN_RELEASE.md` —— Windows 打包与发布清单
