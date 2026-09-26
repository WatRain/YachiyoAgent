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
import json
import logging
import os
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
from core.llm import humanize_error, list_models, test_connection, warm_litellm
from core.paths import PROJECT_ROOT, config_path, renderer_dir
from core.secrets import SecretStore, backend_label_of
from core.tools import (
    ALL_TOOLS,
    build_tools,
    tool_command,
    tool_failed,
    tool_label,
    tool_result,
)

from backend.live2d import Live2DInfo
from backend.live2d import mount as mount_live2d

log = logging.getLogger(__name__)

API_VERSION = 1

# 危险工具的审批弹窗最多等这么久（秒）。等不到按拒绝处理 ——
# 不能让一轮对话因为用户走开了就永远握着 turn_lock。
TOOL_ASK_TIMEOUT = 300.0

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
    # 工具调用档位与 allow/deny（见 core/tools.py）
    "tools",
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
        """当前对话里要落盘的部分：user / assistant / tool 一条不落。

        ★ 连工具轮一起存（以前只存 user / assistant）。只存一半的后果实测过：
          重启后历史里就剩「我查一下…」这类半句，序列是残的；模型看到一整段
          「说了要查、后面什么都没有」的样板会照着抄；界面上那些工具卡片也
          全都蒸发了 —— 用户的原话就是「重新开启应用后，之前调用过的工具看不到」。
          工具结果是喂回模型的东西，和助手回答一样属于这段对话。
        tool_calls 必须留着：有它，重启后的序列才是
        「assistant 调工具 → tool 给结果 → assistant 回答」这种接口认得的形状。
        """
        if self.chat is None:
            return []
        out: list[dict] = []
        for m in self.chat.messages:
            role = m.get("role")
            content = m.get("content")
            text = content if isinstance(content, str) else ""
            if role == "user":
                if text:
                    out.append({"role": "user", "content": text})
            elif role == "assistant":
                entry: dict = {"role": "assistant", "content": text}
                if m.get("tool_calls"):
                    entry["tool_calls"] = m["tool_calls"]
                if text or entry.get("tool_calls"):
                    out.append(entry)
            elif role == "tool" and m.get("tool_call_id"):
                out.append({
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "name": m.get("name") or "",
                    "content": text,
                })
        return out

    def conversation_items(self) -> list[dict]:
        """给界面看的对话流：气泡和工具卡片混在一条流里，顺序就是发生的顺序。

        跟 history() 的区别：history() 是喂模型和存盘用的原始形状，
        这里是翻成「人能直接画出来」的形状 —— 工具条目带上
        label / command / result / failed，界面拿到就能画，不必自己解释参数
        （翻译只住在 core/tools.py，这是 README 第三条设计规矩）。

        command 要用那次调用**真正的参数**，而参数在前一条 assistant 的
        tool_calls 里，所以边扫边攒一张 id → 参数的账。
        """
        out: list[dict] = []
        args_of: dict[str, dict | None] = {}
        for item in store.load_conversation():
            role = item.get("role")
            if role == "tool":
                name = item.get("name") or ""
                content = item.get("content") or ""
                args = args_of.get(item.get("tool_call_id") or "")
                out.append({
                    "role": "tool",
                    "name": name,
                    "label": tool_label(name),
                    "command": tool_command(name, args) if args is not None else "",
                    "result": tool_result(content),
                    "failed": tool_failed(content),
                })
                continue
            if role == "assistant":
                for call in item.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        parsed = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        parsed = None
                    args_of[call.get("id") or ""] = parsed if isinstance(parsed, dict) else None
                if item.get("content"):
                    out.append({"role": "assistant", "content": item["content"]})
                continue
            if role == "user" and item.get("content"):
                out.append({"role": "user", "content": item["content"]})
        return out

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
            # 只把最近几个来回发给模型（理由见 core/chat.py 的 _prompt_messages）
            keep_recent_turns=self.cfg.keep_recent_turns,
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
        if assets:
            # SDK 导入较慢，放后台预热以免拖慢窗口启动；首轮若抢在预热前，
            # get_litellm_acompletion() 会在线程池等待同一次导入。
            _app.state.litellm_warmup_task = asyncio.create_task(
                warm_litellm(), name="litellm-warmup"
            )
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
            # 界面上要画的那条流：气泡 + 工具卡片（tool 条目已翻好 label/command/result）
            "conversation": st.conversation_items(),
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

    # ─────────────────────── 工具 ───────────────────────
    @app.get("/api/tools", dependencies=guard)
    async def get_tools() -> dict:
        """工具目录 + 当前档位。设置界面拿它画开关。

        目录是从 core/tools.py 现场读的，不在前端抄一份 ——
        否则加了工具忘了同步界面，用户就永远看不到。
        """
        return {
            "profile": st.cfg.tools.profile,
            "allow": st.cfg.tools.allow,
            "deny": st.cfg.tools.deny,
            "confirm": st.cfg.tools.confirm,
            "catalog": [
                {
                    "name": t.name,
                    "label": t.label,
                    "description": t.description,
                    "safe": t.safe,
                    "needs_confirm": t.preview is not None,
                }
                for t in ALL_TOOLS
            ],
        }

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
        return {"messages": st.conversation_items()}

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
        # 危险工具正在等用户点头的请求：id -> Future[bool]。
        # 必须建在这里（每条连接一份），因为审批回答是从这个接收循环里来的，
        # 而 _run_turn 是另一个 task。
        approvals: dict[str, asyncio.Future] = {}
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

                if kind == "tool_decision":
                    # 用户在弹窗上点了同意/拒绝。id 对不上就忽略（可能是上一轮迟到的回答）。
                    fut = approvals.get(str(msg.get("id") or ""))
                    if fut is not None and not fut.done():
                        fut.set_result(bool(msg.get("ok")))
                    continue

                if kind != "user":
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if turn is not None and not turn.done():
                    await ws.send_json({"type": "error", "message": "上一句还在说呢，稍等一下。"})
                    continue
                turn = asyncio.create_task(_run_turn(ws, st, text, approvals))
                turn.add_done_callback(_swallow)
        finally:
            # 连接断了就别让工具那头继续等着
            for fut in list(approvals.values()):
                if not fut.done():
                    fut.set_result(False)
            approvals.clear()
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


def _make_approver(
    ws: WebSocket,
    st: BackendState,
    approvals: dict[str, asyncio.Future] | None,
) -> Callable[[str, dict, str], object] | None:
    """造一个「问用户同不同意」的回调，交给 core/tools.py 里的 Toolbox。

    返回 None 表示**没有确认能力** —— 那时 core/tools.py 会拒绝所有危险工具。
    这是有意的 fail-closed：宁可少做一件事，也不能替用户按了回车。
    """
    if approvals is None or not st.cfg.tools.confirm:
        return None

    async def ask(name: str, args: dict, preview: str) -> bool:
        aid = os.urandom(6).hex()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        approvals[aid] = fut
        try:
            await ws.send_json({
                "type": "tool_ask",
                "id": aid,
                "name": name,
                "label": tool_label(name),
                "preview": preview,
            })
        except Exception:
            approvals.pop(aid, None)
            return False
        try:
            # 用户一直不理也不能把这一轮永远挂住（turn_lock 还握着呢）。
            # 等不到就按拒绝处理。
            return await asyncio.wait_for(fut, timeout=TOOL_ASK_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("工具审批等超时了，按拒绝处理：%s", name)
            return False
        finally:
            approvals.pop(aid, None)

    return ask


async def _run_turn(
    ws: WebSocket,
    st: BackendState,
    text: str,
    approvals: dict[str, asyncio.Future] | None = None,
) -> None:
    """跑一轮：用户说 → 模型流式回 → 落盘。

    事件协议（前端只认这几种）：
      {"type":"start"}                                  可以开始清空气泡了
      {"type":"delta","text":...}                       一段增量文字
      {"type":"tool_start","name":...,"label":...,"command":...}
                                                        模型要调工具了，
                                                        command 是那句"执行了什么"
      {"type":"tool_end","name":...,"result":...,"failed":...}
                                                        工具返回了；result 是压成
                                                        一句话的摘要（原始结果可能
                                                        上万字），failed 只影响
                                                        卡片长什么样
      {"type":"live2d_control","expression":...,"motion":...}
                                                        有限的角色表情 / 动作意图
      {"type":"tool_ask","id":...,"name":...,"label":...,"preview":...}
                                                        要动真格了，等用户点头
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

    # 工具事件直接从回调里发出去，不攒着 ——
    # 这两个回调是协程函数，core/chat.py 的 _run_tool 会 await 它们，
    # 所以「卡片出现」就是「模型刚决定要调它」的那一刻，而不是等这一轮的文字
    # 吐完才一起冒出来（用户反馈：卡片和答案是一次性全出来的，看着像没在干活）。
    # command 是 core 那边翻好的"执行了什么"，界面直接拿去显示 ——
    # 界面层不该自己解释参数（那是 core/providers.py 那条规矩的同一条：翻译只有一处）。
    async def on_tool_start(name: str, args: dict | None) -> None:
        nonlocal collected
        # 工具一到，上一段文字就收笔了：done 只该带最后一段。
        # 不重置的话，「我查一下…」会被原样粘在答案前面 ——
        # 界面上表现为答案里先重复一遍宣告语（前端那边同时会把气泡收笔，
        # 让答案另起一个落在卡片下面）。
        collected = ""
        await _safe_send(ws, {
            "type": "tool_start",
            "name": name,
            "label": tool_label(name),
            "command": tool_command(name, args),
        })

    async def on_tool_end(name: str, content: str) -> None:
        # 只发压过的一句话，不发整份结果：run_command 的上限是两万字，
        # 全套序列化过去只为在卡片上显示一行，纯属白烧内存。
        await _safe_send(ws, {
            "type": "tool_end",
            "name": name,
            "result": tool_result(content),
            "failed": tool_failed(content),
        })

    async def on_live2d_control(expression: str | None, motion: str | None) -> bool:
        try:
            await ws.send_json({
                "type": "live2d_control",
                "expression": expression,
                "motion": motion,
            })
            return True
        except Exception:
            return False

    chat.on_tool_start = on_tool_start
    chat.on_tool_end = on_tool_end

    # 工具每轮现挂：approve 要绑在当前这条连接上（审批弹窗是发给它的），
    # 而档位可能刚被用户在设置里改过。
    # 挂工具失败不该让整轮变成哑巴 —— 降级成纯聊天，原因留在日志里。
    # （这里出错要是直接抛出去，任务会静悄悄死掉，前端只会看到永远不来的回复。）
    try:
        chat.add_tools(*build_tools(
            st.cfg.tools.profile,
            approve=_make_approver(ws, st, approvals),
            live2d_control=on_live2d_control,
            allow=st.cfg.tools.allow,
            deny=st.cfg.tools.deny,
        ))
    except Exception as exc:
        log.warning("挂载工具失败，这一轮退化成纯聊天：%s", exc)
        try:
            chat.clear_tools()
        except Exception:
            log.debug("clear_tools 也不可用，忽略", exc_info=True)

    chat.add_message(text)
    await ws.send_json({"type": "start"})

    collected = ""
    try:
        async with st.turn_lock:
            async for piece in chat.reply_stream():
                collected += piece
                await ws.send_json({"type": "delta", "text": piece})
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
