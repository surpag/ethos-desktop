"""本机沙箱：用本地目录实现工具层需要的文件 / 命令原语。

**契约位置**
`tools.py` 有一千行，全部通过 `sandbox.files.*` / `sandbox.commands.*` 与沙箱交互。
本类就是这套契约的**唯一**实现：`.files` / `.commands` / `.sandbox_id` 三个属性，
文件项形状见 `types.FileType`。工具层不认识"后端"这个概念，也就不存在
「换一条路走」的分支要维护。

**安全边界（诚实说明）**

文件原语做了路径约束：只允许 `WORKSPACE.root`（`/home/user`）之下，`..` 与
符号链接逃逸都会被拒。这是**应用层**约束，不是内核约束。

`commands.run` 不再自己决定怎么起进程：argv 与环境变量由 `internal/isolation`
生成（ASRT / AppContainer 后端 + 按工作区的策略）。**沙箱不可用时它抛
`SandboxUnavailable`，进程根本不会启动**——没有"先跑着看"的降级路径。
唯一例外是显式打开 `ETHOS_SANDBOX_ALLOW_UNSAFE_HOST=1`（仅 pytest 使用，
见 `tests/conftest.py`），那条路径会在审计里以 `backend=unsafe-host` 落一条
warning。

所以本文件现在的职责只剩两件：把虚拟路径翻译成真实路径（`resolve`），
以及把隔离层给的 argv 落到 `asyncio` 子进程上。**边界不在这个文件里**。
"""

from __future__ import annotations

import asyncio
import errno
import locale
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import data_root
from .. import isolation
from ..skills.types import SKILLS_PATH
from ..workspace.context import BindingState, WorkspaceContext, WorkspaceUnavailableError
from ..workspace.layout import WORKSPACE
from . import processes as process_registry
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


# 挂载失败的六个原因码。**只用于诊断**（日志、HTTP 细节、测试断言）。
# 恢复动作与原因无关：抛出来就表示「挂载不可用」，调用方一律按失败处理。
# 这条边界是有意画的——一旦按原因分支出「这种可以降级、那种不可以」，
# 就等于允许某些失败带着两套文件视图继续跑，那正是本轮要消灭的东西。
MOUNT_CREATE_FAILED = "create_failed"  # 首次创建联接失败
MOUNT_RELINK_FAILED = "relink_failed"  # 摘掉旧链接后重建失败
MOUNT_UNLINK_FAILED = "unlink_failed"  # 旧链接摘不掉（被占用 / 权限不足）
MOUNT_OCCUPIED = "occupied"  # 挂载点被非空目录 / 同名文件占着，不能在不碰用户数据的前提替换
MOUNT_PREPARE_FAILED = "prepare_failed"  # 目标建不出、父目录建不出、占位目录因权限等原因删不掉
MOUNT_VERIFY_FAILED = "verify_failed"  # 建完回读，目标与预期不符（返回契约不可兑现）


class SandboxMountError(RuntimeError):
    """挂载点不可用：坏了，或者根本建不出来。

    与 `SandboxPathError` 的区别是"谁的错"：路径越界是**调用方**给错了路径，
    重试也没用但换个路径就行；挂载不可用是**本机状态**坏了，同一个沙箱里
    所有工作区 / Skill 访问都会失败。

    刻意不继承 PermissionError：把它混成 403 会让用户以为是权限问题去翻档位，
    而实际要做的是修目录。

    刻意**没有**「降级」档位（2026-11-18 修正）：原先区分「首次创建失败只告警」
    与「重建失败抛错」，判据是"有没有弄坏既有状态"。这个判据是错的——
    真正的判据是**两个视图是否一致**：联接没建成时，文件工具走
    `workspace-volumes`、shell 走 `workspace`，它们是两个不同的目录；
    对 Agent 来说这比直接报错危险得多（模型会一直以为文件丢了）。
    所以只要不能保证一致就抛错，`reason` 只用来区分原因。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        # 诊断标签，不是控制流开关：调用方不得据此决定"要不要继续"。
        self.reason = reason


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

# 虚拟路径前缀后面必须跟分隔符或行尾，否则 `workspace/ws-1` 会匹配到 `workspace/ws-10`。
_VIRTUAL_BOUNDARY = r"(?=[/\s'\"`;|&)]|$)"

# 输出归一化：从 `/home/user` 起、到命令分隔符为止的一段（内含反斜杠混排要拉平）。
_VIRTUAL_SEGMENT_RE = re.compile(r"/home/user[^\s'\"`;|&)]*")


def _replace_virtual_prefix(cmd: str, virtual: str, real: str) -> str:
    """把命令里出现的 `virtual` 前缀替换成 `real`（带边界，不误伤同前缀的兄弟路径）。"""
    if virtual not in cmd or not real:
        return cmd
    return re.sub(re.escape(virtual) + _VIRTUAL_BOUNDARY, lambda _m: real, cmd)


def _relative_or_none(path: Path, root: Path) -> Path | None:
    """`path` 在 `root` 之下就返回相对路径，否则 None。

    用 `is_relative_to` 而不是字符串前缀：`/a/bc` 不算在 `/a/b` 之内。
    """
    try:
        return path.relative_to(root)
    except ValueError:
        return None


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


def _link_target_text(raw: str) -> str:
    """把 `os.readlink` 的原始字符串变成可直接使用的**绝对路径写法**。

    `os.readlink` 在 Windows 上返回的是 Win32 扩展路径，三种形态各有正确解法：

    | 读回形态 | 目标 |
    | --- | --- |
    | `\\\\?\\C:\\dir`（单盘符） | 剥前缀 → `C:\\dir` |
    | `\\\\?\\UNC\\server\\share\\dir` | 还原标准 UNC → `\\\\server\\share\\dir` |
    | `\\\\?\\Volume{…}\\dir`、`\\\\?\\GLOBALROOT\\…` | **原样保留** |

    前两种剥前缀是"还原常规写法"，第三种不同：`\\\\?\\Volume{...}` 里的
    `Volume{...}` 是设备名而不是相对路径片段，剥掉 `\\\\?\\` 会把整条路径变成
    相对路径（实测 `Path("Volume{...}\\dir").is_absolute()` 为 False），
    随后被拼到挂载点父目录下，变成一条凭空编造的路径——于是这种挂载每次检查
    都被判成"指向错误"并重建一次。保留前缀时三种形态都是绝对路径
    （实测 `is_absolute()` 均为 True），交给 `_same_dir` 用 `resolve()` 比较即可。
    """
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[len("\\\\?\\UNC\\") :]
    # 只剥「盘符 + 冒号」这一种：`\\?\C:\…`。字母表以外的一律不动。
    if len(raw) > 6 and raw.startswith("\\\\?\\") and raw[4].isalpha() and raw[5] == ":":
        return raw[4:]
    return raw


def _link_target(link: Path) -> Path | None:
    """读取目录联接 / 符号链接指向的目标；读不到返回 None。

    用 `os.readlink` 而不是 `Path.resolve()`：resolve 会**跟随**链接，健康时
    直接把目标内容当成链接自身的位置，判断"链接指哪"就绕了远路；而目标不存在时
    它给回的是一个无法展开的短名路径，跟规范路径比必然不等——正好把"健康"
    和"损坏"糊成同一种结果。

    Windows 上 readlink 返回带 `\\\\?\\` 扩展前缀的形式，而且沿用创建时写的路径
    形状（可能是 8.3 短名），所以调用方拿到后还要按 `_same_dir` 归一比较。
    POSIX 的相对符号链接按规范相对于**链接所在目录**解析，不是进程 cwd。
    """
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    text = _link_target_text(raw)
    candidate = Path(text)
    # 只有真正的相对目标才拼链接所在目录。`\\?\…` 形态已实测均为绝对路径，
    # 这里再挡一次前缀，避免某天 Path 解析规则变动把设备路径拼坏。
    if not candidate.is_absolute() and not text.startswith("\\\\?\\"):
        candidate = link.parent / candidate
    return candidate



def _same_dir(left: Path, right: Path) -> bool:
    """两个路径是否指向同一个目录（跟随联接，忽略大小写与 8.3 别名）。

    只用 `Path.resolve()` 不够：目标不存在时它展开不了短名，两次同一目录的
    写法会判不等。所以再退一步按 normcase + abspath 比一次——两种都失配，
    才认定不是同一个目录。

    **这个降级分支是字符串等价，不是文件系统对象身份验证**（没有 file id /
    volume serial 参与），所以它只能用来回答"要不要保留一个已存在的挂载"，
    绝不能拿它当删除的依据。真正的安全依据是 `_unlink_mount` 里的
    「只摘链接项、不递归」——即使这里判错了，代价也只是多重挂一次、
    或重建一个本来健康的联接，不会动到任何用户数据。
    """
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _unlink_mount(link: Path) -> bool:
    """摘掉挂载联接**本身**，绝不触碰它指向的目录。

    顺序是 rmdir → unlink：junction 与目录符号链接在 Windows 上都能用 rmdir
    摘掉（只删链接项，不递归目标），POSIX 上的符号链接只认 unlink。
    **绝不能换成 `shutil.rmtree`**：那会顺着链接把用户目录整个删掉——
    这正是 `remove()` 要递归时先断链接的原因。

    非链接一律拒绝：空实体目录也能被 rmdir 删掉，但那属于用户数据。
    """
    if not _is_link(link):
        logger.warning("refuse to unlink non-link mount: %s", link)
        return False
    try:
        os.rmdir(link)
        return True
    except OSError:
        pass
    try:
        link.unlink()
        return True
    except OSError as exc:
        logger.warning("failed to unlink mount %s: %s", link, exc)
        return False


def _verify_mount(mount: Path, target: Path) -> bool:
    """回读挂载点：它此刻真的指向 `target` 吗。

    这是"返回即承诺"的**唯一**依据——不靠自己刚做过什么，靠事后读回的事实。
    """
    current = _link_target(mount)
    return current is not None and _same_dir(current, target)


def _is_not_empty_error(exc: OSError) -> bool:
    """`rmdir` 失败是不是因为目录非空。

    只认这一种：Windows `ERROR_DIR_NOT_EMPTY`(145) 与 POSIX `ENOTEMPTY`。
    `EEXIST` 一并收进来是因为部分平台/文件系统会这么报，代价是错判概率极低。
    其他 `OSError`（权限、文件系统不可用、被占用）**不是** occupied——
    把它们混成一句"目录非空"会让排查方向彻底跑偏。
    """
    return getattr(exc, "winerror", None) == 145 or exc.errno in (
        errno.ENOTEMPTY,
        errno.EEXIST,
    )


def _ensure_mount(mount: Path, target: Path, *, label: str) -> None:
    """保证 `mount` 是**指向 `target` 的目录联接**；做不到就抛 `SandboxMountError`。

    两种挂载（`workspace` / `skills`）共用这一份实现：它们的分支完全同构，
    各写一份的必然结果是第二份慢慢落后（本轮修的就是这个——skills 是 workspace
    的孪生缺陷，缺陷一样、修法也得一样）。

    **返回即承诺**：正常返回 = 此刻 shell（走联接）与文件工具（走 `target`）
    看到同一片字节。承诺不靠"我刚调用过 `_make_link`"，而是**每次都回读**
    （`_verify_mount`）——建链接成功但落点不对（旧版本遗留的 mklink 行为差异、
    中间被别的进程改指）同样要报错，否则函数就在骗调用方。

    | 现状 | 行为 |
    | --- | --- |
    | 联接指向 `target`（回读确认） | 复用，不做任何写入 |
    | 联接损坏 / 指向别处 | 摘链接（不碰目标）后重建，再回读确认 |
    | 摘链接失败 | 抛 `unlink_failed` |
    | 重建失败 | 抛 `relink_failed` |
    | 建完回读仍不符 | 抛 `verify_failed` |
    | 空实体目录占位 | 删掉（无数据）后建链接 |
    | 非空目录 / 同名文件占位 | 抛 `occupied`——不能为了挂载删用户数据 |
    | 从未建过 / 创建失败 | 抛 `create_failed` |
    | 目标/父目录建不出、占位因权限删不掉 | 抛 `prepare_failed` |

    非空实体目录刻意**不**自动迁移：里面可能是用户真实文件，也可能是旧版本
    留下的按工作区联接布局，两种情况都只能由人来判断。错误消息里给出可执行动作。
    """
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxMountError(
            f"{label} 挂载目标无法创建: {target}（{exc}）。"
            "没有目标目录就没有权威副本，已停止继续。",
            reason=MOUNT_PREPARE_FAILED,
        ) from exc

    if _is_link(mount):
        if _verify_mount(mount, target):
            return
        current = _link_target(mount)
        logger.warning(
            "%s mount 指向异常（%s → %s，期望 %s），重建联接", label, mount, current, target
        )
        if not _unlink_mount(mount):
            raise SandboxMountError(
                f"{label} 挂载点损坏且无法摘除: {mount}（指向 {current}）。"
                f"请手动删除该链接后重试；不要删除 {target}。",
                reason=MOUNT_UNLINK_FAILED,
            )
        if not _make_link(mount, target):
            raise SandboxMountError(
                f"{label} 挂载点重建失败: {mount} → {target}。"
                "文件工具与 shell 将看到不同目录，已停止继续（拒绝静默降级）。",
                reason=MOUNT_RELINK_FAILED,
            )
        if not _verify_mount(mount, target):
            raise SandboxMountError(
                f"{label} 挂载点重建后回读不符: {mount} → {_link_target(mount)}，"
                f"期望 {target}。已停止继续。",
                reason=MOUNT_VERIFY_FAILED,
            )
        return

    if mount.exists():
        # 旧版本在这里建过实体目录。只有空的才敢替换；非空说明里面有东西，
        # 宁可报错让人来看，也不能删——那是用户数据。
        try:
            mount.rmdir()
        except FileNotFoundError:
            # 检查与删除之间被别的进程删掉了：目标状态已达成，直接往下建链接。
            pass
        except OSError as exc:
            if isinstance(exc, NotADirectoryError):
                kind = "同名文件"
            elif _is_not_empty_error(exc):
                kind = "非空实体目录"
            else:
                raise SandboxMountError(
                    f"{label} 挂载点无法清空: {mount}（{exc}）。"
                    "这不是「目录非空」——更可能是权限、文件系统或被其他进程占用，"
                    "无法在不确认的前提下继续。",
                    reason=MOUNT_PREPARE_FAILED,
                ) from exc
            raise SandboxMountError(
                f"{label} 挂载点被{kind}占用: {mount}（{exc}）。"
                f"无法在不碰其中内容的前提下替换它，继续跑会让 shell 与文件工具"
                f"看到不同目录。请把该目录移走或改名后重试；不要动 {target}。",
                reason=MOUNT_OCCUPIED,
            ) from exc

    try:
        mount.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxMountError(
            f"{label} 挂载点的父目录无法创建: {mount.parent}（{exc}）。",
            reason=MOUNT_PREPARE_FAILED,
        ) from exc

    if not _make_link(mount, target):
        raise SandboxMountError(
            f"{label} 挂载点创建失败: {mount} → {target}。"
            "文件工具与 shell 将看到不同目录，已停止继续（拒绝静默降级）。",
            reason=MOUNT_CREATE_FAILED,
        )
    if not _verify_mount(mount, target):
        raise SandboxMountError(
            f"{label} 挂载点创建后回读不符: {mount} → {_link_target(mount)}，"
            f"期望 {target}。已停止继续。",
            reason=MOUNT_VERIFY_FAILED,
        )


class LocalSandbox:
    """一个会话一个实例；目录在磁盘上持久，重启后可重新打开。"""

    # 能力声明：读取没有网络成本，不需要"大文件改用 sed 按行读"的优化。
    # tools.py 依据这个标记跳过 sed 分支——那个分支在 Windows 上没有 sed 可执行。
    supports_large_read = True

    def __init__(
        self,
        root: Path,
        sandbox_id: str,
        context: WorkspaceContext | None = None,
    ) -> None:
        self._root = root
        self.sandbox_id = sandbox_id
        # 审计用标识：由 `open()` / `attach()` 填。`__init__` 收不到它们，
        # 而改构造函数签名会波及所有调用点，所以放在这里按需补。
        self._user_id = ""
        self._session_id = ""
        #: 本次执行的工作区上下文快照。`None` = 调用方没有提供（面板 / 旧调用路径），
        #: 此时完全走改造前的路径（`workspace-volumes/{id}` + 按需创建）。
        #: **它是一次执行认定的根，不是"当前绑定"**：run 内不重新读取，换绑不影响。
        self._context = context
        self.files = LocalFiles(self)
        self.commands = LocalCommands(self)

    @property
    def workspace_context(self) -> WorkspaceContext | None:
        """本次执行认定的工作区上下文（可能为 `None`，见 `__init__`）。"""
        return self._context

    @property
    def _process_owner(self) -> process_registry.Owner:
        """后台进程的归属键（跨实例稳定）。

        **台账不能挂在实例上**：`Service.workspace()` 每次工具调用都新建一个
        `LocalSandbox`，挂在实例上等于"启动完就丢"——下一次 list 看到空表，
        kill 只能退回按 pid 盲杀（无归属校验）。见 `sandbox/processes.py`。
        """
        return process_registry.owner_of(
            self._user_id, self._session_id, self.sandbox_id
        )

    # ---- 生命周期 ----

    @classmethod
    def open(
        cls,
        user_id: str,
        session_id: str,
        context: WorkspaceContext | None = None,
    ) -> LocalSandbox:
        """打开（或重建）本地沙箱。

        与远端沙箱的关键差别：目录被外部删掉时这里直接重建，不会像失效的
        `sandbox_id` 那样让 `connect` 永远失败。本地模式下「沙箱」就是一个目录，
        重建的代价是零。

        `context` 只影响**工作区路径的落点**（见 `_workspace_volume_dir`），
        不影响沙箱自身的目录结构：沙箱根永远按会话建。
        """
        root = sandbox_root_for(user_id, session_id)
        root.mkdir(parents=True, exist_ok=True)
        # 平铺目录先建好：模型第一次 `ls` 时不该看到"目录不存在"。
        # `workspace/` 刻意不在这里建——它是挂载点（联接），由
        # `_ensure_workspace_mount` 在首次解析工作区路径时创建。
        # `skills/` 是固定挂载点，不依赖任何参数，所以这里就能建。
        for name in (WORKSPACE.uploads, WORKSPACE.work, WORKSPACE.artifacts):
            (root / name).mkdir(parents=True, exist_ok=True)
        sandbox = cls(
            root,
            f"local-{_safe_segment(user_id)}-{_safe_segment(session_id)}",
            context=context,
        )
        sandbox._user_id = user_id
        sandbox._session_id = session_id
        sandbox._ensure_skills_mount()
        return sandbox

    @classmethod
    def attach(
        cls,
        user_id: str,
        session_id: str,
        context: WorkspaceContext | None = None,
    ) -> LocalSandbox:
        """只打开已存在的沙箱，不创建。不存在时抛 FileNotFoundError。"""
        root = sandbox_root_for(user_id, session_id)
        if not root.is_dir():
            raise FileNotFoundError(f"本地沙箱目录不存在: {root}")
        sandbox = cls(
            root,
            f"local-{_safe_segment(user_id)}-{_safe_segment(session_id)}",
            context=context,
        )
        sandbox._user_id = user_id
        sandbox._session_id = session_id
        return sandbox

    async def kill(self) -> None:
        """结束本会话所有后台进程。**不删目录**：工作区文件是用户的，不能随沙箱一起没。

        走台账而不是本实例的字典：同一个会话的命令可能由多个实例启动，
        "结束这个沙箱"必须指向会话，而不是某一次工具调用拿到的那个对象。
        """
        process_registry.release(self._process_owner)

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
        #
        # `declared` 必须按**与归一化相同的规则**取片段（丢掉空段与 `.`）：
        # 用原始片段比对会把合法的 `workspace/./ws/a.txt` 判成跳界——那里的
        # `declared[1]` 是 `.`，归一化后是 `ws`，两者当然不等。
        declared = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if declared and declared[0] == WORKSPACE.workspace:
            normalized_parts = normalized.split("/")
            if len(declared) >= 2 and (
                len(normalized_parts) < 2
                or normalized_parts[0] != WORKSPACE.workspace
                or normalized_parts[1] != declared[1]
            ):
                raise SandboxPathError(f"路径不能跳出工作区边界: {virtual}")

        parts = normalized.split("/")
        if parts and parts[0] == WORKSPACE.workspace:
            # `workspace` 本身（不带 {id}）是挂载根，与 `skills` 一样直接返回。
            # 落到下面的沙箱根分支会走不通：`{root}/workspace` 是联接，`resolve()`
            # 之后是 `workspace-volumes`，不在沙箱根下 → 误判越界 403。
            if len(parts) == 1:
                return self._ensure_workspace_mount()
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
        """`workspace/{id}/…` → `workspace-volumes/{id}/…`（权威副本，零拷贝）。

        `base` 与 `target` 都在同一规范化平面上（都由 `_workspace_volume_dir`
        归一过），所以 `is_relative_to` 的判断是可信的——根和子路径返回同一语义。
        """
        base = self._workspace_volume_dir(workspace_id)
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
        """让 `{root}/skills` 指向 `<data_root>/skills`，返回该根的真实路径。

        **返回值契约**（与 `_ensure_workspace_mount` 一致）：绝对 + 已归一 + 存在的
        目录，且是"联接指向的目标"而不是联接本身。两个调用方都用同一句
        `is_relative_to(base)` 判断越界，契约不一致会让结果取决于目录碰巧是不是联接。

        与 workspace 挂载同一套理由：文件原语可以自己翻译路径，但 `run_command`
        跑的是真实进程，只认文件系统。少了这个联接，`ls skills` 看到空目录而
        `sandbox_read_file` 能读到内容——两个视图分裂，模型会反复重试同一件事。

        挂在 `skills` 而不是逐个子目录：MSYS 把 junction 识别成符号链接后
        `ls -la` 只显示链接本身、`find` 默认不进去；挂在父层则 `skills/<name>`
        就是普通目录。

        自检 / 重建 / 报错全部复用 `_ensure_mount`：这里是 workspace 缺陷的孪生位置，
        两边的失败语义必须逐条相同，否则"改了一处、另一处还静默分裂"。
        """
        target = (data_root() / SKILLS_PATH).resolve()
        _ensure_mount(self._root / SKILLS_PATH, target, label="skills")
        return target

    def _ensure_workspace_mount(self) -> Path:
        """让 `{root}/workspace` 指向权威副本根 `workspace-volumes`，返回该根的真实路径。

        **返回值契约**（与 `_ensure_skills_mount` 一致）：绝对 + 已归一 + 存在的目录，
        且是"联接指向的目标"而不是联接本身。修这条契约不是洁癖：两个调用方都用
        同一句 `is_relative_to(base)` 判断越界，只要一方返回未归一路径，判定就会
        取决于目录**碰巧**是不是联接——绑定了本地目录之后它一定是联接，那时报出来的
        是 403，看上去像路径越界，而不是契约不一致。

        **挂载点必须自检，失败必须喊**：联接在、但指向的不是 `workspace-volumes`
        （目标被删、被改指、旧版本遗留），就摘链接重建；建不出来（包括首次创建
        就失败）一律抛 `SandboxMountError`。返回的路径会被文件工具拿去读写，而
        shell 走的是这个联接本身——两者一旦不同源，就退回到「刚写完就 ls 不到」
        那个原始故障，而且这次没有任何信号。所以这个函数**只有两种结局**：
        返回一个已验证一致的根，或抛错；没有"先跑着看"。

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
        _ensure_mount(self._root / WORKSPACE.workspace, volumes, label="workspace")
        return volumes

    def _workspace_volume_dir(self, workspace_id: str) -> Path:
        """`workspace/{id}` 的权威根目录（真实路径）。

        两种情况泾渭分明：

        - **调用方没给工作区上下文**（面板 / 旧调用路径）→ 走改造前的唯一行为：
          `workspace-volumes/{id}`，按需创建。这条分支逐字节不变。
        - **给了上下文**（Agent 执行链）→ 按 `_contextual_workspace_dir` 的状态表
          分流：未绑定与改造前一致，绑定态落在用户目录，异常态 fail-closed。

        为什么不在这里统一读一次注册表：注册表是**运行期缓存**，而上下文是
        **一次执行的认定**。读缓存会让"执行中被换绑"改变当前 run 的落点，
        而读上下文不会——这也是 S1 把快照做成不可变对象的原因。
        """
        if self._context is not None:
            return self._contextual_workspace_dir(workspace_id)
        return self._volume_copy_dir(workspace_id)

    def _volume_copy_dir(self, workspace_id: str) -> Path:
        """未绑定态的权威副本目录：`workspace-volumes/{id}`，**按需创建**。

        这是 S1 之前唯一的行为，必须原样保留：未绑定工作区的目录由执行链按需
        初始化，去掉这一步新工作区就没有目录可用（"禁止 mkdir"只对绑定态成立）。
        """
        volumes = self._ensure_workspace_mount()
        volume = volumes / _safe_segment(workspace_id)
        volume.mkdir(parents=True, exist_ok=True)
        return volume.resolve()

    def _contextual_workspace_dir(self, workspace_id: str) -> Path:
        """按本次执行的工作区上下文决定权威根。

        | 状态 | 落点 | 是否创建 |
        | --- | --- | --- |
        | `UNBOUND` | `workspace-volumes/{id}` | 按需创建（同改造前） |
        | `BOUND_READY` | 绑定目录 `local_root` | **不创建** |
        | missing / denied / offline | 抛 `WorkspaceUnavailableError` | 不创建 |

        **异常态绝不回退到 Volume**：一旦回退，用户读到的是旧副本，而且没有任何
        信号说明"这不是你绑定的目录"——那正是 P0-2 要消灭的静默切换。

        **绑定态不创建**：权威根由用户提供，执行链只是使用者。目录被改名后
        如果在这里 mkdir，就会在原路径造出一个空目录（`LocalFiles.write` 与
        `LocalCommands.run` 都会对"解析出来的父目录"mkdir），用户把原目录
        恢复回来时会看到两份内容不同。
        """
        context = self._context
        if context is None:  # pragma: no cover - 由 _workspace_volume_dir 保证
            return self._volume_copy_dir(workspace_id)

        # 一次执行固定在一个工作区上。路径指向别的工作区时必须拒绝：
        # 授权层（`_scope_violation`）正常情况下已经拦住了，这里是执行层的
        # 第二道——两层用的是同一个 id，不该出现"授权通过、执行落到别处"。
        if context.workspace_id and context.workspace_id != workspace_id:
            raise SandboxPathError(
                f"路径指向工作区 {workspace_id}，本次执行固定为 {context.workspace_id}"
            )

        if not context.executable:
            raise WorkspaceUnavailableError(context)

        if context.state is BindingState.UNBOUND:
            return self._volume_copy_dir(workspace_id)

        root = context.root
        if not root:  # pragma: no cover - BOUND_READY 必有 root
            raise WorkspaceUnavailableError(context)
        if context.prefix:
            # 带前缀的绑定：虚拟视图里多一层目录（`workspace/{id}/Data/…` ↔
            # `local_root/…`），而 Agent 的相对路径补全不带这一层。半映射的结果是
            # "读得到一部分、写错另一部分"，所以宁可停在这里。
            raise SandboxPathError(
                f"该工作区绑定目录带前缀层 {context.prefix!r}，虚拟路径与本地路径"
                "不是一一对应，当前版本未接入这种绑定。请在工作区设置里重新绑定"
                "到实际的工作目录。"
            )

        # 仍然维护 `{root}/workspace` 联接。S4 之后 shell 不再依赖它（cwd 是工作区根、
        # 命令里的虚拟路径由 `_translate` 分流），但**未绑定态**与面板仍在用它，
        # 撤掉会让 `ls workspace` 从"看到旧副本"退化成"目录不存在"，后者更难解释。
        # 它只创建联接与 `workspace-volumes` 根本身，不会创建 `{id}` 子目录。
        self._ensure_workspace_mount()

        root_path = Path(root)
        # 快照说 ready，不代表现在还 ready：目录可能在 run 期间被移动、改名或
        # 所在磁盘掉线。这里必须**事后再确认一次**，因为下游有多个"看到路径就
        # mkdir 父目录"的调用点（`LocalFiles.write` 的 `parent.mkdir`、
        # `LocalCommands.run` 对 cwd 的 mkdir）——不在这里拦住，它们会在原路径
        # 重建一条空目录树，用户把原目录恢复回来就看到两份内容不同。
        if not root_path.is_dir():
            raise WorkspaceUnavailableError(
                context,
                "绑定目录在本次执行期间已不可访问（可能被移动、改名，或所在磁盘掉线）",
            )
        return root_path.resolve()

    def virtual(self, real: Path) -> str:
        """真实路径 → 虚拟路径（回给模型的路径必须是 `/home/user/...`）。

        输入分为两类，两类都必须接住：

        1. **沙箱树里的路径**（`files.list` / `get_info` / `rename` 交回来的）。
           这一类要用**未解析**的路径去比沙箱根：`skills` 与 `workspace` 是联接，
           `resolve()` 会跳出沙箱。之前的实现只走解析后的路径，于是**联接点本身**
           落到了死局——`<root>/skills` 解析后正好等于 `data_root/skills`，被
           `resolved != skills` 排除；`<root>/workspace` 解析后正好等于
           `workspace-volumes`，同样被排除；最后兜底 `str(real)` 返回一个
           `C:\\Users\\...` 绝对路径。文件面板把它当节点 path 回传，下一次请求
           就被顶级目录白名单判成非法（线上表现：`workspace` 节点报
           「top-level directory must be one of ...」，`skills` 节点同样打不开）。

        2. **权威副本的真实路径**（workspace 存储 / agent_input 直接给的）。
           要还原成 `workspace/{id}/…` 与 `skills/…`，否则模型拿到一个它无法
           再次传入的路径，下一条命令必然失败。
        """
        raw = Path(real)
        root = Path(self._root)
        rel = _relative_or_none(raw, root) or _relative_or_none(raw.resolve(), root.resolve())
        if rel is not None:
            posix = rel.as_posix()
            return WORKSPACE.root if posix in ("", ".") else posixpath.join(WORKSPACE.root, posix)

        # 绑定工作区：权威根既不在沙箱树里，也不是 `workspace-volumes`，
        # 必须在这里还原。否则文件工具返回的是宿主绝对路径（`C:\Users\…`），
        # 模型把它原样传回来会被 `resolve()` 判成越界——表现为"刚写完就读不到"，
        # 而且模型会把同一个路径重试到熔断。
        context = self._context
        if (
            context is not None
            and context.state is BindingState.BOUND_READY
            and context.root
        ):
            bound = Path(context.root)
            for candidate in (raw, raw.resolve()):
                bound_rel = _relative_or_none(candidate, bound)
                if bound_rel is None:
                    continue
                base = posixpath.join(
                    WORKSPACE.root, WORKSPACE.workspace, context.workspace_id
                )
                posix = bound_rel.as_posix()
                return base if posix in ("", ".") else posixpath.join(base, posix)

        resolved = raw.resolve()

        # 工作区权威副本（含 `workspace-volumes` 根本身）。
        volumes = (data_root() / "workspace-volumes").resolve()
        if resolved == volumes:
            return posixpath.join(WORKSPACE.root, WORKSPACE.workspace)
        if resolved.is_relative_to(volumes):
            rel = resolved.relative_to(volumes).as_posix()
            return posixpath.join(WORKSPACE.root, WORKSPACE.workspace, rel)

        # Skill 同理（含 `skills` 根本身）。
        skills = (data_root() / SKILLS_PATH).resolve()
        if resolved == skills:
            return posixpath.join(WORKSPACE.root, SKILLS_PATH)
        if resolved.is_relative_to(skills):
            rel = resolved.relative_to(skills).as_posix()
            return posixpath.join(WORKSPACE.root, SKILLS_PATH, rel)

        return str(raw)


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

        # **唯一执行入口**：argv 与 env 由隔离层生成（见 internal/isolation）。
        # 后端不可用时这里抛 `SandboxUnavailable`——**不捕获、不回落**，进程根本
        # 不会被启动。这是 spec 里「沙箱初始化失败禁止无保护执行」的落点。
        try:
            exec_plan = isolation.plan(
                sandbox_root=self._sb.root,
                cwd=work_dir,
                command=command,
                env=env,
                shell_exe=POSIX_SHELL,
                bound_root=self._bound_root(),
                background=background,
                session_id=self._sb._session_id,
                workspace_id=(self._sb.workspace_context.workspace_id if self._sb.workspace_context else ""),
            )
        except isolation.SandboxUnavailable as exc:
            # 拒绝也要留痕：安全事件的价值在于"事后能查到何时被拒、为什么"。
            await isolation.record_refusal(
                exc,
                command=command,
                session_id=self._sb._session_id,
                workspace_id=(self._sb.workspace_context.workspace_id if self._sb.workspace_context else ""),
            )
            raise
        argv, env = exec_plan.argv, exec_plan.env
        started = time.monotonic()

        async def _finish(exit_code: int, outcome: str, pid: int | None = None) -> None:
            duration_ms = (time.monotonic() - started) * 1000
            isolation.finish(
                exec_plan, exit_code=exit_code, duration_ms=duration_ms, outcome=outcome
            )
            # 审计落库：await 而不是 fire-and-forget，避免 run 结束/取消时把任务
            # 一起取消掉（那会留下"执行过但没有记录"的空洞）。
            await isolation.record(
                exec_plan,
                exit_code=exit_code,
                duration_ms=duration_ms,
                outcome=outcome,
                pid=pid,
            )

        if background:
            proc = await self._spawn(argv, work_dir, env, devnull=True)
            assert proc.pid is not None
            # 登记到**模块级台账**：下一次工具调用是新实例，只有台账能看见它。
            # `cmd` 存原始命令（翻译前的写法），`cwd` 存虚拟路径——工具结果会
            # 原样回给模型，宿主路径会让下一轮调用被判越界。
            # 不把 process 对象交给台账：它绑在这个事件循环上，而台账比循环活得久。
            try:
                process_registry.register(
                    self._sb._process_owner,
                    pid=proc.pid,
                    cmd=cmd,
                    cwd=self._sb.virtual(work_dir),
                )
            except process_registry.PersistenceFailed as exc:
                # 登记失败**不能**当作启动成功返回：模型会以为这个进程受管理，
                # 重启后却没有任何人知道它存在。立即回滚，再把失败如实抛上去。
                await self._rollback_background(proc, exc, command=cmd)
                await _finish(1, "failed", proc.pid)
                raise
            # 必须有人 wait：否则 POSIX 上每次启动都留一个 zombie，且 transport
            # 会活过事件循环、GC 时对已失效的句柄报警。台账不持有 Process，
            # 回收责任交给这个后台 task。
            process_registry.reap(proc)
            await _finish(0, "started", proc.pid)
            return LocalCommandInfo(pid=proc.pid, cmd=cmd, cwd=self._sb.virtual(work_dir))

        proc = await self._spawn(argv, work_dir, env, devnull=False)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await self._kill_tree(proc)
            await _finish(124, "timeout", proc.pid)
            return LocalCommandResult(
                stdout="",
                stderr=f"命令超时（{timeout} 秒）已被终止: {cmd}",
                exit_code=124,
            )
        except asyncio.CancelledError:
            # 用户点了停止 / run 被取消：不能把子进程留成孤儿继续占着文件。
            await self._kill_tree(proc)
            await _finish(130, "canceled", proc.pid)
            raise

        await _finish(proc.returncode or 0, "exited", proc.pid)
        return LocalCommandResult(
            stdout=self._normalize(_decode_output(out or b"")),
            stderr=self._normalize(_decode_output(err or b"")),
            exit_code=proc.returncode or 0,
        )

    def _bound_root(self) -> str | None:
        """本次执行的绑定工作区根（未绑定 / 无上下文返回 None）。

        隔离策略据此决定"哪里可写"：绑定态必须只放行用户选定的那个目录，
        而不是整个 `workspace-volumes`。
        """
        context = self._sb.workspace_context
        if (
            context is not None
            and context.state is BindingState.BOUND_READY
            and context.root
        ):
            return str(context.root)
        return None

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
        # 宿主路径的 `str()` 形式用反斜杠，替换之后虚拟前缀后面会留下 `\`：
        # `C:\p\ws\app.py` → `/home/user/workspace/ws1\app.py`。`resolve()` 能靠
        # `replace("\\", "/")` 救回来，但两种分隔符混排会留在工具结果和历史里，
        # 徒增歧义；绑定态尤其明显，因为绑定根只有 `str()` 形式会命中。
        return _VIRTUAL_SEGMENT_RE.sub(lambda m: m.group(0).replace("\\", "/"), text)

    def _path_pairs(self) -> list[tuple[str, str]]:
        """(宿主路径写法, 虚拟路径) —— 覆盖 bash 的 `/c/...` 与 Windows 的两种写法。

        绑定态多一条：用户目录 ↔ `workspace/{id}`。少了它，`ls` 的输出里会是
        `C:\\Users\\...` 而不是模型刚写过的虚拟路径，模型会照着宿主路径再调一次
        文件工具，而那个路径会被 `resolve()` 判成越界（它确实不在虚拟命名空间里）。
        """
        root = self._sb.root
        volumes = data_root() / "workspace-volumes"
        targets: list[tuple[Path, str]] = []
        context = self._sb.workspace_context
        if context is not None and context.state is BindingState.BOUND_READY and context.root:
            targets.append(
                (
                    Path(context.root),
                    posixpath.join(WORKSPACE.root, WORKSPACE.workspace, context.workspace_id),
                )
            )
        targets.append((volumes, f"{WORKSPACE.root}/{WORKSPACE.workspace}"))
        targets.append((root, WORKSPACE.root))
        pairs: list[tuple[str, str]] = []
        for real_path, virtual in targets:
            for form in (self._shell_path_of(real_path), real_path.as_posix(), str(real_path)):
                pairs.append((form, virtual))
        # 长的先换：绑定目录可能整个包住沙箱根（用户绑到 data_root 的上级），
        # 也可能被沙箱根包住；按长度倒序是唯一不需要假设两者关系的排法。
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def _virtual_pairs(self) -> list[tuple[str, str]]:
        """(虚拟路径前缀, shell 写法) —— 长的在前，供 `_translate` 使用。

        与 `_path_pairs` 是同一张表的两个方向，但**不能直接复用**：后者为每个真实
        路径生成三种写法（`/c/...` / `C:/...` / `C:\\...`），而虚拟路径在命令里
        只会以 `/home/user/...` 这一种形式出现。

        `workspace/{id}` 在绑定态必须排在最前：否则 `/home/user/workspace/ws-1`
        会被更短的 `/home/user/workspace` 先吃掉，翻译成 `workspace-volumes/ws-1`
        ——那正是 S4 要消灭的旧副本视图。
        """
        workspace_base = posixpath.join(WORKSPACE.root, WORKSPACE.workspace)
        pairs: list[tuple[str, str]] = []
        context = self._sb.workspace_context
        if context is not None and context.state is BindingState.BOUND_READY and context.root:
            pairs.append(
                (
                    posixpath.join(workspace_base, context.workspace_id),
                    self._shell_path_of(Path(context.root)),
                )
            )
        pairs.append((workspace_base, self._shell_path_of(data_root() / "workspace-volumes")))
        pairs.append(
            (
                posixpath.join(WORKSPACE.root, SKILLS_PATH),
                self._shell_path_of(data_root() / SKILLS_PATH),
            )
        )
        pairs.append((WORKSPACE.root, self._shell_path_of(self._sb.root)))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def _translate(self, cmd: str) -> str:
        """把命令里的虚拟路径换成真实路径，**长的前缀优先**。

        这是**唯一**一处替模型做路径翻译的地方，和文件原语里的 `resolve()` 是同一件事：
        模型永远只需要知道 `/home/user`（prompt、工具描述、历史记录都这么写），
        真实路径只在这里出现一次。

        为什么不能靠"让模型用 Windows 路径"绕过：`/home/user` 写在 prompt 里，
        改成 `D:\\...` 会让同一条规则在不同执行环境里都不一样，
        而且模型对 `/home/user` 的先验远比对一个 UUID 目录强。

        **为什么不再只换 `/home/user` 一个前缀**：绑定态下 `workspace/{id}` 与
        `workspace-volumes/{id}` 是两份数据。只换沙箱根，命令里的
        `/home/user/workspace/ws-1/a.txt` 会经 `{root}/workspace` 联接落到旧副本上，
        而文件工具写的是用户目录——模型「刚写完就 cat 不到」，同一个路径重试到熔断。
        按前缀分流之后，绝对虚拟路径与 cwd 走的是同一个根。

        **不宣称越界隔离**：命令本身是 `unrestricted`，`cd /` 照样能读整台机器。
        这里只保证"模型写的虚拟路径指向它以为的那份数据"，不是沙箱边界。
        """
        for virtual, real in self._virtual_pairs():
            cmd = _replace_virtual_prefix(cmd, virtual, real)
        return cmd

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
        """本会话存活的后台进程（含**其它工具调用实例**启动的）。

        已退出 / pid 被复用的条目在这里顺手摘掉，台账不会无限增长。
        """
        return [
            LocalCommandInfo(pid=entry.pid, cmd=entry.cmd, cwd=entry.cwd)
            for entry in process_registry.list_processes(self._sb._process_owner)
        ]

    async def kill(self, pid: int) -> None:
        """结束本会话登记的进程。

        **不再按 pid 盲杀**：pid 属于别的会话（或别的用户）时直接拒绝。旧实现
        对未登记的 pid 仍会 `taskkill`，等于任何一个会话都能结束同机任意进程——
        归属校验是这次改造的第二个目标，不是附带清理。
        """
        process_registry.kill(self._sb._process_owner, pid)

    # ---- 内部 ----

    async def _rollback_background(
        self,
        proc: asyncio.subprocess.Process,
        exc: process_registry.PersistenceFailed,
        *,
        command: str,
    ) -> None:
        """登记失败后的回滚：终结 → 等待退出 → 回收 → 失败留高优先级审计。

        `terminate_tree` 只发出结束请求，进程不一定会立刻消失。**必须 wait**：
        否则 transport 还挂着子进程，工具返回失败的同时进程仍在跑，用户看到的
        是"报错了但端口被占着"。
        """
        killed, detail = process_registry.rollback(exc.entry)
        exc.terminated = killed
        exc.rollback_detail = detail
        if not killed:
            # 杀不掉 / 无法确认身份：这是"登记没落盘 + 进程还在"的双重残留，
            # 必须留下可追的审计（要求里点名要高优先级）。
            process_registry.record_rollback_failure(exc.entry, exc.owner, detail=detail)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except RuntimeError:
            # 跨事件循环：Process 的 waiter 绑在别的循环上。进程树已经由
            # terminate_tree 结束，这里等不到完成信号是正常的。
            pass

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
        except RuntimeError:
            # 跨事件循环：进程对象是在别的 loop 里创建的（测试用多次 asyncio.run
            # 分别调 run / kill），它的完成 Future 绑在原循环上，这里 await 会抛
            # "attached to a different loop"。taskkill /T /F 已经同步结束进程树，
            # 不需要再等 —— 继续等反而会让 kill 失败、后台表里留下僵尸项。
            pass

    def _kill_pid(self, pid: int) -> None:
        """结束进程及其子进程（实现见 `processes.terminate_tree`）。

        前台超时/取消与台账回收必须用同一份终结逻辑：两处各写一遍，会出现
        "一个说已结束、另一个说还在"的分歧，而且子进程树只有一处会带走。
        """
        process_registry.terminate_tree(pid)


# ---- 供 Service 使用的小工具 ----
