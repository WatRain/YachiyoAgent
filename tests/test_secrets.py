"""secrets.py 的测试。

分两层：
  1. 逻辑层 —— 用一个假的同步后端（FakeBackend）跑，不碰真实系统凭据库，
     验证"内存态 vs 持久态"的优先级、降级行为、错误传播。
  2. 真机层 —— 在 Windows 上真的往凭据管理器里写一条再读回来。
     这一条是**不能省**的：整个模块存在的意义就是"真的能存住"，
     只测假后端等于什么都没验证。
"""

import sys

import pytest

from core.secrets import (
    SecretStore,
    WindowsCredentialBackend,
    cred_target,
    detect_backend,
    entry_key,
)


class FakeBackend:
    """内存版凭据库，接口和 WindowsCredentialBackend 完全一致（同步）。"""

    label = "假后端"

    instances: list["FakeBackend"] = []

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.calls: list[tuple] = []
        FakeBackend.instances.append(self)

    def set(self, target: str, value: str) -> None:
        if value is None:
            raise ValueError("value can't be None")
        self.calls.append(("set", target))
        self.data[target] = value

    def get(self, target: str):
        self.calls.append(("get", target))
        return self.data.get(target)      # 不存在返回 None

    def exists(self, target: str) -> bool:
        self.calls.append(("exists", target))
        return target in self.data

    def delete(self, target: str) -> None:
        self.calls.append(("delete", target))
        self.data.pop(target, None)

    def clear(self) -> None:
        self.calls.append(("clear",))
        self.data.clear()


class ExplodingBackend(FakeBackend):
    """每次调用都炸 —— 模拟凭据库坏了。"""

    label = "坏后端"

    def set(self, target: str, value: str) -> None:
        raise OSError(1312, "CredWriteW 失败：没有可用的登录会话")

    def get(self, target: str):
        raise OSError(1312, "CredReadW 失败：没有可用的登录会话")

    def exists(self, target: str) -> bool:
        raise OSError(1312, "no logon session")


@pytest.fixture(autouse=True)
def _clear_instances():
    FakeBackend.instances.clear()
    yield
    FakeBackend.instances.clear()


# ------------------------------------------------------------ 纯函数

def test_entry_key_is_stable():
    assert entry_key("deepseek") == "apikey:deepseek"
    assert cred_target("deepseek") == "YachiyoAgent/apikey:deepseek"


@pytest.mark.parametrize("key,expected_hidden", [
    ("sk-1234567890abcdef", "1234567890"),
    ("short", "short"),
    (None, ""),
])
def test_mask_never_reveals_middle(key, expected_hidden):
    masked = SecretStore.mask(key)
    if expected_hidden:
        assert expected_hidden not in masked
    assert len(masked) > 0


def test_mask_distinguishes_unset_from_saved():
    assert "未设置" in SecretStore.mask(None, known_saved=False)
    assert "已保存" in SecretStore.mask(None, known_saved=True)


# ------------------------------------------------------------ 持久化路径

@pytest.mark.asyncio
async def test_save_load_delete_roundtrip():
    backend = FakeBackend()
    store = SecretStore(backend)

    await store.save("deepseek", "sk-abcdefghijklmnop")
    assert await store.load("deepseek") == "sk-abcdefghijklmnop"
    assert await store.exists("deepseek") is True

    await store.delete("deepseek")
    assert await store.load("deepseek") is None
    assert await store.exists("deepseek") is False


@pytest.mark.asyncio
async def test_values_land_in_backend_not_memory():
    """persist=True 必须真的写进系统凭据库，而不是只留着内存里。"""
    backend = FakeBackend()
    store = SecretStore(backend)
    await store.save("p", "sk-xyz", persist=True)

    assert store._session == {}
    assert backend.data[cred_target("p")] == "sk-xyz"


@pytest.mark.asyncio
async def test_session_only_never_touches_backend():
    backend = FakeBackend()
    store = SecretStore(backend)
    await store.save("p", "sk-temp", persist=False)

    assert await store.load("p") == "sk-temp"
    assert backend.data == {}
    assert store.session_ids() == ["p"]


@pytest.mark.asyncio
async def test_session_value_shadows_backend():
    store = SecretStore(FakeBackend())
    await store.save("p", "sk-old", persist=True)
    await store.save("p", "sk-new", persist=False)

    assert await store.load("p") == "sk-new"


@pytest.mark.asyncio
async def test_persist_clears_session_copy():
    store = SecretStore(FakeBackend())
    await store.save("p", "sk-1", persist=False)
    await store.save("p", "sk-2", persist=True)

    assert store.session_ids() == []
    assert await store.load("p") == "sk-2"


@pytest.mark.asyncio
async def test_empty_key_rejected():
    store = SecretStore(FakeBackend())
    with pytest.raises(ValueError):
        await store.save("p", "   ")


@pytest.mark.asyncio
async def test_delete_clears_both_layers():
    backend = FakeBackend()
    store = SecretStore(backend)
    await store.save("p", "sk-a", persist=True)
    await store.save("p", "sk-b", persist=False)
    await store.delete("p")

    assert await store.load("p") is None
    assert backend.data == {}


@pytest.mark.asyncio
async def test_purge_all():
    backend = FakeBackend()
    store = SecretStore(backend)
    await store.save("a", "sk-1")
    await store.save("b", "sk-2", persist=False)
    await store.purge_all()

    assert await store.load("a") is None
    assert await store.load("b") is None
    assert store.session_ids() == []
    assert backend.data == {}


@pytest.mark.asyncio
async def test_exists_does_not_read_plaintext_out():
    """exists() 走后端的 exists，不去 get 明文。"""
    backend = FakeBackend()
    store = SecretStore(backend)
    await store.save("p", "sk-secret")
    backend.calls.clear()

    assert await store.exists("p") is True
    assert ("exists", cred_target("p")) in backend.calls
    assert not [c for c in backend.calls if c[0] == "get"]


# ------------------------------------------------------------ 状态自述

def test_backend_label_and_persistent():
    assert SecretStore(FakeBackend()).persistent is True
    assert SecretStore(FakeBackend()).backend_label() == "假后端"

    # 没有后端 = 只有内存
    degraded = SecretStore(detect=False)
    assert degraded.persistent is False
    assert "仅本次运行" in degraded.backend_label()


# ------------------------------------------------------------ 降级 / 出错

@pytest.mark.asyncio
async def test_degrades_gracefully_without_backend():
    store = SecretStore(detect=False)
    assert store._backend is None

    # 读：不炸，返回 None
    assert await store.load("p") is None
    assert await store.exists("p") is False

    # 临时密钥仍然可用
    await store.save("p", "sk-temp", persist=False)
    assert await store.load("p") == "sk-temp"

    # 要求持久化时给出明确错误，而不是静默失败
    with pytest.raises(RuntimeError):
        await store.save("p", "sk-x", persist=True)


@pytest.mark.asyncio
async def test_broken_backend_save_raises_but_load_does_not():
    """写失败要抛（调用方必须知道没存住）；读失败只当读不到。

    读为什么可以不抛：用户已经配好的 key 读不出来，界面该做的是"提示重新填"，
    而不是弹一个他看不懂的 OSError 把启动流程打断。
    """
    store = SecretStore(ExplodingBackend())

    with pytest.raises(OSError):
        await store.save("p", "sk-x", persist=True)

    assert await store.load("p") is None
    assert await store.exists("p") is False
    # 删除和清空也不能炸
    await store.delete("p")
    await store.purge_all()


# ------------------------------------------------------------ 真机：Windows 凭据管理器

@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")
def test_windows_backend_real_roundtrip():
    """真的写进 Windows 凭据管理器，再读回来比对。

    这一步证明的不是"代码逻辑对"，而是"用户关掉程序再打开，key 还在"。
    """
    backend = WindowsCredentialBackend()
    target = cred_target("__pytest_roundtrip__")
    secret = "sk-pytest-0123456789abcdef"

    backend.delete(target)                     # 先清干净，免得受上次残留影响
    try:
        assert backend.exists(target) is False

        backend.set(target, secret)
        assert backend.exists(target) is True
        assert backend.get(target) == secret   # ★ 逐字节相同

        backend.set(target, secret + "-v2")    # 覆盖写
        assert backend.get(target) == secret + "-v2"
    finally:
        backend.delete(target)

    assert backend.exists(target) is False


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")
@pytest.mark.asyncio
async def test_real_store_through_public_api():
    """走 SecretStore 的公开接口再验一遍（含 asyncio.to_thread 那一层）。"""
    store = SecretStore(WindowsCredentialBackend())
    pid = "__pytest_public__"

    try:
        await store.save(pid, "sk-public-api-test")
        assert await store.exists(pid) is True
        assert await store.load(pid) == "sk-public-api-test"
    finally:
        await store.delete(pid)

    assert await store.exists(pid) is False


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")
def test_detect_backend_finds_something_on_windows():
    backend = detect_backend()
    assert backend is not None
    assert backend.label == "Windows 凭据管理器"


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")
def test_enumerate_finds_our_entries_but_only_ours():
    """purge_all 靠枚举"YachiyoAgent/*"来删干净 —— 这个前缀必须真的能匹配上。

    ⚠️ 这里**故意不调 clear()**：clear() 会删掉这台机器上真实保存的 key，
       在开发者自己的电脑上跑测试时那是灾难。只验证"枚举得到、且不越界"。
    """
    backend = WindowsCredentialBackend()
    mine = [cred_target("__pytest_enum_a__"), cred_target("__pytest_enum_b__")]
    for target in mine:
        backend.set(target, "sk-enum")

    try:
        found = backend._targets()
        for target in mine:
            assert target in found, f"枚举不到自己写的条目：{target}（拿到 {found}）"
        # 前缀过滤不能把别人的凭据也带进来
        assert all(t.startswith("YachiyoAgent/") for t in found), (
            f"枚举越界了，带出了不属于本应用的条目：{found}"
        )
    finally:
        for target in mine:
            backend.delete(target)
