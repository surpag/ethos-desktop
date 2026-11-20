"""sandbox 服务：把用户会话绑定到本机沙箱目录。

**这里只有一种执行后端。** 桌面版的「沙箱」就是一个目录：
`workspace/{id}` 与 `skills/` 都是目录联接，直指权威副本
（`workspace-volumes/{id}` / `skills/`），磁盘上只有一份文件。

于是远端资源挂载、副本同步、出口白名单这些事情在这里都不存在——不是"关掉了"，
是没有对应的东西。留在代码里只会变成"另一条看起来能走的路"。
"""

from __future__ import annotations

import logging

import aiosqlite

from ..workspace.context import WorkspaceContext
from ..workspace.store import WorkspaceStore
from ..workspace.volume_service import WorkspaceVolumeService
from .local import LocalSandbox

logger = logging.getLogger(__name__)


class SandboxNotFound(Exception):
    """会话没有可用沙箱（目录不存在）。HTTP 层据此返回 404。"""


class Service:
    def __init__(
        self,
        db: aiosqlite.Connection,
        workspaces: WorkspaceStore | None = None,
        workspace_volumes: WorkspaceVolumeService | None = None,
    ) -> None:
        self._db = db
        self._workspaces = workspaces
        self._workspace_volumes = workspace_volumes

    async def _load_sandbox_id(self, user_id: str, session_id: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT sandbox_id FROM user_sandboxes WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["sandbox_id"] if row else None

    async def session_workspace_id(self, user_id: str, session_id: str) -> str:
        """会话绑定的工作区 id；普通对话（未选工作区）返回空串。

        路径校验需要它：`workspace/{id}/…` 缺 ID 段时要靠它补全，有 ID 段时要靠它
        判越权。HTTP 层（文件面板）没有别的途径知道这件事——它只拿得到 sessionId。
        """
        cursor = await self._db.execute(
            "SELECT workspace_id FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id.strip(), session_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (row["workspace_id"] or "") if row else ""

    async def existing_workspace(self, user_id: str, session_id: str) -> LocalSandbox:
        """只打开已存在的沙箱，不创建（只读场景：浏览 / 预览 / 下载）。

        `attach` 抛的是 `FileNotFoundError`，这里转成 `SandboxNotFound`：HTTP 层
        只认识后者并映射 404。让两种"不存在"以不同异常漏上去，表现是一个 404、
        一个 500——同一个原因两种状态码，排查时会误以为是两件事。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        try:
            return LocalSandbox.attach(user_id, session_id)
        except FileNotFoundError as exc:
            raise SandboxNotFound("sandbox mapping not found") from exc

    async def local_workspace_path(self, user_id: str, relative: str):
        """`workspace/{id}/...` 对应的本地磁盘路径；不属于任何工作区时返回 None。

        文件面板命中后直接本地读，不必经过沙箱路径解析——列表/预览是最高频的操作，
        走一次解析只是多一次 resolve。归属校验仍然要做：路径里的 workspace_id
        必须属于当前用户。
        """
        if self._workspace_volumes is None:
            return None
        parts = (relative or "").strip().replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] == "workspace" and self._workspaces is not None:
            workspace_id = parts[1]
            record = await self._workspaces.get(user_id, workspace_id)
            if record is None:
                return None
            # 懒绑定：DB 里记了 local_root 的工作区（用户选择的已有目录）在这里注册
            await self._workspace_volumes.load_and_bind_local_root(user_id, workspace_id)
        backend = self._workspace_volumes.backend
        return backend.resolve_workspace_path(relative)

    async def workspace(
        self,
        user_id: str,
        session_id: str,
        context: WorkspaceContext | None = None,
    ) -> LocalSandbox:
        """返回可用的沙箱；目录不存在就建。

        workspace_id 不参与创建：`workspace/{id}` 的联接是在第一次解析工作区路径时
        按需建立的（`LocalSandbox._workspace_volume_dir`）。会话可以换绑工作区，
        在这里预先按当前 workspace_id 建一次，反而会在换绑后留下指向旧工作区的联接。

        `context` 是**本次执行**的工作区快照，原样交给沙箱：绑定状态由此进入
        执行链（S2）。缺省 `None` = 调用方没有上下文，路径行为与改造前一致
        （面板 / 旧调用路径）。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        sandbox = LocalSandbox.open(user_id, session_id, context=context)

        # `user_sandboxes` 仍要记：UI 的沙箱面板和 `existing_workspace` 都读它。
        existing = await self._load_sandbox_id(user_id, session_id)
        if existing is None:
            await self._db.execute(
                "INSERT INTO user_sandboxes (user_id, session_id, sandbox_id) VALUES (?, ?, ?)",
                (user_id, session_id, sandbox.sandbox_id),
            )
        else:
            # 目录被外部删过 / sandbox_id 变化：就地更新，不必先删行。
            await self._db.execute(
                "UPDATE user_sandboxes SET sandbox_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND session_id = ?",
                (sandbox.sandbox_id, user_id, session_id),
            )
        await self._db.commit()
        return sandbox
