"""本地后端（backend/）的接口测试。

这里测的都是"前端能观察到的东西"：状态码、字段、事件流。
密钥永远不返回明文，是其中一条硬要求 —— 这里也断言它。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import BackendState, create_app
from backend.legacy import migrate_legacy_data
from core.config import ProviderConfig
from core.secrets import SecretStore

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "backend"
TOKEN = "test-token"


class FakeKeyring:
    """假凭据库：只把一个 dict 摆成 WindowsCredentialBackend 的样子。"""

    label = "测试用内存凭据库"

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set(self, target: str, value: str) -> None:
        self.data[target] = value

    def get(self, target: str) -> str | None:
        return self.data.get(target)

    def exists(self, target: str) -> bool:
        return target in self.data

    def delete(self, target: str) -> None:
        self.data.pop(target, None)

    def clear(self) -> None:
        self.data.clear()


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    root = _TMP_ROOT / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def client(keyring):
    state = BackendState(secrets=SecretStore(backend=keyring))
    app = create_app(token=TOKEN, state=state, assets=False)
    with TestClient(app) as c:
        c.headers.update({"X-Yachiyo-Token": TOKEN})
        c.state_obj = state  # type: ignore[attr-defined]
        yield c


def _provider(**over) -> dict:
    base = {
        "id": "deepseek",
        "display_name": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model_id": "deepseek-chat",
    }
    base.update(over)
    return base


class FakeChat:
    """替身 Chat：吐固定的几段话，用来测事件流。"""

    def __init__(self, pieces=("你", "好", "呀"), *, hang: bool = False) -> None:
        self.messages: list[dict] = [{"role": "system", "content": "test"}]
        self.pieces = pieces
        self.hang = hang
        self.on_tool_start = None
        self.on_tool_end = None

    def add_message(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    async def reply_stream(self):
        for piece in self.pieces:
            yield piece
        if self.hang:
            await asyncio.sleep(30)


class ReadyState(BackendState):
    """ensure_chat 永远就绪，chat 换成替身 —— 不碰网络。"""

    def __init__(self, chat: FakeChat, **kw) -> None:
        super().__init__(**kw)
        self.chat = chat

    async def ensure_chat(self, *, force: bool = False) -> str:  # noqa: ARG002
        return ""


# ─────────────────────────── 基础 ───────────────────────────


def test_health_needs_no_token(client):
    plain = TestClient(client.app)
    resp = plain.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_requires_token(client):
    plain = TestClient(client.app)
    assert plain.get("/api/bootstrap").status_code == 401
    assert plain.get("/api/bootstrap", headers={"X-Yachiyo-Token": "wrong"}).status_code == 401
    assert plain.get("/api/bootstrap", headers={"X-Yachiyo-Token": TOKEN}).status_code == 200


def test_bootstrap_first_run_then_has_everything(client, isolated_data_dir):
    body = client.get("/api/bootstrap").json()
    assert body["first_run"] is True
    assert body["active_has_key"] is False
    for key in ("config", "providers", "conversation", "memories", "reminders", "secrets", "presets"):
        assert key in body
    assert body["secrets"]["persistent"] is True
    assert body["secrets"]["backend"] == "测试用内存凭据库"
    # 预设必须来自 BUILTIN_PRESETS：只配了数据目录、还没建过 provider 的新用户
    # 也要能"点一下就填好地址"。曾经这里是空的（预设错用了已配置的 providers）
    presets = body["presets"]
    assert [p["id"] for p in presets][:2] == ["deepseek", "openai"]
    assert len(presets) >= 5
    assert all({"id", "display_name", "protocol", "base_url", "model_id"} <= set(p) for p in presets)
    assert body["providers"] == []          # 预设不是已配置的服务商，别混
    assert not any("api_key" in p for p in presets)


# ─────────────────────────── 引导 ───────────────────────────


def test_oobe_finish_saves_config_and_key(client, keyring, isolated_data_dir):
    resp = client.post(
        "/api/oobe/finish",
        json={**_provider(), "api_key": "sk-test-1234567890"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["persisted"] is True
    assert body["message"] == ""
    # 配置文件真的落盘了（first_run 靠它判断）
    assert (isolated_data_dir / "config.json").exists()
    # 密钥进了凭据库，而且从没有明文回到接口上
    assert keyring.data["YachiyoAgent/apikey:deepseek"] == "sk-test-1234567890"
    assert "sk-test-1234567890" not in json.dumps(body)

    after = client.get("/api/bootstrap").json()
    assert after["first_run"] is False
    assert after["active_provider"] == "deepseek"
    assert after["active_has_key"] is True
    assert after["providers"][0]["has_key"] is True


def test_oobe_finish_rejects_incomplete_provider(client):
    resp = client.post("/api/oobe/finish", json=_provider(model_id=""))
    assert resp.status_code == 400
    assert "模型 ID" in resp.json()["detail"]


def test_secret_falls_back_to_session_when_no_keyring(monkeypatch):
    """凭据库不可用时：降级成仅本次运行，但人不能因此被挡在门外。"""
    state = BackendState(secrets=SecretStore(detect=False))
    app = create_app(token="", state=state, assets=False)
    with TestClient(app) as c:
        resp = c.post("/api/oobe/finish", json={**_provider(), "api_key": "sk-session"})
        body = resp.json()
        assert body["persisted"] is False
        assert body["note"]
        assert body["ok"] is True  # 仍然进得了主界面
        assert body["config"]["active_provider"] == "deepseek"
        assert state.secrets.session_ids() == ["deepseek"]


# ─────────────────────────── provider ───────────────────────────


def test_provider_crud(client, keyring):
    created = client.post("/api/providers", json=_provider()).json()
    assert created["active_provider"] == "deepseek"
    assert created["provider"]["has_key"] is False

    created2 = client.post(
        "/api/providers",
        json=_provider(id="my-relay", display_name="中转站", base_url="https://relay.example.com/v1"),
    ).json()
    assert len(created2["config"]["providers"]) == 2
    assert created2["active_provider"] == "my-relay"

    # 保存密钥 + 显示状态
    client.post("/api/secrets", json={"provider_id": "my-relay", "api_key": "sk-relay"})
    secrets_body = client.get("/api/secrets").json()
    assert secrets_body["saved_ids"] == ["my-relay"]

    # 删除 provider 要顺手把密钥也删掉（否则凭据库里留垃圾）
    client.delete("/api/providers/my-relay")
    left = client.get("/api/providers").json()["providers"]
    assert [p["id"] for p in left] == ["deepseek"]
    assert "YachiyoAgent/apikey:my-relay" not in keyring.data


def test_provider_id_is_generated_when_missing(client):
    body = client.post("/api/providers", json=_provider(id="", display_name="OpenRouter")).json()
    assert body["provider"]["id"].startswith("openrouter")


def test_test_connection_with_temporary_provider(client, monkeypatch):
    """引导页在保存之前就要测连接，所以得支持"临时 provider + 临时 key"。"""
    calls = {}

    async def fake_test(provider, api_key, *, timeout=30.0):
        calls["id"] = provider.id
        calls["key"] = api_key
        return True, "连接成功。目标：api.deepseek.com"

    monkeypatch.setattr("backend.app.test_connection", fake_test)
    resp = client.post(
        "/api/providers/test",
        json={"provider": _provider(), "api_key": "sk-temp"},
    )
    assert resp.json() == {"ok": True, "message": "连接成功。目标：api.deepseek.com"}
    assert calls == {"id": "deepseek", "key": "sk-temp"}


def test_test_connection_without_key_says_so(client):
    client.post("/api/providers", json=_provider())
    resp = client.post("/api/providers/test", json={"provider_id": "deepseek"})
    assert resp.json()["ok"] is False
    assert "API Key" in resp.json()["message"]


def test_list_models_failure_is_not_an_error(client, monkeypatch):
    async def fake_list(provider, api_key, *, timeout=15.0):
        return None

    monkeypatch.setattr("backend.app.list_models", fake_list)
    client.post("/api/providers", json=_provider())
    body = client.post("/api/providers/models", json={"provider_id": "deepseek"}).json()
    assert body["models"] is None
    assert body["message"]


# ─────────────────────────── 配置 / 存储 ───────────────────────────


def test_config_only_accepts_editable_fields(client):
    assert client.post("/api/config", json={"temperature": 0.5}).json()["temperature"] == 0.5
    assert client.post("/api/config", json={"schema_version": 99}).status_code == 400
    assert client.post("/api/config", json={"temperature": "很热"}).status_code == 400


def test_memories_and_reminders_roundtrip(client):
    client.post("/api/memories", json={"key": "名字", "value": "八千代", "confidence": 0.9})
    memories = client.get("/api/memories").json()["memories"]
    assert memories[0]["key"] == "名字"
    assert memories[0]["value"] == "八千代"

    client.delete("/api/memories/名字")
    assert client.get("/api/memories").json()["memories"] == []

    client.post("/api/reminders", json={"content": "喝水", "due_at": "下午三点"})
    assert client.get("/api/reminders").json()["reminders"][0]["content"] == "喝水"
    client.delete("/api/reminders")
    assert client.get("/api/reminders").json()["reminders"] == []


def test_conversation_endpoints(client, isolated_data_dir):
    from core import store

    store.save_conversation([{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在"}])
    assert len(client.get("/api/conversation").json()["messages"]) == 2
    client.post("/api/conversation/clear")
    assert client.get("/api/conversation").json()["messages"] == []


# ─────────────────────────── 聊天（WS） ───────────────────────────


def _ws_client(chat: FakeChat, keyring=None):
    state = ReadyState(chat, secrets=SecretStore(backend=keyring or FakeKeyring()))
    app = create_app(token=TOKEN, state=state, assets=False)
    client = TestClient(app)
    return client, state


def test_ws_requires_token():
    chat = FakeChat()
    client, _ = _ws_client(chat)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/chat?token=wrong"):
            pass


def test_ws_streams_deltas_and_saves_conversation(isolated_data_dir):
    chat = FakeChat(pieces=("你", "好"))
    client, state = _ws_client(chat)
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}

        ws.send_json({"type": "user", "text": "你好"})
        assert ws.receive_json() == {"type": "start"}
        assert ws.receive_json() == {"type": "delta", "text": "你"}
        assert ws.receive_json() == {"type": "delta", "text": "好"}
        assert ws.receive_json() == {"type": "done", "text": "你好"}

    # 一轮结束就落盘（关窗口也不会丢）
    from core import store

    saved = store.load_conversation()
    assert saved == [{"role": "user", "content": "你好"}]


def test_ws_reports_missing_key(isolated_data_dir):
    state = BackendState(secrets=SecretStore(detect=False))
    app = create_app(token=TOKEN, state=state, assets=False)
    with TestClient(app).websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"type": "user", "text": "你好"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "模型服务" in msg["message"]


def test_ws_cancel_keeps_partial_text(isolated_data_dir):
    chat = FakeChat(pieces=("半",), hang=True)
    client, state = _ws_client(chat)
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"type": "user", "text": "讲个长故事"})
        assert ws.receive_json() == {"type": "start"}
        assert ws.receive_json() == {"type": "delta", "text": "半"}
        ws.send_json({"type": "cancel"})
        stopped = ws.receive_json()
        assert stopped["type"] == "stopped"
        assert stopped["text"] == "半"

    from core import store

    # 取消也要把已经说出去的字留下，否则下次她会"忘了自己说过"
    assert store.load_conversation() == [{"role": "user", "content": "讲个长故事"}]


def test_ws_rejects_second_message_while_busy(isolated_data_dir):
    chat = FakeChat(pieces=("嗯",), hang=True)
    client, _ = _ws_client(chat)
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"type": "user", "text": "第一句"})
        assert ws.receive_json()["type"] == "start"
        ws.send_json({"type": "user", "text": "第二句"})
        msg = ws.receive_json()
        assert msg["type"] == "delta"  # 先把第一轮的增量发完
        assert ws.receive_json() == {"type": "error", "message": "上一句还在说呢，稍等一下。"}
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "stopped"


# ─────────────────────────── 数据搬家 ───────────────────────────


def test_migrate_legacy_data_copies_without_overwriting(monkeypatch, isolated_data_dir):
    import backend.legacy as legacy

    old = isolated_data_dir.parent / (isolated_data_dir.name + "-old")
    old.mkdir(parents=True, exist_ok=True)
    (old / "config.json").write_text('{"schema_version": 1, "active_provider": "deepseek"}', encoding="utf-8")
    (old / "conversation.json").write_text('[{"role": "user", "content": "旧话"}]', encoding="utf-8")
    monkeypatch.setattr(legacy, "LEGACY_DIRS", (old,))

    moved = migrate_legacy_data()
    assert sorted(moved) == ["config.json", "conversation.json"]
    assert (isolated_data_dir / "config.json").exists()

    # 已经有的文件不覆盖，第二次搬就没什么可搬的了
    (isolated_data_dir / "config.json").write_text('{"schema_version": 1, "active_provider": "mine"}', encoding="utf-8")
    assert migrate_legacy_data() == []
    assert "mine" in (isolated_data_dir / "config.json").read_text(encoding="utf-8")

    shutil.rmtree(old, ignore_errors=True)


def test_provider_id_field_is_stable_for_credentials():
    """provider.id 是密钥的存储键，改它 = 用户的 key 失联。这条锁住它。"""
    p = ProviderConfig(id="deepseek", display_name="改个名字")
    assert p.id == "deepseek"


# ─────────────────────────── 渲染页（Live2D）同源挂载 ───────────────────────────


def test_live2d_page_shares_the_backend_origin():
    """渲染页必须和后端同源：Electron 里 iframe 要靠同源才能直接调 window.yachiyo。"""
    app = create_app(token="", state=BackendState(secrets=SecretStore(backend=FakeKeyring())))
    with TestClient(app) as c:
        info = c.get("/api/bootstrap").json()["live2d"]
        if not info["ready"]:
            pytest.skip("这台机器上没有 Live2D 模型：" + info["message"])

        # 设置浮层靠 name 显示"现在是哪个角色"（也是模型版权声明该出现的地方）。
        # 给的是目录名，不是完整路径 —— 别把本机路径漏到界面上。
        assert info["name"], "没有模型名，界面会显示成「没找到模型」"
        assert "\\" not in info["name"] and "/" not in info["name"]

        page = c.get(info["page"])
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "window.yachiyo" in page.text

        # ?model= 指向的那份定义文件要真的能取到，且是合法 JSON
        model = c.get(info["model"])
        assert model.status_code == 200
        assert "FileReferences" in model.json()

        # ES module 的 MIME 给错浏览器会拒绝执行，这条特意写死
        vendor = c.get("/static/vendor/pixi.min.mjs")
        assert vendor.status_code == 200
        assert vendor.headers["content-type"].startswith("text/javascript")


def test_live2d_assets_block_traversal_and_misses():
    app = create_app(token="", state=BackendState(secrets=SecretStore(backend=FakeKeyring())))
    with TestClient(app) as c:
        # 想爬出根目录：编码过的 ../ 也要被挡住
        assert c.get("/static/..%2F..%2Fcore%2Fsecrets.py").status_code == 404
        assert c.get("/model/..%2F..%2Fcore%2Fsecrets.py").status_code == 404
        assert c.get("/static/不存在的文件.mjs").status_code == 404
        assert c.get("/model/不存在的文件.moc3").status_code == 404
