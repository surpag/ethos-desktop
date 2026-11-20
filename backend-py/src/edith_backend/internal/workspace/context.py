"""工作区上下文：绑定状态的探测、解析与不可变快照。

P0-2 的前置模块。**本模块只描述"这次执行认定哪个根"，不接任何消费者**：
文件工具、Git、Shell、面板都还没有调用它（见 docs/p0-2-s1-plan.md 的改动面）。

三层职责刻意分开，因为它们的可测试性与失效方式不同：

1. `probe_root()` —— **唯一做 I/O 的函数**。只回答客观事实，不决定业务策略。
   成功也只代表"此刻能 stat 到、能列举"，不代表后续写入/删除会成功。
2. `resolve_state()` —— 纯函数。把"绑定记录 + 探测结果"映射到 5 个状态之一。
   不碰磁盘、不读库、不读环境变量。
3. `build_context()` —— 纯函数。产出不可变的 `WorkspaceContext` 快照。

**只有 `UNBOUND` 才拿得到 `workspace-volumes/{id}` 这个根。**
任何绑定态异常（missing/denied/offline）的 `root` 都是绑定路径本身或 `None`，
绝不回退到 Volume——否则消费者只看 `root` 不看 `state` 时就会静默用上旧副本，
那正是 P0-2 要消灭的行为。
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ...config import data_root
from .layout import WORKSPACE


class BindingState(str, Enum):
    """绑定工作区当前能否作为权威工作区使用（不是文件系统错误类型的复刻）。"""

    UNBOUND = "unbound"
    BOUND_READY = "bound_ready"
    #: 绑定路径上不存在可用的工作区目录（不存在 **或** 存在但不是目录）。
    #: 刻意不叫 deleted：运维看到它不能断定目录被删了，具体原因看 `detail`。
    BOUND_MISSING = "bound_missing"
    BOUND_DENIED = "bound_denied"
    #: 根不可用且原因无法可靠细分（设备/网络不可达、或其他 `OSError`）。
    BOUND_OFFLINE = "bound_offline"


#: 允许本次执行继续的状态。其余三个（missing/denied/offline）一律失败关闭。
_EXECUTABLE_STATES = frozenset({BindingState.UNBOUND, BindingState.BOUND_READY})


class ProbeReason(str, Enum):
    """探测层的客观结论。不含"该怎么办"——策略在 `resolve_state()`。"""

    OK = "ok"
    NOT_EXISTS = "not_exists"
    NOT_DIRECTORY = "not_directory"
    PERMISSION_DENIED = "permission_denied"
    DEVICE_UNAVAILABLE = "device_unavailable"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class RootProbe:
    reason: ProbeReason
    #: 原始错误文本（`BOUND_READY` 时为空）。只用于日志与诊断，不参与判定。
    detail: str = ""


@dataclass(frozen=True)
class BindingRecord:
    """绑定事实，不含任何 I/O。

    `local_root` 为 `None` = 从未绑定或已解绑。两者对路径解析同构，差异属于审计。
    """

    workspace_id: str
    local_root: str | None = None
    #: 绑定目录在虚拟文件树中的前缀；空串 = 虚拟根直连 `local_root`
    prefix: str = ""


@dataclass(frozen=True)
class WorkspaceContext:
    """一次执行的快照：不可变，run 内只读，运行中换绑不影响它。"""

    workspace_id: str
    state: BindingState
    #: 权威根。`None` 表示"没有可执行的根"（绑定记录本身无效），
    #: 消费者必须先看 `state` 再用它。
    root: str | None
    #: `/home/user/workspace/{id}`，映射关系只在这里组合一次
    virtual_root: str
    prefix: str = ""
    detail: str = ""
    #: 只用于诊断与 trace，**不参与任何判定**
    captured_at: float = 0.0

    @property
    def executable(self) -> bool:
        """本次执行是否被允许。

        只有两种状态放行：`UNBOUND`（沿用改造前的路径，行为必须逐字节不变）与
        `BOUND_READY`（探测通过）。异常态一律不放行——**不回退到 Volume**。
        """
        return self.state in _EXECUTABLE_STATES


class WorkspaceUnavailableError(RuntimeError):
    """绑定工作区不可用：本次执行必须失败，不得回退到未绑定的 Volume。

    定义在领域层而不是执行层，因为「绑定状态决定能不能跑」是工作区的语义，
    与具体消费者（Agent run / 面板 / 定时任务）无关。带上快照本身，
    上层既可以只显示文案，也可以拿 `state`/`detail` 做更细的诊断。
    """

    def __init__(self, context: WorkspaceContext) -> None:
        self.context = context
        super().__init__(
            f"工作区绑定目录不可用（{context.state.value}）："
            f"{context.detail or '未提供具体原因'}。"
            "请确认该目录仍然存在且可访问，或在工作区设置里重新绑定。"
        )


# ---- 探测层（唯一 I/O） ----

#: 收口 I/O 入口：测试要模拟"stat 通过但 scandir 失败"只能从这里注入。
_stat = os.stat
_scandir = os.scandir

_DENIED_ERRNOS = {errno.EACCES, errno.EPERM}
_DEVICE_ERRNOS = {
    code
    for code in (
        getattr(errno, "ENODEV", None),
        getattr(errno, "EIO", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ESTALE", None),
    )
    if code is not None
}
#: Windows 网络/设备类错误：53 找不到网络路径、59 意外的网络错误、64 网络名不再可用、
#: 67 找不到网络名、1231 网络位置不可达。权限类只有 5（拒绝访问）。
_DEVICE_WINERRORS = {53, 59, 64, 67, 1231}


def _classify_oserror(exc: OSError) -> ProbeReason:
    """把 `OSError` 归到可区分的几类；认不出来就是 `UNEXPECTED`，不猜。

    **必须按 `winerror` 优先判定**：Windows 上不可达的网络路径
    （`ERROR_BAD_NETPATH` = 53）会被 Python 映射成 `FileNotFoundError` + `errno=2`，
    与"本地路径不存在"的异常形状完全一样（2026-11-20 实测）。只看异常类型的话，
    网络盘掉线会被判成 `BOUND_MISSING`——错误信息里写"目录不存在"，
    实际是盘不见了，排查方向直接跑偏。
    """
    winerror = getattr(exc, "winerror", None)
    if winerror:
        if winerror == 5:
            return ProbeReason.PERMISSION_DENIED
        if winerror in _DEVICE_WINERRORS:
            return ProbeReason.DEVICE_UNAVAILABLE
        if winerror in (2, 3):  # 找不到文件 / 找不到路径
            return ProbeReason.NOT_EXISTS
        return ProbeReason.UNEXPECTED
    if isinstance(exc, FileNotFoundError):
        return ProbeReason.NOT_EXISTS
    if exc.errno in _DENIED_ERRNOS:
        return ProbeReason.PERMISSION_DENIED
    if exc.errno in _DEVICE_ERRNOS:
        return ProbeReason.DEVICE_UNAVAILABLE
    if exc.errno == errno.ENOENT:
        return ProbeReason.NOT_EXISTS
    return ProbeReason.UNEXPECTED


def probe_root(path: str | Path) -> RootProbe:
    """探测这个路径能否作为工作区根使用。

    顺序不能换：`Path.is_dir()` 会把 `OSError` 吞成 `False`，先调它就把
    "没权限"和"不存在"混成一件事，也丢掉了可诊断的原因。

    两次探测的分工：
    - `os.stat` 回答"存在吗 / 是目录吗"，并保留真实错误；
    - `os.scandir` 回答"能列举吗"。少了这一步，"目录存在且有 stat 权限、但
      没有列举权限"会被判成 ready，然后在 S2 的第一次 I/O 才炸。
    """
    try:
        info = _stat(path)
    except OSError as exc:
        # 所有异常都交给同一个分类器：按异常类型分支会让"网络盘掉线"
        # （FileNotFoundError + winerror 53）走错分支。
        return RootProbe(_classify_oserror(exc), f"stat: {exc}")

    if not stat_module.S_ISDIR(info.st_mode):
        return RootProbe(ProbeReason.NOT_DIRECTORY, "stat: exists but is not a directory")

    try:
        with _scandir(path):
            pass
    except OSError as exc:
        return RootProbe(_classify_oserror(exc), f"scandir: {exc}")

    return RootProbe(ProbeReason.OK)


# ---- 解析层（纯函数） ----

_STATE_BY_REASON = {
    ProbeReason.OK: BindingState.BOUND_READY,
    # 存在但不是目录，与不存在同归：这个路径上没有可用的工作区目录
    ProbeReason.NOT_EXISTS: BindingState.BOUND_MISSING,
    ProbeReason.NOT_DIRECTORY: BindingState.BOUND_MISSING,
    ProbeReason.PERMISSION_DENIED: BindingState.BOUND_DENIED,
    ProbeReason.DEVICE_UNAVAILABLE: BindingState.BOUND_OFFLINE,
    ProbeReason.UNEXPECTED: BindingState.BOUND_OFFLINE,
}


def resolve_state(
    record: BindingRecord, probe: RootProbe | None
) -> BindingState:
    """绑定记录 + 探测结果 → 状态。纯函数，全序（任何输入都有返回值）。

    两种"调用方搞错"的情况一律 fail-closed 到 `BOUND_OFFLINE`：
    - 未绑定却给了探测结果；
    - 有绑定却漏了探测。

    为什么不用 `UNBOUND` 兜底：`UNBOUND` 会拿到 Volume 根，等于把一次实现错误
    变成静默的数据源切换。
    """
    if record.local_root is None:
        return BindingState.UNBOUND if probe is None else BindingState.BOUND_OFFLINE
    if probe is None:
        return BindingState.BOUND_OFFLINE
    return _STATE_BY_REASON[probe.reason]


def _root_for(record: BindingRecord, state: BindingState) -> str | None:
    if state is BindingState.UNBOUND:
        # 归一到与 `_workspace_volume_dir()` 同一平面（它返回 `volume.resolve()`），
        # **但不创建目录**：按需创建仍归执行链（S1 不动）。
        return str((data_root() / "workspace-volumes" / record.workspace_id).resolve())
    # 绑定态（含异常）：一律给绑定路径本身，绝不回退到 Volume。
    return record.local_root


def build_context(
    record: BindingRecord,
    probe: RootProbe | None,
    *,
    now: float | None = None,
) -> WorkspaceContext:
    """产出不可变快照。纯函数：除 `data_root()` 这个纯配置读取外不碰外部状态。"""
    state = resolve_state(record, probe)
    return WorkspaceContext(
        workspace_id=record.workspace_id,
        state=state,
        root=_root_for(record, state),
        virtual_root=WORKSPACE.workspace_path(record.workspace_id),
        prefix=record.prefix,
        detail=probe.detail if probe is not None else "",
        captured_at=time.time() if now is None else now,
    )
