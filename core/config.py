"""用户配置层：非机密数据。

**这里永远不放 API Key。** 机密只走 core/secrets.py。

存放位置：<数据目录>/config.json（见 core/paths.py 的 data_dir()）
写入方式：先写 .tmp 再原子替换，中途崩溃不会毁掉原配置。

两个名字一旦定下就永不更改，否则用户数据和系统凭据会失联：
  - ProviderConfig.id   （它是 API Key 的存储键）
  - 安装包的目录名（Electron 的 userData，见 desktop/main.js）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from core.paths import config_path

log = logging.getLogger(__name__)

# 改结构时 +1，并在 _migrate() 里写升级逻辑
SCHEMA_VERSION = 2

Protocol = Literal["openai", "anthropic", "gemini", "custom"]


class ProviderConfig(BaseModel):
    """一个模型服务端点。字段名保持直观，翻译成 SDK 参数是 providers.py 的事。"""

    id: str                      # 稳定标识，用作密钥存储键，只用 ascii
    display_name: str
    protocol: Protocol = "openai"
    base_url: str = ""           # 用户可改；空 = 用协议默认端点
    model_id: str = ""           # 用户可改
    supports_stream: bool = True
    is_builtin: bool = False     # 来自预设（仅用于 UI 上打标记，行为无差别）
    extra_headers: dict[str, str] = Field(default_factory=dict)


class ConsentRecord(BaseModel):
    """用户对隐私政策 / 许可条款的同意记录。

    为什么单独存一条，而不是「走完了引导就算同意」：这个程序是要发给别人用的，
    得能拿得出「他何时同意了哪一版」。条款有实质改动时把前端那个
    LEGAL_VERSION（desktop/renderer/app.js）和这里的 version 一起 +1，
    据此就能判断要不要重新征求同意。

    默认值 = 从没同意过 —— 老配置文件升上来就是这个状态。
    """

    version: int = 0
    at: str = ""        # 同意时间，ISO 8601（带时区）


class AppConfig(BaseModel):
    schema_version: int = SCHEMA_VERSION
    active_provider: str = ""
    providers: list[ProviderConfig] = Field(default_factory=list)

    # 给 agent loop 用的参数（你写 loop 时会读这几个）
    history_token_budget: int = 8000
    keep_recent_turns: int = 12
    max_tool_rounds: int = 4
    stream: bool = True
    temperature: float = 0.8

    # UI 偏好
    theme: str = "dark"
    # 角色物理（头发/衣摆的摆动）。实测很吃帧预算：开着约 36fps，关掉能到 60fps。
    # 但界面上已经不提供这个开关了 —— 关掉之后角色会僵在那儿，用户说太怪。
    # 字段留着是为了兼容老配置，主界面不再读它；调试要关物理走 pet.html 的 ?physics=0。
    live2d_physics: bool = True
    # 角色是不是脱离在桌面浮窗里（面板右侧那一格会收起）。
    # 只是"上次住在哪儿"的偏好：真正的窗口位置/大小存在 Electron 的 userData
    # （desktop/main.js 的 pet-window.json），那属于界面状态，不进这里。
    live2d_detached: bool = False

    # 隐私政策 / 许可条款的同意记录（见 ConsentRecord）。
    # 这是**证据**，不是开关：界面上该拦还是拦，不看这个字段。
    consent: ConsentRecord = Field(default_factory=ConsentRecord)


def _migrate(raw: dict) -> AppConfig:
    """把任意历史版本的配置升到当前版本。"""
    version = int(raw.get("schema_version", 0) or 0)

    if version < 1:
        # 0 -> 1：最初的版本，只保证字段存在
        raw.setdefault("providers", [])
        raw.setdefault("active_provider", "")

    if version < 2:
        # 1 -> 2：加了「同意记录」。字段自带默认值，老配置其实不转换也能读，
        # 这里显式补一条是想把升级路径留在纸面上（也是以后加字段的样板）。
        raw.setdefault("consent", {})

    # 以后加字段的写法（示例）：
    # if version < 3:
    #     raw["new_field"] = 默认值

    raw["schema_version"] = SCHEMA_VERSION
    return AppConfig.model_validate(raw)


def load_config() -> AppConfig:
    """读配置。文件不存在或损坏时返回默认配置，绝不抛异常。"""
    path = config_path()
    if not path.exists():
        return AppConfig()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("配置文件无法读取（%s），改用默认配置", type(exc).__name__)
        return AppConfig()

    if not isinstance(raw, dict):
        log.error("配置文件结构不对，改用默认配置")
        return AppConfig()

    try:
        return _migrate(raw)
    except ValidationError as exc:
        log.error("配置字段校验失败（%d 处），改用默认配置", exc.error_count())
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    """原子写入：先写临时文件再替换。"""
    path = config_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)          # 同目录内的 replace 是原子的


def get_provider(cfg: AppConfig, provider_id: str | None = None) -> ProviderConfig | None:
    """按 id 取 provider；不传 id 就取当前激活的那个。"""
    pid = provider_id or cfg.active_provider
    return next((p for p in cfg.providers if p.id == pid), None)


def upsert_provider(cfg: AppConfig, provider: ProviderConfig) -> AppConfig:
    """存在就替换，不存在就追加。返回同一个 cfg（方便链式调用）。"""
    for i, existing in enumerate(cfg.providers):
        if existing.id == provider.id:
            cfg.providers[i] = provider
            break
    else:
        cfg.providers.append(provider)
    return cfg


def remove_provider(cfg: AppConfig, provider_id: str) -> AppConfig:
    """只从配置里移除。**调用方还要负责删掉对应的密钥**（见 secrets.py）。"""
    cfg.providers = [p for p in cfg.providers if p.id != provider_id]
    if cfg.active_provider == provider_id:
        cfg.active_provider = cfg.providers[0].id if cfg.providers else ""
    return cfg
