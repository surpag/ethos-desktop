"""档位解析链：把「用户级 / 工作区级 / 会话级 / 本次执行上限」fold 成一个生效值。

## 为什么单独成一个模块

同一句「这个 run 到底按几档跑」有三处要问：

1. `agentrun/service.py`：真的要按它执行；
2. `conversation/history.py`：界面要显示「本次会话实际能做什么」；
3. 闸门/排障日志：要知道是**谁**把档位压下来的。

三处各写一遍 `min(...)`，迟早在某个分支上漂移成「界面显示完全、实际按只读跑」，
而这种偏差没有任何报错——用户只会发现 Agent 突然什么都干不了。

## 唯一策略：只能收紧

见 `policy.resolve_chain`。这里只负责**取数**，不重复判断。

## 取不到数时按最严处理

读失败（DB 异常）与「没设置」必须分开：

- 没设置 = 中性（不参与收紧），这是绝大多数情况；
- 读失败 = **按只读**。反过来当没设置，等于「数据库抖一下就能提权」。
  日志里会单独记一条，因为这种情况下用户会遇到「明明设了完全却写不了」。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .policy import Resolution, resolve_chain
from .types import FALLBACK_LEVEL

logger = logging.getLogger(__name__)

#: 只按 user_id 取（用户级设置）。
LevelReader = Callable[[str], Awaitable[object]]
#: 按 (user_id, scope_id) 取（工作区级 / 会话级）。
ScopedLevelReader = Callable[[str, str], Awaitable[object]]


class LevelChain:
    """解析链的取数端。三个 reader 都可为 None（对应来源不参与）。"""

    def __init__(
        self,
        user_level: LevelReader | None = None,
        workspace_level: ScopedLevelReader | None = None,
        session_level: ScopedLevelReader | None = None,
    ) -> None:
        self._user = user_level
        self._workspace = workspace_level
        self._session = session_level

    async def resolve(
        self,
        user_id: str,
        *,
        workspace_id: str = "",
        session_id: str = "",
        ceiling: object = "",
    ) -> Resolution:
        """算出本次的生效档位。`ceiling` 是本次执行的额外上限（定时任务快照）。"""
        sources: dict[str, object] = {"ceiling": ceiling}
        errors: list[str] = []

        if self._user is not None:
            sources["user"] = await _read(
                "user", errors, lambda: self._user(user_id)
            )
        if self._workspace is not None and workspace_id:
            sources["workspace"] = await _read(
                "workspace",
                errors,
                lambda: self._workspace(user_id, workspace_id),
            )
        if self._session is not None and session_id:
            sources["session"] = await _read(
                "session", errors, lambda: self._session(user_id, session_id)
            )

        resolution = resolve_chain(**sources)  # type: ignore[arg-type]
        if errors:
            logger.warning(
                "permission_read_failed user=%s session=%s workspace=%s failed=%s effective=%s",
                user_id,
                session_id or "-",
                workspace_id or "-",
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

    返回的是**最严档字符串**而不是抛错：解析链要继续跑完，让日志里同时看到
    「哪一级读失败了」和「最终生效几档」。
    """
    try:
        return await reader()
    except Exception:  # 数据库抖动 / 表结构不一致 / 被删的行
        logger.warning("permission source %s read failed", name, exc_info=True)
        errors.append(name)
        return str(FALLBACK_LEVEL)
