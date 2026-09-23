# models/ —— Live2D 模型放这里

放一个目录进去，里面要有 `*.model3.json`（Cubism 3/4/5）或 `*.model.json`（Cubism 2）。
程序启动时按目录名排序取第一个能用的。

```
models/
└── 你的模型名/
    ├── 你的模型名.model3.json
    ├── 你的模型名.moc3
    └── 贴图.png ...
```

## 为什么这里默认是空的

模型是**美术作品，有自己的授权**，跟代码不是一回事：

- 你自己做的模型 / 你有权分发的模型 → 可以打进安装包：把整个模型目录复制到
  `desktop/package.json` 的 `extraResources` 里（照着已有的 `backend` 那条写，
  `"from": "../models", "to": "models"`）。不配这条的话，安装包里**不含模型**，
  用户得自己放（见下面）。
- 网上找的、别人给的模型 → 你只有本地使用的份，**不要随公开版分发**。
  放进 `models/` 自己用没问题：`models/` 下的内容不进 git（见 `.gitignore`）。

想放在别处也行：设环境变量 `YACHIYO_MODEL_DIR=<目录>`，或者把模型放到
数据目录下的 `models/`（开发期是 `.devdata/models`，打包后是
`%APPDATA%\月见八千代\data\models`）—— 后者的优先级比这里高。
