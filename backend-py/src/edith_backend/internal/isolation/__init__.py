"""OS 级执行隔离：统一入口、策略、能力探测与审计。

对外只暴露四个动作：

- `plan(...)`：为一次执行生成 argv/env；**不可用时抛 `SandboxUnavailable`**；
- `finish(...)`：记录结束事件（退出码 / 耗时 / 结局）到日志；
- `record(...)` / `record_refusal(...)`：把同一条事件**落库**（Trace 可查询）；
- `health()`：给设置页与诊断用的能力报告；
- `reset()`：测试与"刚装完后端"时清掉探测缓存。

调用方（`sandbox/local.py::LocalCommands`）不判断后端、不改 argv、不碰环境变量——
**"怎么起进程"只有这一个答案**。
"""

from . import audit, store
from .errors import SandboxUnavailable
from .manager import (
    BACKEND_ENV,
    UNSAFE_HOST_ENV,
    ExecPlan,
    finish,
    health,
    plan,
    policy_for,
    record,
    record_refusal,
    reset,
    resolve,
)

__all__ = [
    "BACKEND_ENV",
    "UNSAFE_HOST_ENV",
    "ExecPlan",
    "SandboxUnavailable",
    "audit",
    "finish",
    "health",
    "plan",
    "policy_for",
    "record",
    "record_refusal",
    "reset",
    "resolve",
    "store",
]
