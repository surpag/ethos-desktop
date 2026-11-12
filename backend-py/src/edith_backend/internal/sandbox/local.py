"""本机沙箱：用本地目录实现工具层需要的文件 / 命令原语。

**契约位置**
`tools.py` 有一千行，全部通过 `sandbox.files.*` / `sandbox.commands.*` 与沙箱交互。
本类就是这套契约的**唯一**实现：`.files` / `.commands` / `.sandbox_id` 三个属性，
文件项形状见 `types.FileType`。工具层不认识"后端"这个概念，也就不存在
「换一条路走」的分支要维护。

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
import locale
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import data_root
from ..skills.types import SKILLS_PATH
from ..workspace.layout import WORKSPACE
from .types import FileType

logger = logging.getLogger(__name__)

# 单次读文件上限：本地磁盘没有网络成本，但没有上限时一次 `files.read` 就能把
# 几百 MB 塞进上下文。
MAX_READ_BYTES = 10 * 1024 * 1024


class SandboxPathError(PermissionError):
    """路径越出沙箱根（`/home/user`）。

    刻意继承 PermissionError：上层已把 PermissionError 映射成 403，
    这里不需要再加一条新的错误分支。
    """


@dataclass
class LocalEntryInfo:
    """目录项：工具层只用到这 4 个字段。"""

    name: str
    path: str
    type: FileType
    size: int = 0


@dataclass
class LocalCommandResult:
    """命令执行结果。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class LocalCommandInfo:
    """后台进程句柄。"""

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


# ---- 命令执行环境 ----
#
# 三条都来自同一个事实：**模型是按 Linux 习惯写命令的**。
# 它看到的是 `/home/user`、`sed`、`wc -l`、管道；这些在 Linux 沙箱里天然成立，本机不成立。
# 所以本地后端的职责不是"尽量能跑"，而是把这三件事补齐，否则每条命令都是试错。

# Git for Windows 自带的 bash 是最好的替代品：POSIX 语义 + 能在 Windows 上跑 exe。
# 只在导入时探测一次；探测不到就退回系统 shell（语义降级，但至少能跑）。
_SHELL_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _find_posix_shell() -> str | None:
    if sys.platform != "win32":
        return shutil.which("bash") or "/bin/sh"
    for candidate in _SHELL_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    # 非默认安装路径：从 PATH 找，且必须排除 WSL 的 System32\bash.exe
    # （它会把命令送进另一个 Linux 发行版，看不到本机的沙箱目录）。
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


POSIX_SHELL = _find_posix_shell()


def _shell_path() -> str:
    """给子进程一套**确定的** PATH。

    为什么要重写：应用若从开始菜单/双击启动，PATH 里没有 Git 的 `usr\\bin`，
    `ls`、`sed`、`grep` 全部消失——而模型的命令里到处是它们。
    开发时从 Git Bash 里 `npm start` 会"恰好"继承到，测出来的通过是假的。
    """
    parts: list[str] = []
    if POSIX_SHELL:
        shell_dir = Path(POSIX_SHELL).parent          # ...\Git\bin 或 ...\Git\usr\bin
        usr_bin = shell_dir.parent / "usr" / "bin"    # 无论哪种来源，POSIX 工具都在这里
        parts += [str(usr_bin), str(shell_dir)]
    for key in ("SystemRoot", "windir"):
        root = os.environ.get(key)
        if root:
            parts += [str(Path(root) / "System32"), root]
    existing = os.environ.get("PATH", "")
    if existing:
        parts.append(existing)
    # 去重但保序：靠前的先赢
    seen: set[str] = set()
    ordered: list[str] = []
    for item in parts:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            ordered.append(item)
    return os.pathsep.join(ordered)


def _decode_output(raw: bytes) -> str:
    """子进程输出 → str。

    只按 UTF-8 解码是错的：Windows 上 `cmd`/系统程序按控制台代码页（中文为 GBK）
    输出，按 UTF-8 解会得到 `ϵͳ�Ҳ���ָ����·����` 这种乱码，模型会把它当成
    "命令本身出错了"而不是"路径不存在"，于是换着花样重试。
    先严格 UTF-8，再按本地代码页，最后兜底 replace。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in {locale.getpreferredencoding(False), "gbk", "mbcs"}:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# 只替换"完整的一段路径开头"：`/home/user/x` 要换，`/home/username` 不能换。
# 后面的分隔符涵盖引号、空白、管道与重定向——这些都是命令字符串里真实会出现的边界。
_VIRTUAL_ROOT_RE = re.compile(r"/home/user(?=[/\s'\"`;|&)]|$)")


def _is_link(path: Path) -> bool:
    """判断是不是目录联接 / 符号链接。

    `Path.is_symlink()` 对 Windows junction 返回 False（junction 是 reparse point，
    不是 symlink），所以还要看 stat 的 FILE_ATTRIBUTE_REPARSE_POINT 位。
    """
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _make_link(link: Path, target: Path) -> bool:
    """建目录联接（Windows）或符号链接（POSIX）。失败返回 False，由调用方决定告警。"""
    if sys.platform != "win32":
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    # mklink 是 cmd 内建命令，必须经 cmd /c；/J 的联接不需要管理员权限。
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and link.exists()


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

        与远端沙箱的关键差别：目录被外部删掉时这里直接重建，不会像失效的
        `sandbox_id` 那样让 `connect` 永远失败。本地模式下「沙箱」就是一个目录，
        重建的代价是零。
        """
        root = sandbox_root_for(user_id, session_id)
        root.mkdir(parents=True, exist_ok=True)
        # 平铺目录先建好：模型第一次 `ls` 时不该看到"目录不存在"。
        # `workspace/` 刻意不在这里建——它是挂载点（联接），由
        # `_ensure_workspace_mount` 在首次解析工作区路径时创建（需要 workspace_id）。
        # `skills/` 是固定挂载点，不依赖任何参数，所以这里就能建。
        for name in (WORKSPACE.uploads, WORKSPACE.work, WORKSPACE.artifacts):
            (root / name).mkdir(parents=True, exist_ok=True)
        sandbox = cls(root, f"local-{_safe_segment(user_id)}-{_safe_segment(session_id)}")
        sandbox._ensure_skills_mount()
        return sandbox

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

        特例：`workspace/{id}/...` 直接落到 `workspace-volumes/{id}/`。
        这是本地模式最重要的简化——**只有一份文件**。凡是要在"沙箱副本"与
        权威副本之间搬运、比对、熔断的设计，在这里都没有存在前提。
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
        # `skills` 同样是一个虚拟挂载点（指向 `<data_root>/skills`）。
        # 必须在这里分流：下面的 `is_relative_to(base)` 会跟随联接后的真实路径，
        # 而真实路径不在沙箱根下，否则每个 Skill 读写都会被判成越界。
        if normalized == SKILLS_PATH or normalized.startswith(SKILLS_PATH + "/"):
            return self._resolve_skills(normalized, virtual)

        base = self._root.resolve()
        target = base if normalized == "." else (base / normalized).resolve()
        # resolve() 会跟随符号链接，所以这一步同时挡住了「软链指向 C:\Windows」。
        # 用 is_relative_to 而不是字符串前缀：`/a/bc` 不能算在 `/a/b` 之内。
        if target != base and not target.is_relative_to(base):
            raise SandboxPathError(f"路径解析后越出沙箱根: {virtual} → {target}")
        return target

    def _resolve_workspace_volume(self, workspace_id: str, rest: list[str], virtual: str) -> Path:
        """`workspace/{id}/…` → `workspace-volumes/{id}/…`（权威副本，零拷贝）。"""
        base = self._ensure_workspace_mount(workspace_id)
        if not rest or rest == [""]:
            return base
        normalized = posixpath.normpath("/".join(rest))
        if normalized == ".." or normalized.startswith("../"):
            raise SandboxPathError(f"路径不能上溯到工作区根之外: {virtual}")
        target = (base / normalized).resolve()
        if target != base and not target.is_relative_to(base):
            raise SandboxPathError(f"路径解析后越出工作区根: {virtual} → {target}")
        return target

    def _resolve_skills(self, normalized: str, virtual: str) -> Path:
        """`skills/…` → `<data_root>/skills/…`（权威副本，零拷贝）。"""
        base = self._ensure_skills_mount()
        rest = normalized[len(SKILLS_PATH) :].lstrip("/")
        if not rest:
            return base
        target_normalized = posixpath.normpath(rest)
        if target_normalized == ".." or target_normalized.startswith("../"):
            raise SandboxPathError(f"路径不能上溯到 skills 之外: {virtual}")
        target = (base / target_normalized).resolve()
        if target != base and not target.is_relative_to(base):
            raise SandboxPathError(f"路径解析后越出 skills 根: {virtual} → {target}")
        return target

    def _ensure_skills_mount(self) -> Path:
        """让 `{root}/skills` 指向 `<data_root>/skills`。

        与 workspace 挂载同一套理由：文件原语可以自己翻译路径，但 `run_command`
        跑的是真实进程，只认文件系统。少了这个联接，`ls skills` 看到空目录而
        `sandbox_read_file` 能读到内容——两个视图分裂，模型会反复重试同一件事。

        挂在 `skills` 而不是逐个子目录：MSYS 把 junction 识别成符号链接后
        `ls -la` 只显示链接本身、`find` 默认不进去；挂在父层则 `skills/<name>`
        就是普通目录。
        """
        target = (data_root() / SKILLS_PATH).resolve()
        target.mkdir(parents=True, exist_ok=True)

        mount = self._root / SKILLS_PATH
        if _is_link(mount):
            return target
        if mount.exists():
            # 与 workspace 同样的保守处理：非空实体目录不删（可能已有真实文件）
            try:
                mount.rmdir()
            except OSError:
                logger.warning(
                    "skills mount skipped: %s 是非空实体目录，shell 看不到 Skill", mount
                )
                return target

        if not _make_link(mount, target):
            logger.warning(
                "skills mount failed: %s → %s（文件工具仍正常，shell 看不到 Skill）",
                mount,
                target,
            )
        return target

    def _ensure_workspace_mount(self, workspace_id: str) -> Path:
        """让 `{root}/workspace` 指向权威副本根 `workspace-volumes`。

        **为什么必须有这一步**：文件原语可以自己把 `workspace/{id}` 翻译到
        `workspace-volumes/{id}`，但 `run_command` 是真实进程，它只认文件系统。
        少了这个联接，`pwd`/`ls` 看到的是沙箱里那个空目录，而文件工具写的是
        权威副本——模型「刚写完就 ls 不到」，开始怀疑自己并疯狂重试
        （2026-11-12 实测 21 次调用里 15 次在找同一个文件）。

        **为什么挂 `workspace/` 而不是逐个 `workspace/{id}`**：接在 `{id}` 上时，
        MSYS 把联接识别成符号链接，于是 `ls -la` 只显示链接本身、
        `find` 默认不进去——这两条都是模型最常用的探查命令。挂在上一层，
        `workspace/{id}` 就是普通目录，`ls`/`find` 都能正常进入。
        代价是 shell 能看到同级工作区：命令本来就是 `unrestricted`（能读整台机器），
        这不是新增暴露面；文件工具的边界仍由 `resolve()` 与授权层守。

        用联接（junction）而不是复制：复制会重新引入"两份副本 + 同步"，
        正是 M2 要删掉的东西。junction 在 Windows 上不需要管理员权限。
        """
        volumes = (data_root() / "workspace-volumes").resolve()
        volume = volumes / _safe_segment(workspace_id)
        volume.mkdir(parents=True, exist_ok=True)

        mount = self._root / WORKSPACE.workspace
        if _is_link(mount):
            return volume
        if mount.exists():
            # 旧版本在这里建过实体目录（可能还带若干子联接）。只有空的才敢替换；
            # 非空说明里面有真实文件，宁可让 shell 少看见，也不能删用户数据。
            try:
                mount.rmdir()
            except OSError:
                logger.warning(
                    "workspace mount skipped: %s 是非空实体目录，shell 将看不到工作区文件", mount
                )
                return volume

        self._root.mkdir(parents=True, exist_ok=True)
        if not _make_link(mount, volumes):
            logger.warning(
                "workspace mount failed: %s → %s（文件工具仍正常，shell 看不到工作区）",
                mount,
                volumes,
            )
        return volume

    def virtual(self, real: Path) -> str:
        """真实路径 → 虚拟路径（回给模型的路径必须是 `/home/user/...`）。"""
        resolved = real.resolve()

        # 工作区权威副本要还原成 `workspace/{id}/…`，否则模型会看到
        # `D:\...\workspace-volumes\...` 这种它根本无法再次传入的路径。
        volumes = (data_root() / "workspace-volumes").resolve()
        if resolved != volumes and resolved.is_relative_to(volumes):
            rel = resolved.relative_to(volumes).as_posix()
            return posixpath.join(WORKSPACE.root, WORKSPACE.workspace, rel)

        # Skill 同理：真实路径在数据目录下，必须还原成 `skills/…`，
        # 否则模型拿到一个它无法再次传入的绝对路径（下一条命令必然失败）。
        skills = (data_root() / SKILLS_PATH).resolve()
        if resolved != skills and resolved.is_relative_to(skills):
            rel = resolved.relative_to(skills).as_posix()
            return posixpath.join(WORKSPACE.root, SKILLS_PATH, rel)

        base = self._root.resolve()
        try:
            rel = resolved.relative_to(base).as_posix()
        except ValueError:
            return str(real)
        return WORKSPACE.root if rel in ("", ".") else posixpath.join(WORKSPACE.root, rel)


class LocalFiles:
    """`sandbox.files` 的实现。"""

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
        # depth 语义：1 = 只列直接子项，2 = 再往下一层。
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

        `files.remove` 对目录是递归的，这里保持一致。
        调用方（`_prune_empty_parents`）已经先 list 确认空目录才删，
        所以递归删除不会误伤。
        """
        real = self._sb.resolve(path)
        if not real.exists():
            raise FileNotFoundError(f"{path} 不存在")
        # 挂载点绝不能删：`shutil.rmtree` 会顺着联接把权威副本整个删掉
        # （Python 的 is_symlink() 对 junction 返回 False，这道判断不能省）。
        if _is_link(real):
            raise SandboxPathError(f"不能删除工作区挂载点: {path}")
        if real.is_dir():
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
    """`sandbox.commands` 的实现。"""

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
        # PATH 必须确定：继承到的 PATH 依赖"应用从哪启动"，双击启动就没有 Git 工具。
        env["PATH"] = _shell_path()
        if envs:
            env.update({str(k): str(v) for k, v in envs.items()})
        # 子进程一律 UTF-8：Windows 默认用 GBK 输出中文，会让工具结果变乱码。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")

        command = self._translate(cmd)
        argv = [POSIX_SHELL, "-c", command] if POSIX_SHELL else command

        if background:
            proc = await self._spawn(argv, work_dir, env, devnull=True)
            assert proc.pid is not None
            self._sb._background[proc.pid] = proc
            # 退出后由 commands.list() 摘除。调用方不一定会 list，所以表可能
            # 残留已退出项；它们不占资源（Process 对象很小），且 kill 前会先判 returncode。
            return LocalCommandInfo(pid=proc.pid, cmd=cmd, cwd=self._sb.virtual(work_dir))

        proc = await self._spawn(argv, work_dir, env, devnull=False)
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
            stdout=self._normalize(_decode_output(out or b"")),
            stderr=self._normalize(_decode_output(err or b"")),
            exit_code=proc.returncode or 0,
        )

    def _normalize(self, text: str) -> str:
        """把输出里的宿主真实路径还原成虚拟路径。

        与 `_translate` 是同一件事的两侧：进去时 `/home/user` → 真实路径，
        出来时真实路径 → `/home/user`。少了这一半，`find`/`grep` 的输出里就全是
        `C:\\Users\\<用户名>\\AppData\\...`：模型会照着这个路径再调一次文件工具，
        而那个路径会被 `resolve()` 判为越界（它确实不在虚拟命名空间里）。
        顺带不让宿主机用户名出现在给模型的上下文里。
        """
        pairs = self._path_pairs()
        for real, virtual in pairs:
            if real and real in text:
                text = re.sub(
                    re.escape(real) + r"(?=[\\/\s'\"`),:;|&]|$)",
                    virtual,
                    text,
                )
        return text

    def _path_pairs(self) -> list[tuple[str, str]]:
        """(宿主路径写法, 虚拟路径) —— 覆盖 bash 的 `/c/...` 与 Windows 的两种写法。"""
        root = self._sb.root
        volumes = data_root() / "workspace-volumes"
        pairs: list[tuple[str, str]] = []
        for real_path, virtual in ((volumes, f"{WORKSPACE.root}/{WORKSPACE.workspace}"), (root, WORKSPACE.root)):
            for form in (self._shell_path_of(real_path), real_path.as_posix(), str(real_path)):
                pairs.append((form, virtual))
        # 长的先换：workspace-volumes 与 sandbox 根互不为前缀，但保险起见按长度倒序。
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def _translate(self, cmd: str) -> str:
        """把命令里的 `/home/user` 换成真实的沙箱根。

        这是**唯一**一处替模型做路径翻译的地方，和文件原语里的 `resolve()` 是同一件事：
        模型永远只需要知道 `/home/user`（prompt、工具描述、历史记录都这么写），
        真实路径只在这里出现一次。

        为什么不能靠"让模型用 Windows 路径"绕过：`/home/user` 写在 prompt 里，
        改成 `D:\\...` 会让同一条规则在不同执行环境里都不一样，
        而且模型对 `/home/user` 的先验远比对一个 UUID 目录强。
        """
        root = WORKSPACE.root
        if root not in cmd:
            return cmd
        return _VIRTUAL_ROOT_RE.sub(lambda _m: self._shell_path_of(self._sb.root), cmd)

    @staticmethod
    def _shell_path_of(real: Path) -> str:
        """真实路径 → shell 能识别、且与 POSIX 语义一致的写法。

        Git Bash 接受 `C:/Users/...`，但 `/c/Users/...` 才是它的原生形式
        （`cd /c/...`、`cd C:/...` 都能工作，后者在某些内建命令里会被当成相对路径）。
        """
        text = real.resolve().as_posix()
        if POSIX_SHELL and sys.platform == "win32" and len(text) > 1 and text[1] == ":":
            return "/" + text[0].lower() + text[2:]
        return text

    async def _spawn(
        self,
        argv: list[str] | str,
        work_dir: Path,
        env: dict[str, str],
        *,
        devnull: bool,
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "cwd": str(work_dir),
            "env": env,
            "stdout": asyncio.subprocess.DEVNULL if devnull else asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.DEVNULL if devnull else asyncio.subprocess.PIPE,
            **self._spawn_kwargs(),
        }
        if isinstance(argv, list):
            return await asyncio.create_subprocess_exec(*argv, **kwargs)
        return await asyncio.create_subprocess_shell(argv, **kwargs)

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
