"""core/tools.py 的行为约定。

这里不打真实的网络请求，也不真跑命令 —— 那些是探针（scratch/）的活。
单测要钉住的是「选哪些工具」「什么情况下拒绝动手」「出错怎么说话」这几条，
因为它们直接关系到用户的安全感：一个 fail-open 的 bug 会让模型
在没人点头的情况下动用户的文件。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from core.tools import (
    ALL_TOOLS,
    PROFILES,
    TOOLS_BY_NAME,
    Toolbox,
    _clip,
    _decode_output,
    _looks_like_no_results,
    _public_host,
    _resolve,
    _select,
    _web_search,
    build_tools,
    tool_command,
    tool_failed,
    tool_label,
    tool_names,
    tool_result,
)


# ───────────────────────── 档位 ─────────────────────────


def test_profiles_are_the_three_we_document() -> None:
    assert PROFILES == ("off", "safe", "full")


def test_off_profile_has_no_tools() -> None:
    assert tool_names("off") == []


def test_safe_profile_excludes_every_risky_tool() -> None:
    safe = set(tool_names("safe"))
    risky = {t.name for t in ALL_TOOLS if not t.safe}
    assert risky, "应该至少有一个危险工具，否则这个测试没意义"
    assert not (safe & risky)


def test_full_profile_is_a_superset_of_safe() -> None:
    safe = set(tool_names("safe"))
    full = set(tool_names("full"))
    assert safe < full


def test_unknown_profile_falls_back_to_safe() -> None:
    # 配置里写了个不认识的值不该让人丢掉全部能力，也不该偷偷全开
    assert tool_names("safety") == tool_names("safe")


def test_allow_is_a_whitelist() -> None:
    # 顺序跟着 ALL_TOOLS 走，不是跟着 allow 里写的顺序 —— 只要集合对就行
    picked = _select("full", ("web_search", "get_time"), ())
    assert {t.name for t in picked} == {"web_search", "get_time"}


def test_allow_cannot_unlock_risky_tools_from_a_narrower_profile() -> None:
    # allow 是在"当前档位挑出来的集合"里再筛，不是绕过档位
    assert _select("safe", ("run_command",), ()) == []


def test_deny_wins_over_everything() -> None:
    picked = _select("full", (), ("run_command", "web_search"))
    names = [t.name for t in picked]
    assert "run_command" not in names
    assert "web_search" not in names


def test_deny_beats_allow() -> None:
    assert _select("full", ("run_command",), ("run_command",)) == []


def test_blank_allow_entries_are_ignored() -> None:
    # 前端传 ["", " "] 这种不该把工具筛成空
    picked = _select("safe", ("", "  "), ())
    assert [t.name for t in picked] == tool_names("safe")


# ───────────────────────── 工具定义本身 ─────────────────────────


def test_every_tool_has_a_chinese_label() -> None:
    for tool in ALL_TOOLS:
        assert tool.label and tool.label != tool.name, tool.name


def test_tool_names_are_unique() -> None:
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == len(set(names))


def test_risky_tools_always_require_confirmation() -> None:
    """危险工具必须带 preview —— 没 preview 就等于不弹窗，等于悄悄动手。"""
    for tool in ALL_TOOLS:
        if not tool.safe:
            assert tool.preview is not None, tool.name


def test_safe_tools_never_require_confirmation() -> None:
    for tool in ALL_TOOLS:
        if tool.safe:
            assert tool.preview is None, tool.name


def test_schema_shape_is_what_litellm_expects() -> None:
    for tool in ALL_TOOLS:
        schema = tool.schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == tool.name
        assert fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])


def test_tool_label_falls_back_to_the_raw_name() -> None:
    assert tool_label("run_command") == "执行命令"
    assert tool_label("no_such_tool") == "no_such_tool"


def test_tools_by_name_covers_everything() -> None:
    assert set(TOOLS_BY_NAME) == {t.name for t in ALL_TOOLS}


# ───────────────────────── 审批 ─────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_risky_tool_without_an_approver_is_refused() -> None:
    """fail closed：拿不到确认界面就不动手。"""
    _, impl = build_tools("full", approve=None)
    result = _run(impl["run_command"](command="echo hi"))
    assert "没有可用的确认界面" in result


def test_risky_tool_is_refused_when_the_user_says_no() -> None:
    async def deny(name, args, preview):
        return False

    _, impl = build_tools("full", approve=deny)
    result = _run(impl["write_file"](path="x.txt", content="hi"))
    assert "用户拒绝" in result


def test_approver_receives_a_human_readable_preview() -> None:
    seen: list[tuple] = []

    async def approve(name, args, preview):
        seen.append((name, preview))
        return False

    _, impl = build_tools("full", approve=approve)
    _run(impl["run_command"](command="echo 你好"))
    assert seen == [("run_command", "执行命令：echo 你好")]


def test_approver_that_raises_is_treated_as_a_no() -> None:
    async def boom(name, args, preview):
        raise RuntimeError("界面没了")

    _, impl = build_tools("full", approve=boom)
    assert "用户拒绝" in _run(impl["write_file"](path="x.txt", content="hi"))


def test_safe_tools_never_call_the_approver() -> None:
    calls: list[str] = []

    async def approve(name, args, preview):
        calls.append(name)
        return True

    _, impl = build_tools("safe", approve=approve)
    _run(impl["get_time"]())
    assert calls == []


def test_approver_is_awaited_not_assumed_sync() -> None:
    """approve 是协程。要是哪天有人改成同步函数，这里会炸出来。"""
    order: list[str] = []

    async def approve(name, args, preview):
        order.append("asked")
        await asyncio.sleep(0)
        order.append("answered")
        return True

    async def main():
        _, impl = build_tools("full", approve=approve)
        order.append("before")
        # 用 list_dir 会真读盘，这里只要它过了审批这一关就行 —— 故意给个
        # 不存在的路径，让它走到"不存在"那条分支就够验证顺序了
        await impl["open_path"](path="__definitely_not_here__")
        order.append("after")

    _run(main())
    assert order == ["before", "asked", "answered", "after"]


# ───────────────────────── 出错时说什么 ─────────────────────────


def test_tool_never_raises_it_returns_a_sentence() -> None:
    async def main():
        _, impl = build_tools("safe")
        # 模型给的参数乱七八糟（少参数 / 类型不对）也不能抛出来
        return await impl["read_file"]()

    result = _run(main())
    assert isinstance(result, str)
    assert result


def test_missing_file_says_so_in_chinese() -> None:
    async def main():
        _, impl = build_tools("safe")
        return await impl["read_file"](path="__no_such_file__.txt")

    assert "不存在" in _run(main())


def test_list_dir_rejects_a_file() -> None:
    async def main():
        _, impl = build_tools("safe")
        return await impl["list_dir"](path="requirements.txt")

    assert "不是目录" in _run(main())


def test_memory_add_requires_both_fields() -> None:
    async def main():
        _, impl = build_tools("safe")
        return await impl["memory_add"](key="", value="x")

    assert "不能为空" in _run(main())


# ───────────────────────── 小工具 ─────────────────────────


def test_clip_keeps_both_ends() -> None:
    text = "头" * 100 + "尾" * 100
    out = _clip(text, limit=60)
    assert out.startswith("头")
    assert out.endswith("尾")
    assert "省略" in out


def test_clip_leaves_short_text_alone() -> None:
    assert _clip("短", limit=60) == "短"


def test_decode_output_prefers_utf8() -> None:
    assert _decode_output("你好".encode()) == "你好"


def test_decode_output_falls_back_to_the_local_codepage() -> None:
    """老的 Windows 命令行工具会按 GBK 吐中文，硬解 UTF-8 只会得到一串替换符。"""
    raw = "工具调用测试".encode("gbk")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert _decode_output(raw) == "工具调用测试"


def test_resolve_expands_the_home_shorthand() -> None:
    assert _resolve("~").is_absolute()


def test_resolve_strips_quotes_the_model_likes_to_add() -> None:
    assert str(_resolve('"~"')).startswith(str(_resolve("~")))


# ───────────────────────── Toolbox ─────────────────────────


def test_toolbox_build_returns_matching_schemas_and_impls() -> None:
    schemas, impl = Toolbox().build("safe")
    assert {s["function"]["name"] for s in schemas} == set(impl)


def test_toolbox_defaults_to_the_safe_profile() -> None:
    schemas, _ = Toolbox().build()
    assert {s["function"]["name"] for s in schemas} == set(tool_names("safe"))


@pytest.mark.parametrize("profile", PROFILES)
def test_every_profile_builds(profile: str) -> None:
    schemas, impl = Toolbox().build(profile)
    assert len(schemas) == len(impl)


# ─────────────── 界面上那句「到底执行了什么」 ───────────────
#
# 模型调工具时用户只看到一句状态行，他不知道模型在动什么，也就没法决定
# 要不要安心。tool_command() 就是卡片上那行字的数据来源 —— 这些用例守的是
# 两件事：翻得准（用户看得懂），以及翻得干净（别把文件正文之类摆上屏幕）。


def test_tool_command_drops_the_label_the_card_already_shows() -> None:
    """卡片上已经写着「执行命令」了，命令本身别再带一遍这个前缀。"""
    assert tool_command("run_command", {"command": "echo 你好"}) == "echo 你好"


def test_tool_command_shows_a_lone_path_or_query_as_is() -> None:
    """只有一个字符串参数，它就是那条路径/那个关键词 —— 原样给最好读。"""
    assert tool_command("read_file", {"path": "C:/tmp/a.txt"}) == "C:/tmp/a.txt"
    assert tool_command("web_search", {"query": "今晚吃什么"}) == "今晚吃什么"


def test_tool_command_never_puts_file_contents_on_screen() -> None:
    """写文件的 preview 只说「写入 X（N 字）」，正文绝不能摆到界面上。"""
    shown = tool_command("write_file", {"path": "a.txt", "content": "机密" * 500})
    assert "a.txt" in shown
    assert "1000" in shown
    assert "机密" not in shown


def test_tool_command_names_the_keys_when_there_are_several() -> None:
    assert tool_command("memory_add", {"key": "prefers_nickname", "value": "小八"}) == (
        "key=prefers_nickname value=小八"
    )


def test_tool_command_is_blank_for_tools_that_take_no_arguments() -> None:
    """看时间、截屏本来就没什么可说的，卡片矮一截就行。"""
    assert tool_command("get_time", {}) == ""
    assert tool_command("screenshot", None) == ""


def test_tool_command_skips_blank_values() -> None:
    """模型爱把用不上的参数送成空串，界面上不该出现 `path=` 这种半截话。"""
    assert tool_command("read_file", {"path": "", "encoding": None}) == ""


def test_tool_command_still_works_for_an_unknown_tool() -> None:
    """配置里可能残留我们不再注册的工具名，别在这儿炸。"""
    assert tool_command("mystery", {"a": 1}) == "a=1"
    assert tool_command("mystery", {}) == ""


def test_tool_command_never_raises_on_odd_argument_types() -> None:
    """它只服务于界面，翻不出来也得让工具照常跑。"""
    assert tool_command("mystery", {"a": object()}).startswith("a=")
    assert tool_command("write_file", {"path": None, "content": None}).startswith("写入")


# ──────────────────────────────────────────────────────────
#  卡片上那行「拿回来了什么」
#
#  对应后端事件里的 result / failed（backend/app.py 的 on_tool_end）。
#  完整结果可能上万字，界面上只留开头一小截；失败与否只影响卡片颜色，
#  不参与任何逻辑判断 —— 但判断本身得钉死，不然颜色会骗人。
# ──────────────────────────────────────────────────────────


def test_tool_result_folds_whitespace_into_one_line() -> None:
    assert tool_result("第一行\n第二行\n\n   第三行  ") == "第一行 第二行 第三行"


def test_tool_result_keeps_a_short_answer_whole() -> None:
    assert tool_result("没有搜到「折叠屏」的结果。") == "没有搜到「折叠屏」的结果。"
    assert tool_result("") == ""
    assert tool_result(None) == ""


def test_tool_result_truncates_and_says_how_much_there_was() -> None:
    """截了就得说自己截了 —— 不然用户以为工具就返回了这么点东西。"""
    text = tool_result("字" * 1000)
    assert text.endswith("（共 1000 字）")
    assert len(text) < 1000


def test_tool_failed_spots_the_project_error_sentences() -> None:
    """项目约定：工具不抛异常、把问题当结果喂回去 —— 所以只能认开头。"""
    for sentence in (
        "错误：参数不是合法 JSON（Expecting value）。请重新调用。",
        "错误：query 不能为空。",
        "工具执行失败（ValueError）。换个方式再试，或者告诉用户这条路走不通。",
        "没有可用的确认界面，这次调用已取消。",
        "用户拒绝了这次调用。不要重试同一个动作。",
        "搜索失败（DDGSException）。可以换个说法再试一次。",
        "搜索超时了（超过 20 秒）。换个更短的关键词再试一次。",
    ):
        assert tool_failed(sentence) is True, sentence


def test_tool_failed_leaves_a_real_answer_alone() -> None:
    """★ 零结果是结果，不是失败。

    判成失败的话，界面会亮一盏红灯；更要紧的是模型会把「没搜到」读成
    「搜索不可用」，转头告诉用户搜不了 —— 那正是用户报的
    "有时候搜索工具无法调用"。
    """
    for sentence in (
        "没有搜到「折叠屏」的结果。换个关键词可以再搜一次。",
        "「折叠屏」的搜索结果：\n1. 某某\n   https://example.com\n   正文",
        "现在时间：2026-02-14 20:31:07",
        "",
        None,
    ):
        assert tool_failed(sentence) is False, sentence


def test_tool_failed_ignores_leading_whitespace() -> None:
    assert tool_failed("\n  错误：query 不能为空。") is True


# ──────────────────────────────────────────────────────────
#  搜索：一次「没搜到」不该被说成「搜索坏了」
#
#  用户报过"有时候搜索工具无法调用"，一半根因就在这儿：ddgs 在零结果时
#  也抛异常，我们原样转述成「搜索失败」，模型便以为这条路走不通，
#  转头跟用户说"我搜不了"。
# ──────────────────────────────────────────────────────────


def test_looks_like_no_results_separates_empty_from_broken() -> None:
    from ddgs.exceptions import TimeoutException

    assert _looks_like_no_results(Exception("No results found.")) is True
    # 超时和限流都是真的出事了，别混进「没搜到」
    assert _looks_like_no_results(TimeoutException("timed out")) is False
    assert _looks_like_no_results(Exception("429 Too Many Requests")) is False


def test_a_zero_result_search_is_not_reported_as_broken(monkeypatch) -> None:
    """★ 零结果走的是「换个词再搜」，不是「搜索失败」。"""
    import ddgs as ddgs_mod

    class FakeDDGS:
        def text(self, query, **kwargs):
            raise ddgs_mod.exceptions.DDGSException("No results found.")

    monkeypatch.setattr(ddgs_mod, "DDGS", FakeDDGS)
    out = asyncio.run(_web_search(None, "一个根本不存在的词"))
    assert out.startswith("没有搜到")
    assert "失败" not in out
    # 得顺手告诉模型「换个关键词再试」：一次零结果不该让它放弃
    assert "换个关键词" in out


def test_a_real_search_failure_still_says_failure(monkeypatch) -> None:
    """真连不上就得说真话 —— 说成「没搜到」会让模型对着网络故障白试好几轮。"""
    import ddgs as ddgs_mod

    class FakeDDGS:
        def text(self, query, **kwargs):
            raise ddgs_mod.exceptions.DDGSException("429 Too Many Requests")

    monkeypatch.setattr(ddgs_mod, "DDGS", FakeDDGS)
    out = asyncio.run(_web_search(None, "折叠屏"))
    assert out.startswith("搜索失败")


def test_search_reports_its_results(monkeypatch) -> None:
    """好路上别把结果吞掉 —— 这是每次改动都最容易碰坏的地方。"""
    import ddgs as ddgs_mod

    class FakeDDGS:
        def text(self, query, **kwargs):
            return [{"title": "标题", "href": "https://example.com", "body": "正文"}]

    monkeypatch.setattr(ddgs_mod, "DDGS", FakeDDGS)
    out = asyncio.run(_web_search(None, "折叠屏", max_results=3))
    assert "折叠屏" in out
    assert "https://example.com" in out
    assert "正文" in out


# ──────────────────────────────────────────────────────────
#  地址检查：能走到本机和内网的才挡，别把代理的 fake-IP 一起挡了
#
#  用户报过"搜索能用、打开网页不能用"。他那台机器上代理开着 fake-IP 模式，
#  所有域名都解析到 198.18.x.x（保留的 benchmarking 段），而原实现拿
#  is_global 当白名单，于是每个域名都被判「不是公网地址」，web_fetch 全军覆没。
#  下面这几条把两头都钉住：该挡的还挡着，不该挡的别再挡。
# ──────────────────────────────────────────────────────────


def _resolves_to(monkeypatch, *ips: str) -> None:
    """把 DNS 换成一个固定答案（照 getaddrinfo 的形状给）。"""
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr("core.tools.socket.getaddrinfo", lambda *a, **k: infos)


def test_host_check_allows_a_proxy_fake_ip(monkeypatch) -> None:
    """★ 198.18.0.0/15 是代理 fake-IP 的池子，放行；不然整台机器都上不了网。"""
    _resolves_to(monkeypatch, "198.18.33.77")
    assert _public_host("www.reuters.com") is True


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # 环回 —— 这个程序的后端就在这儿
        "0.0.0.0",
        "10.1.2.3",
        "172.20.0.1",
        "192.168.1.1",
        "169.254.169.254",  # 云元数据
        "100.64.0.1",
    ],
)
def test_host_check_still_blocks_every_local_network(monkeypatch, ip: str) -> None:
    _resolves_to(monkeypatch, ip)
    assert _public_host("evil.example") is False


def test_host_check_blocks_ipv6_loopback(monkeypatch) -> None:
    _resolves_to(monkeypatch, "::1")
    assert _public_host("evil.example") is False


def test_host_check_blocks_a_v4_mapped_loopback(monkeypatch) -> None:
    """::ffff:127.0.0.1 绕一圈还是环回，不能因为写成 IPv6 就放过。"""
    _resolves_to(monkeypatch, "::ffff:127.0.0.1")
    assert _public_host("evil.example") is False


def test_host_check_refuses_when_any_answer_is_local(monkeypatch) -> None:
    """DNS 轮询 / rebinding 会同时给公网和内网地址，不能赌 httpx 挑哪个。"""
    _resolves_to(monkeypatch, "198.18.33.77", "127.0.0.1")
    assert _public_host("evil.example") is False


def test_host_check_refuses_when_dns_fails(monkeypatch) -> None:
    """解析不了就说打不开，别放一个没查清楚的地址出去。"""

    def boom(*args, **kwargs):
        raise socket.gaierror("名字解析不了")

    monkeypatch.setattr("core.tools.socket.getaddrinfo", boom)
    assert _public_host("nope.invalid") is False
