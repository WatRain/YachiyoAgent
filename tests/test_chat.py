"""core/chat.py 的流式 + 工具调用。

为什么值得单独测：这两件事以前是**打架**的 ——
挂了工具之后，"先问要不要调工具"那一轮走的是非流式，模型不调工具时
整段答案一次性交出来，界面的打字机效果整个没了（用户反馈过）。
现在每一轮都开流式，代价是工具调用在流里变成**分片**来的：
第一片给 id 和函数名，后面的片只给 arguments 的一截，要按 index 拼回去。
这两点都不靠真模型，用假流测。
"""

from types import SimpleNamespace

import litellm
import pytest

from core.chat import Chat, _Step, _ToolCall, _ToolFunction

# ───────────────────────── 假流 ─────────────────────────


def _chunk(text=None, tool_calls=None, empty=False):
    """一个流式块，长得跟 LiteLLM 给的一样（只带我们真正用到的字段）。"""
    delta = SimpleNamespace(content=text, tool_calls=tool_calls)
    choices = [] if empty else [SimpleNamespace(delta=delta)]
    return SimpleNamespace(choices=choices)


def _slice(index, *, id=None, name=None, arguments=None):
    """工具调用的**一片** —— 真流里参数就是这么一截一截来的。"""
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _fake(monkeypatch, *scripts):
    """把 litellm.acompletion 换成照剧本演出的假流；每次调用吃一份剧本。

    返回 seen：每次调用收到的 kwargs，用来断言"这一轮到底给没给工具"。
    """
    seen = []
    queue = list(scripts)

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        script = queue.pop(0) if queue else []

        async def stream():
            for chunk in script:
                yield chunk

        return stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


def _chat(**kwargs):
    """没有长期记忆的 Chat —— system 读的是仓库里的 prompt.md。"""
    return Chat({"model": "openai/test", "api_key": "x"}, use_memory=False, **kwargs)


SCHEMA = {"type": "function", "function": {"name": "demo", "parameters": {}}}


async def _ok(**kwargs) -> str:
    """一个"什么都不做"的工具。

    ★ 必须是协程 —— core/chat.py 的 _run_tool 是 `await impl(**args)`，
      同步函数会当场炸成"参数对不上（'str' object can't be awaited）"。
    """
    return "ok"


# ───────────────────── 打字机（这次要修的那条） ─────────────────────


async def test_the_answer_streams_piece_by_piece_even_with_tools_wired(monkeypatch):
    """★ 挂了工具也必须一块一块地出。

    以前这一轮是非流式的，模型没调工具时 yield 出来的是**一整段**，
    界面就只能等它说完再一次性画出来 —— 打字机效果没了。
    """
    _fake(monkeypatch, [_chunk("你"), _chunk("好"), _chunk("呀")])

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("在吗")

    pieces = [piece async for piece in chat.reply_stream()]

    assert pieces == ["你", "好", "呀"]          # 不是 ["你好呀"]
    assert chat.messages[-1] == {"role": "assistant", "content": "你好呀"}


async def test_empty_chunks_and_none_content_are_skipped(monkeypatch):
    """心跳空块、只报 role 的首块、只报 finish_reason 的末块，都不能当成字。"""
    _fake(monkeypatch, [
        _chunk(text=None),                  # 首块只报身份
        _chunk(empty=True),                 # 空块（有些服务的心跳）
        _chunk("嗯"),
        _chunk(text=None),                  # 末块只有 finish_reason
    ])

    chat = _chat()
    chat.add_message("在吗")

    assert [piece async for piece in chat.reply_stream()] == ["嗯"]


# ───────────────────── 工具调用：分片要拼回来 ─────────────────────


async def test_tool_call_slices_are_assembled_into_one_call(monkeypatch):
    """参数分三片来，到工具手里必须是一条完整的 JSON。"""
    ran = []

    async def demo(**kwargs):
        ran.append(kwargs)
        return "ok"

    _fake(
        monkeypatch,
        [   # 第一轮：要调工具，参数被切成三片
            _chunk(tool_calls=[_slice(0, id="call_1", name="demo")]),
            _chunk(tool_calls=[_slice(0, arguments='{"tz"')]),
            _chunk(tool_calls=[_slice(0, arguments=': "Asia/Shanghai"}')]),
        ],
        [_chunk("现在"), _chunk("可以了")],      # 第二轮：拿到结果才开口
    )

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": demo})
    chat.add_message("试试")

    pieces = [piece async for piece in chat.reply_stream()]

    assert ran == [{"tz": "Asia/Shanghai"}]
    assert "".join(pieces) == "现在可以了"
    # 历史必须是 assistant(带 tool_calls) → tool → assistant 三段齐全，
    # 少一段模型下一轮就不知道自己调过工具、也不知道结果是什么。
    assert [m["role"] for m in chat.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert chat.messages[2]["tool_calls"][0]["function"] == {
        "name": "demo", "arguments": '{"tz": "Asia/Shanghai"}',
    }
    assert chat.messages[2]["tool_calls"][0]["type"] == "function"
    assert chat.messages[3]["tool_call_id"] == "call_1"


async def test_several_tool_calls_in_one_round_stay_in_order(monkeypatch):
    """一轮里叫了好几个工具时，顺序不能乱（界面的卡片是 FIFO 认领的）。"""
    _fake(
        monkeypatch,
        [_chunk(tool_calls=[
            _slice(0, id="a", name="demo", arguments='{"n": 1}'),
            _slice(1, id="b", name="demo", arguments='{"n": 2}'),
        ])],
        [_chunk("都好了")],
    )

    async def echo_n(**kwargs):
        return f"n={kwargs['n']}"

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": echo_n})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    tools = [m for m in chat.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["a", "b"]
    assert [m["content"] for m in tools] == ["n=1", "n=2"]


async def test_a_slice_without_a_name_is_not_a_call(monkeypatch):
    """只有半截参数、名字都没拼出来的残片，不能当成一次调用去执行。"""
    ran = []
    _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, arguments='{"a": 1}')]), _chunk("算了")],
    )

    chat = _chat()
    async def spy(**kwargs):
        ran.append(kwargs)
        return "ok"

    chat.add_tools([SCHEMA], {"demo": spy})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    assert ran == []
    assert all(m["role"] != "tool" for m in chat.messages)


# ───────────────────── 界面回调：协程要被 await ─────────────────────


async def test_the_interface_is_told_before_the_tool_actually_runs(monkeypatch):
    """★ 「调用时就告诉我」的全部机制：on_tool_start 一定是**先于**工具执行的。

    后端把它挂成了协程函数，core 会 await 它 —— 事件当场发出去，
    用户看到的卡片就是"模型刚决定要调它"，而不是等工具跑完才补画一张。
    """
    order = []

    async def demo(**kwargs):
        order.append("ran")
        return "ok"

    async def on_start(name, args):
        order.append(("start", name, args))

    async def on_end(name, content):
        order.append(("end", content))

    _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, id="c", name="demo", arguments='{"a": 1}')])],
        [_chunk("好了")],
    )

    chat = _chat()
    chat.on_tool_start = on_start
    chat.on_tool_end = on_end
    chat.add_tools([SCHEMA], {"demo": demo})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    assert order[0] == ("start", "demo", {"a": 1})   # ★ 先通知界面
    assert order[1] == "ran"                          # 然后才真跑
    assert order[2] == ("end", "ok")                  # 跑完再收尾


async def test_a_plain_sync_callback_still_works(monkeypatch):
    """回调不是协程也照样能用 —— 不是每个调用方都需要"当场发出去"。"""
    seen = []
    _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, id="c", name="demo", arguments="{}")])],
        [_chunk("好了")],
    )

    chat = _chat()
    chat.on_tool_start = lambda name, args: seen.append(("start", name))
    chat.on_tool_end = lambda name, content: seen.append(("end", content))
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    assert seen == [("start", "demo"), ("end", "ok")]


async def test_a_broken_callback_never_breaks_the_turn(monkeypatch):
    """界面那边炸了（连接断了之类）不能让整轮对话跟着死。"""
    _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, id="c", name="demo", arguments="{}")])],
        [_chunk("好了")],
    )

    def boom(*_args):
        raise RuntimeError("界面没了")

    async def boom_async(*_args):
        raise RuntimeError("界面没了")

    chat = _chat()
    chat.on_tool_start = boom
    chat.on_tool_end = boom_async
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("试试")

    assert "".join([piece async for piece in chat.reply_stream()]) == "好了"


async def test_broken_arguments_are_still_reported_to_the_interface(monkeypatch):
    """参数不是合法 JSON 时也要通知界面（args 给 None）：
    用户至少该看到"它想调哪个工具"，而不是界面毫无动静只等来一句报错。"""
    seen = []
    _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, id="c", name="demo", arguments="{不是 JSON")])],
        [_chunk("我改一下")],
    )

    chat = _chat()
    chat.on_tool_start = lambda name, args: seen.append((name, args))
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    assert seen == [("demo", None)]
    tool_msg = next(m for m in chat.messages if m["role"] == "tool")
    assert tool_msg["content"].startswith("错误：参数不是合法 JSON")


# ───────────────────── 轮次与降级 ─────────────────────


async def test_tools_are_taken_away_on_the_last_round(monkeypatch):
    """模型一直要工具时，最后一轮得把工具撤掉，否则永远收不了尾。"""
    seen = _fake(
        monkeypatch,
        *[
            [_chunk(tool_calls=[_slice(0, id=f"c{i}", name="demo", arguments="{}")])]
            for i in range(3)
        ],
        [_chunk("好了")],
    )

    chat = _chat(max_rounds=4)
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("试试")

    [piece async for piece in chat.reply_stream()]

    assert len(seen) == 4
    assert all("tools" in call for call in seen[:3])
    assert "tools" not in seen[3]


async def test_tools_are_never_sent_when_none_are_wired(monkeypatch):
    """纯聊天（没挂工具）时不该往请求里塞 tools / tool_choice。"""
    seen = _fake(monkeypatch, [_chunk("嗯")])

    chat = _chat()
    chat.add_message("在吗")

    [piece async for piece in chat.reply_stream()]

    assert "tools" not in seen[0]
    assert "tool_choice" not in seen[0]


async def test_a_preamble_before_a_tool_call_is_kept_where_it_belongs(monkeypatch):
    """模型先说了半句再调工具时，那半句要跟 tool_calls 存进**同一条** assistant。

    既不能丢，也不能在结尾被重复存成第二条 assistant（顺手验证 collected 的归零）。
    """
    _fake(
        monkeypatch,
        [
            _chunk("让我看看"),
            _chunk(tool_calls=[_slice(0, id="c", name="demo", arguments="{}")]),
        ],
        [_chunk("好了")],
    )

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("试试")

    pieces = [piece async for piece in chat.reply_stream()]

    assert pieces == ["让我看看", "好了"]       # 前半句也是当场流出来的
    assert [m["role"] for m in chat.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert chat.messages[2]["content"] == "让我看看"
    assert chat.messages[2]["tool_calls"][0]["id"] == "c"
    assert chat.messages[4]["content"] == "好了"   # 不是"让我看看好了"


# ────────── 发给模型的历史要能裁（"假消息"的根在上下文太长） ──────────


def _fill(chat, turns: int) -> None:
    """塞 turns 个来回，全是普通问答。"""
    for i in range(turns):
        chat.add_message(f"问{i}")
        chat.messages.append({"role": "assistant", "content": f"答{i}"})


def test_prompt_history_is_trimmed_to_the_recent_turns():
    """★ keep_recent_turns 生效：只把最近几个来回发给模型。

    为什么这不是"省 token 的优化"，而是**功能开关** ——
    2026-09-26 实测（deepseek-chat，同一条「那oppo呢」）：
      · 带完整 62 条历史 → finish_reason=stop，一次工具都不调，凭记忆编出发布会日期；
      · 只带末尾 10 条    → 老老实实调 web_search；
      · 只带末尾 4 条    → 调了两次 web_search。
    上下文一长，模型就照着自己在这段对话里的习惯往下滑，工具白挂了。
    """
    chat = _chat(keep_recent_turns=2)
    _fill(chat, 4)

    sent = chat._prompt_messages()

    assert sent[0]["role"] == "system"        # 人格设定永远在
    assert [m["content"] for m in sent[1:]] == ["问2", "答2", "问3", "答3"]
    # ★ self.messages 自己不裁：存盘、界面、记忆抽取看的还是完整历史
    assert len(chat.messages) == 9
    assert chat.messages[1]["content"] == "问0"


def test_prompt_history_never_splits_a_tool_round():
    """切口必须落在 user 消息上。

    从中间劈开会剩下「孤零零的 tool 结果」或「带着 tool_calls 却没有结果的
    assistant」，那种残缺口诀接口是会直接报错的。
    """
    chat = _chat(keep_recent_turns=1)
    chat.add_message("第一轮")
    chat.messages.append({
        "role": "assistant", "content": "我查一下。",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "demo", "arguments": "{}"}}],
    })
    chat.messages.append({"role": "tool", "tool_call_id": "c1", "content": "结果"})
    chat.messages.append({"role": "assistant", "content": "查到了。"})
    chat.add_message("第二轮")
    chat.messages.append({"role": "assistant", "content": "嗯。"})

    sent = chat._prompt_messages()

    assert [m["content"] for m in sent[1:]] == ["第二轮", "嗯。"]
    assert all(m["role"] != "tool" for m in sent)          # 没留下孤儿 tool
    assert all(not m.get("tool_calls") for m in sent)      # 也没留下没结果的调用


async def test_prompt_history_is_sent_whole_when_keep_recent_turns_is_zero(monkeypatch):
    """默认 0 ＝ 不裁，老行为一个字都不变。"""
    seen = _fake(monkeypatch, [_chunk("好")])

    chat = _chat()
    _fill(chat, 30)
    chat.add_message("最后")

    await chat.reply()

    assert seen[0]["messages"] == chat.messages
    assert len(seen[0]["messages"]) > 20


async def test_the_trim_reaches_the_request_not_just_the_helper(monkeypatch):
    """裁完的那份必须真的是**发出去的**那份（别只裁了个中间变量）。"""
    seen = _fake(monkeypatch, [_chunk("好")])

    chat = _chat(keep_recent_turns=2)
    _fill(chat, 4)

    await chat.reply()

    contents = [m["content"] for m in seen[0]["messages"]]
    assert contents[0] == chat.messages[0]["content"]      # system
    # 发出去的就是裁过的那 4 条（回复本身还没生成，自然不在里面）
    assert contents[1:] == ["问2", "答2", "问3", "答3"]


# ──────────────── ReAct 循环：想 → 做 → 看（形状本身就是约定） ────────────────


def test_a_step_with_actions_is_not_final():
    """一步收成的语义：没提行动 = 最终答案；提了 = 还要接着转。"""
    idle = _Step(thought="想好了")
    assert idle.is_final is True
    assert idle.content == "想好了"          # content 只是 thought 的别名

    busy = _Step(thought="我查一下")
    busy.actions.append(_ToolCall("c1", _ToolFunction("demo", "{}")))
    assert busy.is_final is False
    # ★ content 只回文字、不回行动：_make_assistant_message 靠它拼那条 assistant，
    #   行动得单独走 tool_calls 字段（见 core/chat.py 的 _make_assistant_message）。
    assert busy.content == "我查一下"


async def test_the_think_step_fills_the_sink(monkeypatch):
    """Thought：文字流出去，要做的行动攒进 sink，而且不碰历史。"""
    _fake(monkeypatch, [
        _chunk("我"),
        _chunk("查一下"),
        _chunk(tool_calls=[_slice(0, id="c1", name="demo", arguments="{}")]),
    ])

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("在吗")

    step = _Step()
    pieces = [p async for p in chat._think(include_tools=True, sink=step)]

    assert pieces == ["我", "查一下"]                      # 一块一块往外吐
    assert step.thought == "我查一下"
    assert [a.function.name for a in step.actions] == ["demo"]
    # ★ _think 只记账，历史由 reply_stream 决定什么时候写
    assert chat.messages[-1] == {"role": "user", "content": "在吗"}


async def test_act_runs_in_order_and_returns_one_observation_each():
    """Action：串行、照模型给的顺序执行，一条行动换回一条观察值。"""
    order = []

    async def first(**kwargs):
        order.append("first")
        return "一号结果"

    async def second(**kwargs):
        order.append("second")
        return "二号结果"

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": first, "other": second})
    actions = [
        _ToolCall("c1", _ToolFunction("demo", "{}")),
        _ToolCall("c2", _ToolFunction("other", "{}")),
    ]

    observations = await chat._act(actions)

    assert order == ["first", "second"]                   # 不并发、不打乱
    assert [o["role"] for o in observations] == ["tool", "tool"]
    # ★ tool_call_id 必须照 actions 的顺序对回去，接口靠它配对
    assert [o["tool_call_id"] for o in observations] == ["c1", "c2"]
    assert [o["content"] for o in observations] == ["一号结果", "二号结果"]


def test_observe_puts_the_observations_back_into_history():
    """Observation：接回 self.messages，下一步模型才看得到工具拿回了什么。"""
    chat = _chat()
    before = len(chat.messages)

    chat._observe([{"role": "tool", "tool_call_id": "c1", "content": "结果"}])

    assert len(chat.messages) == before + 1
    assert chat.messages[-1]["content"] == "结果"


async def test_one_react_cycle_alternates_thought_action_observation(monkeypatch):
    """★ 一整圈的形状：想 → 做 → 看 → 再想，最后以「没有行动」收尾。

    历史里该留下 assistant(想法 + 行动) → tool(观察值) → assistant(最终答案)。
    这正是接口认得的那种工具轮，重启后 core/store.py 的 load_conversation
    也能把整段读回来（界面上那些工具卡片就是这么补回来的）。
    """
    _fake(
        monkeypatch,
        [_chunk("我查一下。"),
         _chunk(tool_calls=[_slice(0, id="c1", name="demo", arguments="{}")])],
        [_chunk("查到了。")],
    )

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("苹果折叠屏出了吗")

    pieces = [p async for p in chat.reply_stream()]

    assert "".join(pieces) == "我查一下。查到了。"
    assert [m["role"] for m in chat.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert chat.messages[2]["content"] == "我查一下。"
    assert chat.messages[2]["tool_calls"][0]["id"] == "c1"
    assert chat.messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert chat.messages[4] == {"role": "assistant", "content": "查到了。"}


async def test_the_second_step_gets_the_observation_back(monkeypatch):
    """第二步的请求里必须带着上一步的观察值 —— 否则 ReAct 就断在这里了。"""
    seen = _fake(
        monkeypatch,
        [_chunk(tool_calls=[_slice(0, id="c1", name="demo", arguments="{}")])],
        [_chunk("拿到了。")],
    )

    chat = _chat()
    chat.add_tools([SCHEMA], {"demo": _ok})
    chat.add_message("去查")

    await chat.reply()

    second = seen[1]["messages"]
    # ★ 注意 seen 里存的是**同一个列表对象**：不裁剪时 _prompt_messages 直接返回
    #   self.messages（不是副本），所以这里看到的是这一轮跑完之后的最终样子。
    #   形状仍然是「assistant(带 tool_calls) → tool(观察值) → assistant(最终答案)」，
    #   观察值排在最终答案前面，就说明它是在第二次请求**之前**接回历史的。
    assert second[-3]["role"] == "assistant"
    assert second[-3]["tool_calls"][0]["function"]["name"] == "demo"
    assert second[-2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert second[-1] == {"role": "assistant", "content": "拿到了。"}
    # 第二步照旧带着工具（还没到最后一步）
    assert seen[1]["tools"] == [SCHEMA]

