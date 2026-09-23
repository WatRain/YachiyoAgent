"""Live2D 角色渲染。

分三块，各自只管一件事：

  server.py    本地只读 HTTP 服务（页面 / 内置依赖 / 模型 三个来源映射成 URL）
  renderer.py  用无头 Edge 渲染，并通过 CDP 逐帧取图 + 反过来驱动模型
  （界面接线在 app/views/ 里）

为什么是"无头浏览器 + 截图"这条路，而不是把网页嵌进 Flet 窗口：
  Flet 1.0 没有 WebView 控件，预编译的桌面客户端里也没有任何 webview 插件，
  而本机没有 Flutter/Dart 工具链（`flet build` 需要）—— 这条死路是在
  dsh 里实测过的，不是猜的。剩下的可行做法就是：让 Edge 把画面画出来，
  再把帧取回 Python，用 ft.Image 显示。
"""
