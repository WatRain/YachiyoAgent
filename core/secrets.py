"""密钥层：**全项目唯一允许接触明文 API Key 的模块。**

设计要点：
  - 落盘交给操作系统：Windows 用凭据管理器，macOS 用 Keychain，Linux 用 libsecret。
    凭据绑定当前系统用户 —— 别人拷走整个程序目录也拿不到 key。
  - API Key 由用户自己提供，本应用不内置任何 key。
  - 任何日志、异常信息里都不允许出现明文 key（只允许出现前 4 位）。
  - persist=False 走"仅本次运行有效"，退出即消失 —— 这是产品该有的礼貌。

用法：
    store = SecretStore()               # 需要在 Flet 运行时里创建
    await store.save("deepseek", "sk-xxx")
    key = await store.load("deepseek")  # -> str | None
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SERVICE = "YachiyoAgent"


def entry_key(provider_id: str) -> str:
    """系统凭据库里的条目名。provider.id 一改，这里的名字就跟着变 —— 所以 id 永不变。"""
    return f"apikey:{provider_id}"


class SecretStore:
    """API Key 存储。同一 provider 的 session 态优先于系统凭据库。"""

    def __init__(self) -> None:
        # 延迟创建：SecureStorage 是 Flet Service，需要 Flet 运行时。
        # 用 try 包住，这样纯 pytest 环境下只失去"系统凭据库"能力，不会直接崩。
        self._ss = None
        self._session: dict[str, str] = {}
        try:
            from flet_secure_storage import SecureStorage

            self._ss = SecureStorage()
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            log.warning("系统凭据库不可用，本次运行只能用临时密钥（%s）", type(exc).__name__)

    # ---------- 读 ----------

    async def load(self, provider_id: str) -> str | None:
        if provider_id in self._session:
            return self._session[provider_id]
        if self._ss is None:
            return None
        try:
            value = await self._ss.get(key=entry_key(provider_id))
            return value or None
        except Exception as exc:
            log.warning("读取密钥失败 provider=%s（%s）", provider_id, type(exc).__name__)
            return None

    async def exists(self, provider_id: str) -> bool:
        """只判断有没有，不把明文取到调用方手里。"""
        if provider_id in self._session:
            return True
        if self._ss is None:
            return False
        try:
            return bool(await self._ss.contains_key(key=entry_key(provider_id)))
        except Exception:
            return False

    # ---------- 写 ----------

    async def save(self, provider_id: str, api_key: str, *, persist: bool = True) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("API Key 不能为空")

        if persist:
            if self._ss is None:
                raise RuntimeError("系统凭据库不可用，无法保存；可改用「仅本次运行有效」")
            await self._ss.set(key=entry_key(provider_id), value=key)
            self._session.pop(provider_id, None)
        else:
            self._session[provider_id] = key

        # 只记录前 4 位，方便用户自己对上号
        log.info("已保存密钥 provider=%s persist=%s prefix=%s…", provider_id, persist, key[:4])

    async def delete(self, provider_id: str) -> None:
        self._session.pop(provider_id, None)
        if self._ss is None:
            return
        try:
            await self._ss.remove(key=entry_key(provider_id))
        except Exception as exc:
            log.warning("删除密钥失败 provider=%s（%s）", provider_id, type(exc).__name__)

    async def purge_all(self) -> None:
        """清空本应用保存的全部密钥。给「清除全部数据」按钮用。"""
        self._session.clear()
        if self._ss is None:
            return
        try:
            await self._ss.clear()
        except Exception as exc:
            log.warning("清空密钥失败（%s）", type(exc).__name__)

    # ---------- 给 UI 用 ----------

    @staticmethod
    def mask(api_key: str | None, *, known_saved: bool = True) -> str:
        """永远不返回完整 key。UI 上显示这个。"""
        if not api_key:
            return "（已保存到系统凭据库）" if known_saved else "（未设置）"
        if len(api_key) <= 8:
            return "••••••••"
        return f"{api_key[:4]}••••{api_key[-4:]}"

    def session_ids(self) -> list[str]:
        """当前只存在内存里的 provider id（退出即失效的那批）。"""
        return sorted(self._session)
