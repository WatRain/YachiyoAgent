from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from core import memory as memory_mod
from core.llm import humanize_error
from core.paths import resource_path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_TEMPERATURE = 0.8
DEFAULT_MAX_ROUNDS = 4


def system_prompt() -> str:
    """读八千代的人格设定。

    用 resource_path 而不是 Path("prompt.md")：打包成 exe 之后，
    只有前者能找到文件。
    """
    return resource_path("prompt.md").read_text(encoding="utf-8")


class Chat:
    """一轮对话。

    ★ 唯一的对话状态就是 self.messages。
      想保留上下文，就必须一直用同一个 Chat 对象
      （别把它建在 while 循环里面）。

    工具：默认一个都不带（self._tool_schemas 是空的），
      所以现在跑起来和纯聊天完全一样。
      以后要加工具，用 add_tools(TOOLS, TOOL_IMPL) 挂上去就行。
    """

    def __init__(
        self,
        kwargs: dict,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        temperature: float = DEFAULT_TEMPERATURE,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        use_memory: bool = True,
    ) -> None:
        """kwargs 里装着怎么调模型（model / api_key / api_base），
        由 core.providers.to_litellm_kwargs() 产出，这里只负责转发。"""
        self.kwargs = kwargs
        self.timeout = timeout
        self.temperature = temperature
        self.max_rounds = max_rounds
        self.use_memory = use_memory

        # 工具：默认空。_tool_schemas 给模型看，_tool_impl 用来真调用
        self._tool_schemas: list[dict] = []
        self._tool_impl: dict = {}
        # 工具执行时的进度回调（界面可以显示"正在查询…"），默认没有
        self.on_tool_start = None
        self.on_tool_end = None

        # 对话历史，第一项永远是 system（人格设定）
        self.messages: list[dict] = [{"role": "system", "content": system_prompt()}]

    # ─────────────────────────────────────────────
    #  公开接口
    # ─────────────────────────────────────────────

    def add_message(self, text: str) -> None:
        """把用户说的话追加进历史。"""
        self.messages.append({"role": "user", "content": text})

    def add_tools(self, schemas: list[dict], impl: dict) -> None:
        """挂上工具。

        schemas 是给模型看的"菜单"（JSON 格式），impl 是"名字 → 真函数"的对照表。
        不调用这个方法，就是一个纯聊天机器人。
        """
        self._tool_schemas = list(schemas)
        self._tool_impl = dict(impl)

    def clear_tools(self) -> None:
        self._tool_schemas = []
        self._tool_impl = {}

    async def reply(self) -> str:
        """问模型，等它说完，把回复记进历史，返回完整文本。

        出错时不会抛异常，而是把一句人话作为返回值给你。
        """
        collected = ""
        async for piece in self.reply_stream():
            collected += piece
        return collected

    async def reply_stream(self) -> AsyncIterator[str]:
        """流式版：模型吐一块就交出一块。

        用法（界面里）：
            acc = ""
            async for piece in chat.reply_stream():
                acc += piece            # 数据层在"累加"
                bubble.value = acc      # 界面层在"覆盖"
                page.update()

        ★ 不管是正常结束、出错、还是中途被取消（用户点"停止"），
          已收到的内容都会被记进历史 ——
          否则下一轮模型会以为自己没说过话，上下文就断了。
        """
        self._refresh_system_prompt()   # 把最新的长期记忆拼进 system

        collected = ""      # 已经交给调用者的文字（记进历史的就是它）
        saved = False

        def save_to_history() -> None:
            nonlocal saved
            if saved:
                return
            saved = True
            self.messages.append({"role": "assistant", "content": collected})

        try:
            # ── 第一种情况：没挂工具 ──
            # 那就没有"要不要调工具"要判断，直接开流式 ——
            # 这样从第一个字就开始出，不用等整段说完。
            if not self._tool_schemas:
                async for piece in self._stream_model(include_tools=False):
                    collected += piece
                    yield piece
                save_to_history()
                return

            # ── 第二种情况：挂了工具，走"多轮 + 工具调用" ──
            for round_no in range(self.max_rounds):
                last_chance = (round_no == self.max_rounds - 1)

                # 最后一轮：不给工具 + 开流式，保证它只会吐文本
                if last_chance:
                    async for piece in self._stream_model(include_tools=False):
                        collected += piece
                        yield piece
                    save_to_history()
                    return

                # 前面几轮：非流式，看它要不要调工具
                answer = await self._call_model(include_tools=True)

                if getattr(answer, "tool_calls", None):
                    # 它要求调工具：
                    #   ① 先把它的要求整条记进历史（这是保存，不算 collected）
                    #   ② 再逐个执行，把结果也记进历史
                    self.messages.append(self._make_assistant_message(answer))
                    for call in answer.tool_calls:
                        self.messages.append(await self._run_tool(call))
                    continue        # 回到循环顶部，再问一次

                # ── 它没要求调工具 → 这就是最终答案 ──
                # 注意：这里【不要】再调 append，否则历史里会出现两条 assistant
                content = answer.content or ""
                collected += content
                yield content
                save_to_history()
                self._refresh_system_prompt()   # 保持 system 是最新的
                return

            # 轮次用完了它还在要工具 → 不给工具，强行要个答案
            async for piece in self._stream_model(include_tools=False):
                collected += piece
                yield piece
            save_to_history()
        except Exception as exc:
            # 出错也保留已收到的部分 —— 用户不该丢掉已经看到的字
            # 记下异常类型 + 消息（只记类型的话，打包后出了问题只能看到
            # "Chat 失败: ValueError"，等于没线索）；traceback 放 DEBUG。
            log.warning("Chat 失败: %s: %s", type(exc).__name__, exc)
            log.debug("Chat 失败的详细调用栈", exc_info=True)
            message = humanize_error(exc)
            collected += message
            save_to_history()
            yield message
        except BaseException:
            # 被取消走这里。
            # 注意：CancelledError 继承自 BaseException 而不是 Exception，
            # 所以要单独接住，否则取消时历史里会缺一条 assistant。
            save_to_history()
            raise

    # ─────────────────────────────────────────────
    #  记忆
    # ─────────────────────────────────────────────

    def _refresh_system_prompt(self) -> None:
        """把长期记忆拼进第 0 条（system）消息。

        每次都重读人格卡有点浪费，但好处是"数据只有一个来源"，
        不会出现 messages 里的人格和 prompt.md 不一致的情况。
        """
        base = system_prompt()
        if self.use_memory:
            base += memory_mod.format_memories(memory_mod.all_memories())
        self.messages[0] = {"role": "system", "content": base}

    async def remember_this_turn(self) -> int:
        """从最后一轮对话里抽取长期记忆。返回写入条数。

        ★ 只在一个回合**结束之后**调用（流式跑完再说），
          否则会拿到半截内容。
        """
        if not self.use_memory:
            return 0

        # 从后往前找最后一条 assistant，和它前面最近的 user
        last_user = ""
        last_assistant = ""
        for msg in reversed(self.messages):
            if msg["role"] == "assistant" and not last_assistant:
                last_assistant = msg.get("content") or ""
            elif msg["role"] == "user" and not last_user:
                last_user = msg.get("content") or ""
                break

        if not last_user or not last_assistant:
            return 0

        items = await memory_mod.extract_memories(self, last_user, last_assistant)
        if not items:
            return 0
        return memory_mod.save_extracted(items)

    # ─────────────────────────────────────────────
    #  内部
    # ─────────────────────────────────────────────

    async def _call_model(self, *, include_tools: bool):
        """调模型（非流式），返回它的 message 对象。

        from litellm import ... 写在函数里面，不放文件顶部：
        import litellm 要 7 秒多，放顶部会让窗口启动慢 7 秒。
        """
        from litellm import acompletion

        extra: dict = {"stream": False}
        if include_tools and self._tool_schemas:
            extra["tools"] = self._tool_schemas
            extra["tool_choice"] = "auto"   # 让模型自己决定用不用（兼容性最好）

        response = await acompletion(
            messages=self.messages,          # 整个历史都发过去（这就是"记忆"）
            temperature=self.temperature,
            timeout=self.timeout,
            **extra,
            **self.kwargs,                   # ← 必须放最后，否则 Python 语法错误
        )
        return response.choices[0].message

    async def _stream_model(self, *, include_tools: bool) -> AsyncIterator[str]:
        """调模型（流式），一块一块吐出文本。

        只负责"往外吐字"，不碰 self.messages。

        关于判空：流式里不是每一块都有文字。
          · 第一块可能只带 role，content 是 None
          · 中间可能夹杂空块（有些服务的"心跳"），choices 是空列表
          · 最后一块带 finish_reason，content 也是 None
        所以每一块都必须判空 —— 这不是保险，是必需品。
        """
        from litellm import acompletion

        extra: dict = {}
        if include_tools and self._tool_schemas:
            extra["tools"] = self._tool_schemas
            extra["tool_choice"] = "auto"

        stream = await acompletion(
            messages=self.messages,
            temperature=self.temperature,
            timeout=self.timeout,
            stream=True,
            **extra,
            **self.kwargs,
        )

        async for chunk in stream:
            # ① 有些块没有 choices（空块），跳过
            if not chunk.choices:
                continue
            # ② content 可能是 None（首块只报身份、末块只报结束）
            piece = chunk.choices[0].delta.content
            if piece:
                yield piece

    def _make_assistant_message(self, answer) -> dict:
        """把模型的回复转成能塞进 messages 的字典。"""
        msg: dict = {"role": "assistant", "content": answer.content or ""}
        if getattr(answer, "tool_calls", None):
            # 用 model_dump() 转，别自己手写字典（容易漏 type 字段）
            msg["tool_calls"] = [c.model_dump() for c in answer.tool_calls]
        return msg

    async def _run_tool(self, call) -> dict:
        """执行一个工具，返回一条 tool 消息。

        关键：工具出错**不抛异常**，而是把错误当成结果喂回去，
        让模型有机会自己改正。
        """
        if self.on_tool_start:
            try:
                self.on_tool_start(call.function.name)
            except Exception:
                pass

        try:
            # arguments 是 JSON 字符串，要拆成字典
            # or "{}" 兜住空字符串（工具不需要参数时可能给空串）
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")

            impl = self._tool_impl.get(call.function.name)
            if impl is None:
                content = f"错误：没有名为 {call.function.name} 的工具"
            else:
                result = await impl(**args)
                # tool 消息的 content 必须是字符串
                content = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, ensure_ascii=False)
                )
        except json.JSONDecodeError as exc:
            content = f"错误：参数不是合法 JSON（{exc.msg}）。请重新调用。"
        except TypeError as exc:
            content = f"错误：参数对不上（{exc}）。请检查参数名和类型。"
        except Exception as exc:
            # 只给类型，不给完整报错 —— 用户会看到，模型还会复述一遍
            content = f"错误：工具执行失败（{type(exc).__name__}）"

        if self.on_tool_end:
            try:
                self.on_tool_end(call.function.name, content)
            except Exception:
                pass

        return {"role": "tool", "tool_call_id": call.id, "content": content}
