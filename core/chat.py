from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from core import memory as memory_mod
from core.llm import humanize_error
from core.paths import resource_path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_TEMPERATURE = 0.8
DEFAULT_MAX_ROUNDS = 4


# ───────────────────────── ReAct 的一步 ─────────────────────────
#
# 整个对话就是一个 ReAct 循环，三步转一圈：
#
#     Thought      模型流式吐出来的那段文字 —— 它"想"了什么
#     Action       它同时提出的 tool_calls —— 它想"做"什么
#     Observation  工具跑出来的结果 —— 作为 tool 消息记回历史，回到 Thought
#
# 转圈的终止条件是「模型没提任何 Action」：那一步想出来的就是最终答案。
# 步数上限是保险丝（见 Chat.max_rounds），最后一步干脆不给工具，保证收得了尾。
#
# 用原生 function calling 表达 ReAct，比让模型写 "Thought:/Action:" 再拿正则
# 去抠结实得多：模型吐的是结构化 JSON，不用解析、也不会因为格式跑偏就让整个
# 循环崩掉。代价是 Action 在 SSE 流里是**分片**来的 —— 第一片给 id 和函数名，
# 后面的片只给 arguments 的一截（'{"pa'、'th": "a'…），要按 index 攒到最后
# 才是完整 JSON（见 _think 末尾）。
#
# 下面几个小结构就是"拼好之后"的样子，字段名故意跟 LiteLLM 给的对象对齐，
# 这样 _run_tool / _make_assistant_message 不用管这次是流的还是非流的。

@dataclass
class _ToolFunction:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _ToolFunction

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


@dataclass
class _Step:
    """走完一步 ReAct 的收成：模型想的（文字）+ 它要做的（行动）。"""

    thought: str = ""
    actions: list = field(default_factory=list)

    @property
    def content(self) -> str:
        """给 _make_assistant_message 用，跟 LiteLLM 的 message 同名。"""
        return self.thought

    @property
    def is_final(self) -> bool:
        """没提出任何行动 —— 这一步想出来的就是最终答案（循环到此为止）。"""
        return not self.actions


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
        keep_recent_turns: int = 0,
        use_memory: bool = True,
    ) -> None:
        """kwargs 里装着怎么调模型（model / api_key / api_base），
        由 core.providers.to_litellm_kwargs() 产出，这里只负责转发。"""
        self.kwargs = kwargs
        self.timeout = timeout
        self.temperature = temperature
        self.max_rounds = max_rounds
        # 一次请求最多带最近几个来回（0 = 全带）。理由见 _prompt_messages 里的实测记录。
        self.keep_recent_turns = keep_recent_turns
        self.use_memory = use_memory

        # 工具：默认空。_tool_schemas 给模型看，_tool_impl 用来真调用
        self._tool_schemas: list[dict] = []
        self._tool_impl: dict = {}
        # 工具执行时的进度回调，默认没有。
        #   on_tool_start(name, args) —— args 是拆好的参数字典；
        #       参数坏掉时给 None（界面照样能显示"它想调哪个工具"）
        #   on_tool_end(name, content) —— content 是喂回模型的那段结果
        # 两个都可以是协程函数：是协程就会被 await。
        #   ★ 界面要"调用时就告诉我"就得靠这个 —— 同步回调没办法在
        #     工具开跑之前把事件发出去，只能攒到下一段文字才一起冒出来。
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
        """ReAct 循环：想 → 做 → 看，直到模型给出最终答案。

        用法（界面里）：
            acc = ""
            async for piece in chat.reply_stream():
                acc += piece            # 数据层在"累加"
                bubble.value = acc      # 界面层在"覆盖"
                page.update()

        每一步的形状（每一步 = 一次模型请求）：

            Thought       _think()    模型边想边说，文字当场流给界面
            ├─ 没提行动   → 这就是最终答案，记历史，收工
            └─ 提了行动   → Action 执行它 → Observation 把结果记回历史 → 下一步

        最多 max_rounds 步；最后一步不给工具，保证一定收得了尾。

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
            # 这一步的文字可能已经随"带工具调用的那条 assistant"记进去了
            # （见下面 collected = "" 那一段），那就别再补一条空的。
            if not collected and self.messages[-1].get("role") == "assistant":
                return
            self.messages.append({"role": "assistant", "content": collected})

        try:
            for step_no in range(self.max_rounds):
                # ── Thought：想 ─────────────────────────────────────────
                # ★ 每一步都开流式 —— 连"先判断要不要调工具"的那几步也一样。
                #   挂了工具之后如果还用非流式去问，模型不调工具时会把整段答案
                #   一次性交出来，界面的打字机效果就整个没了（用户反馈过：
                #   "现在打字机效果好像失效了"）。有工具的代价不该是丢掉流式。
                step = _Step()
                async for piece in self._think(
                    # 最后一步不给工具：这一步只会吐文本，保证能收尾
                    include_tools=step_no < self.max_rounds - 1,
                    sink=step,
                ):
                    collected += piece
                    yield piece         # 出一个字画一个字，不攒

                # ── 没有行动：这一步想出来的就是最终答案，循环到此为止 ──
                if step.is_final:
                    # ★ 这一行是排「模型说了要查却没动静」的唯一线索：日志里
                    #   要是压根没有"要求调用"那行，就说明它没打算调，不是工具坏了。
                    log.info("第 %d 轮：模型没有调用工具，直接回答", step_no + 1)
                    # 注意：这里【不要】再调 append，否则历史里会出现两条 assistant
                    save_to_history()
                    self._refresh_system_prompt()   # 保持 system 是最新的
                    return

                # ── Action：做 ──────────────────────────────────────────
                #   ① 先把这一步的整条记进历史（含它顺口说的那点文字）
                #   ② 再逐个执行，把结果也记进历史
                self.messages.append(self._make_assistant_message(step))
                # 只记工具名：参数是用户自己的东西，不进日志（见 core/logging_setup.py）
                log.info(
                    "第 %d 轮：模型要求调用 %s",
                    step_no + 1,
                    "、".join(action.function.name for action in step.actions),
                )
                # 这一步的文字已经随上面那条消息落进历史了，从 collected 里摘掉，
                # 免得结尾再被 append 成第二条 assistant（取消打断时也不会重复）。
                collected = ""

                # ── Observation：看 ─────────────────────────────────────
                #   结果接回历史，回到循环顶部再想下一步
                self._observe(await self._act(step.actions))

            # 兜底：步数用完了还在要行动（最后一步没给工具，正常走不到这儿）
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

    def _prompt_messages(self) -> list[dict]:
        """这一次请求真正发给模型的那段历史。

        ★ 为什么必须能裁：历史越长，模型越容易"照着自己在这段对话里的习惯"往下走。
        实测（2026-09-26，deepseek-chat，同一条「那oppo呢」）：
          · 带完整 62 条历史 → finish_reason=stop，一次工具都不调，直接凭记忆作答；
          · 只带末尾 10 条    → 老老实实调 web_search；
          · 只带末尾 4 条    → 调了两次 web_search。
        也就是说"编出发布会细节"这类假消息，根子在上下文太长，而不是工具没挂上。

        self.messages 本身**不裁** —— 存盘、界面、记忆抽取看的都还是完整历史，
        这里只决定"这一次请求带多少上下文"，所以裁掉的东西不会真的丢。

        切口一定落在 user 消息上，否则会把「调工具那条 assistant + 它后面几条
        tool 结果」从中间劈开，那种残缺口诀会让接口直接报错。
        """
        rest = self.messages[1:]
        limit = self.keep_recent_turns
        if limit <= 0:
            return self.messages
        starts = [i for i, m in enumerate(rest) if m.get("role") == "user"]
        if len(starts) <= limit:
            return self.messages
        return [self.messages[0], *rest[starts[-limit]:]]

    async def _think(self, *, include_tools: bool, sink: _Step) -> AsyncIterator[str]:
        """ReAct 的 Thought：问一次模型，边想边把字流出去，顺手把它要做的行动拼回来。

        只负责"往外吐字"和"记账"（写进 sink），不碰 self.messages。

        from litellm import ... 写在函数里面，不放文件顶部：
        import litellm 要 7 秒多，放顶部会让窗口启动慢 7 秒。

        关于判空：流式里不是每一块都有内容。
          · 第一块可能只带 role，content 是 None
          · 中间可能夹杂空块（有些服务的"心跳"），choices 是空列表
          · 最后一块带 finish_reason，content 也是 None
        所以每一块都必须判空 —— 这不是保险，是必需品。

        关于行动：它在流里是**分片**来的，第一片给 id 和函数名，后面的片只给
        arguments 的一截。按 index 攒到最后才是完整 JSON，中途拿去 json.loads
        只会得到"参数不是合法 JSON"。名字一般整块给（直接赋值），参数必须
        一截一截接起来 —— 两件事分开写，别图省事都用 +=。
        """
        from litellm import acompletion

        extra: dict = {}
        if include_tools and self._tool_schemas:
            extra["tools"] = self._tool_schemas
            extra["tool_choice"] = "auto"   # 让模型自己决定用不用（兼容性最好）

        stream = await acompletion(
            messages=self._prompt_messages(),   # 只带最近几个来回，见上面那段实测
            temperature=self.temperature,
            timeout=self.timeout,
            stream=True,
            **extra,
            **self.kwargs,                   # ← 必须放最后，否则 Python 语法错误
        )

        parts: dict[int, dict] = {}          # index → {id, name, arguments}

        async for chunk in stream:
            # ① 有些块没有 choices（空块），跳过
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:                # ② 极少见，但别为它崩
                continue

            piece = getattr(delta, "content", None)
            if piece:                        # ③ content 可能是 None（首块 / 末块）
                sink.thought += piece
                yield piece

            for part in getattr(delta, "tool_calls", None) or []:
                slot = parts.setdefault(
                    getattr(part, "index", 0) or 0,
                    {"id": "", "name": "", "arguments": ""},
                )
                if getattr(part, "id", None):
                    slot["id"] = part.id
                func = getattr(part, "function", None)
                if func is None:
                    continue
                if getattr(func, "name", None):
                    slot["name"] = func.name          # 名字一般整块给，赋值即可
                if getattr(func, "arguments", None):
                    slot["arguments"] += func.arguments   # 参数要一截一截接起来

        sink.actions = [
            _ToolCall(id=slot["id"], function=_ToolFunction(slot["name"], slot["arguments"]))
            for _, slot in sorted(parts.items())
            if slot["name"]        # 连名字都没拼出来的残片不算一次调用
        ]

    async def _act(self, actions: list) -> list[dict]:
        """ReAct 的 Action：把这一步提出的行动逐个执行，返回"观察值"。

        ★ 串行、而且严格按模型给的顺序 —— tool 消息要照 tool_calls 的顺序对回去，
          接口才认（界面的卡片也是 FIFO 认领的，见 backend/app.py 的 on_tool_start）。
        ★ 每一步都一定有观察值：_run_tool 约定不抛异常，出错也把原因翻成人话
          当结果带回来，所以循环不会因为一次失败就断掉。
        """
        return [await self._run_tool(action) for action in actions]

    def _observe(self, observations: list[dict]) -> None:
        """ReAct 的 Observation：把观察值接回历史，然后回循环顶部再想下一步。

        单独拎出来是因为"做"和"看"是两件事：以后要给观察值做摘要、去重、
        或者超长时换一种喂法（现在全靠 _clip），都改这一处，不碰执行那半边。
        """
        self.messages.extend(observations)

    def _make_assistant_message(self, answer) -> dict:
        """把模型的回复转成能塞进 messages 的字典。

        ReAct 的"一步"(_Step) 把行动放在 actions 上；这里也认 tool_calls 这个
        拼写 —— 字段名跟 LiteLLM 的对象对齐，流的和非流的就都能喂进来。
        """
        msg: dict = {"role": "assistant", "content": answer.content or ""}
        calls = getattr(answer, "actions", None) or getattr(answer, "tool_calls", None)
        if calls:
            # 用 model_dump() 转，别自己手写字典（容易漏 type 字段）
            msg["tool_calls"] = [c.model_dump() for c in calls]
        return msg

    async def _run_tool(self, call) -> dict:
        """执行一个工具，返回一条 tool 消息。

        关键：工具出错**不抛异常**，而是把错误当成结果喂回去，
        让模型有机会自己改正。
        """
        name = call.function.name

        try:
            # arguments 是 JSON 字符串，要拆成字典
            # or "{}" 兜住空字符串（工具不需要参数时可能给空串）
            args: dict | None = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
        except json.JSONDecodeError as exc:
            args = None
            content = f"错误：参数不是合法 JSON（{exc.msg}）。请重新调用。"
        except Exception:
            args = None
            content = "错误：参数必须是 JSON 对象"
        else:
            content = ""

        # 参数拆好了才通知界面 —— 卡片上要写「执行的命令」，光有名字不够。
        # 参数坏掉时也照常通知（args 给 None）：用户至少该看到它想调哪个工具，
        # 而不是界面上毫无动静、只等来一句报错。
        # ★ 这里是**先通知再执行**：回调可以等，所以卡片是在工具真的开跑之前
        #   就画出来的（后端把它挂成了协程函数，等着它把事件发出去）。
        if self.on_tool_start:
            try:
                started = self.on_tool_start(name, args)
                if inspect.isawaitable(started):
                    await started
            except Exception:
                pass

        if not content:
            try:
                impl = self._tool_impl.get(name)
                if impl is None:
                    content = f"错误：没有名为 {name} 的工具"
                else:
                    result = await impl(**args)
                    # tool 消息的 content 必须是字符串
                    content = (
                        result
                        if isinstance(result, str)
                        else json.dumps(result, ensure_ascii=False)
                    )
            except TypeError as exc:
                content = f"错误：参数对不上（{exc}）。请检查参数名和类型。"
            except Exception as exc:
                # 只给类型，不给完整报错 —— 用户会看到，模型还会复述一遍
                content = f"错误：工具执行失败（{type(exc).__name__}）"

        if self.on_tool_end:
            try:
                finished = self.on_tool_end(name, content)
                if inspect.isawaitable(finished):
                    await finished
            except Exception:
                pass

        return {"role": "tool", "tool_call_id": call.id, "content": content}
