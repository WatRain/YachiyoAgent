"""本地 HTTP + WebSocket 服务：Electron 前端访问 core/ 的唯一入口。

三条设计约定，后面改代码时别破坏：

1. **只监听 127.0.0.1，并且要 token。**
   "在本机"不等于"安全"：同机器上任何程序、任何网页都能扫本地端口，
   而这个后端能读配置、能拿密钥去打模型接口。所以每次启动生成一个
   随机 token（由 __main__.py 通过环境变量/stdout 交给前端），
   /api/* 要 `X-Yachiyo-Token` 头，/ws/* 要 `?token=`。

2. **同源到底。** 界面、Live2D 页面、接口都由这一个服务提供，
   Electron 窗口直接开 http://127.0.0.1:<port>/。
   这样就没有 file:// 的 fetch/CORS 和 iframe 跨源问题。

3. **状态只有一份。** BackendState 持有 AppConfig / SecretStore / Chat，
   和之前 Flet 版的 AppState 语义一一对应（含"换 provider 要重建 Chat
   但把历史搬过去"这条）。

LLM 相关的重活（litellm 的 import 要 7 秒）都在调用点内部发生，
所以这个模块 import 起来很快，窗口不会等它。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from core import config as config_mod
from core import providers as providers_mod
from core import store
from core.chat import Chat
from core.config import AppConfig, ProviderConfig
from core.llm import humanize_error, list_models, test_connection
from core.paths import PROJECT_ROOT, config_path, renderer_dir
from core.secrets import SecretStore, backend_label_of

from backend.live2d import Live2DInfo
from backend.live2d import mount as mount_live2d

log = logging.getLogger(__name__)

API_VERSION = 1

# 前端界面（Electron 渲染进程的页面）由后端一起提供，避免 file:// 的跨源麻烦。
# 打包后位置会变，所以走 core.paths（可用 YACHIYO_RENDERER_DIR 覆盖）。
RENDERER_DIR = renderer_dir()

# 允许通过 POST /api/config 改的字段。providers / schema_version 有专用接口，不给改。
EDITABLE_CONFIG_FIELDS = {
    "active_provider",
    "history_token_budget",
    "keep_recent_turns",
    "max_tool_rounds",
    "stream",
    "temperature",
    "theme",
    "live2d_physics",
    "live2d_detached",
    # 隐私政策 / 许可条款的同意记录（前端过许可页时写一次）。
    "consent",
}

PROVIDER_FIELDS = (
    "id",
    "display_name",
    "protocol",
    "base_url",
    "model_id",
    "supports_stream",
    "is_builtin",
    "extra_headers",
)


class BackendState:
    """后端持有的全部可变状态。和 Flet 版 app/main.py 的 AppState 对应。"""

    def __init__(self, *, secrets: SecretStore | None = None) -> None:
        self.cfg: AppConfig = config_mod.load_config()
        self.secrets: SecretStore = secrets if secrets is not None else SecretStore()
        self.chat: Chat | None = None
        self.applied_provider: str = ""
        self.applied_key: str = ""
        # 一次只跑一轮对话：两个前端页面同时开时，别让它们往同一个 Chat 里塞话
        self.turn_lock = asyncio.Lock()

    # ---------- 对话 ----------

    def history(self) -> list[dict]:
        """当前对话里可持久化的部分（user / assistant）。"""
        if self.chat is None:
            return []
        return [
            m
            for m in self.chat.messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

    async def ensure_chat(self, *, force: bool = False) -> str:
        """确认"可以聊了"。返回 "" 表示就绪，否则返回一句给用户看的原因。"""
        provider = config_mod.get_provider(self.cfg)
        if provider is None:
            self.chat = None
            return "还没有选择模型服务，请先在设置里选一个。"

        try:
            providers_mod.validate_base_url(provider.base_url)
        except ValueError as exc:
            self.chat = None
            return f"provider 地址不合法：{exc}"

        key = await self.secrets.load(provider.id)
        if not key:
            self.chat = None
            return f"「{provider.display_name}」还没有填 API Key。"

        try:
            kwargs = providers_mod.to_litellm_kwargs(provider, key)
        except ValueError as exc:
            self.chat = None
            return f"provider 配置有问题：{exc}"

        # 已经就绪、provider 和 key 都没变 → 保持现状（上下文才留得住）
        if (
            not force
            and self.chat is not None
            and self.applied_provider == provider.id
            and self.applied_key == key
        ):
            return ""

        carried = self.history() or store.load_conversation()
        self.chat = Chat(
            kwargs,
            temperature=self.cfg.temperature,
            max_rounds=self.cfg.max_tool_rounds,
        )
        self.chat.messages.extend(carried)
        self.applied_provider = provider.id
        self.applied_key = key
        log.info("对话已就绪：provider=%s model=%s 历史=%d 条", provider.id, provider.model_id, len(carried))
        return ""

    def save_conversation(self) -> int:
        history = self.history()
        if not history:
            return 0
        store.save_conversation(history)
        return len(history)

    # ---------- 配置 ----------

    def save_config(self) -> None:
        config_mod.save_config(self.cfg)

    async def provider_payload(self, p: ProviderConfig) -> dict:
        """给前端的 provider 视图：配置字段 + 有没有 key（永远不含明文）。"""
        return {**p.model_dump(), "has_key": await self.secrets.exists(p.id)}

    async def secrets_payload(self) -> dict:
        return {
            "persistent": self.secrets.persistent,
            "backend": backend_label_of(self.secrets),
            "session_ids": self.secrets.session_ids(),
            "saved_ids": [
                p.id for p in self.cfg.providers if await self.secrets.exists(p.id)
            ],
        }


def _provider_from_payload(payload: dict, *, existing_ids: list[str]) -> ProviderConfig:
    data = {k: payload[k] for k in PROVIDER_FIELDS if k in payload}
    data.setdefault("display_name", "")
    if not data.get("id"):
        data["id"] = providers_mod.make_provider_id(data["display_name"], existing_ids)
    try:
        return ProviderConfig.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"provider 字段不合法：{exc.error_count()} 处") from exc


def create_app(*, token: str = "", state: BackendState | None = None,
               assets: bool = True,
               on_ready: Callable[[], None] | None = None) -> FastAPI:
    """建应用。token 为空 = 不校验（只在测试里这么用）。

    assets=False 时不挂渲染页（/pet、/static、/model）—— 接口测试不需要，
    也免得每个测试都去扫一遍模型目录。

    on_ready 在**服务器真正开始监听之后**调用（Electron 靠它知道可以连了；
    启动时提前喊一嗓子的话，前端会拿到 connection refused）。
    """
    st = state or BackendState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if on_ready is not None:
            on_ready()
        yield

    app = FastAPI(title="YachiyoAgent backend", version=str(API_VERSION),
                  docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.yachiyo = st
    # 渲染页（Live2D）和界面同源：Electron 里 iframe 才能直接摸到 window.yachiyo
    app.state.live2d = (mount_live2d(app) if assets else Live2DInfo(ready=False)).as_dict()

    def require_token(x_yachiyo_token: str | None = Header(default=None)) -> None:
        if not token:
            return
        if x_yachiyo_token != token:
            raise HTTPException(status_code=401, detail="token 不对")

    guard = [Depends(require_token)]

    # ─────────────────────── 健康检查 ───────────────────────
    # 唯一不要 token 的接口：前端要先知道"后端起来了没"，此时它还没拿到 token
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "name": "yachiyo", "api": API_VERSION}

    # ─────────────────────── 启动时一次性拿全 ───────────────────────
    @app.get("/api/bootstrap", dependencies=guard)
    async def bootstrap() -> dict:
        providers = [await st.provider_payload(p) for p in st.cfg.providers]
        active = config_mod.get_provider(st.cfg)
        return {
            "api": API_VERSION,
            "first_run": not config_path().exists(),
            "config": st.cfg.model_dump(),
            "providers": providers,
            # 内置预设只用于"少打字"：引导页/设置页把它做成可点的芯片。
            # 它不是已配置的服务商，别把两者混起来（密钥只有用户的才有）。
            "presets": [p.model_dump() for p in providers_mod.BUILTIN_PRESETS],
            "active_provider": st.cfg.active_provider,
            "active_has_key": bool(active and await st.secrets.exists(active.id)),
            "conversation": store.load_conversation(),
            "memories": store.load_memories(),
            "reminders": store.list_reminders(),
            "secrets": await st.secrets_payload(),
            "live2d": app.state.live2d,
        }

    # ─────────────────────── 配置 ───────────────────────
    @app.get("/api/config", dependencies=guard)
    async def get_config() -> dict:
        return st.cfg.model_dump()

    @app.post("/api/config", dependencies=guard)
    async def post_config(payload: dict) -> dict:
        changes = {k: v for k, v in payload.items() if k in EDITABLE_CONFIG_FIELDS}
        if not changes:
            raise HTTPException(status_code=400, detail="没有可更新的字段")
        try:
            st.cfg = AppConfig.model_validate({**st.cfg.model_dump(), **changes})
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"配置值不合法：{exc.error_count()} 处") from exc
        await asyncio.to_thread(st.save_config)
        return st.cfg.model_dump()

    # ─────────────────────── Provider ───────────────────────
    @app.get("/api/providers", dependencies=guard)
    async def get_providers() -> dict:
        return {
            "providers": [await st.provider_payload(p) for p in st.cfg.providers],
            "active_provider": st.cfg.active_provider,
        }

    @app.post("/api/providers", dependencies=guard)
    async def post_provider(payload: dict) -> dict:
        p = _provider_from_payload(payload, existing_ids=[x.id for x in st.cfg.providers])
        problems = providers_mod.validate_provider(p)
        if problems:
            raise HTTPException(status_code=400, detail="；".join(problems))

        config_mod.upsert_provider(st.cfg, p)
        if payload.get("set_active", True):
            st.cfg.active_provider = p.id
        await asyncio.to_thread(st.save_config)
        log.info("已保存 provider id=%s model=%s", p.id, p.model_id)
        return {
            "provider": await st.provider_payload(p),
            "active_provider": st.cfg.active_provider,
            "config": st.cfg.model_dump(),
        }

    @app.delete("/api/providers/{provider_id}", dependencies=guard)
    async def delete_provider(provider_id: str) -> dict:
        config_mod.remove_provider(st.cfg, provider_id)
        await st.secrets.delete(provider_id)
        await asyncio.to_thread(st.save_config)
        log.info("已删除 provider id=%s（连同它的密钥）", provider_id)
        return {"config": st.cfg.model_dump(), "providers": [await st.provider_payload(x) for x in st.cfg.providers]}

    @app.post("/api/providers/test", dependencies=guard)
    async def test_provider(payload: dict) -> dict:
        """测连接。可以传 provider_id（用已保存的配置和密钥），
        也可以传一份临时 provider + api_key（引导页还没保存时用）。"""
        api_key = (payload.get("api_key") or "").strip()
        existing = {x.id for x in st.cfg.providers}

        if payload.get("provider"):
            p = _provider_from_payload(payload["provider"], existing_ids=list(existing))
        else:
            p = config_mod.get_provider(st.cfg, payload.get("provider_id") or None)
            if p is None:
                raise HTTPException(status_code=404, detail="找不到这个 provider")

        # 只有测一个**已经存过**的 provider 才回落到已存的密钥。引导页 / 新建时传的是一份
        # 临时 provider，它的 id 是按名字现推的（"DeepSeek" → "deepseek"），这里要是也回落，
        # 凭据管理器里留着同名的旧密钥就会把"没填 Key"测成"连接成功"。
        if not api_key and p.id in existing:
            api_key = await st.secrets.load(p.id) or ""
        if not api_key:
            return {"ok": False, "message": "还没有填 API Key。"}

        ok, message = await test_connection(p, api_key)
        log.info("测连接 provider=%s ok=%s", p.id, ok)
        return {"ok": ok, "message": message}

    @app.post("/api/providers/models", dependencies=guard)
    async def provider_models(payload: dict) -> dict:
        """拉模型列表。和 /api/providers/test 一样，可以传 provider_id（已保存的），
        也可以传一份临时 provider（引导页还没保存时用）。"""
        api_key = (payload.get("api_key") or "").strip()
        existing = {x.id for x in st.cfg.providers}

        if payload.get("provider"):
            p = _provider_from_payload(payload["provider"], existing_ids=list(existing))
        else:
            p = config_mod.get_provider(st.cfg, payload.get("provider_id") or None)
            if p is None:
                raise HTTPException(status_code=404, detail="找不到这个 provider")

        # 同 /api/providers/test：只有已存过的 provider 才回落到已存的密钥
        if not api_key and p.id in existing:
            api_key = await st.secrets.load(p.id) or ""
        if not api_key:
            return {"models": None, "message": "还没有填 API Key。"}
        models = await list_models(p, api_key)
        return {
            "models": models,
            "message": "" if models else "这个端点没有提供模型列表，手填模型 ID 就行。",
        }

    # ─────────────────────── 密钥 ───────────────────────
    @app.get("/api/secrets", dependencies=guard)
    async def get_secrets() -> dict:
        return await st.secrets_payload()

    @app.post("/api/secrets", dependencies=guard)
    async def post_secret(payload: dict) -> dict:
        provider_id = (payload.get("provider_id") or "").strip()
        api_key = (payload.get("api_key") or "").strip()
        if not provider_id:
            raise HTTPException(status_code=400, detail="缺少 provider_id")
        if not api_key:
            raise HTTPException(status_code=400, detail="API Key 不能为空")

        persist = bool(payload.get("persist", True))
        persisted = persist
        note = ""
        try:
            await st.secrets.save(provider_id, api_key, persist=persist)
        except RuntimeError as exc:
            # 系统凭据库不可用 → 降级成"仅本次运行"，但不能因此把人挡在门外
            await st.secrets.save(provider_id, api_key, persist=False)
            persisted = False
            note = str(exc)
            log.warning("密钥无法持久化，已降级为仅本次运行：%s", exc)

        return {
            "persisted": persisted,
            "note": note,
            "secrets": await st.secrets_payload(),
        }

    @app.delete("/api/secrets/{provider_id}", dependencies=guard)
    async def delete_secret(provider_id: str) -> dict:
        await st.secrets.delete(provider_id)
        return {"secrets": await st.secrets_payload()}

    # ─────────────────────── 引导收尾 ───────────────────────
    @app.post("/api/oobe/finish", dependencies=guard)
    async def oobe_finish(payload: dict) -> dict:
        """引导页最后一步：存好 provider 和 key，落盘配置，然后告诉前端能不能聊。"""
        p = _provider_from_payload(payload, existing_ids=[x.id for x in st.cfg.providers])
        problems = providers_mod.validate_provider(p)
        if problems:
            raise HTTPException(status_code=400, detail="；".join(problems))

        persisted = True
        note = ""
        api_key = (payload.get("api_key") or "").strip()
        if api_key:
            persist = bool(payload.get("persist", True))
            try:
                await st.secrets.save(p.id, api_key, persist=persist)
            except RuntimeError as exc:
                await st.secrets.save(p.id, api_key, persist=False)
                persisted = False
                note = str(exc)

        config_mod.upsert_provider(st.cfg, p)
        st.cfg.active_provider = p.id
        await asyncio.to_thread(st.save_config)

        # 先清掉可能存在的旧 Chat，确保下面用的是新 provider
        st.chat = None
        st.applied_provider = ""
        st.applied_key = ""
        ready_error = await st.ensure_chat()
        log.info("引导完成：provider=%s 已持久化=%s 就绪=%s", p.id, persisted, not ready_error)
        return {
            "ok": not ready_error,
            "persisted": persisted,
            "note": note,
            "message": ready_error,
            "config": st.cfg.model_dump(),
        }

    # ─────────────────────── 对话记录 ───────────────────────
    @app.get("/api/conversation", dependencies=guard)
    async def get_conversation() -> dict:
        return {"messages": store.load_conversation()}

    @app.post("/api/conversation/clear", dependencies=guard)
    async def clear_conversation() -> dict:
        await asyncio.to_thread(store.clear_conversation)
        if st.chat is not None:
            # 只保留 system（人格设定），历史清空
            st.chat.messages = st.chat.messages[:1]
        return {"ok": True}

    # ─────────────────────── 记忆 / 提醒 ───────────────────────
    @app.get("/api/memories", dependencies=guard)
    async def get_memories() -> dict:
        return {"memories": store.load_memories()}

    @app.post("/api/memories", dependencies=guard)
    async def post_memory(payload: dict) -> dict:
        key = (payload.get("key") or "").strip()
        value = (payload.get("value") or "").strip()
        if not key or not value:
            raise HTTPException(status_code=400, detail="key 和 value 都不能为空")
        try:
            confidence = float(payload.get("confidence", 0.8))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="confidence 得是数字") from None
        await asyncio.to_thread(store.upsert_memory, key, value, confidence)
        return {"memories": store.load_memories()}

    @app.delete("/api/memories/{key}", dependencies=guard)
    async def delete_memory(key: str) -> dict:
        await asyncio.to_thread(store.delete_memory, key)
        return {"memories": store.load_memories()}

    @app.get("/api/reminders", dependencies=guard)
    async def get_reminders() -> dict:
        return {"reminders": store.list_reminders()}

    @app.post("/api/reminders", dependencies=guard)
    async def post_reminder(payload: dict) -> dict:
        content = (payload.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="内容不能为空")
        item = await asyncio.to_thread(store.add_reminder, content, payload.get("due_at") or "")
        return {"reminder": item, "reminders": store.list_reminders()}

    @app.delete("/api/reminders", dependencies=guard)
    async def clear_reminders() -> dict:
        await asyncio.to_thread(store.clear_reminders)
        return {"reminders": []}

    # ─────────────────────── 聊天（WebSocket） ───────────────────────
    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket, token_q: str | None = Query(default=None, alias="token")) -> None:
        if token and token_q != token:
            await ws.close(code=4401)
            return
        await ws.accept()

        turn: asyncio.Task | None = None
        try:
            while True:
                try:
                    msg = await ws.receive_json()
                except (WebSocketDisconnect, ValueError):
                    break
                if not isinstance(msg, dict):
                    continue

                kind = msg.get("type")
                if kind == "ping":
                    await ws.send_json({"type": "pong"})
                    continue

                if kind == "cancel":
                    if turn is not None and not turn.done():
                        turn.cancel()
                    continue

                if kind != "user":
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if turn is not None and not turn.done():
                    await ws.send_json({"type": "error", "message": "上一句还在说呢，稍等一下。"})
                    continue
                turn = asyncio.create_task(_run_turn(ws, st, text))
                turn.add_done_callback(_swallow)
        finally:
            if turn is not None and not turn.done():
                turn.cancel()

    # ─────────────────────── 前端静态资源 ───────────────────────
    if RENDERER_DIR.exists():
        app.mount("/", StaticFiles(directory=str(RENDERER_DIR), html=True), name="renderer")
    else:
        log.warning("前端目录不存在，界面相关请求会 404：%s", RENDERER_DIR)

    return app


def _swallow(task: asyncio.Task) -> None:
    """取走任务的异常，别让它变成 'Task exception was never retrieved'。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("对话任务异常：%s", type(exc).__name__)


async def _run_turn(ws: WebSocket, st: BackendState, text: str) -> None:
    """跑一轮：用户说 → 模型流式回 → 落盘。

    事件协议（前端只认这几种）：
      {"type":"start"}                                  可以开始清空气泡了
      {"type":"delta","text":...}                       一段增量文字
      {"type":"tool_start","name":...}                   模型要调工具了
      {"type":"tool_end","name":...,"content":...}       工具返回了
      {"type":"done","text":...}                        这一轮完整文本
      {"type":"stopped"}                                被前端取消了
      {"type":"error","message":...}                    说不了话（没 key 之类）
    """
    ready_error = await st.ensure_chat()
    if ready_error:
        await ws.send_json({"type": "error", "message": ready_error})
        return

    chat = st.chat
    assert chat is not None

    # 工具事件是同步回调里来的，先攒着，在流里按顺序发出去
    pending: list[dict] = []
    chat.on_tool_start = lambda name: pending.append({"type": "tool_start", "name": name})
    chat.on_tool_end = lambda name, content: pending.append(
        {"type": "tool_end", "name": name, "content": content}
    )

    chat.add_message(text)
    await ws.send_json({"type": "start"})

    collected = ""
    try:
        async with st.turn_lock:
            async for piece in chat.reply_stream():
                while pending:
                    await ws.send_json(pending.pop(0))
                collected += piece
                await ws.send_json({"type": "delta", "text": piece})
            while pending:
                await ws.send_json(pending.pop(0))
    except asyncio.CancelledError:
        # Chat 自己在被取消时已经把收到的那半截记进历史了，这里只要通知前端 + 落盘
        log.info("这一轮被用户取消，已收到的 %d 字仍保留", len(collected))
        await _safe_send(ws, {"type": "stopped", "text": collected})
        await asyncio.to_thread(st.save_conversation)
        raise
    except Exception as exc:  # pragma: no cover - reply_stream 自己会兜住模型错误
        log.warning("对话异常：%s", type(exc).__name__)
        await _safe_send(ws, {"type": "error", "message": humanize_error(exc)})
        return

    await ws.send_json({"type": "done", "text": collected})
    saved = await asyncio.to_thread(st.save_conversation)
    log.info("这一轮结束：回复 %d 字，历史已存 %d 条", len(collected), saved)


async def _safe_send(ws: WebSocket, payload: dict) -> None:
    """连接已经断了就别再抛异常 —— 取消/关闭时这会很常见。"""
    try:
        await ws.send_json(payload)
    except Exception:
        pass
