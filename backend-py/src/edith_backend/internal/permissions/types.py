"""权限系统的类型：能力、档位、工具结果分类。

## 能力是集合，不是等级

`EXEC` 与 `WRITE` 是并列的两把刀，不是「高一级的写」。一旦按等级比较
（`needed > WRITE` 就拒绝），`{EXEC}` 会落在 `WRITE` 之上被放行——而一条
shell 命令能读写删文件、能联网、能起后台进程。所以判定只用集合包含：
`needed ⊆ allowed`。

## 工具结果分类是显式枚举

旧实现只回一个 `retryable` 布尔值，再从它反推「这是不是系统故障」。于是
「权限不足」这种**确定性的、重试无用的、绝不该计入熔断**的结果，只能被迫写成
`retryable=false`，而 `false` 在那套语义里就等于「系统故障」——三次拒绝就把整个
任务熔断掉。分类必须是枚举，因为「谁该重试、谁该计数、谁该显示成失败」是三个
不同的问题，一个布尔装不下。
"""

from __future__ import annotations

import json
from enum import StrEnum

# ── 工具结果分类（工具结果 JSON 里的 `kind` 字段）─────────────────────────
#: 系统故障：SDK 异常 / 连接断开 / 超时。重试无用，**参与熔断**。
TOOL_FAILURE = "tool_failure"
#: 业务错误：命令失败 / 路径不存在 / 参数不合法。模型可自救，不熔断。
COMMAND_ERROR = "command_error"
#: 当前档位不允许。确定性拒绝，**不参与熔断**（模型换参数也没用）。
PERMISSION_DENIED = "permission_denied"
#: 当前档位要求审批，而审批基础设施尚未实现（P2）。同样不参与熔断。
APPROVAL_REQUIRED = "approval_required"
#: 模型给出的参数不是合法 JSON。属于模型错误，可自救。
INVALID_ARGUMENTS = "invalid_arguments"

#: 计为「失败」的 kind（UI 状态用）。注意它与熔断判据不是同一集合：
#: 权限拒绝显示为失败，但绝不能把整轮任务熔断掉。
_FAILED_KINDS = frozenset(
    {TOOL_FAILURE, PERMISSION_DENIED, APPROVAL_REQUIRED, INVALID_ARGUMENTS}
)


def result_kind(result: str) -> str | None:
    """从工具结果 JSON 里取 `kind`；取不到（MCP 原文 / 非 JSON）返回 None。"""
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        kind = data.get("kind")
        return kind if isinstance(kind, str) else None
    return None


def kind_is_retryable(kind: str | None) -> bool:
    """模型能否通过改写参数/路径来自救。None（无 kind）= 成功结果。"""
    return kind in (COMMAND_ERROR, INVALID_ARGUMENTS)


def kind_counts_toward_breaker(kind: str | None) -> bool:
    """是否参与「同工具连续系统故障」熔断统计。"""
    return kind == TOOL_FAILURE


def kind_status(kind: str | None) -> str:
    """工具调用的 UI 状态：completed / failed。"""
    return "failed" if kind in _FAILED_KINDS else "completed"


class Capability(StrEnum):
    """一个工具**可能**造成的副作用类别。"""

    READ = "read"  # 只读取，不改变任何状态
    WRITE = "write"  # 改变本机文件 / 我们自己的业务数据
    EXEC = "exec"  # 执行任意程序，副作用由程序决定（不可枚举）
    EXTERNAL = "external"  # 效果越出本次 run：远端、未来 run、跨 run 上下文
    METADATA = "metadata"  # 只动我们自己的元数据（计划行），用户可见世界零影响


class PermissionLevel(StrEnum):
    """用户可配置的执行档位。"""

    READ_ONLY = "read_only"
    APPROVAL = "approval"
    FULL = "full"


#: 档位从严到宽的顺序（`min_level` 用）。**只有档位可以排序，能力不能排序。**
LEVEL_ORDER: tuple[PermissionLevel, ...] = (
    PermissionLevel.READ_ONLY,
    PermissionLevel.APPROVAL,
    PermissionLevel.FULL,
)

#: 未配置时使用的档位。桌面端单用户：默认保持既有行为（`full`），
#: 否则一上线就把所有人的文件写入静默关掉。
DEFAULT_LEVEL = PermissionLevel.FULL

#: 库里存了非法档位时的降级目标：宁可锁死，不可提权。
FALLBACK_LEVEL = PermissionLevel.READ_ONLY
