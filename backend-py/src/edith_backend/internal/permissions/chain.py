"""档位解析：把「用户级设置」与「本次执行上限」折成一个生效值。

## 只有一个控制点

档位是**用户**的设置，不是会话或工作区的属性。所以这里只有两个来源：

1. `user`：设置页里的三档，改一次作用于全部会话与定时任务；
2. `ceiling`：定时任务创建时记下的快照。

`ceiling` **不是第二套设置**，而是「这次自动执行最多能到几档」的授权记录。执行时
仍要与当前用户级取最严：用户把档位调低之后，历史定时任务不能成为例外。

（曾经实现过工作区级 / 会话级上限，已回退。理由见
`docs/archive/permissions-p3-scopes.md`：档位是「我允许 Agent 对我的机器做什么」，
这个问题只有一个回答者；多一层作用域不会让它更安全，只会让用户在每个对话里
重新对账一次。）

## 取不到数时按最严处理

读失败（DB 异常）与「没设置」必须分开：

- 没设置 = 中性（走默认档），这是绝大多数情况；
- 读失败 = **按只读**。反过来当没设置，等于「数据库抖一下就能提权」。
  日志里会单独记一条，因为这种情况下用户会遇到「明明设了完全却写不了」。

收成一个函数而不是类，是因为来源只剩一个 reader：再包一层对象只是让
`service.py` 多一个可选依赖，而可选依赖的默认值分支迟早会漂移成「漏读用户级」。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .policy import Resolution, resolve_chain
from .types import FALLBACK_LEVEL

logger = logging.getLogger(__name__)

#: 只按 user_id 取（用户级设置）。
LevelReader = Callable[[str], Awaitable[object]]


async def resolve_user_level(
    load: LevelReader,
    user_id: str,
    *,
    ceiling: object = "",
) -> Resolution:
    """算出本次的生效档位。

    `load` 是用户级设置的读取函数（`UserSettings.load_permission_level`）。
    它必须传进来而不是可选：档位链少读一级用户设置，结果就是「用户设了只读，
    但 Agent 按完全在跑」——这种失效在界面上完全看不出来。
    """
    errors: list[str] = []
    user = await _read("user", errors, lambda: load(user_id))
    resolution = resolve_chain(user=user, ceiling=ceiling)
    if errors:
        logger.warning(
            "permission_read_failed user=%s failed=%s effective=%s",
            user_id,
            ",".join(errors),
            resolution.level,
        )
        return Resolution(
            level=resolution.level,
            sources=resolution.sources,
            binding=resolution.binding,
            invalid=resolution.invalid,
            errors=tuple(errors),
        )
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
