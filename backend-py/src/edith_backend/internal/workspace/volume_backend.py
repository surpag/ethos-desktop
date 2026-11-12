"""工作区文件存储：本地目录。

每个工作区对应磁盘上的**一个**目录：
- 没绑定用户目录 → `<data_root>/workspace-volumes/{id}`
- 绑定了用户目录 → 那个目录本身（读写直连，不存在副本）

两种情形都是"一份文件"，所以这里没有写入后还要推送的步骤，也没有
「卷」这种远端资源的生命周期（创建 / 连接 / 销毁配额）。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ...config import data_root

logger = logging.getLogger(__name__)

# 本地工作区根目录。
#
# 必须是**绝对路径**：原先缺省值是相对串 `"workspace-volumes"`，落点由进程 cwd
# 决定。桌面端由 Electron 拉起 sidecar，cwd 是安装目录，用户文件会被写进
# Program Files（只读）或随卸载一起消失。
_configured_volume_dir = os.getenv("EDITH_WORKSPACE_VOLUME_DIR")
_LOCAL_ROOT = (
    Path(_configured_volume_dir)
    if _configured_volume_dir
    else data_root() / "workspace-volumes"
)


class LocalVolumeBackend:
    """workspace → 本地目录。绑定用户目录后读写直连。"""

    def __init__(self, root: Path | str = _LOCAL_ROOT) -> None:
        self._root = Path(root) if not isinstance(root, Path) else root
        # workspace_id → 用户本地目录绝对路径（绑定后直接作为存储根）
        self._local_roots: dict[str, Path] = {}
        # workspace_id → 本地目录在虚拟文件树中的前缀
        # （"选文件夹上传"创建的工作区，卷里会多一层目录名，如 Data/；空 = 直接对应根）
        self._prefixes: dict[str, str] = {}

    def bind_local_root(
        self, workspace_id: str, local_root: str, prefix: str = ""
    ) -> None:
        """把 workspace 绑定到用户本地目录（消除副本）。"""
        self._local_roots[workspace_id] = Path(local_root)
        self._prefixes[workspace_id] = (prefix or "").strip().strip("/")

    def unbind_local_root(self, workspace_id: str) -> None:
        """解除本地目录绑定（不动用户文件）。"""
        self._local_roots.pop(workspace_id, None)
        self._prefixes.pop(workspace_id, None)

    def local_root_of(self, workspace_id: str) -> Path | None:
        """返回绑定的本地目录（未绑定返回 None）。"""
        return self._local_roots.get(workspace_id)

    def local_root_prefix_of(self, workspace_id: str) -> str:
        """返回绑定的前缀（未绑定或空前缀返回 ""）。"""
        return self._prefixes.get(workspace_id, "")

    def legacy_dir(self, workspace_id: str) -> Path:
        """绑定前的副本目录（workspace-volumes/{id}）；用于绑定时的存量文件合并。"""
        return self._root / workspace_id

    def _strip_prefix(self, rel: str, workspace_id: str) -> str | None:
        """把 workspace 内相对路径转为本地相对路径；不在绑定子树下返回 None。"""
        prefix = self._prefixes.get(workspace_id, "")
        rel = rel.strip("/")
        if not prefix:
            return rel
        if not rel:
            # 有前缀时，workspace 根目录不对应本地目录（应看虚拟根，与 Agent 视图一致）
            return None
        if rel == prefix:
            return ""
        if rel.startswith(prefix + "/"):
            return rel[len(prefix) + 1 :]
        return None

    def _dir(self, workspace_id: str) -> Path:
        # 读写直连：绑定 local_root 时所有存储操作都落在用户目录，否则退回副本
        return self._local_roots.get(workspace_id) or (self._root / workspace_id)

    def resolve_workspace_path(self, relative: str) -> Path | None:
        """仅当 relative 匹配 workspace/{id}/ 且对应本地目录存在时返回路径；否则 None。

        路径安全：拒绝 .. 穿越与绝对路径子段。
        """
        rel = (relative or "").strip().replace("\\", "/")
        if not rel or rel == ".":
            return None
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] != "workspace" or not parts[1]:
            return None
        workspace_id = parts[1]
        target = self._dir(workspace_id)
        if not target.exists():
            return None
        sub = "/".join(parts[2:])
        sub = self._strip_prefix(sub, workspace_id)
        if sub is None:
            # 该路径不在绑定的本地子树内（有前缀时，前缀之外不落在本地目录）
            return None
        if sub:
            if sub.startswith("/") or ".." in sub.split("/"):
                logger.warning("skip unsafe local sub path: %s", sub)
                return None
            target = target / sub
        return target

    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        base = self._dir(workspace_id)
        base.mkdir(parents=True, exist_ok=True)
        written = 0
        for rel_path, content in files:
            # 路径安全：拒绝绝对路径与 .. 穿越（http 层已校验，此处再兜底）
            if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
                logger.warning("skip unsafe local path: %s", rel_path)
                continue
            # 前缀映射：虚拟路径（如 Data/backend/x.py）→ 本地相对路径
            local_rel = self._strip_prefix(rel_path, workspace_id)
            if local_rel is None:
                logger.warning("skip path outside bound local subtree: %s", rel_path)
                continue
            if not local_rel:
                continue
            target = base / local_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            written += 1
        logger.info("local volume wrote %d files for %s", written, workspace_id)
        return written

    async def destroy(self, user_id: str, workspace_id: str) -> None:
        # 安全：绑定的是用户本地目录时，只解绑、绝不删除用户文件
        if workspace_id in self._local_roots:
            self.unbind_local_root(workspace_id)
            logger.info("unbound local root for %s (user dir kept)", workspace_id)
            return

        target = self._dir(workspace_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            logger.info("local volume removed %s", workspace_id)
