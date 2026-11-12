"""sandbox 服务：把用户会话绑定到唯一 E2B Sandbox。

对应 Go 版 internal/sandbox/service.go。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection
from pathlib import Path

import aiosqlite
from e2b import ALL_TRAFFIC, AsyncSandbox
from e2b.sandbox.sandbox_api import SandboxLifecycle

from ..volume.service import Service as VolumeService
from ..workspace.layout import WORKSPACE
from ..workspace.store import WorkspaceStore
from ..workspace.volume_backend import LocalVolumeBackend
from ..workspace.volume_service import WorkspaceVolumeService
from .local import LocalSandbox

CONNECT_TIMEOUT_MS = 60 * 10
logger = logging.getLogger(__name__)

# E2B 硬限制：每个沙箱最多放行 10 个域名，超出服务端直接 400 且不可提额
MAX_ALLOWED_DOMAINS = 10


def allowed_domains_from_env() -> list[str]:
    """环境变量兜底白名单（EDITH_SANDBOX_ALLOWED_DOMAINS，逗号/分号/空白分隔）。"""
    raw = os.getenv("EDITH_SANDBOX_ALLOWED_DOMAINS", "")
    return [d for d in re.split(r"[,;\s]+", raw) if d]


class SandboxNotFound(Exception):
    pass


class Service:
    def __init__(
        self,
        db: aiosqlite.Connection,
        template: str,
        volumes: VolumeService,
        workspaces: WorkspaceStore | None = None,
        workspace_volumes: WorkspaceVolumeService | None = None,
        backend: str = "e2b",
    ) -> None:
        self._db = db
        self._template = template
        # 后端选择：`e2b`（远端微 VM）或 `local`（本机目录，桌面端）。
        # 只影响「怎么拿到沙箱」——两边的返回形状一致，工具层不需要知道这件事。
        self._backend = (backend or "e2b").strip().lower()
        # 启动就把实际生效的后端打出来。否则「为什么工具还在连远端」
        # 只能靠读环境变量猜。
        logger.info("sandbox backend: %s", self._backend)
        self._volumes = volumes
        self._workspaces = workspaces
        self._workspace_volumes = workspace_volumes
        # B3 快路径：{workspace_id}:{sandbox_id} → 本地目录指纹（未变化则跳过逐文件远端比对）
        self._b3_fingerprints: dict[str, tuple] = {}
        # 出口策略：{sandbox_id: (domains...)}，已下发过的策略（避免每次连接重复 update_network）
        self._net_policies: dict[str, tuple[str, ...]] = {}

    async def _resolve_allowed_domains(
        self, user_id: str, workspace_id: str
    ) -> list[str]:
        """解析出口白名单：workspace metadata.allowedDomains 优先，否则取环境变量默认。

        返回空列表 = 不限制（E2B 默认放通全部出口）。
        E2B 硬限制：最多 10 个域名，超出会被服务端 400 拒绝，这里先截断并告警。
        """
        domains: list[str] = []
        if self._workspaces is not None and workspace_id:
            try:
                record = await self._workspaces.get(user_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 - 查询失败不阻塞沙箱
                logger.warning("load workspace for network policy failed: %s", exc)
                record = None
            metadata = (record or {}).get("metadata") or {}
            raw = metadata.get("allowedDomains")
            if isinstance(raw, list):
                domains = [str(d).strip() for d in raw if str(d).strip()]
        if not domains:
            domains = allowed_domains_from_env()
        if len(domains) > MAX_ALLOWED_DOMAINS:
            logger.warning(
                "allowedDomains %d 个，超过 E2B 每沙箱上限 %d，已截断",
                len(domains),
                MAX_ALLOWED_DOMAINS,
            )
            domains = domains[:MAX_ALLOWED_DOMAINS]
        return domains

    async def _ensure_network_policy(
        self, sandbox: AsyncSandbox, user_id: str, workspace_id: str
    ) -> None:
        """把出口白名单下发到已存在的沙箱（用于"先建沙箱、后收紧策略"）。

        只在期望白名单非空时下发：update_network 是**整体替换**（省略字段会被清空），
        传空对象即可恢复全放通；这里不主动恢复，避免每次连接多一次往返。
        下发失败不阻塞连接，但必须显式告警（不能静默当作已收紧）。
        """
        domains = await self._resolve_allowed_domains(user_id, workspace_id)
        if not domains:
            return
        policy = tuple(domains)
        sandbox_id = getattr(sandbox, "sandbox_id", "")
        if self._net_policies.get(sandbox_id) == policy:
            return
        try:
            await sandbox.update_network(
                {"allow_out": list(domains), "deny_out": [ALL_TRAFFIC]}
            )
        except Exception as exc:  # noqa: BLE001 - 失败不阻塞，但不能装作已收紧
            logger.warning(
                "apply egress policy to sandbox %s failed: %s", sandbox_id, exc
            )
            return
        self._net_policies[sandbox_id] = policy
        logger.info(
            "sandbox %s egress restricted to %d domains: %s",
            sandbox_id,
            len(domains),
            domains,
        )

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

        路径校验需要它：`workspace/{id}/…` 与 `skills/project/{id}/…` 都只代表
        「当前会话的工作区」，缺 ID 段时要靠它补全，有 ID 段时要靠它判越权。
        读 HTTP 层（文件面板）没有别的途径知道这件事——它只拿得到 sessionId。
        """
        cursor = await self._db.execute(
            "SELECT workspace_id FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id.strip(), session_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (row["workspace_id"] or "") if row else ""

    async def existing_workspace(self, user_id: str, session_id: str) -> AsyncSandbox:
        """仅连接已经创建的 Sandbox，不会创建资源。

        只读场景（浏览/预览/下载）使用；前端文件面板已走本地直读（P0），
        此处不再同步工作区文件。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        if self._backend == "local":
            # 本地沙箱就是磁盘目录：只要目录还在就能打开。
            # 注意这里用 attach（不创建）——"仅连接已存在"的语义必须与 e2b 一致，
            # 否则一个只读预览请求会凭空造出一个空沙箱目录。
            return LocalSandbox.attach(user_id, session_id)  # type: ignore[return-value]
        sandbox_id = await self._load_sandbox_id(user_id, session_id)
        if sandbox_id is None:
            raise SandboxNotFound("sandbox mapping not found")
        workspace = await AsyncSandbox.connect(sandbox_id, timeout=CONNECT_TIMEOUT_MS)
        return workspace

    # 本次新增（P0 性能优化）：Local 后端下把 workspace/ 相对路径映射到本地磁盘
    async def local_workspace_path(self, user_id: str, relative: str):
        """返回 workspace/{workspace_id}/... 对应的本地磁盘路径；非 Local 后端或不存在返回 None。

        命中后 HTTP 层直接 os.scandir/read_bytes 本地直读，毫秒级，完全绕开 E2B 网络往返。
        """
        if self._workspace_volumes is None:
            return None
        backend = self._workspace_volumes.backend
        if not isinstance(backend, LocalVolumeBackend):
            return None
        # 归属校验：该 workspace 必须属于当前用户，防越权
        parts = (relative or "").strip().replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] == "workspace" and self._workspaces is not None:
            workspace_id = parts[1]
            record = await self._workspaces.get(user_id, workspace_id)
            if record is None:
                return None
            # 方案 B（B1a）：确保该 workspace 的 local_root 已注册（惰性、带缓存）
            await self._workspace_volumes.load_and_bind_local_root(
                user_id, workspace_id
            )
        return backend.resolve_workspace_path(relative)

    async def write_local_workspace_file(
        self, user_id: str, relative: str, content: str
    ) -> bool:
        """把文件内容写回本地工作区源（Agent 修改落地）。

        仅 Local 后端且路径位于 workspace/{id}/ 下时生效；其余（E2B 后端 / uploads 等
        沙箱独有目录）返回 False，调用方静默跳过。

        `local` 沙箱后端下直接返回 False：写入路径与本地权威副本是同一个目录，
        `sandbox_write_file` 已经落盘，不需要再写第二遍。
        """
        if self._backend == "local":
            return False
        local_path = await self.local_workspace_path(user_id, relative)
        if local_path is None or local_path.is_dir():
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content, encoding="utf-8")
        return True

    # ---- 方案 B（B2/B3）：与本地目录增量同步 ----
    #
    # **这两个方法在 `local` 沙箱后端下是 no-op**。本地沙箱的 `workspace/{id}`
    # 直接映射到权威副本目录（同一份文件，不是副本），同步没有可搬运的东西；
    # 硬跑一遍只会把每个文件读出来再写回同一个位置。
    # 判定放在方法首行而不是调用点：调用点有三处（工具执行前 / 后、拿沙箱时），
    # 分散判断必然漏掉一处，而漏掉的那处不会报错，只会白跑。

    async def sync_sandbox_to_local(
        self, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> int:
        """B2：把沙箱中与本地不同的文件拉回本地目录（覆盖 run_command 写文件盲区）。"""
        if self._backend == "local":
            return 0
        if self._workspace_volumes is None or not workspace_id:
            return 0
        local_root = await self._workspace_volumes.load_and_bind_local_root(
            user_id, workspace_id
        )
        if not local_root:
            return 0
        from ..workspace.sync import sync_sandbox_to_local as _sync

        try:
            return await _sync(
                sandbox,
                workspace_id,
                Path(local_root),
                prefix=self._workspace_volumes.local_root_prefix_of(workspace_id),
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用
            logger.warning("B2 sync failed ws=%s: %s", workspace_id, exc)
            return 0

    async def sync_local_to_sandbox(
        self, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> int:
        """B3：把本地新增/修改的文件推送到沙箱（工具调用前 / 用户发消息时）。"""
        if self._backend == "local":
            return 0
        if self._workspace_volumes is None or not workspace_id:
            return 0
        local_root = await self._workspace_volumes.load_and_bind_local_root(
            user_id, workspace_id
        )
        if not local_root:
            return 0
        from ..workspace.sync import scan_local_fingerprint
        from ..workspace.sync import sync_local_to_sandbox as _sync

        root = Path(local_root)
        # 廉价快路径：本地自上次同步（同一沙箱）以来未变化 → 跳过逐文件远端比对
        # 键含 sandbox_id：沙箱重建（新 sandbox_id）时必然重新全量同步，不会误跳过
        key = f"{workspace_id}:{getattr(sandbox, 'sandbox_id', '')}"
        fingerprint: tuple | None = None
        try:
            fingerprint = scan_local_fingerprint(root)
            if self._b3_fingerprints.get(key) == fingerprint:
                return 0
        except Exception as exc:  # noqa: BLE001 - 指纹失败不影响正常同步
            logger.warning("B3 fingerprint failed ws=%s: %s", workspace_id, exc)

        try:
            count = await _sync(
                sandbox,
                workspace_id,
                root,
                prefix=self._workspace_volumes.local_root_prefix_of(workspace_id),
            )
            if fingerprint is not None:
                self._b3_fingerprints[key] = fingerprint
            return count
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用
            logger.warning("B3 sync failed ws=%s: %s", workspace_id, exc)
            return 0

    async def _sync_workspace_files(
        self,
        workspace: AsyncSandbox,
        workspace_id: str = "",
        force: bool = False,
        user_id: str = "",
    ) -> None:
        """Local 后端：把指定工作区文件同步进沙箱（E2B 后端 no-op）。

        - 绑定本地目录的工作区：走**增量同步**（前缀 / 忽略规则 / 指纹快路径 / 逐文件写入），
          且失败只告警不抛出——本地目录是权威源、读取走直连，同步失败不应阻塞工具调用。
        - 未绑定的工作区：仅在沙箱新建（force）或该工作区有新写入（dirty）时做全量推送。

        全量推送失败（WorkspaceSyncError）上抛，让工具执行层感知「工作区文件尚未同步完成」，
        避免 Agent 拿到不完整的沙箱视图后盲目执行命令。

        `local` 沙箱后端下 no-op（同 B2/B3：路径直连权威副本）。
        """
        if self._backend == "local":
            return
        if self._workspace_volumes is None or not workspace_id:
            return
        bound = bool(
            user_id
            and await self._workspace_volumes.load_and_bind_local_root(
                user_id, workspace_id
            )
        )
        if bound:
            await self.sync_local_to_sandbox(user_id, workspace_id, workspace)
            return
        if force or self._workspace_volumes.is_dirty(workspace_id):
            await self._workspace_volumes.sync_to_sandbox(workspace, workspace_id)

    async def _open_local(
        self, user_id: str, session_id: str, workspace_id: str
    ) -> LocalSandbox:
        """本机沙箱：目录即沙箱，没有远端资源、没有 volume 挂载、没有同步。

        与 e2b 分支唯一剩下的共同点是登记 `user_sandboxes`（UI 的沙箱面板和
        `existing_workspace` 都读它）。出口白名单、volume 挂载、文件同步在本地
        语义下都不存在。

        两条路径都由 `LocalSandbox` 直接映射到宿主目录，所以这里**刻意不调用**
        `_sync_workspace_files`：
        - `workspace/{id}` → `<data_root>/workspace-volumes/{id}`
        - `skills`         → `<data_root>/skills`
        是同一个目录，不是副本。同步在这里没有可搬运的东西。
        """
        sandbox = LocalSandbox.open(user_id, session_id)

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

    async def workspace(
        self, user_id: str, session_id: str, workspace_id: str = ""
    ) -> AsyncSandbox:
        """返回可恢复的 Sandbox；首次调用才创建资源。

        workspace_id 用于按需同步（Local 后端）：仅同步当前会话所属工作区，
        且只在「沙箱新建」或「该工作区有新写入（dirty）」时同步，避免每次工具调用全量同步。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        if self._backend == "local":
            return await self._open_local(user_id, session_id, workspace_id)
        sandbox_id = await self._load_sandbox_id(user_id, session_id)
        if sandbox_id is not None:
            workspace = await AsyncSandbox.connect(
                sandbox_id, timeout=CONNECT_TIMEOUT_MS
            )
            await self._db.execute(
                "UPDATE user_sandboxes SET updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            await self._db.commit()
            # 出口策略：先建沙箱、后收紧的场景在这里补发（带 memo，不重复下发）
            await self._ensure_network_policy(workspace, user_id, workspace_id)
            await self._sync_workspace_files(workspace, workspace_id, user_id=user_id)
            return workspace

        # volume_mounts: key = 挂载路径，value = Volume 名称/对象。
        # 与 SDK 签名 Dict[path, Union[AsyncVolume, str]] 一致（SDK 内部 name=vol.name）。
        # Skill 不再需要挂载：桌面端 `/home/user/skills` 由 LocalSandbox 直接
        # junction 到数据目录，走不到 e2b 这条路径。
        volume_mounts: dict[str, str] = {}

        # 确保当前工作区有持久化 Volume（懒创建，否则 git clone 会落到沙箱临时盘、跨会话丢失），
        #    再挂载该用户全部已建卷的工作区。
        if self._workspace_volumes is not None and workspace_id:
            try:
                await self._workspace_volumes.ensure_volume(user_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 - 卷创建失败不阻塞沙箱，但该工作区文件不持久
                logger.warning(
                    "ensure workspace volume failed ws=%s: %s", workspace_id, exc
                )
        if self._workspaces is not None:
            try:
                ws_entries = await self._workspaces.list_mounts(user_id)
            except Exception as exc:  # noqa: BLE001 - 工作区查询失败不阻塞沙箱
                logger.warning("failed to load workspace mounts: %s", exc)
                ws_entries = []
            for volume_name, ws_id in ws_entries:
                volume_mounts[WORKSPACE.workspace_path(ws_id)] = volume_name

        # 创建沙箱
        allowed_domains = await self._resolve_allowed_domains(user_id, workspace_id)
        workspace = await AsyncSandbox.create(
            template=self._template,
            timeout=CONNECT_TIMEOUT_MS,
            metadata={"edith_user_id": user_id, "edith_session_id": session_id},
            lifecycle=SandboxLifecycle(on_timeout="pause", auto_resume=True),
            volume_mounts=volume_mounts,
            # 出口白名单（默认放通；配置了 allowedDomains 才收紧）
            network=(
                {"allow_out": list(allowed_domains), "deny_out": [ALL_TRAFFIC]}
                if allowed_domains
                else None
            ),
        )
        if allowed_domains:
            sandbox_id = getattr(workspace, "sandbox_id", "")
            self._net_policies[sandbox_id] = tuple(allowed_domains)
            logger.info(
                "sandbox %s created with egress allowlist (%d domains): %s",
                sandbox_id,
                len(allowed_domains),
                allowed_domains,
            )
        # Local 后端：沙箱创建后把当前工作区文件同步进沙箱（E2B 后端 no-op）
        await self._sync_workspace_files(
            workspace, workspace_id, force=True, user_id=user_id
        )
        await self._db.execute(
            "INSERT INTO user_sandboxes (user_id, session_id, sandbox_id) VALUES (?, ?, ?)",
            (user_id, session_id, workspace.sandbox_id),
        )
        await self._db.commit()
        return workspace
