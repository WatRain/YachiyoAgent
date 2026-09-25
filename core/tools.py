"""工具集：给模型用的「手」。

档位（见 core/config.py 的 ToolSettings.profile）：

    off    一个都不带 —— 退化成纯聊天
    safe   联网 / 时间 / 记忆 / 截图 / 剪贴板 / 只读文件（默认）
    full   safe + 写文件 / 改文件 / 打开路径 / 执行命令

**危险工具动手之前要先问过用户。** core 层不认识 WebSocket，所以审批走
构造 Toolbox 时注入的 approve 回调（backend/app.py 把它接到界面弹窗上）。
拿不到 approve 时一律拒绝 —— 宁可少做一件事，也不能替用户按了回车。

几条贯穿全文件的约定：

1. 工具**只返回字符串**。模型读得懂人话，读不懂异常栈。
2. 工具**不抛异常**，出错就把原因写进返回值（core/chat.py 的 _run_tool
   也是这么兜的，两边保持一致，模型才有机会自己改正）。
3. 所有输入都当作不可信：路径要展开、输出要截断、命令要超时、
   抓网页要挡内网地址（这个程序自己的后端就蹲在 127.0.0.1）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import locale
import logging
import os
import shutil
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from core.paths import data_dir

log = logging.getLogger(__name__)

# ── 硬上限：模型可以要求读一个 10GB 的日志，我们不该照做 ──
MAX_READ_BYTES = 200_000        # 单次读文件的上限
MAX_OUTPUT_CHARS = 20_000       # 任何工具返回值给模型的字符上限
MAX_DIR_ENTRIES = 200           # 列目录最多列这么多条
MAX_FETCH_BYTES = 2_000_000     # 抓网页最多下这么多字节
FETCH_TIMEOUT = 20.0
SEARCH_TIMEOUT = 20.0            # 一次搜索的总预算（ddgs 自己每个引擎也会超时）
COMMAND_TIMEOUT = 60.0          # 命令默认超时（秒），模型可以往小调不能往大调
COMMAND_TIMEOUT_MAX = 300.0
CLIPBOARD_MAX = 20_000

# 抓网页时用的 UA：有些站点对非浏览器 UA 直接 403
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ═══════════════════════════════════════════════════════════
#  小工具
# ═══════════════════════════════════════════════════════════


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """太长的输出砍中间留两头 —— 头和尾通常才是有信息的那部分。"""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n…（中间省略 {len(text) - limit} 字）…\n{text[-tail:]}"


def _human_size(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def _resolve(path: str) -> Path:
    """把模型给的路径变成绝对路径。

    要展开 ~ 和环境变量：模型经常写 %USERPROFILE%\\Desktop 或 ~/Desktop，
    直接丢给 open() 只会得到一句看不懂的 FileNotFoundError。
    """
    raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    return Path(raw).expanduser().resolve()


def _truncate_note(note: str) -> str:
    return f"\n\n（{note}）"


# 要挡掉的网段。
#
# ★ 为什么是「危险网段黑名单」，而不是「必须 is_global」：
#   is_global 会把保留段一并判死，而不少代理软件（Clash / v2rayN 那类）开着
#   fake-IP 模式，会把**所有**域名解析到 198.18.x.x —— 那是保留的
#   benchmarking 段。实测这台机器：reuters.com / techcrunch.com / baidu.com
#   全部落在 198.18.*，于是 web_fetch 100% 失败（0.0 秒就回「不是公网地址」，
#   请求根本没发出去）。真正要防的是「连到本机或内网上的服务」，不是
#   「这个 IP 有没有登记成公网」，所以只把能走到本机和内网的网段列清楚，
#   其余一律放行 —— 代理的 fake-IP 池子正好都在这「其余」里。
_BLOCKED_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",       # 本网络
        "10.0.0.0/8",      # 私网
        "100.64.0.0/10",   # 运营商级 NAT
        "127.0.0.0/8",     # 环回 —— 这个程序的后端就蹲在这儿
        "169.254.0.0/16",  # 链路本地 —— 云元数据 169.254.169.254 也在里面
        "172.16.0.0/12",   # 私网
        "192.168.0.0/16",  # 私网
        "::/128",          # 未指定
        "::1/128",         # 环回
        "fc00::/7",        # 唯一本地
        "fe80::/10",       # 链路本地
    )
)


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """这个地址能不能走到本机或内网。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped  # ::ffff:127.0.0.1 这种要按 127.0.0.1 来判
    return any(ip in net for net in _BLOCKED_NETS)


def _public_host(host: str) -> bool:
    """这个主机名解析出来的地址，是不是「可以放它出去」的。

    为什么要挡：这个程序自己的后端就跑在 127.0.0.1，模型要是被诱导去抓
    http://127.0.0.1:8000/api/config，那就是自己把自己的配置读出去。
    169.254.169.254 那种云元数据地址同理。所以只要解析结果里**有一个**
    落在危险网段，就整个拒绝 —— 一个域名同时给出公网和内网地址时（DNS
    轮询、rebinding 都会），不能赌 httpx 最后挑了哪个。

    解析失败也返回 False：宁可说「打不开」，不要放一个没查清楚的地址出去。

    ★ 这里查一次、httpx 稍后再解析一次，理论上存在 TOCTOU 窗口；
      对一个本地桌面程序来说这个强度够了，真要较真得把 IP 钉死再连。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if _blocked_ip(ip):
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  工具的描述结构
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Tool:
    """一个工具的全部信息。

    func 的签名是 async (toolbox, **模型给的参数) -> str；
    preview 只在「需要审批」的工具上有，用来给弹窗写一句人话。
    """

    name: str
    label: str                      # 中文名，界面上显示「正在用「查天气」…」
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    func: Callable[..., Awaitable[str]]
    safe: bool = True               # 是否属于 safe 档
    preview: Callable[[dict], str] | None = None   # 非空 = 动手前要审批

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                },
            },
        }


def _denied() -> str:
    return "用户拒绝了这次调用。不要重试同一个动作 —— 换个不需要动手的办法，或者直接告诉用户你做不了。"


# ═══════════════════════════════════════════════════════════
#  基础
# ═══════════════════════════════════════════════════════════


async def _get_time(tb: "Toolbox", timezone: str = "") -> str:
    """查当前时间。默认本机时区，也可以指定 IANA 时区名。"""
    now = datetime.now().astimezone()
    lines = [
        f"本机时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"星期：{WEEKDAYS[now.weekday()]}",
        f"本机时区：{now.tzname() or '未知'}（UTC{now.strftime('%z')}）",
        f"UTC 时间：{now.astimezone(dt_timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    want = (timezone or "").strip()
    if want:
        try:
            from zoneinfo import ZoneInfo

            other = now.astimezone(ZoneInfo(want))
            lines.append(
                f"{want}：{other.strftime('%Y-%m-%d %H:%M:%S')}"
                f"（UTC{other.strftime('%z')}）"
            )
        except Exception:
            lines.append(f"（时区 {want} 认不出来，只给了本机时间）")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  联网
# ═══════════════════════════════════════════════════════════


def _looks_like_no_results(exc: BaseException) -> bool:
    """这次异常其实是「零结果」而不是「搜索坏了」。

    ddgs 在一条都没搜到时也抛 DDGSException("No results found.")，跟网络故障
    长得一模一样。分不清楚就会有下面的后果：模型收到「搜索失败」，以为这条路
    根本走不通，转头跟用户说「我搜不了」—— 用户看到的就是「搜索工具有时候不能用」。
    """
    if type(exc).__name__ in ("TimeoutException", "RatelimitException"):
        return False
    return "no results" in str(exc).lower()


async def _web_search(tb: "Toolbox", query: str, max_results: int = 5) -> str:
    """搜网页。用 ddgs（MIT，免密钥）—— 用户不用再去申请一个搜索 API。"""
    query = (query or "").strip()
    if not query:
        return "错误：query 不能为空。"

    try:
        count = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        count = 5

    def _run() -> list[dict]:
        from ddgs import DDGS

        # 不传 timeout：ddgs 的默认（每批引擎 5 秒）本来就合适。
        # 往大调只会让「本来就慢」的搜索更慢，真正的兜底是外面那层 wait_for。
        return DDGS().text(query, max_results=count)

    try:
        # 外面套一层总预算。ddgs 的超时是「每批引擎」的，它要挨个试好几个引擎，
        # 累起来能拖很久 —— 而它卡住的时候，模型在那头是一直干等的。
        # （线程本身没法中断，超时后它还在后台跑完；但模型不用陪它等了。）
        results = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=SEARCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning("搜索超时（%.0f 秒）", SEARCH_TIMEOUT)
        return f"搜索超时了（超过 {SEARCH_TIMEOUT:.0f} 秒）。换个更短的关键词再试一次。"
    except Exception as exc:
        if _looks_like_no_results(exc):
            return _no_results(query)
        log.warning("搜索失败：%s: %s", type(exc).__name__, exc)
        return f"搜索失败（{type(exc).__name__}）。可以换个说法再试一次。"

    if not results:
        return _no_results(query)

    lines = [f"「{query}」的搜索结果："]
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("href") or item.get("url") or "").strip()
        body = " ".join((item.get("body") or "").split())
        lines.append(f"{i}. {title}\n   {url}\n   {body}")
    return _clip("\n".join(lines))


def _no_results(query: str) -> str:
    """「什么都没搜到」和「搜索坏了」是两回事，这是前者的说法。

    特意带上「换个关键词再搜一次」：否则模型很容易把一次零结果当成工具不可用。
    """
    return f"没有搜到「{query}」的结果。换个关键词可以再搜一次；实在没有就照实告诉用户没查到。"


async def _web_fetch(tb: "Toolbox", url: str, max_chars: int = 8000) -> str:
    """抓一个网页，把正文转成 Markdown 交给模型。"""
    url = (url or "").strip()
    if not url:
        return "错误：url 不能为空。"
    if not url.startswith(("http://", "https://")):
        return "错误：只支持 http/https 开头的网址。"

    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return "错误：网址里没有主机名。"
    if not await asyncio.to_thread(_public_host, host):
        return f"错误：{host} 不是公网地址，拒绝访问。"

    try:
        limit = max(500, min(int(max_chars), MAX_OUTPUT_CHARS))
    except (TypeError, ValueError):
        limit = 8000

    try:
        import httpx

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            raw = resp.content[:MAX_FETCH_BYTES]
    except Exception as exc:
        log.info("抓取失败 %s：%s: %s", url, type(exc).__name__, exc)
        return f"抓取失败（{type(exc).__name__}）。网址可能打不开或者太慢了。"

    if "html" in ctype:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        text = _html_to_text(text)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        text = raw.decode("utf-8", errors="replace")
    elif not ctype:
        text = raw.decode("utf-8", errors="replace")
    else:
        return f"这个地址返回的是 {ctype}，不是文本内容，读不了。"

    text = text.strip()
    if not text:
        return "页面抓到了，但正文是空的（可能整页都是脚本渲染的）。"

    note = ""
    if len(text) > limit:
        text = text[:limit]
        note = f"（只取了前 {limit} 字）"
    return f"来源：{url}{note}\n\n{text}"


def _html_to_text(html: str) -> str:
    """HTML → Markdown。

    用 markdownify（MIT）。没装就退化成「把标签全删掉」——
    宁可给模型一坨没格式的文字，也不要让整个工具挂掉。
    """
    try:
        from markdownify import markdownify

        out = markdownify(html, heading_style="ATX", strip=["script", "style", "noscript"])
    except Exception:
        import re

        out = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        out = re.sub(r"(?s)<[^>]+>", " ", out)

    # 折叠空行：markdownify 会留下大段空白，白占 token
    lines = [ln.rstrip() for ln in out.splitlines()]
    compact: list[str] = []
    blanks = 0
    for ln in lines:
        if ln:
            blanks = 0
            compact.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                compact.append("")
    return "\n".join(compact).strip()


# ═══════════════════════════════════════════════════════════
#  文件
# ═══════════════════════════════════════════════════════════


async def _read_file(tb: "Toolbox", path: str, max_bytes: int = MAX_READ_BYTES) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"错误：{target} 不存在。"
    if target.is_dir():
        return f"错误：{target} 是目录，用 list_dir 看里面的东西。"

    try:
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
    except (TypeError, ValueError):
        limit = MAX_READ_BYTES

    try:
        with target.open("rb") as fh:
            raw = fh.read(limit + 1)
    except OSError as exc:
        return f"读不了 {target}（{type(exc).__name__}）。"

    truncated = len(raw) > limit
    raw = raw[:limit]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{target} 不是 UTF-8 文本（{_human_size(target.stat().st_size)}），读不了。"

    if truncated:
        text += f"\n\n（文件更大，只读了前 {_human_size(limit)}）"
    return text


async def _list_dir(tb: "Toolbox", path: str = ".") -> str:
    target = _resolve(path)
    if not target.exists():
        return f"错误：{target} 不存在。"
    if not target.is_dir():
        return f"错误：{target} 不是目录。"

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f"读不了目录（{type(exc).__name__}）。"

    if not entries:
        return f"{target} 是空目录。"

    lines = [f"{target}（{len(entries)} 项）"]
    for item in entries[:MAX_DIR_ENTRIES]:
        try:
            if item.is_dir():
                lines.append(f"  [目录] {item.name}")
            else:
                lines.append(f"  [文件] {_human_size(item.stat().st_size)}  {item.name}")
        except OSError:
            lines.append(f"  [?]    {item.name}")
    if len(entries) > MAX_DIR_ENTRIES:
        lines.append(f"  …还有 {len(entries) - MAX_DIR_ENTRIES} 项没列出来")
    return _clip("\n".join(lines))


async def _write_file(tb: "Toolbox", path: str, content: str) -> str:
    target = _resolve(path)
    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"写不进去（{type(exc).__name__}）：{exc}"

    verb = "覆盖了" if existed else "新建了"
    return f"已{verb} {target}（{len(content)} 字）。"


async def _edit_file(tb: "Toolbox", path: str, find: str, replace: str) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"错误：{target} 不存在。要新建文件用 write_file。"

    try:
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"读不了 {target}（{type(exc).__name__}）。"

    hits = original.count(find)
    if hits == 0:
        return "错误：要替换的原文在文件里找不到。请先 read_file 看一眼真实内容（空格和换行也要一致）。"
    if hits > 1:
        return f"错误：要替换的原文出现了 {hits} 次，不确定改哪一处。请多带几行上下文让它唯一。"

    updated = original.replace(find, replace, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f"写不进去（{type(exc).__name__}）：{exc}"

    line_no = original[: original.index(find)].count("\n") + 1
    return f"已改好 {target}（第 {line_no} 行附近）。"


async def _open_path(tb: "Toolbox", path: str) -> str:
    target = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    if not os.path.exists(target):
        return f"错误：{target} 不存在。"

    try:
        if sys.platform == "win32":
            os.startfile(target)          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            await asyncio.create_subprocess_exec("open", target)
        else:
            await asyncio.create_subprocess_exec("xdg-open", target)
    except OSError as exc:
        return f"打不开（{type(exc).__name__}）：{exc}"
    return f"已经用系统默认程序打开 {target}。"


# ═══════════════════════════════════════════════════════════
#  命令
# ═══════════════════════════════════════════════════════════


def _shell_argv(command: str) -> list[str]:
    """挑一个 shell。

    Windows 上用 PowerShell 而不是 cmd：模型很容易写成 `ls`、`cat`、`rm`，
    这些在 PowerShell 里都是内置别名，在 cmd 里全是不存在的命令。
    """
    if os.name == "nt":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        # 让 shell 按 UTF-8 吐字。中文 Windows 的控制台默认是 GBK(936)，
        # 不设的话 `echo 你好` 这类输出解出来全是乱码。
        # 用 try 包住：没有真控制台（输出被重定向）时设 OutputEncoding 会抛。
        prefix = (
            "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {};"
            "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        )
        return [exe, "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    return ["/bin/sh", "-c", command]


def _decode_output(raw: bytes) -> str:
    """把命令输出解成字符串。

    先按 UTF-8 解（我们自己让 shell 吐的就是 UTF-8），
    解不开再退回系统本地编码 —— 有些程序（老的 Windows 命令行工具）
    不管你怎么设都按 GBK 吐，硬解 UTF-8 只会得到一串替换符。
    """
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


async def _run_command(tb: "Toolbox", command: str, cwd: str = "", timeout: int = 60) -> str:
    command = (command or "").strip()
    if not command:
        return "错误：command 不能为空。"

    workdir = _resolve(cwd) if cwd else Path.cwd()
    if not workdir.is_dir():
        return f"错误：工作目录 {workdir} 不存在。"

    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = COMMAND_TIMEOUT
    seconds = max(1.0, min(seconds, COMMAND_TIMEOUT_MAX))

    try:
        proc = await asyncio.create_subprocess_exec(
            *_shell_argv(command),
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return f"起不了进程（{type(exc).__name__}）：{exc}"

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds)
    except asyncio.TimeoutError:
        # 超时必须真杀掉：留着的话它会一直占着管道，下一轮工具调用会莫名卡住
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return f"命令跑了超过 {seconds:.0f} 秒，已经强制结束。要么换个更快的写法，要么把 timeout 调大（最多 {COMMAND_TIMEOUT_MAX:.0f} 秒）。"

    text = _decode_output(out).strip()
    code = proc.returncode
    if not text:
        text = "（没有任何输出）"
    head = f"退出码 {code}\n" if code else ""
    return _clip(f"{head}{text}")


# ═══════════════════════════════════════════════════════════
#  桌面
# ═══════════════════════════════════════════════════════════


async def _screenshot(tb: "Toolbox") -> str:
    """截屏存成 PNG，把路径交给模型。

    ★ 现在还没有把图片喂给模型的能力（litellm 那条路只发文字），
      所以这个工具的价值是「把屏幕存下来，用户可以自己打开看」。
    """
    def _grab() -> Path:
        from PIL import ImageGrab

        folder = data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        shot = folder / f"shot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        try:
            image = ImageGrab.grab(all_screens=True)
        except TypeError:
            # 老版本 Pillow 没有 all_screens
            image = ImageGrab.grab()
        image.save(shot, "PNG")
        return shot

    try:
        shot = await asyncio.to_thread(_grab)
    except Exception as exc:
        log.warning("截屏失败：%s: %s", type(exc).__name__, exc)
        return f"截屏失败（{type(exc).__name__}）。"

    return f"截图已存到 {shot}（{_human_size(shot.stat().st_size)}）。"


async def _clipboard_read(tb: "Toolbox") -> str:
    def _read() -> str:
        import pyperclip

        return pyperclip.paste() or ""

    try:
        text = await asyncio.to_thread(_read)
    except Exception as exc:
        return f"读剪贴板失败（{type(exc).__name__}）。"
    if not text.strip():
        return "剪贴板是空的（或者里面不是文字）。"
    return _clip(text, CLIPBOARD_MAX)


async def _clipboard_write(tb: "Toolbox", text: str) -> str:
    def _write() -> None:
        import pyperclip

        pyperclip.copy(text)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        return f"写剪贴板失败（{type(exc).__name__}）。"
    return f"已经把 {len(text)} 个字放进剪贴板。"


# ═══════════════════════════════════════════════════════════
#  记忆
# ═══════════════════════════════════════════════════════════


async def _memory_search(tb: "Toolbox", query: str = "") -> str:
    from core import store

    items = await asyncio.to_thread(store.load_memories)
    if not items:
        return "记忆库还是空的。"

    want = (query or "").strip().lower()
    if want:
        items = [
            m
            for m in items
            if want in str(m.get("key", "")).lower() or want in str(m.get("value", "")).lower()
        ]
    if not items:
        return f"没有和「{query}」相关的记忆。"

    lines = ["记得这些："]
    for m in items:
        lines.append(f"- {m.get('key', '')}：{m.get('value', '')}")
    return _clip("\n".join(lines))


async def _memory_add(tb: "Toolbox", key: str, value: str) -> str:
    from core import store

    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return "错误：key 和 value 都不能为空。"

    await asyncio.to_thread(store.upsert_memory, key[:40], value[:200], 0.9)
    return f"记住了：{key} = {value}"


async def _memory_forget(tb: "Toolbox", key: str) -> str:
    from core import store

    key = (key or "").strip()
    if not key:
        return "错误：key 不能为空。"

    items = await asyncio.to_thread(store.load_memories)
    hit = next((m for m in items if str(m.get("key", "")) == key), None)
    if hit is None:
        return f"记忆里没有 {key} 这一条。"

    await asyncio.to_thread(store.delete_memory, key)
    return f"忘掉了：{key}（原本是「{hit.get('value', '')}」）"


# ═══════════════════════════════════════════════════════════
#  注册表
# ═══════════════════════════════════════════════════════════


def _preview_write(args: dict) -> str:
    path = args.get("path", "?")
    size = len(str(args.get("content", "")))
    return f"写入 {path}（{size} 字）"


def _preview_edit(args: dict) -> str:
    return f"修改 {args.get('path', '?')}"


def _preview_open(args: dict) -> str:
    return f"用系统默认程序打开 {args.get('path', '?')}"


def _preview_command(args: dict) -> str:
    return f"执行命令：{args.get('command', '?')}"


ALL_TOOLS: list[Tool] = [
    # ── 基础 ──
    Tool(
        name="get_time",
        label="看时间",
        description="查现在的日期和时间。用户问「今天几号」「现在几点」时用它。",
        properties={
            "timezone": {
                "type": "string",
                "description": "可选的 IANA 时区名，例如 Asia/Shanghai、America/New_York。不填就是本机时区。",
            }
        },
        required=(),
        func=_get_time,
    ),
    # ── 联网 ──
    Tool(
        name="web_search",
        label="搜索",
        description=(
            "联网搜索。用于查事实、新闻、资料 —— 任何你不确定或者可能已经过时的信息。"
            "搜完通常还要用 web_fetch 打开其中一两个链接看细节。"
        ),
        properties={
            "query": {"type": "string", "description": "搜索关键词，像在搜索引擎里输入的那样"},
            "max_results": {
                "type": "integer",
                "description": "要几条结果，默认 5，最多 10",
            },
        },
        required=("query",),
        func=_web_search,
    ),
    Tool(
        name="web_fetch",
        label="读网页",
        description="打开一个网址，把正文读出来。只支持 http/https，且必须是公网地址。",
        properties={
            "url": {"type": "string", "description": "完整网址"},
            "max_chars": {
                "type": "integer",
                "description": "最多读多少字，默认 8000",
            },
        },
        required=("url",),
        func=_web_fetch,
    ),
    # ── 文件（只读）──
    Tool(
        name="read_file",
        label="读文件",
        description="读一个文本文件的内容。文件必须是 UTF-8 编码的文本。",
        properties={
            "path": {"type": "string", "description": "文件路径，可以是相对路径，也可以用 ~ 或环境变量"},
            "max_bytes": {"type": "integer", "description": "最多读多少字节，默认 20 万"},
        },
        required=("path",),
        func=_read_file,
    ),
    Tool(
        name="list_dir",
        label="看目录",
        description="列出一个目录里有什么文件和子目录。",
        properties={
            "path": {"type": "string", "description": "目录路径，不填就是当前目录"},
        },
        required=(),
        func=_list_dir,
    ),
    # ── 记忆 ──
    Tool(
        name="memory_search",
        label="翻记忆",
        description="翻自己关于用户的长期记忆。不填关键词就全列出来。",
        properties={
            "query": {"type": "string", "description": "关键词，可留空"},
        },
        required=(),
        func=_memory_search,
    ),
    Tool(
        name="memory_add",
        label="记一笔",
        description=(
            "把关于用户的一条长期信息记下来（称呼、偏好、约定、正在做的事）。"
            "只记长期有效的，别记一次性的闲聊；绝对不要记密码、证件号、住址、健康隐私。"
        ),
        properties={
            "key": {"type": "string", "description": "简短的英文小写下划线标识，例如 prefers_nickname"},
            "value": {"type": "string", "description": "中文描述"},
        },
        required=("key", "value"),
        func=_memory_add,
    ),
    Tool(
        name="memory_forget",
        label="忘掉一条",
        description="删掉一条长期记忆。用户说「别记这个」「忘掉」时用。",
        properties={
            "key": {"type": "string", "description": "要删掉的记忆标识"},
        },
        required=("key",),
        func=_memory_forget,
    ),
    # ── 桌面 ──
    Tool(
        name="screenshot",
        label="截屏",
        description="截取整个屏幕并存成图片，返回文件路径。",
        properties={},
        required=(),
        func=_screenshot,
    ),
    Tool(
        name="clipboard_read",
        label="看剪贴板",
        description="读剪贴板里的文字。",
        properties={},
        required=(),
        func=_clipboard_read,
    ),
    Tool(
        name="clipboard_write",
        label="写剪贴板",
        description="把一段文字放进剪贴板。",
        properties={
            "text": {"type": "string", "description": "要放进剪贴板的文字"},
        },
        required=("text",),
        func=_clipboard_write,
    ),
    # ── 下面这几个动手改东西，属于 full 档，且每次都要用户点头 ──
    Tool(
        name="write_file",
        label="写文件",
        description="新建一个文件，或者整个覆盖一个已有文件。只改一小段请用 edit_file。",
        properties={
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整内容"},
        },
        required=("path", "content"),
        func=_write_file,
        safe=False,
        preview=_preview_write,
    ),
    Tool(
        name="edit_file",
        label="改文件",
        description=(
            "在文件里做一次精确替换。find 必须在文件里**只出现一次**，"
            "所以要带上足够的上下文。改之前最好先 read_file 看一眼。"
        ),
        properties={
            "path": {"type": "string", "description": "文件路径"},
            "find": {"type": "string", "description": "要被替换掉的原文（含空格换行，必须一字不差）"},
            "replace": {"type": "string", "description": "替换成什么"},
        },
        required=("path", "find", "replace"),
        func=_edit_file,
        safe=False,
        preview=_preview_edit,
    ),
    Tool(
        name="open_path",
        label="打开",
        description="用系统的默认程序打开一个文件、文件夹或者网址（比如给用户看一张图）。",
        properties={
            "path": {"type": "string", "description": "文件、目录或网址"},
        },
        required=("path",),
        func=_open_path,
        safe=False,
        preview=_preview_open,
    ),
    Tool(
        name="run_command",
        label="执行命令",
        description=(
            "在本机执行一条 shell 命令并返回输出。Windows 上跑的是 PowerShell，"
            "所以用 PowerShell 的语法（ls / cat / rm 这些别名也能用）。"
            "别用它做需要交互的命令 —— 没有输入可以给它。"
        ),
        properties={
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {"type": "string", "description": "在哪个目录下跑，不填就是程序自己的工作目录"},
            "timeout": {"type": "integer", "description": "最多跑多少秒，默认 60，上限 300"},
        },
        required=("command",),
        func=_run_command,
        safe=False,
        preview=_preview_command,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in ALL_TOOLS}

PROFILES = ("off", "safe", "full")


def tool_label(name: str) -> str:
    """工具的中文名，给界面上的状态条用。不认识就退回原名。"""
    tool = TOOLS_BY_NAME.get(name)
    return tool.label if tool else name


def tool_command(name: str, args: dict | None = None) -> str:
    """把一次工具调用翻成界面上那句「到底执行了什么」。

    界面只写工具名的话，用户看到的就是一句干巴巴的「正在用某个工具」，
    他没东西可判断，也就没法决定要不要放心。所以参数得一起摆出来。

    数据来源分两种：

    · 危险工具自己带 preview —— 那句话本来就是写给审批弹窗的，最贴切。
      但它是完整一句「执行命令：xxx」，而卡片上已经写了工具名，
      所以把开头那截「工具名：」摘掉，只留命令本身。
    · 安全工具没有 preview，按参数拼一行：只有一个字符串参数就直接给原样
      （读文件就是那条路径、搜索就是那个关键词），多参数才写成 key=value。

    这里**不许把调用搞挂** —— 它只服务于界面，翻不出来也得让工具照常跑。
    """
    args = args or {}
    tool = TOOLS_BY_NAME.get(name)

    if tool is not None and tool.preview is not None:
        try:
            text = tool.preview(args)
        except Exception:
            log.debug("preview 生成失败，退回通用格式：%s", name, exc_info=True)
        else:
            prefix = f"{tool.label}："
            return text[len(prefix):] if text.startswith(prefix) else text

    return _format_args(args)


def _format_args(args: dict) -> str:
    """把参数拼成一行人话。只有一个字符串参数就直接给值，否则写成 key=value。"""
    items = [(k, v) for k, v in (args or {}).items() if v not in ("", None)]
    if not items:
        return ""
    if len(items) == 1 and isinstance(items[0][1], str):
        return items[0][1]
    parts = []
    for key, value in items:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                # 正常流程走不到这儿（参数来自 json.loads，一定是可序列化的），
                # 但 tool_command 承诺过不把工具搞挂，就剩这一处可能反悔。
                text = str(value)
        parts.append(f"{key}={text}")
    return " ".join(parts)


# 卡片上那行结果最多留这么长。完整结果动辄上万字（run_command 的上限是 20_000），
# 全塞进渲染进程只为显示一行小字，不值当。
RESULT_SUMMARY_CHARS = 240

# 哪些开头算「这次没成」。项目里的约定是工具不抛异常、把问题当结果喂回去，
# 所以只能靠这句话的开头认出来。改任何工具的错误文案时记得同步这儿
# （tests/test_tools.py 钉着它）。
_FAILURE_PREFIXES = (
    "错误：",               # 参数不对（core/chat.py 的 _run_tool）
    "工具执行失败（",        # 工具里炸了（Toolbox._bind）
    "没有可用的确认界面",    # 危险工具没有审批界面 → fail closed
    "用户拒绝了这次调用",    # 用户在弹窗上点了拒绝
    "搜索失败",
    "搜索超时",
)


def tool_result(content: str) -> str:
    """把工具结果压成卡片上那一行「它拿回来了什么」。

    界面只要一眼看出「有没有结果」，不必看到全文。折叠掉换行和连续空白，
    太长就截断并在尾巴上注明总长度 —— 让用户知道是被截了，而不是就这么点。
    """
    text = " ".join(str(content or "").split())
    if len(text) <= RESULT_SUMMARY_CHARS:
        return text
    return f"{text[:RESULT_SUMMARY_CHARS]}…（共 {len(text)} 字）"


def tool_failed(content: str) -> bool:
    """这次调用是不是没成。只影响卡片长什么样，不参与任何判断。"""
    return str(content or "").lstrip().startswith(_FAILURE_PREFIXES)


def tool_names(profile: str = "safe") -> list[str]:
    return [t.name for t in _select(profile, (), ())]


def _select(profile: str, allow: tuple[str, ...] | list[str], deny: tuple[str, ...] | list[str]) -> list[Tool]:
    """按档位 + allow/deny 挑出这次要挂的工具。

    allow 是**白名单**（非空就只在里面挑），deny 是黑名单（永远踢掉）。
    这样想单独开一个危险工具的人可以写 allow=["run_command"]，
    而不必把整个 full 档打开。
    """
    if profile not in PROFILES:
        profile = "safe"
    if profile == "off":
        return []

    chosen = [t for t in ALL_TOOLS if t.safe or profile == "full"]

    allow_set = {str(x).strip() for x in (allow or ()) if str(x).strip()}
    if allow_set:
        chosen = [t for t in chosen if t.name in allow_set]

    deny_set = {str(x).strip() for x in (deny or ()) if str(x).strip()}
    if deny_set:
        chosen = [t for t in chosen if t.name not in deny_set]

    return chosen


class Toolbox:
    """一次会话里所有工具的持有者。

    approve 是 async (name, args, preview) -> bool；由 backend 注入。
    危险工具在动手前会 await 它，用户点「拒绝」就返回 _denied()。
    **approve 是 None 时危险工具一律拒绝**（fail closed）。
    """

    def __init__(self, *, approve: Callable[[str, dict, str], Awaitable[bool]] | None = None) -> None:
        self.approve = approve

    def build(
        self,
        profile: str = "safe",
        *,
        allow: tuple[str, ...] | list[str] = (),
        deny: tuple[str, ...] | list[str] = (),
    ) -> tuple[list[dict], dict[str, Callable[..., Awaitable[str]]]]:
        tools = _select(profile, allow, deny)
        schemas = [t.schema() for t in tools]
        impl = {t.name: self._bind(t) for t in tools}
        return schemas, impl

    def _bind(self, tool: Tool) -> Callable[..., Awaitable[str]]:
        async def call(**args: Any) -> str:
            if tool.preview is not None:
                if self.approve is None:
                    # 没有确认界面还敢动手？不做。
                    log.info("工具 %s：需要审批但没有确认界面，已取消", tool.name)
                    return "没有可用的确认界面，这次调用已取消。"
                preview = tool.preview(args)
                try:
                    ok = await self.approve(tool.name, args, preview)
                except Exception:
                    log.warning("审批回调炸了，按拒绝处理", exc_info=True)
                    ok = False
                if not ok:
                    log.info("工具 %s：用户拒绝了", tool.name)
                    return _denied()

            # ★ 每次调用都留一条 INFO：工具名、成没成、多大、多久。
            # 「有时候搜不出来」这类问题，光看代码是猜的 —— 有没有真的调、
            # 调了多久、回没回东西，只有日志说得清。
            # 只记元信息：参数和结果都带着用户自己的东西，不进日志（见 core/logging_setup.py）。
            started = time.monotonic()
            try:
                result = await tool.func(self, **args)
            except Exception as exc:
                # 约定：工具不抛异常。真炸了也翻译成一句模型能读懂的话。
                log.warning("工具 %s 执行失败：%s: %s", tool.name, type(exc).__name__, exc)
                return f"工具执行失败（{type(exc).__name__}）。换个方式再试，或者告诉用户这条路走不通。"
            log.info(
                "工具 %s：返回 %d 字，%.1f 秒",
                tool.name,
                len(result or ""),
                time.monotonic() - started,
            )
            return result

        return call


def build_tools(
    profile: str = "safe",
    *,
    approve: Callable[[str, dict, str], Awaitable[bool]] | None = None,
    allow: tuple[str, ...] | list[str] = (),
    deny: tuple[str, ...] | list[str] = (),
) -> tuple[list[dict], dict[str, Callable[..., Awaitable[str]]]]:
    """一次性建好（不给后续改 approve 的场合用）。"""
    return Toolbox(approve=approve).build(profile, allow=allow, deny=deny)
