"""工作区实体的 SQLite 访问器：用户命名，系统生成 workspace_id。

对应 `docs/archive/p1-workspace-volume-design.md`：
- workspaces 含 metadata（文件清单）与 local_root（绑定的本地目录）
- 删除工作区时级联删除指令 + 通过回调清理磁盘目录
"""

from __future__ import annotations

import json
from uuid import uuid4

import aiosqlite


class DuplicateWorkspace(Exception):
    """同名工作区已存在。"""


class WorkspaceStore:
    """提供工作区实体的创建、查询、本地目录绑定与级联删除。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create(
        self,
        user_id: str,
        name: str,
        metadata: dict | None = None,
        local_root: str | None = None,
        session_id: str | None = None,
        local_root_prefix: str | None = None,
    ) -> str:
        """创建工作区并返回系统生成的 workspace_id。metadata 存为 JSON 字符串。

        local_root：绑定本地目录（方案 B）；session_id：绑定会话（决策 1，一 workspace 一 session）。
        local_root_prefix：本地目录在沙箱文件树中的前缀（见 schema.py 注释）。
        """
        workspace_id = uuid4().hex
        try:
            await self._db.execute(
                "INSERT INTO workspaces "
                "(workspace_id, user_id, name, metadata, local_root, session_id, local_root_prefix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    user_id,
                    name,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    local_root or None,
                    session_id or None,
                    local_root_prefix or None,
                ),
            )
            await self._db.commit()
        except aiosqlite.IntegrityError as exc:
            raise DuplicateWorkspace(f"workspace name already exists: {name}") from exc
        return workspace_id

    async def get_local_root(self, user_id: str, workspace_id: str) -> str | None:
        """返回工作区绑定的本地目录（方案 B）；未绑定返回 None。"""
        async with self._db.execute(
            "SELECT local_root FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def get_local_root_prefix(self, user_id: str, workspace_id: str) -> str:
        """返回本地目录在沙箱文件树中的前缀；空串表示直接对应 workspace 根。"""
        async with self._db.execute(
            "SELECT local_root_prefix FROM workspaces "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return (row[0] or "") if row else ""

    async def bind_local_root(
        self,
        user_id: str,
        workspace_id: str,
        local_root: str,
        prefix: str = "",
    ) -> bool:
        """给已有 workspace 绑定本地目录（方案 B：无需重建工作区）。返回是否命中记录。"""
        cursor = await self._db.execute(
            "UPDATE workspaces SET local_root = ?, local_root_prefix = ? "
            "WHERE user_id = ? AND workspace_id = ?",
            (local_root, prefix or None, user_id, workspace_id),
        )
        affected = cursor.rowcount or 0
        await cursor.close()
        await self._db.commit()
        return affected > 0

    async def get_session_id(self, user_id: str, workspace_id: str) -> str | None:
        """返回工作区绑定的会话 id（决策 1）；未绑定返回 None。"""
        async with self._db.execute(
            "SELECT session_id FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def bind_session(
        self, user_id: str, workspace_id: str, session_id: str
    ) -> None:
        """惰性绑定会话（旧 workspace 首次访问时调用）。"""
        await self._db.execute(
            "UPDATE workspaces SET session_id = ? "
            "WHERE user_id = ? AND workspace_id = ? AND session_id IS NULL",
            (session_id, user_id, workspace_id),
        )
        await self._db.commit()

    async def list(self, user_id: str) -> list[dict]:
        """列出用户全部工作区（按创建时间），metadata 解析为 dict。"""
        async with self._db.execute(
            "SELECT workspace_id, name, metadata, local_root FROM workspaces "
            "WHERE user_id = ? ORDER BY created_at, workspace_id",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "workspaceId": r[0],
                    "name": r[1],
                    "metadata": self._parse_metadata(r[2]),
                    "localRoot": r[3] or "",
                }
                for r in rows
            ]

    async def exists(self, user_id: str, workspace_id: str) -> bool:
        """该 workspace 是否属于该 user（Skill 作用域权限校验的唯一判据）。

        刻意只回 bool、不区分「不存在」与「不属于你」：区分开就等于提供一个
        「这个 id 是否存在」的探测器。也刻意不返回 `local_root`——普通沙箱
        工作区没有绑定本地目录，用「有 local_root」当判据会把它们全判成无权。
        """
        async with self._db.execute(
            "SELECT 1 FROM workspaces WHERE user_id = ? AND workspace_id = ? LIMIT 1",
            (user_id, workspace_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get(self, user_id: str, workspace_id: str) -> dict | None:
        """查询单个工作区（用于聊天页 workspaceId → 名称映射）。"""
        async with self._db.execute(
            "SELECT workspace_id, name, metadata, local_root, session_id FROM workspaces "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "workspaceId": row[0],
            "name": row[1],
            "metadata": self._parse_metadata(row[2]),
            "localRoot": row[3] or "",
            "sessionId": row[4] or "",
        }

    # ---- 删除（级联 + 存储清理回调） ----

    async def delete(
        self,
        user_id: str,
        workspace_id: str,
        on_destroy_storage: object | None = None,
    ) -> bool:
        """删除工作区并级联删除其项目指令；先调用存储清理回调。

        on_destroy_storage: async callable (user_id, workspace_id) -> None，
        由调用方注入（`WorkspaceVolumeService.on_delete`），职责是清掉工作区
        在磁盘上的目录。
        """
        if on_destroy_storage is not None:
            await on_destroy_storage(user_id, workspace_id)  # type: ignore[misc]

        async with self._db.execute(
            "DELETE FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            deleted = cursor.rowcount > 0
        if deleted:
            await self._db.execute(
                "DELETE FROM project_memories WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            )
            # 保留关联会话：把该工作区下的消息 workspace_id 置 NULL（变为"普通对话"，
            # 出现在侧边栏「最近对话」），不丢用户历史——与 DeepSeek 等产品的行为一致。
            await self._db.execute(
                "UPDATE chat_messages SET workspace_id = NULL "
                "WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            )
        await self._db.commit()
        return deleted

    # ---- metadata 更新 ----

    async def update_files_metadata(
        self, user_id: str, workspace_id: str, file_paths: list[str]
    ) -> None:
        """把新上传的文件路径并入 metadata（camelCase，对齐前端 filePaths/fileCount）。"""
        async with self._db.execute(
            "SELECT metadata FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        metadata = json.loads(row[0]) if row and row[0] else {}

        existing = set(metadata.get("filePaths", []))
        added = [p for p in file_paths if p not in existing]
        if not added:
            return
        existing.update(added)
        metadata["filePaths"] = sorted(existing)
        metadata["fileCount"] = len(existing)

        await self._db.execute(
            "UPDATE workspaces SET metadata = ? WHERE user_id = ? AND workspace_id = ?",
            (json.dumps(metadata, ensure_ascii=False), user_id, workspace_id),
        )
        await self._db.commit()

    async def update_metadata(
        self, user_id: str, workspace_id: str, patch: dict
    ) -> dict | None:
        """合并 metadata 键（浅合并，patch 里的键覆盖旧值），返回更新后的 metadata。

        不存在/归属不符返回 None（调用方转 404）。
        """
        async with self._db.execute(
            "SELECT metadata FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        metadata = self._parse_metadata(row[0])
        metadata.update(patch)
        await self._db.execute(
            "UPDATE workspaces SET metadata = ? WHERE user_id = ? AND workspace_id = ?",
            (json.dumps(metadata, ensure_ascii=False), user_id, workspace_id),
        )
        await self._db.commit()
        return metadata

    @staticmethod
    def _parse_metadata(raw: str | None) -> dict:
        """metadata 列是 JSON 字符串，解析失败返回空 dict（容错）。"""
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
