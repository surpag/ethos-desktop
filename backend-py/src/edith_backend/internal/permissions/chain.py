"""档位解析：把「项目设置 / 用户设置」与「本次执行上限」折成一个生效值。

## 两个作用域，一种关系：覆盖

1. `workspace`：项目级设置。有工作区、且这个工作区设了档位，就用它；
2. `user`：用户级设置。项目没设（或这次对话没有工作区）时用它。

项目覆盖用户，不做 min。理由写在 `policy.resolve_chain` 的表格里，一句话：
同一个用户在两处设置，粒度细的那处更接近他此刻的意图。「子层只能缩」那套
OAuth 直觉在这里不成立——那里的子层是别人的应用，这里的子层是他自己的项目。

`ceiling` 是第三个来源，但它**不是作用域**，是一次授权记录（定时任务创建时的
快照），与结果取最严。两者混为一谈就会出现上一版的问题：把「项目」也当成上限，
于是用户在一个项目里选了完全、行为还是只读，界面上没有人能解释为什么。

## 取不到数时按最严处理

读失败（DB 异常）与「没设置」必须分开：

- 没设置 = 中性：工作区为空则回落用户级，用户级为空则走默认档。绝大多数情况；
- 读失败 = **按只读**。反过来当没设置，等于「数据库抖一下就能提权」。日志里会
  单独记一条，因为这种情况下用户会遇到「明明设了完全却写不了」。

「工作区不存在」不是读失败：`store` 返回空串，按未设置处理、回落用户级。删掉一个
项目不该把它变成一个永久的只读陷阱。

收成一个函数而不是类，是因为读取来源只有两个且都是 `(user_id, ...) -> value`
形状：再包一层对象只是让 `service.py` 多一个可选依赖，而可选依赖的默认值分支
迟早会漂移成「漏读某一级」——那种失效在界面上完全看不出来。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from .policy import Resolution, resolve_chain
from .types import FALLBACK_LEVEL

logger = logging.getLogger(__name__)

#: 用户级设置的读取函数：`user_id -> level`。
LevelReader = Callable[[str], Awaitable[object]]

#: 项目级设置的读取函数：`(user_id, workspace_id) -> level`。
WorkspaceLevelReader = Callable[[str, str], Awaitable[object]]


async def resolve_effective_level(
    load_user: LevelReader,
    user_id: str,
    *,
    load_workspace: WorkspaceLevelReader | None = None,
    workspace_id: str | None = None,
    ceiling: object = "",
) -> Resolution:
    """算出本次的生效档位。

    `load_user` 必须传进来而不是可选：档位链少读一级用户设置，结果就是
    「用户设了只读，但 Agent 按完全在跑」——这种失效在界面上完全看不出来。

    `load_workspace` 与 `workspace_id` 同时给出时才读项目级；缺一个就按「这次
    没有项目」处理（普通对话、定时任务、测试装配）。这种降级是显式的：参数在
    调用点上就写明了要不要项目级，而不是由 `None` 默默替所有调用方做决定。
    """
    errors: list[str] = []
    user_level = await _read("user", errors, lambda: load_user(user_id))

    workspace_level: object = ""
    if load_workspace is not None and workspace_id:
        workspace_level = await _read(
            "workspace", errors, lambda: load_workspace(user_id, workspace_id)
        )

    resolution = resolve_chain(
        user=user_level, workspace=workspace_level, ceiling=ceiling
    )
    if errors:
        logger.warning(
            "permission_read_failed user=%s workspace=%s failed=%s effective=%s",
            user_id,
            workspace_id or "-",
            ",".join(errors),
            resolution.level,
        )
        return replace(resolution, errors=tuple(errors))
    return resolution


async def _read(
    name: str, errors: list[str], reader: Callable[[], Awaitable[object]]
) -> object:
    """读一个来源；失败时返回最严档并把名字记进 errors。

    返回的是**最严档字符串**而不是抛错：解析要继续跑完，让日志里同时看到
    「哪一级读失败了」和「最终生效几档」。
    """
    try:
        return await reader()
    except Exception:  # 数据库抖动 / 表结构不一致 / 被删的行
        logger.warning("permission source %s read failed", name, exc_info=True)
        errors.append(name)
        return str(FALLBACK_LEVEL)
