"""工作区文件服务：门面，负责写入、本地目录绑定与销毁。

对应 `docs/archive/p1-workspace-volume-design.md` 的懒创建语义：首次上传文件才在磁盘上
建目录。存储只有一个实现（本地目录），所以这里不做后端选择——留一个 `VolumeBackend`
抽象加一次 `isinstance` 判断，只会让人以为还有另一条路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .context import BindingRecord, WorkspaceContext, build_context, probe_root
from .merge import merge_dir_add_only, plan_merge
from .store import WorkspaceStore
from .volume_backend import LocalVolumeBackend, normalize_local_root

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
        root = await self._store.get_local_root(user_id, workspace_id)
        if root:
            prefix = await self._store.get_local_root_prefix(user_id, workspace_id)
            self._backend.bind_local_root(workspace_id, root, prefix)
            # 日志打注册表里的值（= 归一后的），与沙箱层实际用的路径逐字一致：
            # 库里可能是 8.3 短名，而真实比较用的是长名，两边不一致时排查毫无线索。
            logger.info(
                "workspace %s bound to local root %s (prefix=%r)",
                workspace_id,
                self.local_root_of(workspace_id),
                prefix,
            )
        # 只有读成功才标记"已加载"：原来这一句在读库**之前**，一旦那次查询抛错，
        # 该工作区就被永久标记成"已加载"却什么都没注册 —— 之后每次读都拿到 None，
        # 把"绑定态"误报成"未绑定"，正好是本模块要消灭的那类静默回退。
        self._local_root_loaded.add(workspace_id)
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

        **顺序**：先归一 + 校验，再写库。反过来会留下两种坏状态——失败却永久写进一条
        指向不可用目录的记录，或者把原来那条有效绑定覆盖掉。

        返回 BindOutcome：本次合并文件数 + 旧副本中仍未合并（本地缺失）的文件数，
        后者供 UI 提示「还有 N 个文件未合并」。
        """
        root = normalize_local_root(local_root, strict=True)
        await self._store.bind_local_root(user_id, workspace_id, str(root), prefix)
        self._local_root_loaded.add(workspace_id)
        self._backend.bind_local_root(workspace_id, str(root), prefix)

        legacy = self._backend.legacy_dir(workspace_id)
        merged = merge_dir_add_only(legacy, root, prefix=prefix) if merge_existing else 0
        # 合并后重算：仍在旧副本、本地没有的文件（含合并失败项）
        unmerged = len(plan_merge(legacy, root, prefix=prefix))
        return BindOutcome(merged_files=merged, unmerged_files=unmerged)

    def local_root_prefix_of(self, workspace_id: str) -> str:
        """返回已注册的本地目录前缀（未绑定返回 ""）。"""
        return self._backend.local_root_prefix_of(workspace_id)

    def local_root_of(self, workspace_id: str) -> str | None:
        """返回已注册的本地目录（未绑定返回 None）。"""
        root = self._backend.local_root_of(workspace_id)
        return str(root) if root else None

    # ---- 执行上下文快照（P0-2 / S1 第二阶段）----

    async def capture(self, user_id: str, workspace_id: str) -> WorkspaceContext:
        """抓取一次不可变的工作区上下文快照（agentrun 的 `WorkspaceContextSource`）。

        三步，缺一不可：

        1. 把绑定记录从库里加载进运行期缓存。**`local_root_of()` 不是权威来源**：
           它只是运行期缓存，只有「打开沙箱」和「面板绑定」两处会填充，而 run 启动时
           这两处都还没跑。只读缓存会把"已绑定"读成"未绑定"——那正是 P0-2 要修的 bug
           换了个位置复现。
        2. 探测磁盘（`probe_root` 是全模块唯一做 I/O 的地方）。
        3. 用纯函数解析状态、产出快照。

        **不创建目录、不打开沙箱、不改绑定**：绑定根不可用只如实上报状态，
        要不要失败由消费者按 `context.executable` 决定。
        """
        await self.load_and_bind_local_root(user_id, workspace_id)
        root = self.local_root_of(workspace_id)
        record = BindingRecord(
            workspace_id=workspace_id,
            local_root=root,
            prefix=self.local_root_prefix_of(workspace_id) if root else "",
        )
        # 未绑定就不探测：`probe_root(None)` 没有意义，还会把"没绑定"和"探测失败"混成一件事
        probe = probe_root(root) if root else None
        return build_context(record, probe)

    async def on_delete(self, user_id: str, workspace_id: str) -> None:
        """删除工作区时调用：清理本地目录（绑定的是用户目录时只解绑）。"""
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
