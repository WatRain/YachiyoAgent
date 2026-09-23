"""本地 HTTP/WS 后端：给 Electron 前端用的一层薄封装。

它自己不实现任何业务逻辑 —— 配置、密钥、对话、记忆都还是 core/ 里那几份
代码说了算。这里只负责"把 core 的能力翻译成 HTTP/WebSocket"。
"""
