"""sandbox 的文件浏览、上传与下载 HTTP 接口。

对应 Go 版 internal/sandbox/http.go + routes.go。
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path

from fastapi import APIRouter, UploadFile
from fastapi.responses import StreamingResponse

from ..http_utils import EDITHError
from ..permissions.paths import PathRejected, check_path, guard_real_path
from ..workspace.volume_backend import LocalLookup, LocalLookupKind
from .local import LocalSandbox, SandboxMountError, SandboxPathError
from .service import SandboxNotFound, Service
from .workspace import relative_path, workspace_path

DEFAULT_READ_LIMIT = 32 * 1024
MAX_UPLOAD_SIZE = 50 << 20


def _guard_virtual(path: str) -> None:
    """第一遍：用户请求的路径本身命中敏感段就直接拒绝。

    与 Agent 文件工具同一个规则源（`permissions/paths.check_path`）。面板过去只做
    归属校验，敏感段完全没判：同一台机器上，Agent 读不到 `.env`、面板却能预览，
    这种"两套语义"既解释不清，也挡不住链接绕过（第二遍见 `_guard_real`）。
    """
    hit = check_path(path)
    if hit:
        raise EDITHError(
            403, "path_forbidden", f"敏感路径不可访问：{path}（命中 {hit}）"
        )


def _guard_real(real: Path, root: Path | None) -> None:
    """第二遍：对解析后的**真实路径**再判一次（敏感段 + 越界）。

    逻辑路径可以完全干净，落点却是 `~/.ssh/id_rsa` 或绑定根之外的目录——
    只有看真实路径才拦得住。`root` 为空时只判敏感段（越界由沙箱 `resolve()` 保证）。
    """
    try:
        guard_real_path(real, root=root)
    except PathRejected as exc:
        raise EDITHError(403, "path_forbidden", str(exc)) from exc


def _guard_local(lookup: LocalLookup) -> None:
    """面板直读本地磁盘前的核验；`READY` 之外的分类不在这里处理。"""
    if lookup.path is None:
        return
    _guard_real(lookup.path, lookup.root)


def _guard_sandbox(sandbox: LocalSandbox, virtual: str) -> Path:
    """沙箱分支的核验：`resolve()` 已保证不越界，这里补"落点是否敏感"。"""
    try:
        resolved = sandbox.resolve(virtual)
    except SandboxPathError as exc:
        raise EDITHError(403, "path_forbidden", str(exc)) from exc
    try:
        real = Path(os.path.realpath(resolved))
    except OSError as exc:  # 无法解析 = 无法判定，按拒绝处理
        raise EDITHError(403, "path_forbidden", f"路径无法解析为真实位置：{exc}") from exc
    _guard_real(real, None)
    return resolved


def _local_entries(relative: str, directory: Path) -> list[dict]:
    """把本地目录扫成与列表接口一致的响应结构。"""
    entries: list[dict] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return entries
    for child in children:
        entry_path = posixpath.join(relative, child.name)
        # 敏感条目不出现在列表里：面板不该成为"枚举有哪些密钥"的入口，
        # 而这些条目点开也会被 `_guard_virtual` 拒绝——列表与读取必须同一套语义。
        if check_path(entry_path):
            continue
        entries.append(
            {
                "name": child.name,
                "path": entry_path,
                "type": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return entries


class HTTP:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.router = self._build_router()

    async def _existing(self, user_id: str, session_id: str, path: str, allow_root: bool):
        """校验路径 + 打开已存在的沙箱。

        **不碰文件系统挂载**：只用 DB 查工作区上下文、纯字符串拼虚拟路径、`root.is_dir()`
        打开沙箱，全程不调 `sandbox.resolve()`（那会触发 `_ensure_*_mount`）。
        这个约束有调用方依赖——`list_files` 的 `except SandboxMountError` 只包住
        `sandbox.files.list()`。若将来在这里加路径解析，那处异常映射会漏捕，
        挂载损坏会退化成不带错误码的 500。
        """
        if not user_id.strip() or not session_id.strip():
            raise EDITHError(400, "invalid_request", "userId and sessionId are required")
        # 文件面板只拿得到 sessionId，工作区上下文要从会话本身取：
        # `workspace` 与 `skills/project` 是虚拟命名空间，缺 ID 段时按它补全，
        # 有 ID 段时按它判越权。不给这个上下文，面板展开 `skills/project` 必然失败。
        current_workspace_id = await self.service.session_workspace_id(user_id, session_id)
        try:
            real_path = workspace_path(
                path, allow_root, current_workspace_id=current_workspace_id
            )
        except ValueError as exc:
            raise EDITHError(400, "invalid_path", str(exc))
        except PermissionError as exc:
            # 不是「路径写错了」而是「这条路径在本次会话里不存在/不属于你」→ 403
            raise EDITHError(403, "path_forbidden", str(exc))
        try:
            sandbox = await self.service.existing_workspace(user_id, session_id)
        except SandboxNotFound:
            raise EDITHError(404, "sandbox_not_found", "sandbox has not been created for this session")
        return sandbox, real_path

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/sandbox/files")
        async def list_files(userId: str = "", sessionId: str = "", path: str = "") -> dict:
            # 工作区路径优先直读本地磁盘（毫秒级）。本地没有时**是否回落取决于
            # 权威根是谁**：未绑定工作区与非工作区路径回落到沙箱（沿用改造前行为），
            # 绑定工作区不回落——那里的权威就是用户目录，回落只会读到
            # `workspace-volumes/{id}` 那份旧副本，用户会以为自己看的是绑定目录。
            _guard_virtual(path)
            lookup = await self.service.local_workspace_lookup(userId, path)
            if lookup.kind is LocalLookupKind.UNAVAILABLE:
                raise EDITHError(409, "workspace_unavailable", lookup.detail)
            if lookup.kind is LocalLookupKind.READY and lookup.path is not None:
                # `is_dir()` 会跟随链接：先核验真实落点，再决定要不要读它
                _guard_local(lookup)
                if lookup.path.is_dir():
                    return {"path": path, "entries": _local_entries(path, lookup.path)}
                if not lookup.fallback_allowed:
                    return {"path": path, "entries": []}
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=True)
            try:
                _guard_sandbox(sandbox, real_path)
                entries = await sandbox.files.list(real_path, depth=1)
            except EDITHError:
                raise
            except SandboxMountError as exc:
                # 挂载点坏了不是「目录为空」。这个分支原本把所有异常吞成空列表，
                # 会把「两个视图分裂」伪装成「工作区里没有文件」——正是要修的病。
                raise EDITHError(500, "sandbox_mount_broken", str(exc)) from exc
            except Exception:  # noqa: BLE001
                # 目录不存在（如工作区 Volume 尚未挂载到当前沙箱）→ 按空目录处理，避免 500
                entries = []
            listed = [
                {"name": e.name, "path": relative_path(e.path),
                 "type": "directory" if e.type.value == "dir" else "file", "size": e.size or 0}
                for e in entries
            ]
            # 与本地直读分支同一套过滤：敏感条目既不展示，也读不到
            return {
                "path": relative_path(real_path),
                "entries": [item for item in listed if not check_path(item["path"])],
            }

        @router.get("/internal/sandbox/files/content")
        async def read_content(userId: str = "", sessionId: str = "", path: str = "") -> dict:
            # 与列表同一套判定：绑定态的文件不在本地就是不存在（404），
            # 不回落——回落读的是另一份数据。
            _guard_virtual(path)
            lookup = await self.service.local_workspace_lookup(userId, path)
            if lookup.kind is LocalLookupKind.UNAVAILABLE:
                raise EDITHError(409, "workspace_unavailable", lookup.detail)
            if lookup.kind is LocalLookupKind.READY and lookup.path is not None:
                # `is_file()` 与 `read_text()` 都会跟随链接：先判真实落点
                _guard_local(lookup)
                if lookup.path.is_file():
                    data = lookup.path.read_text(encoding="utf-8", errors="replace")
                    truncated = len(data) > DEFAULT_READ_LIMIT
                    content = data[:DEFAULT_READ_LIMIT]
                    if "\x00" in content:
                        raise EDITHError(422, "file_not_previewable", "only UTF-8 text files can be previewed")
                    return {"path": path, "content": content, "truncated": truncated}
                if not lookup.fallback_allowed:
                    raise EDITHError(404, "file_not_found", f"{path} 不在绑定的工作区目录里")
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=False)
            _guard_sandbox(sandbox, real_path)
            data = await sandbox.files.read(real_path)
            truncated = len(data) > DEFAULT_READ_LIMIT
            content = data[:DEFAULT_READ_LIMIT]
            if "\x00" in content:
                raise EDITHError(422, "file_not_previewable", "only UTF-8 text files can be previewed")
            return {"path": relative_path(real_path), "content": content, "truncated": truncated}

        @router.post("/internal/sandbox/files/upload")
        async def upload_file(
            userId: str = "",
            sessionId: str = "",
            file: UploadFile | None = None,
        ) -> dict:
            if not userId.strip() or not sessionId.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionId are required")
            if file is None or not file.filename:
                raise EDITHError(400, "invalid_upload", "file is required")
            name = file.filename.replace("\\", "/").split("/")[-1]
            if not name or name in (".", "/"):
                raise EDITHError(400, "invalid_upload", "invalid file name")
            # 上传是**写入**入口：`.env` / 私钥类文件名一律拒绝，否则面板可以往
            # 工作区里放一个 Agent 工具永远读不到、但 shell 可能读到的文件。
            _guard_virtual(f"uploads/{name}")
            data = await file.read()
            if not data or len(data) > MAX_UPLOAD_SIZE:
                raise EDITHError(400, "invalid_upload", "file must be non-empty and at most 50 MB")
            try:
                sandbox = await self.service.workspace(userId, sessionId)
                target = workspace_path(f"uploads/{name}", allow_root=False)
                await sandbox.files.write(target, data)
            except Exception as exc:  # noqa: BLE001
                raise EDITHError(502, "sandbox_files_unavailable", str(exc))
            return {"path": relative_path(target), "name": name, "size": len(data)}

        @router.get("/internal/sandbox/files/download")
        async def download_file(userId: str = "", sessionId: str = "", path: str = "") -> StreamingResponse:
            _guard_virtual(path)
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=False)
            if not relative_path(real_path).startswith("artifacts/"):
                raise EDITHError(403, "download_not_allowed", "only artifacts files can be downloaded")
            _guard_sandbox(sandbox, real_path)
            data = await sandbox.files.read(real_path)
            filename = path.split("/")[-1]
            return StreamingResponse(
                iter([data]),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        return router
