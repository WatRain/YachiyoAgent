"""Live2D 角色的资源服务与贴图处理。

分两块，各自只管一件事：

  server.py    本地只读 HTTP 服务（页面 / 内置依赖 / 模型 三个来源映射成 URL）
  textures.py  超大贴图先压成能跑的大小（8192×8192 直接喂给渲染器会卡爆）

渲染本身不在这里：角色由 **Electron 渲染进程里的 pet.html** 直接画
（`app/assets/live2d/pet.html`，引擎和 pixi 都在 vendor/ 里），后端只负责
把页面和模型资源用同一个源提供出去（见 backend/live2d.py）。

历史：早期版本用"无头 Edge + CDP 逐帧取图"把画面搬进 Flet 窗口
（Flet 1.0 没有 WebView 控件，本机也没有 Flutter/Dart 工具链）。换到 Electron
之后这条路整个删掉了 —— 直绘实测能到 55~60fps，而抓帧那条路只有 18~20fps。
"""
