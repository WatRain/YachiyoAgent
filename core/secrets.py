"""密钥层：**全项目唯一允许接触明文 API Key 的模块。**

设计要点：
  - 落盘交给操作系统：Windows 用凭据管理器，凭据绑定当前系统用户 ——
    别人拷走整个程序目录也拿不到 key。
  - API Key 由用户自己提供，本应用不内置任何 key。
  - 任何日志、异常信息里都不允许出现明文 key（只允许出现前 4 位）。
  - persist=False 走"仅本次运行有效"，退出即消失 —— 这是产品该有的礼貌。

后端只有两个，取舍写在下面这段，因为它是一个**踩过坑之后**的决定：

  以前这里用的是 flet-secure-storage（一个 Flutter 插件，通过 Flet 的
  invoke-method IPC 调用）。真机日志留下的现场：

    · 它其实**没有**存进 Windows 凭据管理器，而是写了一个 DPAPI 加密文件
      `%APPDATA%\\Appveyor Systems Inc\\Flet\\flutter_secure_storage.dat`。
        创建时间 21:47:03（唯一一次写成功，和日志里的"已保存密钥"对上）
        修改时间 21:47:03（之后再也没被写过）
    · 但 23:00/23:01/23:06 三次走引导，config.json 都写下去了（23:07:12），
      密钥那一步再没回来 —— 那个文件也再没被写过。
    · 日志里还有三处 `读取密钥失败 provider=deepseek（RuntimeError）`，
      也就是说连"读"都是时好时坏的。

  为什么会"挂着不动"：Flet 1.0 的 `Session.invoke_method()` 默认 `timeout=None`，
  即 `await asyncio.wait_for(evt.wait(), None)` —— 客户端不响应就**永远等下去**。
  flet-secure-storage 调它时没有传超时。用户看到的就是"点了没反应"。

  现在改成 ctypes 直调 advapi32 的 CredReadW / CredWriteW / CredDeleteW：
    · 不经过任何第三方库、不经过 IPC，调用发生在**当前进程内**
    · 实测一次写 8ms、一次读 1ms（tests/test_secrets.py 里有真机往返测试）
    · 失败就是明确的 Win32 错误码，不会再出现"挂着不动"
    · 也才真正兑现了界面上那句"存进 Windows 凭据管理器"

非 Windows 平台暂时降级为"仅本次运行有效"，而不是假装存上了。

用法：
    store = SecretStore()
    await store.save("deepseek", "sk-xxx")
    key = await store.load("deepseek")  # -> str | None
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

log = logging.getLogger(__name__)

SERVICE = "YachiyoAgent"

# ── Win32 常量（取自 wincred.h）──
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


def entry_key(provider_id: str) -> str:
    """系统凭据库里的条目名。provider.id 一改，这里的名字就跟着变 —— 所以 id 永不变。"""
    return f"apikey:{provider_id}"


def cred_target(provider_id: str) -> str:
    """凭据管理器里看到的完整条目名（会显示在"控制面板 → 凭据管理器 → Windows 凭据"里）。"""
    return f"{SERVICE}/{entry_key(provider_id)}"


# ─────────────────────────────────────────────
#  Windows 凭据管理器的 ctypes 封装
# ─────────────────────────────────────────────

class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_byte)),
    ]


class _CREDENTIAL(ctypes.Structure):
    """对应 wincred.h 里的 CREDENTIALW。

    字段顺序和名字必须和头文件**完全一致** —— ctypes 是按内存布局读的，
    少一个字段后面全部错位，表现是读到乱码或者直接崩。
    """

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialBackend:
    """Windows 凭据管理器。同步接口 —— 调用方负责丢进线程池，别堵事件循环。"""

    label = "Windows 凭据管理器"

    def __init__(self) -> None:
        # use_last_error=True 才会把错误码存进线程局部变量，供 get_last_error() 取
        self._api = ctypes.WinDLL("advapi32", use_last_error=True)
        self._declare()

    def _declare(self) -> None:
        """给每个函数声明参数类型。

        不声明也能跑，但 64 位下指针会被当成 32 位 int 截断 —— 那种崩溃很难查。
        """
        api = self._api
        api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL

        api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
        ]
        api.CredReadW.restype = wintypes.BOOL

        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL

        api.CredEnumerateW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))),
        ]
        api.CredEnumerateW.restype = wintypes.BOOL

        # CredFree 释放上面几个函数返回的内存；参数是裸指针
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None

    # ---------- 写 ----------

    def set(self, target: str, value: str) -> None:
        # 凭据的 blob 是任意字节。按惯例存 UTF-16-LE：
        # 这样在"凭据管理器"界面里点开也能看懂，不是乱码。
        blob = value.encode("utf-16-le")
        # create_string_buffer 负责分配内存；必须活到 CredWriteW 返回为止，
        # 所以用局部变量存住它（不要写成 ctypes.cast(value.encode(), ...)）。
        buffer = ctypes.create_string_buffer(blob, len(blob))

        cred = _CREDENTIAL()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "月见八千代保存的 API Key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = SERVICE

        if not self._api.CredWriteW(ctypes.byref(cred), 0):
            raise _win_error("CredWriteW")

    # ---------- 读 ----------

    def get(self, target: str) -> Optional[str]:
        cred = self._read(target)
        if cred is None:
            return None
        try:
            size = cred.contents.CredentialBlobSize
            raw = ctypes.string_at(cred.contents.CredentialBlob, size)
        finally:
            self._api.CredFree(cred)

        # 正常情况下是我们写的 UTF-16-LE。用户手动在凭据管理器里改过的话
        # 可能是普通 UTF-8，所以留一个退路，而不是直接抛异常。
        if size % 2 == 0:
            try:
                return raw.decode("utf-16-le")
            except UnicodeDecodeError:
                pass
        return raw.decode("utf-8", errors="replace")

    def exists(self, target: str) -> bool:
        """只判断在不在，不解码明文。"""
        cred = self._read(target)
        if cred is None:
            return False
        self._api.CredFree(cred)
        return True

    def _read(self, target: str):
        ptr = ctypes.POINTER(_CREDENTIAL)()
        if not self._api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:      # 不存在不算错误
                return None
            raise OSError(err, f"CredReadW 失败：{ctypes.FormatError(err)}")
        return ptr

    # ---------- 删 ----------

    def delete(self, target: str) -> None:
        if not self._api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:      # 本来就没有 = 已经达到目的
                return
            raise _win_error("CredDeleteW", err)

    def clear(self) -> None:
        """删掉本应用写进去的全部条目（前缀匹配，不会碰别的程序的凭据）。"""
        for target in self._targets():
            self.delete(target)

    def _targets(self) -> list[str]:
        """列出本应用写进去的全部条目名。

        ⚠️ CredEnumerateW 的 Filter **不是**通配符模式，而是"前缀 + *"：
           文档原文 "The filter specifies a name prefix followed by an asterisk"。
           所以 `f"{SERVICE}/*"` 是对的（匹配所有以 `YachiyoAgent/` 开头的条目），
           而 `"*Yachiyo*"` 会被当成字面前缀，一条都匹配不到 —— 这个坑我踩过，
           当时以为是"凭据没存进去"，差点把好好的实现改坏。
        """
        count = wintypes.DWORD(0)
        array = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
        ok = self._api.CredEnumerateW(
            f"{SERVICE}/*", 0, ctypes.byref(count), ctypes.byref(array)
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:      # 一条都没有
                return []
            raise _win_error("CredEnumerateW", err)
        try:
            return [array[i].contents.TargetName for i in range(count.value)]
        finally:
            self._api.CredFree(array)


def _win_error(call: str, err: int | None = None) -> OSError:
    if err is None:
        err = ctypes.get_last_error()
    return OSError(err, f"{call} 失败：{ctypes.FormatError(err)}")


def detect_backend():
    """挑一个能用的持久化后端。挑不到就返回 None（只有内存可用）。"""
    if sys.platform != "win32":
        log.info("非 Windows 平台，跳过系统凭据库")
        return None
    try:
        backend = WindowsCredentialBackend()
        # 探一下：能读到"不存在"就说明凭据管理器是通的
        backend.exists(f"{SERVICE}/__probe__")
        return backend
    except Exception as exc:
        log.warning("系统凭据库不可用（%s: %s）", type(exc).__name__, exc)
        return None


# ─────────────────────────────────────────────
#  对外接口
# ─────────────────────────────────────────────

class SecretStore:
    """API Key 存储。同一 provider 的 session 态优先于系统凭据库。"""

    def __init__(self, backend=None, *, detect: bool = True) -> None:
        """backend: 直接指定后端（测试用）。
        detect=False: 明确"不要系统凭据库"，只留内存（测试用）。
        """
        if backend is not None:
            self._backend = backend
        elif detect:
            self._backend = detect_backend()
        else:
            self._backend = None
        self._session: dict[str, str] = {}

    # ---------- 给界面看的状态 ----------

    @property
    def persistent(self) -> bool:
        """key 能不能活过这次运行。设置页和引导页用它决定要不要提醒用户。"""
        return self._backend is not None

    def backend_label(self) -> str:
        return self._backend.label if self._backend else "仅本次运行（内存）"

    # ---------- 读 ----------

    async def load(self, provider_id: str) -> str | None:
        if provider_id in self._session:
            return self._session[provider_id]
        if self._backend is None:
            return None
        try:
            return await asyncio.to_thread(self._backend.get, cred_target(provider_id))
        except Exception as exc:
            log.warning("读取密钥失败 provider=%s（%s: %s）",
                        provider_id, type(exc).__name__, exc)
            return None

    async def exists(self, provider_id: str) -> bool:
        """只判断有没有，不把明文取到调用方手里。"""
        if provider_id in self._session:
            return True
        if self._backend is None:
            return False
        try:
            return await asyncio.to_thread(self._backend.exists, cred_target(provider_id))
        except Exception:
            return False

    # ---------- 写 ----------

    async def save(self, provider_id: str, api_key: str, *, persist: bool = True) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("API Key 不能为空")

        if persist:
            if self._backend is None:
                raise RuntimeError("系统凭据库不可用，无法保存；可改用「仅本次运行有效」")
            # 丢到线程池：域账号 / 漫游配置下 CredWrite 可能慢到几百毫秒，
            # 直接在事件循环里调会把界面冻住。
            await asyncio.to_thread(self._backend.set, cred_target(provider_id), key)
            self._session.pop(provider_id, None)
        else:
            self._session[provider_id] = key

        # 只记录前 4 位，方便用户自己对上号
        log.info("已保存密钥 provider=%s persist=%s prefix=%s…", provider_id, persist, key[:4])

    async def delete(self, provider_id: str) -> None:
        self._session.pop(provider_id, None)
        if self._backend is None:
            return
        try:
            await asyncio.to_thread(self._backend.delete, cred_target(provider_id))
        except Exception as exc:
            log.warning("删除密钥失败 provider=%s（%s: %s）",
                        provider_id, type(exc).__name__, exc)

    async def purge_all(self) -> None:
        """清空本应用保存的全部密钥。给「清除全部数据」按钮用。"""
        self._session.clear()
        if self._backend is None:
            return
        try:
            await asyncio.to_thread(self._backend.clear)
        except Exception as exc:
            log.warning("清空密钥失败（%s: %s）", type(exc).__name__, exc)

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


def backend_label_of(store) -> str:
    """问一个 store-like 对象"密钥存在哪"，界面拿它来如实显示。

    故意写成宽容的：界面只需要一句说明，不该因为 store 是个测试替身就崩掉。
    问不出来时给个保守的说法，而不是编一个更好听的。
    """
    try:
        return store.backend_label() or "系统凭据库"
    except Exception:
        return "系统凭据库"
