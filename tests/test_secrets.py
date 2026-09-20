"""secrets.py 的测试。

策略：用假的 flet_secure_storage 替换真实模块。
这样既能验证完整的读写删逻辑，又不需要 Flet 运行时（纯 pytest 里没有它）。
"""

import sys
import types

import pytest

from core.secrets import SERVICE, SecretStore, entry_key


class FakeSecureStorage:
    """内存版凭据库，行为对齐真实 SecureStorage 的契约。"""

    instances: list["FakeSecureStorage"] = []

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.calls: list[tuple] = []
        FakeSecureStorage.instances.append(self)

    async def set(self, *, key, value, **kw) -> None:
        if value is None:
            raise ValueError("value can't be None")
        self.calls.append(("set", key))
        self.data[key] = value

    async def get(self, *, key, **kw):
        self.calls.append(("get", key))
        return self.data.get(key)          # 不存在返回 None（与真实实现一致）

    async def contains_key(self, *, key, **kw) -> bool:
        self.calls.append(("contains_key", key))
        return key in self.data

    async def remove(self, *, key, **kw) -> None:
        self.calls.append(("remove", key))
        self.data.pop(key, None)

    async def clear(self, **kw) -> None:
        self.calls.append(("clear",))
        self.data.clear()


@pytest.fixture
def fake_backend(monkeypatch):
    FakeSecureStorage.instances.clear()
    module = types.ModuleType("flet_secure_storage")
    module.SecureStorage = FakeSecureStorage
    monkeypatch.setitem(sys.modules, "flet_secure_storage", module)
    return FakeSecureStorage


@pytest.fixture
def broken_backend(monkeypatch):
    """模拟 flet-secure-storage 不可用（比如纯命令行环境）。"""
    def boom(*a, **kw):
        raise RuntimeError("no flet runtime")

    module = types.ModuleType("flet_secure_storage")
    module.SecureStorage = boom
    monkeypatch.setitem(sys.modules, "flet_secure_storage", module)
    return module


# ------------------------------------------------------------ 纯函数

def test_entry_key_is_stable():
    assert entry_key("deepseek") == "apikey:deepseek"
    assert SERVICE == "YachiyoAgent"


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
async def test_save_load_delete_roundtrip(fake_backend):
    store = SecretStore()
    await store.save("deepseek", "sk-abcdefghijklmnop")

    assert await store.load("deepseek") == "sk-abcdefghijklmnop"
    assert await store.exists("deepseek") is True

    await store.delete("deepseek")
    assert await store.load("deepseek") is None
    assert await store.exists("deepseek") is False


@pytest.mark.asyncio
async def test_values_land_in_backend_not_memory(fake_backend):
    """persist=True 必须真的写进系统凭据库，而不是只留着内存里。"""
    store = SecretStore()
    await store.save("p", "sk-xyz", persist=True)

    assert store._session == {}
    assert fake_backend.instances[0].data[entry_key("p")] == "sk-xyz"


@pytest.mark.asyncio
async def test_session_only_never_touches_backend(fake_backend):
    store = SecretStore()
    await store.save("p", "sk-temp", persist=False)

    assert await store.load("p") == "sk-temp"
    assert fake_backend.instances[0].data == {}
    assert store.session_ids() == ["p"]


@pytest.mark.asyncio
async def test_session_value_shadows_backend(fake_backend):
    store = SecretStore()
    await store.save("p", "sk-old", persist=True)
    await store.save("p", "sk-new", persist=False)

    assert await store.load("p") == "sk-new"


@pytest.mark.asyncio
async def test_persist_clears_session_copy(fake_backend):
    store = SecretStore()
    await store.save("p", "sk-1", persist=False)
    await store.save("p", "sk-2", persist=True)

    assert store.session_ids() == []
    assert await store.load("p") == "sk-2"


@pytest.mark.asyncio
async def test_empty_key_rejected(fake_backend):
    store = SecretStore()
    with pytest.raises(ValueError):
        await store.save("p", "   ")


@pytest.mark.asyncio
async def test_delete_clears_both_layers(fake_backend):
    store = SecretStore()
    await store.save("p", "sk-a", persist=True)
    await store.save("p", "sk-b", persist=False)
    await store.delete("p")

    assert await store.load("p") is None
    assert fake_backend.instances[0].data == {}


@pytest.mark.asyncio
async def test_purge_all(fake_backend):
    store = SecretStore()
    await store.save("a", "sk-1")
    await store.save("b", "sk-2", persist=False)
    await store.purge_all()

    assert await store.load("a") is None
    assert await store.load("b") is None
    assert store.session_ids() == []


# ------------------------------------------------------------ 降级

@pytest.mark.asyncio
async def test_degrades_gracefully_without_backend(broken_backend):
    store = SecretStore()
    assert store._ss is None

    # 读：不炸，返回 None
    assert await store.load("p") is None
    assert await store.exists("p") is False

    # 临时密钥仍然可用
    await store.save("p", "sk-temp", persist=False)
    assert await store.load("p") == "sk-temp"

    # 要求持久化时给出明确错误，而不是静默失败
    with pytest.raises(RuntimeError):
        await store.save("p", "sk-x", persist=True)
