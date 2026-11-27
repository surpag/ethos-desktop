"""浏览器工具集：把 7 个 `browser_*` 工具接进现有 Tool Registry。

三条设计约束（与方案 §3.5 / §7 一致）：

1. **不绕过权限体系**：能力声明在 `permissions/catalog.py`，档位裁决、审批、
   只读调度走的都是既有链路。这一层只做「参数 → RPC → 结构化结果」。
2. **模型不能提交 selector、不能提交 JS**：`ref` 只能来自最近一次快照，形如 `el_007`；
   校验在 Electron 侧（`SnapshotStore`），这里不重复实现，也不放宽。
3. **失败要能自救**：`stale_observation` / `element_missing` 这类是 `command_error`
   （可重试 + 给建议），`browser_disabled` / RPC 不可用是 `tool_failure`
   （不计入「模型乱试」的熔断语义，且明确告诉模型别再试）。
"""

from __future__ import annotations

import json
import time

from ..permissions.types import COMMAND_ERROR, TOOL_FAILURE
from ..tools import record_tool_trace, tool_error_result
from .service import BrowserService
from .types import BrowserRpcError

#: trace 里的动作名：去掉 `browser_` 前缀，比工具名更适合聚合（`snapshot` / `click`）。
_ACTION_NAMES = {
    "browser_get_url": "get_url",
    "browser_open_url": "open_url",
    "browser_get_page_snapshot": "snapshot",
    "browser_click": "click",
    "browser_fill": "fill",
    "browser_press": "press",
    "browser_scroll": "scroll",
}


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _trace_fields(**fields) -> dict:
    """空值不进 metadata：`tab_id=""` 只会让查询方多一次 `is not null` 判断。"""
    return {key: value for key, value in fields.items() if value not in (None, "")}

#: 模型换个做法就能继续的错误：重新取快照、换个 ref、先开页面。
_SELF_HEAL_HINTS: dict[str, str] = {
    "stale_observation": "页面在取快照之后发生了变化。请重新调用 browser_get_page_snapshot，用新返回的 ref 操作。",
    "element_missing": "这个 ref 不在当前页面上。请重新调用 browser_get_page_snapshot，只使用返回列表里的 ref。",
    "element_not_visible": "元素当前不可见（可能折叠或在懒加载区域）。请滚动页面后重新取快照。",
    "element_disabled": "元素处于禁用状态。请换一个可操作的元素，或先完成前置步骤。",
    "element_changed": "这个位置的内容在取快照之后变了（页面局部刷新）。请重新调用 browser_get_page_snapshot，确认新内容后再操作。",
    "no_active_tab": "用户当前没有打开任何标签页。请让用户在浏览器面板里打开一个页面后再继续。",
    "invalid_arguments": "参数不合法。ref 必须来自最近一次快照，key 只能是允许的按键，direction 只能是 up/down/top/bottom。",
    "stale_page": "页面已经变化，请重新取快照。",
    "user_took_control": "用户刚刚接管了页面（导航/切换标签页）。请稍等一秒，重新调用 browser_get_page_snapshot 确认新页面后再操作；如果用户正在操作，先问是否可以继续。",
    "page_crashed": "页面渲染进程崩溃了，现在显示的是错误页。请重新调用 browser_open_url 打开目标页面，再取快照继续。",
    # 导航目标打不开是**模型能处理**的：本地服务没起来就先去起，URL 写错就改。
    # 之前这种情况被 RPC 兜底成 500，模型只看到「内部错误」而白白重试或放弃。
    "navigation_failed": (
        "目标页面打不开（连接被拒 / DNS / 证书）。若这是本地服务，说明它还没起来："
        "先用 sandbox_start_process 启动（命令里不要带 &），"
        "再用 sandbox_run_command 确认端口返回 200，然后重新打开；"
        "否则请核对 URL 是否写错。"
    ),
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """OpenAI function calling 定义（与其它 tool set 保持同一形状）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _str_arg(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BrowserRpcError("invalid_arguments", f"缺少参数 {key}（应为非空字符串）")
    return value.strip()


def browser_error_result(tool_name: str, error: BrowserRpcError) -> str:
    """把 RPC 错误转成工具结果 JSON。

    分类原则：**只有「模型能靠自己修正」的错误**才给 `retryable=True`。
    其余（浏览器没开、令牌不对、RPC 挂了）一律 `tool_failure`，让执行链停止重试，
    否则模型会对着一堵墙试到熔断。
    """
    hint = _SELF_HEAL_HINTS.get(error.code)
    if hint:
        return json.dumps(
            {
                "error": error.message,
                "kind": COMMAND_ERROR,
                "retryable": True,
                "code": error.code,
                "suggestion": hint,
            },
            ensure_ascii=False,
        )

    if error.code == "browser_busy":
        # 另一个 run 正持有控制权。这是**环境状态**，不是模型能改的参数：
        # 重试只会把两个任务都拖死，必须停下来告诉用户。
        return json.dumps(
            {
                "error": "另一个任务正在使用内置浏览器。",
                "kind": TOOL_FAILURE,
                "retryable": False,
                "code": error.code,
                "suggestion": "不要重试。请告诉用户当前有另一个任务在操作浏览器，等它结束后再继续。",
            },
            ensure_ascii=False,
        )

    if error.code == "browser_disabled":
        return json.dumps(
            {
                "error": error.message,
                "kind": TOOL_FAILURE,
                "retryable": False,
                "code": error.code,
                "suggestion": "内置浏览器只在 ETHOS Desktop 中可用。请告知用户改用桌面应用，"
                "或不要继续使用浏览器工具。",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "error": f"浏览器操作失败：{error.message}",
            "kind": TOOL_FAILURE,
            "retryable": False,
            "code": error.code,
            "suggestion": "这是浏览器服务侧的问题（不是参数问题）。请停止调用浏览器工具并向用户说明。",
        },
        ensure_ascii=False,
    )


class BrowserToolSet:
    """Agent 操作「用户正在看的那个浏览器」的唯一入口。"""

    def __init__(self, service: BrowserService) -> None:
        self._service = service

    def tool_definitions(self) -> list[dict]:
        return [
            _tool(
                "browser_open_url",
                "在用户当前可见的内置浏览器里打开一个网址（http/https）。"
                "用户能实时看到页面变化；如果用户没有打开浏览器面板，会提示让你请用户先打开。",
                {"url": {"type": "string", "description": "完整网址，例如 https://example.com"}},
                ["url"],
            ),
            _tool(
                "browser_get_page_snapshot",
                "读取当前页面里可交互的元素，返回 observation_id 与元素列表（ref/role/name）。"
                "**点击、填写、按键之前必须先调用它**；页面变化（导航、点击跳转）后必须重新调用，"
                "旧 ref 会失效。"
                "元素很多时会分页：返回里 `nextCursor` 不为 null 表示还有下一段，"
                "把它作为 `cursor` 再调一次即可（同一份快照，ref 可跨页使用）。",
                {
                    "max_elements": {
                        "type": "integer",
                        "description": "每页最多返回多少个元素，默认 120，上限 200",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "上一页返回的 nextCursor；不填表示从第一页开始",
                    },
                },
                [],
            ),
            _tool(
                "browser_click",
                "点击当前页面上的一个元素。必须使用最近一次 browser_get_page_snapshot 返回的 "
                "observation_id 与 ref。",
                {
                    "observation_id": {"type": "string", "description": "快照返回的 observation_id"},
                    "ref": {"type": "string", "description": "快照返回的元素 ref，形如 el_007"},
                },
                ["observation_id", "ref"],
            ),
            _tool(
                "browser_fill",
                "向当前页面的输入框/文本域填写内容（覆盖原值），并触发 input/change 事件。"
                "填写后通常还需要 browser_press 回车或 browser_click 提交按钮。",
                {
                    "observation_id": {"type": "string", "description": "快照返回的 observation_id"},
                    "ref": {"type": "string", "description": "输入框的 ref"},
                    "value": {"type": "string", "description": "要填入的文本"},
                },
                ["observation_id", "ref", "value"],
            ),
            _tool(
                "browser_press",
                "在指定元素上按一个键。用于回车提交表单、Tab 切换焦点、Escape 关闭弹层。",
                {
                    "observation_id": {"type": "string", "description": "快照返回的 observation_id"},
                    "ref": {"type": "string", "description": "目标元素的 ref"},
                    "key": {
                        "type": "string",
                        "description": "Enter / Tab / Escape / Backspace / ArrowUp / ArrowDown / ArrowLeft / ArrowRight / 空格",
                    },
                },
                ["observation_id", "ref", "key"],
            ),
            _tool(
                "browser_scroll",
                "滚动当前页面。滚动后如需操作新出现的元素，请重新取快照。",
                {
                    "direction": {
                        "type": "string",
                        "description": "up / down / top / bottom，默认 down",
                    },
                    "amount": {"type": "integer", "description": "滚动像素，默认 500"},
                },
                [],
            ),
            _tool(
                "browser_get_url",
                "获取当前标签页的网址与标题。用于确认页面是否真的跳转成功。",
                {},
                [],
            ),
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",  # 仅接口兼容（ToolRegistry 统一透传），浏览器与工作区无关
        context=None,  # 仅接口兼容（ToolRegistry 统一透传），本工具集不使用
        tool_call_id: str = "",  # 仅接口兼容（ToolRegistry 统一透传），本工具集不使用
        request_id: str = "",  # 控制权归属：哪个 run 在操作浏览器
    ) -> str:
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            return json.dumps(
                {"error": f"unknown browser tool: {name}", "kind": TOOL_FAILURE},
                ensure_ascii=False,
            )
        action = _ACTION_NAMES.get(name, name)
        started = time.monotonic()
        try:
            payload = await handler(arguments, request_id)
        except BrowserRpcError as error:
            # 失败也要进 trace：只有成功路径有记录的话，「模型为什么卡住」永远查不到。
            record_tool_trace(
                **_trace_fields(
                    action=action,
                    status="error",
                    error_code=error.code,
                    tool_ms=_elapsed_ms(started),
                )
            )
            return browser_error_result(name, error)
        except Exception as error:  # noqa: BLE001 - 与其它 tool set 一样统一出口
            record_tool_trace(
                **_trace_fields(
                    action=action,
                    status="error",
                    error_code=type(error).__name__,
                    tool_ms=_elapsed_ms(started),
                )
            )
            return tool_error_result(name, error)

        elements = payload.get("elements")
        record_tool_trace(
            **_trace_fields(
                action=action,
                status="ok",
                tab_id=str(payload.get("tabId") or ""),
                observation_id=str(
                    payload.get("observationId") or arguments.get("observation_id") or ""
                ),
                elements=len(elements) if isinstance(elements, list) else None,
                tool_ms=_elapsed_ms(started),
            )
        )
        return json.dumps(payload, ensure_ascii=False)

    # ── 各工具实现 ────────────────────────────────────────────────────────

    async def _op_browser_get_url(self, arguments: dict, request_id: str) -> dict:
        # `page.url` 不接管控制权：只读当前地址，不改页面，任何 run 都可以看。
        return await self._service.current_url()

    async def _op_browser_open_url(self, arguments: dict, request_id: str) -> dict:
        return await self._service.navigate(_str_arg(arguments, "url"), request_id=request_id)

    async def _op_browser_get_page_snapshot(self, arguments: dict, request_id: str) -> dict:
        max_elements = arguments.get("max_elements")
        cursor = arguments.get("cursor")
        params: dict = {}
        if isinstance(max_elements, int):
            params["max_elements"] = max_elements
        if isinstance(cursor, str) and cursor.strip():
            params["cursor"] = cursor.strip()
        return await self._service.call("page.snapshot", params, request_id=request_id)

    async def _op_browser_click(self, arguments: dict, request_id: str) -> dict:
        return await self._service.call(
            "page.click",
            {"observation_id": _str_arg(arguments, "observation_id"), "ref": _str_arg(arguments, "ref")},
            request_id=request_id,
        )

    async def _op_browser_fill(self, arguments: dict, request_id: str) -> dict:
        value = arguments.get("value")
        if not isinstance(value, str):
            raise BrowserRpcError("invalid_arguments", "缺少参数 value（应为字符串）")
        return await self._service.call(
            "page.fill",
            {
                "observation_id": _str_arg(arguments, "observation_id"),
                "ref": _str_arg(arguments, "ref"),
                "value": value,
            },
            request_id=request_id,
        )

    async def _op_browser_press(self, arguments: dict, request_id: str) -> dict:
        return await self._service.call(
            "page.press",
            {
                "observation_id": _str_arg(arguments, "observation_id"),
                "ref": _str_arg(arguments, "ref"),
                "key": _str_arg(arguments, "key"),
            },
            request_id=request_id,
        )

    async def _op_browser_scroll(self, arguments: dict, request_id: str) -> dict:
        direction = arguments.get("direction")
        amount = arguments.get("amount")
        params: dict = {}
        if isinstance(direction, str) and direction:
            params["direction"] = direction
        if isinstance(amount, int):
            params["amount"] = amount
        return await self._service.call("page.scroll", params, request_id=request_id)
