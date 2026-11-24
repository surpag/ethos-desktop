"""权限系统：能力声明、档位裁决、安全约束。

与执行层的关系（一句话）：**执行层只负责执行，是否允许执行只在这里回答。**

分两层，不可互相替代：

- **档位裁决**（`policy`）：可配置，两个作用域——用户级（全局）与项目级
  （工作区）。**项目覆盖用户**，没设项目就用用户级；定时任务的授权快照只能
  再往回压。只读 / 审批 / 完全三档，按「能力集合」判定。
- **安全约束**（`paths`）：不可配置。敏感路径三档皆拦，`full` 也拦。

为什么分开：档位是「用户想让我做多少事」，安全约束是「无论用户想做什么都不做」。
把后者做成档位的一个子集，就等于「完全档 = 没有任何底线」。
"""

from .catalog import MCP_CAPABILITIES, capabilities_of, is_readonly
from .chain import resolve_effective_level
from .context import bind_level, current_level
from .paths import PathRejected, check_path, guard_real_path
from .policy import (
    InvalidLevelValue,
    Resolution,
    Verdict,
    allowed_capabilities,
    evaluate,
    evaluate_call,
    min_level,
    parse_level,
    parse_level_input,
    resolve_chain,
)
from .types import (
    APPROVAL_REQUIRED,
    COMMAND_ERROR,
    INVALID_ARGUMENTS,
    PERMISSION_DENIED,
    TOOL_FAILURE,
    Capability,
    PermissionLevel,
    kind_counts_toward_breaker,
    kind_is_retryable,
    kind_status,
    result_kind,
)

__all__ = [
    "APPROVAL_REQUIRED",
    "COMMAND_ERROR",
    "Capability",
    "INVALID_ARGUMENTS",
    "InvalidLevelValue",
    "MCP_CAPABILITIES",
    "PERMISSION_DENIED",
    "PathRejected",
    "PermissionLevel",
    "Resolution",
    "TOOL_FAILURE",
    "Verdict",
    "allowed_capabilities",
    "bind_level",
    "capabilities_of",
    "check_path",
    "current_level",
    "evaluate",
    "evaluate_call",
    "guard_real_path",
    "is_readonly",
    "kind_counts_toward_breaker",
    "kind_is_retryable",
    "kind_status",
    "min_level",
    "parse_level",
    "parse_level_input",
    "resolve_chain",
    "resolve_effective_level",
    "result_kind",
]
