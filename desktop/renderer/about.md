关于本项目

这是一位住在桌面上的 AI 伙伴。

月见八千代 Agent 将大语言模型对话与 Live2D 角色相结合，让交流不只停留在文字里。连接你选择的模型服务后，就可以在应用中与她聊天；回复到来时，文字会逐步呈现，她也能根据对话回应表情与动作。

你可以在主窗口中聊天，也可以将角色放到桌面浮窗里，并选择在主窗口或角色旁的小窗中输入。深浅色外观、角色摆放方式等设置，都可以按自己的习惯调整，让她融入日常使用，而不是只在打开应用时出现。

当然，你也可以自行拉取源码，添加你自己想要的功能。~~实现之后如果可以提PR就再好不过了。~~

我们不会，也**永远不会**内置模型服务。你可以自行选择并配置服务与 API 密钥，密钥保存在本机；与模型交谈时，对话内容会发送至你所配置的服务进行处理。

## 技术栈与开源项目

### 技术栈

- **桌面界面：** Electron 44、HTML、CSS 与原生 JavaScript。
- **后端服务：** Python、FastAPI 与 Uvicorn，运行在本机。
- **模型接入：** LiteLLM，连接你自行配置的模型服务。
- **Live2D 渲染：** PixiJS 与 `untitled-pixi-live2d-engine`。
- **本地数据：** JSON 文件保存配置、记忆与对话记录；API 密钥保存在 Windows 凭据管理器。
- **应用打包：** electron-builder 与 PyInstaller。

### 主要开源项目

- [Electron](https://www.electronjs.org/)（MIT）：桌面应用外壳。
- [FastAPI](https://fastapi.tiangolo.com/)（MIT）与 [Uvicorn](https://www.uvicorn.org/)（BSD-3-Clause）：本机后端服务。
- [LiteLLM](https://github.com/BerriAI/litellm)（MIT）：接入不同的模型服务。
- [PixiJS](https://pixijs.com/) 8.13.1 与 [untitled-pixi-live2d-engine](https://www.npmjs.com/package/untitled-pixi-live2d-engine) 1.4.0（MIT）：绘制画面并加载 Live2D 模型。
- [Pydantic](https://docs.pydantic.dev/)（MIT）、[HTTPX](https://www.python-httpx.org/)（BSD-3-Clause）与 [Pillow](https://python-pillow.org/)（MIT-CMU）：配置校验、网络请求与贴图处理。
- [ddgs](https://github.com/deedy5/ddgs)（MIT）、[markdownify](https://github.com/matthewwithanm/python-markdownify)（MIT）、[pyperclip](https://github.com/asweigart/pyperclip)（BSD-3-Clause）与 [tiktoken](https://github.com/openai/tiktoken)（MIT）：联网搜索、网页内容整理、剪贴板与 Token 计数。
- [Inter](https://github.com/rsms/inter)（SIL OFL 1.1）：界面拉丁字形。
- [electron-builder](https://www.electron.build/)（MIT）与 [PyInstaller](https://pyinstaller.org/)（GPLv2-or-later，含分发例外）：制作 Windows 安装包。

Live2D Cubism Core 是 Live2D 的专有运行库，不属于开源项目。它不随项目或安装包分发，首次使用时从官方 CDN 获取。

完整的第三方依赖与许可信息见 [仓库里的清单](https://github.com/WatRain/YachiyoAgent/blob/main/THIRD_PARTY_NOTICES.md)。

作者的碎碎念：

非常感谢你的使用，这算是我真正意义上的第一款应用。众所周知，今年年初有一颗"核弹"——OpenClaw。我至今仍记得，当初配置完 OpenClaw 后，向她发出第一条消息并收到回应时，内心那份难以抑制的激动。当然，那时还有一部伟大的百合电影上映——《超时空辉夜姬》，在~~看了五集~~之后，我便萌生了制作一个"八千代"智能体的想法~~（就像彩叶那样）~~。然而那时的我正身处高三，鲜有机会使用电脑，于是这一拖，便拖到了现在。

这个项目是我在系统了解了 AI Agent 各方面知识后才正式着手的。后端由 Python 实现，前端界面与部分后端在前期由 DeepSeek V4.1 Flash 编写，中后期则交由 GPT6 完成。后端中的 Agent Loop 以及 Chat 部分由我手动编写完成后喂给大肥鱼与 GPT6 完成后续开发~~（不过在后续的 vibe coding 过程中，已经被大肥鱼和 GPT 大幅重构了）~~。

鄙人目前是大一新生，本项目中有大量 AI 生成的代码，我仍在努力学习 coding 中，恳请各位老资历多多包涵~~（不要压力我口牙）~~。当然，如果遇到了 Bug，或者你有任何好的想法与建议，欢迎提交 Issue 😇。

本项目未来大概或许可能会进行重构，前端会逐步脱离 Electron 框架。当然，还会有更多功能陆续加入。

感谢你耐心阅读到这里。如果你对 AI 感兴趣，或者想参与构建本项目，欢迎通过我的邮箱联系我：*waterrainbow@foxmail.com*

另：

我认为，人往往是需要陪伴的，尤其是在如今生活节奏普遍加快的情况下。

不论是从早期的 "AI" —— [ELIZA](https://zh.wikipedia.org/wiki/ELIZA)，抑或是如今的各种聊天机器人，都可以证实——人的确是会对 "机器" 产生感情的。

所以我制作了这个 Agent ，她不仅可以陪伴你，也可以在~~一定程度~~上帮助你干活。当然，我也希望她可以给你带来欢乐。

而在较为深入的理解了当前 AI 的原理之后，说实话我有些许失望——它和我想象中的 AI 并不完全一样。但我始终相信，未来终有一天，我们会拥有属于自己的"八千代"。

在文末，引用一句我 [Hibays](https://github.com/hibays) 师兄对我说过的话：

***"Only I can tell you that AGI is something.***

***Something we are all searching for."***

我们仍在前进，未来由你我共同构建。

AGI 时代，终会到来。
