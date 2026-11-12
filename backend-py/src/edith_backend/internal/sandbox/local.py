"""本机沙箱：把 E2B 的文件 / 命令原语换成对本地目录的操作。

**为什么是"形状兼容"而不是"重新抽象"**
`tools.py` 有一千行，全部通过 `sandbox.files.*` / `sandbox.commands.*` 与沙箱交互。
只要本类提供同名同形状的 `.files` / `.commands` / `.sandbox_id`，工具层一行都不用改，
分歧就只存在于 `service.workspace()` 那一个选择点上——这比在每个 `_op_*` 里判后端
要可靠得多（后者迟早会漏一个）。

**安全边界（诚实说明）**
文件原语做了路径约束：只允许 `WORKSPACE.root`（`/home/user`）之下，`..` 与
符号链接逃逸都会被拒。这是**应用层**约束，不是内核约束。
`commands.run` 执行的是本机真实进程，没有 OS 级隔离——它能读写用户有权限的一切。
所以：本地后端的适用前提是"用户在自己的机器上跑自己的 Agent"。
给别人分发时需要额外一层（受限 token / AppContainer / 容器），那是 M4 的事，
不能靠本文件补。
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e2b import FileType

from ...config import data_root
from ..workspace.layout import WORKSPACE

# 单次读文件上限：本地磁盘没有网络成本，但没有上限时一次 `files.read` 就能把
# 几百 MB 塞进上下文。与 E2B 后端的 `SYNC_MAX_FILE_BYTES` 保持同一量级。
MAX_READ_BYTES = 10 * 1024 * 1024


class SandboxPathError(PermissionError):
    """路径越出沙箱根（`/home/user`）。

    刻意继承 PermissionError：上层已把 PermissionError 映射成 403，
    这里不需要再加一条新的错误分支。
    """


@dataclass
class LocalEntryInfo:
    """与 E2B `EntryInfo` 的形状子集对齐（tools.py 只用到这 4 个字段）。"""

    name: str
    path: str
    type: FileType
    size: int = 0


@dataclass
class LocalCommandResult:
    """与 E2B `CommandResult` 的形状子集对齐。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class LocalCommandInfo:
    """与 E2B `CommandInfo` 的形状子集对齐。"""

    pid: int
    cmd: str
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    tag: str = ""


def sandbox_root_for(user_id: str, session_id: str) -> Path:
    """某会话的本地沙箱根目录。`{data}/sandbox/{user}/{session}`。"""
    safe_user = _safe_segment(user_id)
    safe_session = _safe_segment(session_id)
    return data_root() / "sandbox" / safe_user / safe_session


def _safe_segment(raw: str) -> str:
    """把 id 变成安全的单段目录名（与 skills/storage/local.py 同一套规则）。"""
    text = (raw or "").strip()
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in text)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("sandbox id cannot be mapped to a safe directory name")
    return cleaned


class LocalSandbox:
    """一个会话一个实例；目录在磁盘上持久，重启后可重新打开。"""

    # 能力声明：读取没有网络成本，不需要"大文件改用 sed 按行读"的优化。
    # tools.py 依据这个标记跳过 sed 分支——那个分支在 Windows 上没有 sed 可执行。
    supports_large_read = True

    def __init__(self, root: Path, sandbox_id: str) -> None:
        self._root = root
        self.sandbox_id = sandbox_id
        self.files = LocalFiles(self)
        self.commands = LocalCommands(self)
        # 后台进程登记表：pid → asyncio 子进程。只用于 list/kill，
        # 进程退出后由 `_reap` 摘掉，避免无限增长。
        self._background: dict[int, asyncio.subprocess.Process] = {}

    # ---- 生命周期 ----

    @classmethod
    def open(cls, user_id: str, session_id: str) -> LocalSandbox:
        """打开（或重建）本地沙箱。

        与 E2B 的关键差别：目录被外部删掉时这里直接重建，不会像失效的
        `sandbox_id` 那样让 `connect` 永远失败。本地模式下「沙箱」就是一个目录，
        重建的代价是零。
        """
        root = sandbox_root_for(user_id, session_id)
        root.mkdir(parents=True, exist_ok=True)
        # 三个约定目录先建好：模型第一次 `ls` 时不该看到"目录不存在"。
        for name in (WORKSPACE.uploads, WORKSPACE.work, WORKSPACE.artifacts, WORKSPACE.workspace):
            (root / name).mkdir(parents=True, exist_ok=True)
        return cls(root, f"local-{_safe_segment(user_id)}-{_safe_segment(session_id)}")

    @classmethod
    def attach(cls, user_id: str, session_id: str) -> LocalSandbox:
        """只打开已存在的沙箱，不创建。不存在时抛 FileNotFoundError。"""
        root = sandbox_root_for(user_id, session_id)
        if not root.is_dir():
            raise FileNotFoundError(f"本地沙箱目录不存在: {root}")
        return cls(root, f"local-{_safe_segment(user_id)}-{_safe_segment(session_id)}")

    async def kill(self) -> None:
        """结束所有后台进程。**不删目录**：工作区文件是用户的，不能随沙箱一起没。"""
        for pid in list(self._background):
            await self.commands.kill(pid)
        self._background.clear()

    # ---- 路径 ----

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, virtual: str) -> Path:
        """虚拟路径 → 本机真实路径，越界即拒。

        接受三种写法：`/home/user/...`（绝对）、`work/a.txt`（相对沙箱根）、
        `.`（沙箱根本身）。其他绝对路径（`/etc`、`C:\\...`）一律拒绝——
        沙箱的语义就是"只有这一个根"。

        特例：`workspace/{id}/...` 直接落到 `workspace-volumes/{id}/`，**不做同步**。
        这是本地后端最重要的简化——E2B 后端里沙箱与权威副本是两份，才需要
        `workspace/sync.py` 那 842 行的推送/回拉/护栏/熔断；本地模式下它们
        本来就是同一个目录，同步问题从根上不存在。
        """
        raw = (virtual or "").replace("\\", "/").strip()
        if raw.startswith(WORKSPACE.root):
            rel = raw[len(WORKSPACE.root) :].lstrip("/")
        elif raw.startswith("/"):
            raise SandboxPathError(f"路径越出沙箱根 {WORKSPACE.root}: {virtual}")
        else:
            rel = raw

        normalized = posixpath.normpath(rel) if rel else "."
        if normalized == ".." or normalized.startswith("../"):
            raise SandboxPathError(f"路径不能上溯到沙箱根之外: {virtual}")

        # `workspace/{id}` 是一个**虚拟挂载点**，不是沙箱里的普通目录。
        # 允许 `workspace/ws-1/../../x` 归一化成 `x` 落回沙箱根，等于让模型
        # 用相对路径静默跳出工作区边界（虽然没出沙箱，但"工作区文件"的语义就乱了）。
        # 规则：一旦路径以 workspace/ 开头，归一化后必须仍在同一个 workspace/{id} 下。
        original = [p for p in rel.replace("\\", "/").split("/") if p]
        if original and original[0] == WORKSPACE.workspace:
            normalized_parts = normalized.split("/")
            if (
                len(original) >= 2
                and (
                    len(normalized_parts) < 2
                    or normalized_parts[0] != WORKSPACE.workspace
                    or normalized_parts[1] != original[1]
                )
            ):
                raise SandboxPathError(f"路径不能跳出工作区边界: {virtual}")

        parts = normalized.split("/")
        if parts and parts[0] == WORKSPACE.workspace and len(parts) >= 2:
            return self._resolve_workspace_volume(parts[1], parts[2:], virtual)

        base = self._root.resolve()
        target = base if normalized == "." else (base / normalized).resolve()
        # resolve() 会跟随符号链接，所以这一步同时挡住了「软链指向 C:\Windows」。
        # 用 is_relative_to 而不是字符串前缀：`/a/bc` 不能算在 `/a/b` 之内。
        if target != base and not target.is_relative_to(base):
            raise SandboxPathError(f"路径解析后越出沙箱根: {virtual} → {target}")
        return target

    def _resolve_workspace_volume(self, workspace_id: str, rest: list[str], virtual: str) -> Path:
        """`workspace/{id}/…` → `workspace-volumes/{id}/…`（权威副本，零拷贝）。"""
        base = (data_root() / "workspace-volumes" / _safe_segment(workspace_id)).resolve()
        if not rest or rest == [""]:
            return base
        normalized = posixpath.normpath("/".join(rest))
        if normalized == ".." or normalized.startswith("../"):
            raise SandboxPathError(f"路径不能上溯到工作区根之外: {virtual}")
        target = (base / normalized).resolve()
        if target != base and not target.is_relative_to(base):
            raise SandboxPathError(f"路径解析后越出工作区根: {virtual} → {target}")
        return target

    def virtual(self, real: Path) -> str:
        """真实路径 → 虚拟路径（回给模型的路径必须是 `/home/user/...`）。"""
        resolved = real.resolve()

        # 工作区权威副本要还原成 `workspace/{id}/…`，否则模型会看到
        # `D:\...\workspace-volumes\...` 这种它根本无法再次传入的路径。
        volumes = (data_root() / "workspace-volumes").resolve()
        if resolved != volumes and resolved.is_relative_to(volumes):
            rel = resolved.relative_to(volumes).as_posix()
            return posixpath.join(WORKSPACE.root, WORKSPACE.workspace, rel)

        base = self._root.resolve()
        try:
            rel = resolved.relative_to(base).as_posix()
        except ValueError:
            return str(real)
        return WORKSPACE.root if rel in ("", ".") else posixpath.join(WORKSPACE.root, rel)


class LocalFiles:
    """E2B `sandbox.files` 的本机实现。"""

    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sb = sandbox

    async def read(self, path: str, format: str = "text") -> str | bytes:
        real = self._sb.resolve(path)
        if not real.is_file():
            raise FileNotFoundError(f"{path} 不存在或不是文件")
        if real.stat().st_size > MAX_READ_BYTES:
            raise ValueError(
                f"文件超过 {MAX_READ_BYTES // (1024 * 1024)}MB，请用 run_command 按行读取"
            )
        data = real.read_bytes()
        if format == "bytes":
            return data
        return data.decode("utf-8", errors="replace")

    async def write(self, path: str, content: str | bytes) -> LocalEntryInfo:
        real = self._sb.resolve(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            real.write_bytes(content)
        else:
            real.write_bytes(content.encode("utf-8"))
        return self._entry(real)

    async def list(self, path: str, depth: int = 1) -> list[LocalEntryInfo]:
        real = self._sb.resolve(path)
        if not real.exists():
            raise FileNotFoundError(f"{path} 不存在")
        if not real.is_dir():
            return [self._entry(real)]

        entries: list[LocalEntryInfo] = []
        # depth 语义与 E2B 一致：1 = 只列直接子项，2 = 再往下一层。
        # 用显式栈而不是递归：深层目录 + 符号链接环不会把栈打爆。
        stack: list[tuple[Path, int]] = [(real, 1)]
        while stack:
            current, level = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                continue
            for child in children:
                entries.append(self._entry(child))
                if level < depth and child.is_dir() and not child.is_symlink():
                    stack.append((child, level + 1))
        return entries

    async def make_dir(self, path: str) -> bool:
        self._sb.resolve(path).mkdir(parents=True, exist_ok=True)
        return True

    async def remove(self, path: str) -> None:
        """删除文件或目录（目录递归）。

        E2B 的 `files.remove` 对目录同样是递归的，这里保持一致。
        调用方（`_prune_empty_parents`）已经先 list 确认空目录才删，
        所以递归删除不会误伤。
        """
        real = self._sb.resolve(path)
        if not real.exists():
            raise FileNotFoundError(f"{path} 不存在")
        if real.is_dir() and not real.is_symlink():
            shutil.rmtree(real)
        else:
            real.unlink()

    async def rename(self, old_path: str, new_path: str) -> LocalEntryInfo:
        src = self._sb.resolve(old_path)
        dst = self._sb.resolve(new_path)
        if not src.exists():
            raise FileNotFoundError(f"{old_path} 不存在")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return self._entry(dst)

    async def get_info(self, path: str) -> LocalEntryInfo:
        real = self._sb.resolve(path)
        if not real.exists():
            raise FileNotFoundError(f"{path} 不存在")
        return self._entry(real)

    def _entry(self, real: Path) -> LocalEntryInfo:
        try:
            stat = real.stat()
            size = stat.st_size
        except OSError:
            size = 0
        is_dir = real.is_dir() and not real.is_symlink()
        return LocalEntryInfo(
            name=real.name,
            path=self._sb.virtual(real),
            type=FileType.DIR if is_dir else FileType.FILE,
            size=0 if is_dir else size,
        )


class LocalCommands:
    """E2B `sandbox.commands` 的本机实现。"""

    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sb = sandbox

    async def run(
        self,
        cmd: str,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        background: bool = False,
        **_: Any,
    ) -> LocalCommandResult | LocalCommandInfo:
        work_dir = self._sb.resolve(cwd or WORKSPACE.work_path())
        work_dir.mkdir(parents=True, exist_ok=True)

        env = {**os.environ}
        if envs:
            env.update({str(k): str(v) for k, v in envs.items()})
        # 子进程一律 UTF-8：Windows 默认用 GBK 解码中文输出，`decode` 会在
        # 工具结果里变成乱码（模型会把它当成命令失败）。
        env.setdefault("PYTHONIOENCODING", "utf-8")

        if background:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(work_dir),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **self._spawn_kwargs(),
            )
            assert proc.pid is not None
            self._sb._background[proc.pid] = proc
            # 退出后由 commands.list() 摘除。调用方不一定会 list，所以表可能
            # 残留已退出项；它们不占资源（Process 对象很小），且 kill 前会先判 returncode。
            return LocalCommandInfo(pid=proc.pid, cmd=cmd, cwd=self._sb.virtual(work_dir))

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(work_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **self._spawn_kwargs(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await self._kill_tree(proc)
            return LocalCommandResult(
                stdout="",
                stderr=f"命令超时（{timeout} 秒）已被终止: {cmd}",
                exit_code=124,
            )
        except asyncio.CancelledError:
            # 用户点了停止 / run 被取消：不能把子进程留成孤儿继续占着文件。
            await self._kill_tree(proc)
            raise

        return LocalCommandResult(
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )

    async def list(self) -> list[LocalCommandInfo]:
        alive: list[LocalCommandInfo] = []
        for pid, proc in list(self._sb._background.items()):
            if proc.returncode is not None:
                self._sb._background.pop(pid, None)
                continue
            alive.append(LocalCommandInfo(pid=pid, cmd="", cwd=self._sb.virtual(self._sb.root)))
        return alive

    async def kill(self, pid: int) -> None:
        proc = self._sb._background.get(pid)
        if proc is None:
            # 不是本会话登记的后台进程：仍然尝试按 pid 结束（可能是 run 里
            # background=True 之外启动、但被模型用 pid 引用的进程）。
            self._kill_pid(pid)
            return
        await self._kill_tree(proc)
        self._sb._background.pop(pid, None)

    # ---- 内部 ----

    def _spawn_kwargs(self) -> dict[str, Any]:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NO_WINDOW}
        # POSIX：独立进程组，便于整组结束（shell 会派生子进程）
        return {"start_new_session": True}

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        self._kill_pid(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    def _kill_pid(self, pid: int) -> None:
        """结束进程及其子进程。

        只 kill 父进程会留下孤儿：`cmd1 && cmd2`、`python x.py` 派生的解释器
        都还在跑，继续占着工作区文件（Windows 上表现为"文件被占用无法删除"）。
        """
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            return
        try:
            os.killpg(os.getpgid(pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass


# ---- 供 Service 使用的小工具 ----
