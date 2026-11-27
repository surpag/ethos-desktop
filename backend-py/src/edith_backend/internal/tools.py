"""tools 聚合器：汇总各模块的 Agent 工具。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .isolation.errors import SandboxUnavailable
from .permissions.types import COMMAND_ERROR, TOOL_FAILURE

# 工具错误分类：取值定义在 `permissions.types`（唯一权威源）——因为「谁该重试、
# 谁该计熔断、谁该显示成失败」这三件事必须由同一张表回答。这里只做再导出，
# 兼容既有 import（历史代码与本模块的语义一致）。
__all__ = [
    "COMMAND_ERROR",
    "TOOL_FAILURE",
    "ToolRegistry",
    "command_stderr_hint",
    "describe_tool_failure",
    "record_tool_trace",
    "redact_tool_input",
    "redact_tool_output",
    "tool_error_result",
    "tool_trace_scope",
]

# 工具自报的追踪元数据（浏览器工具用它补 tab_id / action / observation_id ...）。
#
# 为什么用 ContextVar 而不是让工具返回「结果 + 元数据」：工具结果必须保持纯字符串，
# 它是模型历史的一部分；一旦混入 `_trace` 字段，模型就会看到平台内部字段，而且
# 每个消费方（SSE、工具卡、历史）都要记得剥离。ContextVar 让元数据走旁路，
# 只被工具调用点读取。
#
# 并发安全：只读工具用 asyncio.gather 并发执行，每个任务复制一份上下文，
# 因此各自 set 的 dict 互不覆盖。
_tool_trace: ContextVar[dict[str, Any] | None] = ContextVar(
    "tool_trace_meta", default=None
)


@contextmanager
def tool_trace_scope() -> Iterator[dict[str, Any]]:
    """在工具执行期间开启一个元数据收集器；退出后仍可读返回的 dict。"""
    meta: dict[str, Any] = {}
    token = _tool_trace.set(meta)
    try:
        yield meta
    finally:
        _tool_trace.reset(token)


def record_tool_trace(**fields: Any) -> None:
    """工具实现写一条追踪字段；不在 scope 内时静默忽略（例如单元测试直调）。"""
    meta = _tool_trace.get()
    if meta is not None:
        meta.update(fields)


# 浏览器结果进 trace 时的字段白名单。
#
# 刻意不含 `elements`（快照明细）与任何 `name`：快照里的输入框名称可能是用户
# 刚填进去的内容，`browser_fill` 的 value 更是直接的表单隐私。trace 的职责是
# 「还原操作链」，不是「复制页面」。
_BROWSER_TRACE_FIELDS = (
    "tabId",
    "url",
    "title",
    "observationId",
    "pageVersion",
    "truncated",
    "omitted",
    "nextCursor",
    "page",
    "budget",
    "clicked",
    "filled",
    "pressed",
    "ref",
    "key",
    "direction",
    "before",
    "after",
    "height",
    "length",
    "tag",
    "submitted",
    "error",
    "kind",
    "retryable",
    "code",
    "suggestion",
)


def redact_tool_input(name: str, raw: str) -> str:
    """trace 里的入参摘要必须先脱敏：`browser_fill` 的 value 可能是密码或隐私输入。"""
    if name != "browser_fill" or not raw:
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict):
        return raw
    value = payload.get("value")
    if isinstance(value, str):
        payload["value"] = f"<redacted: {len(value)} chars>"
    return json.dumps(payload, ensure_ascii=False)


def redact_tool_output(name: str, result: str) -> str:
    """浏览器工具结果进 trace 前裁剪成结构化摘要（去掉元素明细与用户输入）。"""
    if not name.startswith("browser_") or not result:
        return result
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return "<unparsable browser result>"
    if not isinstance(payload, dict):
        return "<non-object browser result>"
    kept = {key: payload[key] for key in _BROWSER_TRACE_FIELDS if key in payload}
    elements = payload.get("elements")
    if isinstance(elements, list):
        kept["elements_count"] = len(elements)
    return json.dumps(kept, ensure_ascii=False)


def describe_tool_failure(tool_name: str, exc: Exception) -> str:
    """把工具系统故障包装成模型可理解、且明确提示「重试无用」的描述。"""
    return (
        f"工具 {tool_name} 执行时发生系统故障：{exc}。"
        "这不是你的命令或参数问题，重试无法解决，请停止调用该工具并向用户说明故障。"
    )


# 业务性异常 → retryable + 针对性 suggestion，引导模型自救（不触发熔断）。
# 每个异常类型配一条可执行的修正建议，让模型「看到错误就知道下一步该怎么做」。
_BUSINESS_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    (
        FileNotFoundError,
        "文件或目录不存在，请先用 sandbox_list_files 查看实际目录结构，再以正确路径重试。",
    ),
    (IsADirectoryError, "目标路径是目录而非文件，请核对目标类型后重试。"),
    (NotADirectoryError, "目标路径是文件而非目录，请核对目标类型后重试。"),
    (PermissionError, "权限不足，请检查目标路径的读写权限，或换用有权限的目录重试。"),
    (
        # 后台进程台账的归属校验：未登记 / 已退出 / pid 被复用都归到这里。
        # 不要重试同一个 pid——它要么不属于本会话，要么已经是另一个进程了。
        ProcessLookupError,
        "该 PID 不属于当前会话的沙箱，或对应进程已经退出（pid 可能已被系统复用）。"
        "请先用 sandbox_list_processes 查看仍存活的进程，再按其中的 pid 操作。",
    ),
)


def tool_error_result(tool_name: str, exc: Exception) -> str:
    """把工具异常转成结构化错误结果 JSON。

    业务错误（文件/路径/权限）→ command_error + retryable=true + suggestion，交给模型自救；
    沙箱不可用 → tool_failure + retryable=false + 明确的安装/诊断指引（不是命令问题）；
    系统故障（超时/SDK 异常等）→ tool_failure + retryable=false，参与熔断统计。
    """
    if isinstance(exc, SandboxUnavailable):
        # 单独一条：这类失败换命令、换路径、重试都不会成功，而且用户需要的是
        # "去装后端/看设置"这样的动作，不是"再试一次"。文案里不要出现"系统故障"，
        # 否则模型会误判成偶发问题并重试到熔断。
        return json.dumps(
            {
                "error": f"命令未执行：{exc}",
                "kind": TOOL_FAILURE,
                "retryable": False,
                "reason": f"sandbox_{exc.reason}",
                "suggestion": "这是本机沙箱配置问题，与你写的命令无关。请按错误信息里的指引修复后重试；"
                "在修复之前，所有命令类工具都会被拒绝执行（不会回落到直接运行）。",
            },
            ensure_ascii=False,
        )
    for err_type, suggestion in _BUSINESS_ERRORS:
        if isinstance(exc, err_type):
            return json.dumps(
                {
                    "error": str(exc),
                    "kind": COMMAND_ERROR,
                    "retryable": True,
                    "suggestion": suggestion,
                },
                ensure_ascii=False,
            )
    return json.dumps(
        {
            "error": describe_tool_failure(tool_name, exc),
            "kind": TOOL_FAILURE,
            "retryable": False,
        },
        ensure_ascii=False,
    )


# 命令 stderr 常见业务错误关键词 → 针对性建议。
# 用于识别「exit_code=0 但 stderr 有错」的伪成功（如 cat 找不到文件），避免模型误判为成功。
_COMMAND_STDERR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "no such file or directory",
        "命令引用的文件或目录不存在，请先用 sandbox_list_files 确认实际路径后重试。",
    ),
    (
        "command not found",
        "命令不存在，请检查命令名是否拼写正确，或换用沙箱中已安装的命令。",
    ),
    (
        "permission denied",
        "权限不足，请检查目标路径的读写/执行权限，或换用有权限的目录。",
    ),
    ("not a directory", "目标路径不是目录，请核对目标类型后重试。"),
    ("is a directory", "目标路径是目录而非文件，请核对目标类型后重试。"),
    ("cannot access", "无法访问目标路径，请确认路径存在且可读后重试。"),
)


def command_stderr_hint(stderr: str) -> str | None:
    """从命令 stderr 识别业务错误，返回针对性建议；无命中返回 None。"""
    low = (stderr or "").lower()
    for keyword, suggestion in _COMMAND_STDERR_HINTS:
        if keyword in low:
            return suggestion
    return None


class ToolRegistry:
    """汇总多个工具集，统一提供工具定义与执行分发。"""

    def __init__(self, tool_sets: list) -> None:
        self._tool_sets = tool_sets
        self._by_name: dict[str, object] = {}
        for tool_set in tool_sets:
            for definition in tool_set.tool_definitions():
                self._by_name[definition["function"]["name"]] = tool_set

    def tool_definitions(self) -> list[dict]:
        return [d for tool_set in self._tool_sets for d in tool_set.tool_definitions()]

    def has(self, name: str) -> bool:
        """该名字是否是本注册表里已注册的工具。

        权限裁决要用它区分「未注册」（模型幻觉的名字）与「已注册但未声明能力」
        （平台漏登记）——两者都必须拒绝，但排查方向完全相反。
        """
        return name in self._by_name

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
        """统一执行入口（透传 workspace_id / context / tool_call_id / request_id 给各工具集）。

        `context`（S2）：本次执行的工作区快照，只对 Sandbox 工具集有意义，
        决定 `workspace/{id}` 的真实落点。其余工具集为接口兼容接收但不使用。

        `tool_call_id`：审计归属（`isolation.audit.bind_tool_call`），只对 Sandbox
        工具集有意义。**这个形参必须与调用方同步**：`execution.py` 已经按关键字
        传了它，转发层少一个形参就会在参数绑定阶段抛 `TypeError`——而那个异常会被
        执行链的兜底包装成 `tool_failure(retryable=false)`，表现为「所有工具一起
        系统故障」，2026-11-20（`context`）与 2026-11-24（`tool_call_id`）各发生过
        一次。防回归用例见 `tests/test_tool_registry_forwarding.py`。
        """
        tool_set = self._by_name.get(name)
        if tool_set is None:
            # 模型幻觉出的工具名是**模型错误**，不是系统故障：写成 tool_failure 会被
            # 熔断统计当成「工具坏了」，三次幻觉就能把整轮任务杀掉。
            return json.dumps(
                {
                    "error": f"工具 {name} 不存在",
                    "kind": COMMAND_ERROR,
                    "retryable": True,
                    "suggestion": "这个工具名不存在，请只调用工具列表中出现的工具。",
                },
                ensure_ascii=False,
            )
        try:
            return await tool_set.execute(
                name,
                arguments,
                user_id,
                session_id,
                workspace_id,
                context=context,
                tool_call_id=tool_call_id,
                request_id=request_id,
            )  # 执行三个不同类型的tool，systemtool,sandbox,CronTool
        except Exception as exc:  # noqa: BLE001 - 单个工具的任何异常都转成工具结果返回
            return tool_error_result(name, exc)
