"""工作区文件服务：门面，负责写入、本地目录绑定与销毁。

对应 `docs/p1-workspace-volume-design.md` 的懒创建语义：首次上传文件才在磁盘上
建目录。存储只有一个实现（本地目录），所以这里不做后端选择——留一个 `VolumeBackend`
抽象加一次 `isinstance` 判断，只会让人以为还有另一条路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .merge import merge_dir_add_only, plan_merge
from .store import WorkspaceStore
from .volume_backend import LocalVolumeBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BindOutcome:
    """绑定本地目录的结果：本次合并文件数 + 旧副本中仍未合并（本地缺失）的文件数。"""

    merged_files: int = 0
    unmerged_files: int = 0


class WorkspaceVolumeService:
    """工作区文件存储的门面。"""

    def __init__(
        self, store: WorkspaceStore, backend: LocalVolumeBackend | None = None
    ) -> None:
        self._store = store
        self._backend = backend or LocalVolumeBackend()
        # 已从 DB 加载过 local_root 的 workspace_id（避免每次读都查库）
        self._local_root_loaded: set[str] = set()

    # 暴露后端实例：文件面板与 sandbox 层要直接解析本地路径
    @property
    def backend(self) -> LocalVolumeBackend:
        return self._backend

    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        """写入一批文件到工作区目录（路径为工作区内相对路径）。"""
        if not files:
            return 0
        written = await self._backend.write_files(user_id, workspace_id, files)
        written_paths = [p for p, _ in files[:written]]
        if written_paths:
            await self._store.update_files_metadata(
                user_id, workspace_id, written_paths
            )
        logger.info(
            "wrote %d files to workspace %s (%s total)",
            written,
            workspace_id,
            len(files),
        )
        return written

    # ---- 本地目录绑定（B1a）----

    async def load_and_bind_local_root(
        self, user_id: str, workspace_id: str
    ) -> str | None:
        """从 DB 读取 workspace 的 local_root 并注册到后端。

        注册后读写直连该本地目录，彻底绕开 workspace-volumes/{id}/ 副本。
        """
        if workspace_id in self._local_root_loaded:
            return self.local_root_of(workspace_id)
        self._local_root_loaded.add(workspace_id)
        root = await self._store.get_local_root(user_id, workspace_id)
        if root:
            prefix = await self._store.get_local_root_prefix(user_id, workspace_id)
            self._backend.bind_local_root(workspace_id, root, prefix)
            logger.info(
                "workspace %s bound to local root %s (prefix=%r)",
                workspace_id,
                root,
                prefix,
            )
        return root

    async def bind_local_root(
        self,
        user_id: str,
        workspace_id: str,
        local_root: str,
        prefix: str = "",
        *,
        merge_existing: bool = False,
    ) -> BindOutcome:
        """给已有 workspace 绑定本地目录。

        绑定后该 workspace 读写直连 local_root。旧副本（workspace-volumes/{id}，即未绑定
        期间的产物）**默认不合并**——绑定一个既有目录不应附带全量文件迁移；调用方如需旧
        产物，显式传 ``merge_existing=True``（仍**只新增不覆盖**）。

        返回 BindOutcome：本次合并文件数 + 旧副本中仍未合并（本地缺失）的文件数，
        后者供 UI 提示「还有 N 个文件未合并」。
        """
        await self._store.bind_local_root(user_id, workspace_id, local_root, prefix)
        self._local_root_loaded.add(workspace_id)
        self._backend.bind_local_root(workspace_id, local_root, prefix)

        legacy = self._backend.legacy_dir(workspace_id)
        dst = Path(local_root)
        merged = merge_dir_add_only(legacy, dst, prefix=prefix) if merge_existing else 0
        # 合并后重算：仍在旧副本、本地没有的文件（含合并失败项）
        unmerged = len(plan_merge(legacy, dst, prefix=prefix))
        return BindOutcome(merged_files=merged, unmerged_files=unmerged)

    def local_root_prefix_of(self, workspace_id: str) -> str:
        """返回已注册的本地目录前缀（未绑定返回 ""）。"""
        return self._backend.local_root_prefix_of(workspace_id)

    def local_root_of(self, workspace_id: str) -> str | None:
        """返回已注册的本地目录（未绑定返回 None）。"""
        root = self._backend.local_root_of(workspace_id)
        return str(root) if root else None

    async def on_delete(
        self, user_id: str, workspace_id: str, volume_id: str | None = None
    ) -> None:
        """删除工作区时调用：清理本地目录（绑定的是用户目录时只解绑）。

        `volume_id` 是历史签名（曾用于区分远端卷 id），保留参数只为不改调用方；
        本地存储不看它。
        """
        try:
            await self._backend.destroy(user_id, workspace_id)
        except Exception as exc:  # noqa: BLE001 - 销毁失败不影响删除
            logger.warning("failed to destroy workspace storage %s: %s", workspace_id, exc)

    async def destroy(self, user_id: str, workspace_id: str) -> None:
        """销毁工作区存储（绑定的是用户目录时只解绑，不删用户文件）。"""
        await self._backend.destroy(user_id, workspace_id)

    @staticmethod
    def is_safe_rel_path(rel_path: str) -> bool:
        """工作区内相对路径安全校验：拒绝绝对路径与 .. 穿越。"""
        if not rel_path or rel_path.startswith("/"):
            return False
        return ".." not in rel_path.split("/")
