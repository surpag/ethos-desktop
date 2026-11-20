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
from dataclasses import dataclass
from enum import Enum
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


class LocalLookupKind(str, Enum):
    """面板直读的三种结果。

    **为什么不是一个 `Path | None`**：`None` 把三种完全不同的情况压成一个值，
    调用方只能"不是本地就去读沙箱"。绑定目录被移动/删除时它恰好也是 `None`，
    于是面板静默显示旧副本——用户以为自己在看绑定目录。这正是 P0-2 要消灭的
    静默切换，只是换到了面板这条链上。
    """

    #: 本地没有对应的权威文件，交给沙箱（`skills/…`、`uploads/…`、
    #: 未绑定且副本目录还没建出来、有前缀时前缀之外的路径）
    SANDBOX = "sandbox"
    #: 是工作区路径且本地路径已解析；**对应文件/目录是否存在由调用方判断**
    READY = "ready"
    #: 绑定目录不可用（被移动/改名/掉线）→ **必须报错，不得回落**
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LocalLookup:
    kind: LocalLookupKind
    path: Path | None = None
    #: 本地没有时是否允许回落读沙箱。**只有未绑定的工作区允许**：
    #: 绑定态的权威就是本地目录，回落等于展示另一份数据。
    fallback_allowed: bool = True
    #: 不可用的原因（给用户看，`UNAVAILABLE` 时非空）
    detail: str = ""


class LocalRootError(ValueError):
    """绑定入口的路径不能用作工作区根。

    继承 ValueError：HTTP 层原本就把「本地目录不合法」映射成 400，
    不需要再引入新的错误家族。子类只区分**原因**，不区分处理方式。
    """

    code = "invalid_local_root"


class LocalRootNotAbsolute(LocalRootError):
    """相对路径：落点取决于进程 cwd，桌面端会写进安装目录。"""

    code = "local_root_not_absolute"


class LocalRootMissing(LocalRootError):
    """路径（或链接目标）不存在。"""

    code = "local_root_missing"


class LocalRootNotDirectory(LocalRootError):
    """存在但不是目录。"""

    code = "local_root_not_directory"


class LocalRootUnavailable(LocalRootError):
    """存在性未知：权限不足、网络盘掉线、重解析点损坏。原始 `OSError` 在 `__cause__`。"""

    code = "local_root_unavailable"


def normalize_local_root(raw: str | Path, *, strict: bool = False) -> Path:
    """把绑定入口的路径规范化为**唯一形式**：绝对 + 真实大小写 + 展开 8.3 短名/联接。

    为什么必须归一：`is_relative_to` 比较的是路径字符串，而磁盘上同一个目录有很多
    写法（`Proj` / `proj` / `PROJ~1` / 联接的真实目标）。只要一侧是别名、另一侧是
    真实路径，判定就失败——表现得像「文件明明在，却 403」。所以在入口归一一次，
    内部只使用归一后的路径，之后每次比较都不必猜对方是哪种写法。

    `strict=True` 用于**绑定新目录**：要求真实存在，失败按原因分三类报出，
    不合并成一句「绑定失败」（不存在 / 不是目录 / 不可用，后者带 `__cause__`）。
    `strict=False` 用于**从库里读回**旧绑定：目录被删不该让会话起不来，
    错误应该从真实读写时冒出来，而不是在启动期把整条链打断。
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LocalRootNotAbsolute(f"工作区根必须是绝对路径: {raw}")
    try:
        resolved = path.resolve(strict=strict)
    except FileNotFoundError as exc:
        raise LocalRootMissing(f"工作区根不存在: {path}") from exc
    except OSError as exc:
        raise LocalRootUnavailable(
            f"工作区根不可用（权限不足/网络盘掉线/链接损坏）: {path}: {exc}"
        ) from exc
    if strict and not resolved.is_dir():
        raise LocalRootNotDirectory(f"工作区根必须是目录: {resolved}")
    return resolved


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
        """把 workspace 绑定到用户本地目录（消除副本）。

        **登记即归一**：注册表里只允许出现规范化后的路径。否则沙箱层每次
        `is_relative_to` 都要猜对面写法，而绑定之后 `workspace-volumes/{id}`
        必然是联接，猜错就是 403。
        归一失败（盘不可达、权限不足）不等于绑定成功：保留原样并告警，让错误在
        真正读写时冒出来——在会话启动期抛异常会连累整个对话，且看不出原因。
        """
        try:
            root = normalize_local_root(local_root)
        except LocalRootError as exc:
            logger.warning("local root not normalized (%s): %s", exc.code, exc)
            root = Path(local_root)
        self._local_roots[workspace_id] = root
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

    def lookup_workspace_path(self, relative: str) -> LocalLookup:
        """解析面板给的相对路径，并说明"本地不可用时该怎么办"。

        分类依据是**权威根是谁**，不是"路径碰巧存在与否"：

        | 情况 | 结果 |
        | --- | --- |
        | 不是 `workspace/{id}/…` | `SANDBOX` |
        | 未绑定，副本目录还不存在 | `SANDBOX`（沙箱会按需初始化，与改造前一致） |
        | 有前缀，路径在前缀之外 | `SANDBOX`（这是设计：虚拟根不对应本地目录） |
        | 绑定态，绑定目录不可用 | `UNAVAILABLE` → 调用方必须报错 |
        | 绑定态，子路径本地没有 | `READY` + `fallback_allowed=False` → 调用方按"没有"处理 |

        最后一行是关键：绑定态的权威就是那个目录，本地没有就是没有。
        回落到沙箱只会读到 `workspace-volumes/{id}` 那份旧数据。

        路径安全：拒绝 `..` 穿越与绝对路径子段（与 `write_files` 同一规则）。
        """
        rel = (relative or "").strip().replace("\\", "/")
        if not rel or rel == ".":
            return LocalLookup(LocalLookupKind.SANDBOX)
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] != "workspace" or not parts[1]:
            return LocalLookup(LocalLookupKind.SANDBOX)

        workspace_id = parts[1]
        bound = workspace_id in self._local_roots
        target = self._dir(workspace_id)
        if not target.exists():
            if bound:
                return LocalLookup(
                    LocalLookupKind.UNAVAILABLE,
                    fallback_allowed=False,
                    detail=(
                        f"工作区绑定的本地目录不可用：{target}"
                        "（可能被移动、改名，或所在磁盘掉线）。"
                        "已停止读取，避免显示另一份数据。"
                    ),
                )
            # 未绑定：副本目录还没建出来是正常状态，交给沙箱按需初始化
            return LocalLookup(LocalLookupKind.SANDBOX)

        sub = "/".join(parts[2:])
        stripped = self._strip_prefix(sub, workspace_id)
        if stripped is None:
            # 有前缀时，前缀之外不落在本地目录——这是定义的映射，不是故障
            return LocalLookup(LocalLookupKind.SANDBOX)
        if stripped:
            if stripped.startswith("/") or ".." in stripped.split("/"):
                logger.warning("skip unsafe local sub path: %s", stripped)
                return LocalLookup(LocalLookupKind.SANDBOX)
            target = target / stripped
        return LocalLookup(LocalLookupKind.READY, target, fallback_allowed=not bound)

    def resolve_workspace_path(self, relative: str) -> Path | None:
        """`lookup_workspace_path` 的路径视图（`READY` 时有值，否则 `None`）。

        保留是因为有调用方只关心路径；**新代码应当用 lookup**，
        否则又会出现"`None` 到底是什么"的歧义。
        """
        found = self.lookup_workspace_path(relative)
        return found.path if found.kind is LocalLookupKind.READY else None

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
