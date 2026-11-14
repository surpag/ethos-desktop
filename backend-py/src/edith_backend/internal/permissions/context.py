"""当前 run 的档位上下文。

只有一个用途：**让工具在「创建时」把档位记下来**（`create_cron_job` 的权限快照）。

不把它当成权限判断的依据——判断一律走显式传参的 `policy.evaluate`。理由正是评审
里那条要求：「定时任务应当有独立的执行身份和授权记录，不能简单继承创建者当时的
执行上下文」。上下文是**记录事实**的，不是**行使权力**的。

默认值是 `None` 而不是 `FULL`：没有上下文时，调用方必须显式决定怎么处理
（cron 工具的选择是回落到用户设置，并把回落写进结果），而不是被一个默认值悄悄
授予最高权限。

生命周期与 `skills.budget` 的账本一致：作用域是 run 的 task，`asyncio.create_task`
复制上下文，run 结束随 task 消失，不需要回收或 TTL。
"""

from __future__ import annotations

from contextvars import ContextVar

from .types import PermissionLevel

_level: ContextVar[PermissionLevel | None] = ContextVar(
    "permission_level", default=None
)


def bind_level(level: PermissionLevel) -> None:
    """绑定当前 run 的生效档位。

    覆盖写即幂等：每次 run 都在自己的 task 里重新绑一次，不存在「上一个 run 的
    档位被下一个 run 继承」——那正是进程级可变状态才会出的错。
    """
    _level.set(level)


def current_level() -> PermissionLevel | None:
    """当前 run 的生效档位；不在 run 内时为 None。"""
    return _level.get()
