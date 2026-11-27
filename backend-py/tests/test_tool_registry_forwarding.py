"""ToolRegistry 转发签名的一致性守卫。

为什么要有这组用例：2026-11-20 与 2026-11-24 各踩过一次同一形态的故障——
`agentrun/execution.py` 给 `ToolRegistry.execute` 传了新的关键字参数（先 `context`、
后 `tool_call_id`），而中间那层转发没跟着加形参。Python 在**参数绑定阶段**就抛
`TypeError`，函数体一行都不执行，于是**所有**工具（包括 `get_current_time` 这种
与沙箱无关的工具）一起挂；执行链的兜底又把它包装成 `tool_failure(retryable=false)`，
三次之后熔断整轮任务。现场看到的是「工具层系统故障，重试无法解决」，与实际原因
（一个参数名没对上）差得很远。

为什么 809 个用例没抓到：此前只有 `test_permissions.py` 用 `ToolRegistry([])` 做过
一次纯位置参数调用——注册表为空、不带 `tool_call_id`、也不经过任何真实工具集。
也就是说整条「注册表 → 工具集」的转发路径没有覆盖。

这里钉三件事：

1. 注册表自身声明了它要透传的关键字；
2. 每个工具集的 `execute` 都接受这些关键字（反射检查，不需要实例化）；
3. 真实 `ToolRegistry` + 真实工具集带 `tool_call_id` 的调用能拿到正常结果。
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from edith_backend.internal.agentrun.plan_tools import PlanToolSet
from edith_backend.internal.browser.service import BrowserService
from edith_backend.internal.browser.tools import BrowserToolSet
from edith_backend.internal.cronjob.tools import CronToolSet
from edith_backend.internal.sandbox.tools import SandboxToolSet
from edith_backend.internal.systemtools.tools import SystemToolSet
from edith_backend.internal.tools import ToolRegistry

#: `ToolRegistry.execute` 按关键字透传给每个工具集的参数。
#: 新增一个透传参数时，这里和 `_TOOL_SET_CLASSES` 一起覆盖，缺一即红。
_FORWARDED_KEYWORDS = ("context", "tool_call_id", "request_id")

#: 所有会被注册进 `ToolRegistry` 的工具集（见 `main.py` 的装配）。
_TOOL_SET_CLASSES = (
    SandboxToolSet,
    PlanToolSet,
    CronToolSet,
    SystemToolSet,
    BrowserToolSet,
)


def test_registry_declares_forwarded_keywords() -> None:
    """注册表自己要收下这些关键字，否则调用方传进来就当场 TypeError。"""
    params = inspect.signature(ToolRegistry.execute).parameters
    missing = [name for name in _FORWARDED_KEYWORDS if name not in params]
    assert not missing, f"ToolRegistry.execute 缺少 {missing}"


@pytest.mark.parametrize("cls", _TOOL_SET_CLASSES, ids=lambda cls: cls.__name__)
def test_tool_set_accepts_registry_forwarded_kwargs(cls: type) -> None:
    """工具集必须接受注册表透传的全部关键字。

    反射检查而不是实例化：这些工具集需要 DB、沙箱、计划注册表等依赖，
    构造它们的代价与本用例要证明的事情无关。签名错了就是签名错了。
    """
    params = inspect.signature(cls.execute).parameters
    missing = [name for name in _FORWARDED_KEYWORDS if name not in params]
    assert not missing, (
        f"{cls.__name__}.execute 缺少 ToolRegistry 透传的关键字 {missing}："
        f"注册表会按关键字调用它，缺一个就会让**所有**工具调用抛 TypeError"
    )
    # 必须带默认值：新增的透传参数对老调用点（测试、脚本）保持兼容。
    no_default = [
        name
        for name in _FORWARDED_KEYWORDS
        if params[name].default is inspect.Parameter.empty
    ]
    assert not no_default, f"{cls.__name__}.execute 的 {no_default} 必须带默认值"


def test_registry_forwards_tool_call_id_end_to_end() -> None:
    """真实注册表 + 真实工具集：带 `tool_call_id` 调用必须成功。

    这是回归用例的重点——`get_current_time` 与沙箱无关，它挂了就说明故障在
    分发之前（注册表入口），而不是某个工具自己的问题。
    """

    async def body() -> None:
        registry = ToolRegistry([SystemToolSet()])
        raw = await registry.execute(
            "get_current_time",
            {"timezone": "Asia/Shanghai"},
            "local-user",
            "session-1",
            "",
            context=None,
            tool_call_id="call-1",
        )
        payload = json.loads(raw)
        assert "time" in payload, payload
        assert payload["timezone"] == "Asia/Shanghai"
        assert "error" not in payload, payload

    asyncio.run(body())


class _Recorder:
    """只记录注册表转发进来的关键字，用来验证 request_id 真的走到了工具集。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "record_me",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",
        context=None,
        tool_call_id: str = "",
        request_id: str = "",
    ) -> str:
        self.seen.append({"tool_call_id": tool_call_id, "request_id": request_id})
        return json.dumps({"ok": True})


def test_registry_forwards_request_id_to_tool_set() -> None:
    """`request_id` 必须真的到工具集手里：控制权绑定靠它区分是哪个 run。"""
    recorder = _Recorder()

    async def body() -> None:
        raw = await ToolRegistry([recorder]).execute(
            "record_me",
            {},
            "u",
            "s",
            "",
            context=None,
            tool_call_id="call-1",
            request_id="req-9",
        )
        assert json.loads(raw) == {"ok": True}

    asyncio.run(body())
    assert recorder.seen == [{"tool_call_id": "call-1", "request_id": "req-9"}]


def test_registry_unknown_tool_still_classified_as_business_error() -> None:
    """加了透传参数之后，「未知工具」的错误分类不能被带坏（仍不计熔断）。"""

    async def body() -> None:
        raw = await ToolRegistry([]).execute(
            "nope", {}, "u", "s", "", context=None, tool_call_id="call-1"
        )
        payload = json.loads(raw)
        assert payload["retryable"] is True
        assert payload["kind"] == "command_error"

    asyncio.run(body())
