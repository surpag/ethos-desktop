"""会话后台进程台账：比 `LocalSandbox` 实例活得更久的 pid 登记表。

**为什么必须跨实例。** `Service.workspace()` 每次工具调用都 `LocalSandbox.open()`
新建一个实例。登记表挂在实例上时，`sandbox_start_process` 写进去的那一条会随实例
一起消失——下一次 `sandbox_list_processes` 看到空表，`sandbox_kill_process` 只能
退回"按 pid 盲杀"，于是既没有归属校验，也解释不了"进程明明在跑，列表是空的"。

**为什么只存 pid 与元数据，不存 `asyncio.subprocess.Process`。**
`Process` 的 waiter 绑在创建它的那个事件循环上。工具调用、run 收尾、HTTP 请求
可能落在不同循环里，跨循环 `await proc.wait()` 会抛 "attached to a different
loop"（`LocalCommands._kill_tree` 里那条 `RuntimeError` 分支就是为此打的补丁）。
终结进程本来就不需要它：`taskkill /T /F`（Windows）与 `killpg`（POSIX）只认 pid，
而且能带走整棵子进程树。反过来，**持有它会让对象活过事件循环**：GC 落在循环
关闭之后时，`Popen.__del__` 拿到的是已失效的句柄（WinError 6），只能打一条
无用的警告。登记完就放手——后台进程的输出走 DEVNULL，与父进程的管道已经解耦。

**归属是 `(user_id, session_id)`，不是 run。** run 结束**不**回收：`sandbox_start_process`
的语义就是"长期进程"，跑完一轮对话就杀掉等于让 dev server 不可用。回收点只有两个：
删除会话、后端退出。进程持有工作区文件句柄，会话没了还留着它，用户看到的是
"删了会话但文件删不掉"——一个无法解释的现象。

**台账是本进程的内存表，不是跨进程服务。** 后端被强杀时 `lifespan` 不执行，
内存表随进程消失；那一层由 `process_store` 的登记文件 + `recover()` 补：
启动时读回登记，用"创建时刻 + 镜像路径"证明归属，证明不了就只报告、不杀。

**pid 会被系统复用。** 登记的进程自己退出、系统把同一个 pid 分给另一个进程之后，
"按 pid 杀"就会杀错人。所以登记时记录进程创建时刻，终结前再比一次：对不上就判定
记录过期，摘掉并且**不杀**。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from . import process_store

logger = logging.getLogger(__name__)

#: 归属键。用二元组而不是拼成字符串：`user/session` 里可能有分隔符，
#: 拼接后的字符串会出现"两个不同会话落到同一个桶"的碰撞。
Owner = tuple[str, str]

#: 登记记录的状态。`active` 是"本次运行正在管"的进程；其余三种都只出现在
#: 上次运行留下的持久化记录里（内存台账从不产生 suspected / acknowledged）。
STATUS_ACTIVE = "active"
STATUS_SUSPECTED = "suspected"
STATUS_ACKNOWLEDGED = "acknowledged"


class ProcessNotOwned(ProcessLookupError):
    """pid 不在当前会话的台账里，或登记的进程已退出、pid 已被系统复用。

    继承 `ProcessLookupError`（`OSError`）：工具层的业务错误表已经按它给出
    "先 list 再 kill"的指引，不需要再引入一套错误家族。
    """


class PersistenceFailed(RuntimeError):
    """后台进程已经启动，但登记写不进磁盘。

    **不能静默放行**：没有登记的进程在后端被强杀后无人可知，而且调用方拿到的
    "启动成功"是假的。正确做法是立即回滚（终结刚启动的进程树），把失败如实
    返回给工具层。

    带上 `entry` 与 `owner`：回滚需要身份证据（创建时刻）来判断"这个 pid 上还是
    不是我刚启动的那个进程"，凭 pid 直接杀可能误伤。
    """

    def __init__(self, entry: BackgroundProcess, owner: Owner, detail: str = "") -> None:
        self.entry = entry
        self.owner = owner
        self.detail = detail
        #: 回滚结果，由调用方在终结尝试之后填（`None` = 尚未尝试）。
        self.terminated: bool | None = None
        self.rollback_detail = ""
        super().__init__(
            f"后台进程登记写入失败（pid={entry.pid}）：{detail or '数据目录不可写'}"
        )


@dataclass(frozen=True)
class ProcessIdentity:
    """一次探测的客观结果。`image` 是镜像路径，恢复时用作归属证据。"""

    alive: bool
    create_time: float | None = None
    image: str = ""


@dataclass
class BackgroundProcess:
    """一条后台进程登记。字段只用于展示与归属校验。"""

    pid: int
    cmd: str
    #: 虚拟路径（`/home/user/...`）。**不存宿主路径**：工具结果会原样回给模型，
    #: 宿主绝对路径会让下一轮调用被判越界（S2 的教训）。
    cwd: str
    started_at: float
    #: 进程创建时刻（平台原生时间基准，只用于比较）。无法取得时为 `None`。
    create_time: float | None = None
    #: 镜像路径（`bash.exe` / `/bin/bash`）。persist 之后，下次启动用它判断
    #: "这个 pid 上跑的还是不是当初那类程序"——只有这一条能跨重启验证。
    executable: str = ""
    #: 登记它的后端实例 id（`uuid4`）。只用于诊断"这批是上一次进程留下的"，
    #: **不作为杀进程的依据**：重启后它一定对不上，拿它做判据等于永不恢复。
    instance_id: str = ""


def owner_of(user_id: str, session_id: str, sandbox_id: str = "") -> Owner:
    """归一化归属键。

    `open()` / `attach()` 都会填好 user/session；只有直接构造实例（测试或内部
    工具）才可能为空。那种情况下退回 `sandbox_id`——它由同一组 user+session
    派生，跨实例稳定。
    """
    user = (user_id or "").strip()
    session = (session_id or "").strip()
    if user and session:
        return (user, session)
    return ("", sandbox_id or "")


# ---- 存活探测（唯一 I/O） ----

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000
    #: `GetExitCodeProcess` 的"仍在运行"取值（`STATUS_PENDING`）
    _STILL_ACTIVE = 259

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]

    def _filetime_seconds(value: "wintypes.FILETIME") -> float:
        return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10_000_000.0

    def _windows_image(handle: "wintypes.HANDLE") -> str:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value

    def _handle_identity(handle: "wintypes.HANDLE") -> ProcessIdentity:
        """从一个**已打开的句柄**读身份。

        核验与终结共用这个函数：`terminate_verified` 拿到句柄后在这里复核，
        而不是拿外部那次 `_inspect` 的结果去信任。
        """
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return ProcessIdentity(True, image=_windows_image(handle))
        if code.value != _STILL_ACTIVE:
            return ProcessIdentity(False)
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        image = _windows_image(handle)
        if not _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ProcessIdentity(True, image=image)
        return ProcessIdentity(True, _filetime_seconds(created), image)

    def _inspect(pid: int) -> ProcessIdentity:
        """(是否存活, 创建时刻, 镜像路径)。

        打不开句柄时用 `os.kill(pid, 0)` 兜底：它在 Windows 上只是
        `OpenProcess(PROCESS_QUERY_INFORMATION)` + `CloseHandle`，不会终结进程。
        两种方式都失败才判定已退出——**不能只凭一次 `OpenProcess` 失败就摘掉记录**，
        那会让"权限不足"被误判成"进程没了"。
        """
        if pid <= 0:
            return ProcessIdentity(False)
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            try:
                os.kill(pid, 0)
            except OSError:
                return ProcessIdentity(False)
            return ProcessIdentity(True)
        try:
            return _handle_identity(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def _open_verified(
        pid: int, recorded_time: float | None, recorded_image: str
    ) -> "wintypes.HANDLE | None":
        """在**同一个句柄**上完成身份核验并保留它，供调用方终结。

        这是 TOCTOU 的解法：Windows 的 pid 只在进程对象引用计数归零后才可能被
        复用，而我们持有句柄期间计数不为零。于是"核验通过 → 按 pid 终结"之间，
        这个 pid 不可能被分配给别的进程。

        返回 `None` 表示"没有可安全终结的对象"：进程已退出、打不开、或身份不符。
        调用方需要再用 `_inspect` 区分这三种（决定是记 `gone` 还是 `reused`）。
        """
        if pid <= 0:
            return None
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE | _PROCESS_TERMINATE,
            False,
            pid,
        )
        if not handle:
            return None
        if not _identity_matches(_handle_identity(handle), recorded_time, recorded_image):
            _kernel32.CloseHandle(handle)
            return None
        return handle

else:

    def _posix_create_time(pid: int) -> float | None:
        """进程启动时刻（`/proc/<pid>/stat` 第 22 字段，单位 tick）。

        macOS 没有 `/proc`：那里只能判存活，判不了 pid 复用。宁可少一层防护，
        也不要为了统一而引入 `ps` 子进程（每次 list 都 fork 一次，代价更高）。
        """
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                raw = handle.read()
            # comm 字段可能含空格和括号，必须从最后一个 ')' 之后切
            fields = raw[raw.rindex(b")") + 2 :].split()
            return float(fields[19]) / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None

    def _posix_image(pid: int) -> str:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""

    def _inspect(pid: int) -> ProcessIdentity:
        if pid <= 0:
            return ProcessIdentity(False)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProcessIdentity(False)
        except PermissionError:
            # 存在但属于别的用户：能确认存活，取不到创建时刻与镜像
            return ProcessIdentity(True)
        except OSError:
            return ProcessIdentity(False)
        return ProcessIdentity(True, _posix_create_time(pid), _posix_image(pid))


def _probe(pid: int) -> tuple[bool, float | None]:
    """(是否存活, 创建时刻)。兼容既有调用的薄包装。"""
    identity = _inspect(pid)
    return (identity.alive, identity.create_time)


def _still_same(entry: BackgroundProcess) -> bool:
    """登记的那一条是否仍然是"同一个进程"。

    存活但创建时刻对不上 = pid 已被系统复用给另一个进程。这种情况必须先摘记录再
    拒绝操作，否则下一次 kill 就会杀错人。
    """
    identity = _inspect(entry.pid)
    if not identity.alive:
        return False
    if entry.create_time is None or identity.create_time is None:
        # 有一侧拿不到指纹：以存活为准。登记时已经探测过一次。
        return True
    return abs(identity.create_time - entry.create_time) < 0.5


def _identity_verdict(
    recorded_time: float | None, recorded_image: str, identity: ProcessIdentity
) -> str:
    """恢复时的归属判定：`gone` / `reused` / `unknown` / `confirmed`。

    顺序是有意安排的——**能证伪的先证伪**：

    - 进程不在 → 记录过期（删记录，不杀）；
    - 创建时刻对不上 → pid 已被复用（删记录，不杀）；
    - 有一侧拿不到创建时刻或镜像路径 → `unknown`（**不杀**，保留记录）；
    - 镜像不一致 → 同一个 pid 上跑的是别的程序（`exec` 换了镜像也算），不杀。

    只有"创建时刻精确匹配 + 镜像一致"才返回 `confirmed`。宁可留一个孤儿让用户
    手动处理，也不杀一个无法证明属于 ETHOS 的进程。
    """
    if not identity.alive:
        return "gone"
    if recorded_time is None or identity.create_time is None:
        return "unknown"
    if abs(identity.create_time - recorded_time) >= 0.5:
        return "reused"
    if not recorded_image or not identity.image:
        return "unknown"
    normalize = lambda value: os.path.normcase(os.path.normpath(value))  # noqa: E731
    if normalize(recorded_image) != normalize(identity.image):
        return "unknown"
    return "confirmed"


def _normalize_image(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _identity_matches(
    identity: ProcessIdentity, recorded_time: float | None, recorded_image: str
) -> bool:
    """严格判定：两侧证据齐全且一致才算匹配（fail-closed）。

    与 `_identity_verdict` 的区别：这里只回答"能不能动手"，不区分 gone/reused。
    拿不到创建时刻或镜像路径一律返回 False——**不能因为"看起来还活着"就终结**。
    """
    if not identity.alive:
        return False
    if recorded_time is None or identity.create_time is None:
        return False
    if abs(identity.create_time - recorded_time) >= 0.5:
        return False
    if not recorded_image or not identity.image:
        return False
    return _normalize_image(recorded_image) == _normalize_image(identity.image)


def _taskkill_tree(pid: int) -> bool:
    try:
        done = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # taskkill 对"进程不存在"也返回非零；那种情况记录本来就该摘掉，不算失败。
    return done.returncode == 0 or not _probe(pid)[0]


def _terminate_posix(pid: int) -> tuple[bool, str]:
    # `pidfd` 是 POSIX 侧最接近"句柄"的东西：它绑定进程对象，
    # 原进程退出、pid 被复用之后，对这个 fd 发信号仍然作用在原对象上（或直接报 ESRCH）。
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            fd = None
        if fd is not None:
            try:
                # pidfd 只管单个进程；进程组仍按 pgid 处理。组号要等组内全部退出
                # 才可能释放，复用风险远低于 pid。
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except OSError:
                    pass
                return (True, "terminated")
            finally:
                os.close(fd)
    try:
        os.killpg(os.getpgid(pid), 9)
        return (True, "terminated")
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, 9)
            return (True, "terminated")
        except (ProcessLookupError, PermissionError, OSError):
            return (False, "terminate_failed")


def terminate_tree(pid: int) -> bool:
    """只按 pid 结束进程树。**调用方必须自己已经确认归属。**

    只杀父进程会留下孤儿：`cmd1 && cmd2`、`python x.py` 派生的解释器都还在跑，
    继续占着工作区文件（Windows 上表现为"文件被占用无法删除"）。

    保留它是为了前台命令的超时/取消：那些进程刚由本会话启动，生命周期完全在
    我们手里，没有"记录来自上一次运行"的归属问题。**后台进程一律走
    `terminate_verified`**，不要用这个。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _taskkill_tree(pid)
    ok, _reason = _terminate_posix(pid)
    return ok


def terminate_verified(
    pid: int, *, create_time: float | None, image: str
) -> tuple[bool, str]:
    """在身份确认的前提下终结进程树。返回 `(是否确认结束, 原因)`。

    原因取值：`terminated` / `already_exited` / `identity_mismatch` / `terminate_failed`。

    **这是 TOCTOU 的修复点。** 旧流程是"先 `_inspect` 核验，再 `terminate_tree(pid)`"，
    两步之间原进程可能退出、pid 被系统复用，于是杀掉一个无关进程。现在把核验
    与终结绑在同一个进程对象上：

    - Windows：`_open_verified` 拿到的句柄让进程对象引用计数不为零，持有期间 pid
      不可能被复用；`taskkill` 失败时再对**句柄**调 `TerminateProcess`（句柄绑定
      对象，不存在 pid 歧义）。
    - Linux：`pidfd` 绑定进程对象，信号不随 pid 复用而转向别人。

    返回 `identity_mismatch` 有两种含义：拿不到证据（fail-closed），或证据对不上。
    调用方据此决定"记 reused 删记录"还是"记 unknown 保留"。
    """
    if pid <= 0:
        return (False, "identity_mismatch")

    if sys.platform == "win32":
        handle = _open_verified(pid, create_time, image)
        if handle is None:
            identity = _inspect(pid)
            return (True, "already_exited") if not identity.alive else (
                False,
                "identity_mismatch",
            )
        try:
            if _taskkill_tree(pid):
                return (True, "terminated")
            if _kernel32.TerminateProcess(handle, 1):
                return (True, "terminated")
            return (False, "terminate_failed")
        finally:
            _kernel32.CloseHandle(handle)

    identity = _inspect(pid)
    if not identity.alive:
        return (True, "already_exited")
    if not _identity_matches(identity, create_time, image):
        return (False, "identity_mismatch")
    return _terminate_posix(pid)


def _row_context(row: dict) -> dict:
    """审计与返回值共用的原始字段。

    要求是"保留原始 PID、create_time、executable、user_id、session_id、
    instance_id"——所以这里不做裁剪，也不做脱敏：这些都不是凭据。
    """
    return {
        "pid": int(row["pid"]),
        "user_id": row.get("user_id", ""),
        "session_id": row.get("session_id", ""),
        "instance_id": row.get("instance_id", ""),
        "create_time": row.get("create_time"),
        "executable": row.get("executable", ""),
        "cmd": row.get("cmd", ""),
        "cwd": row.get("cwd", ""),
    }


def _recorded_time(row: dict) -> float | None:
    """记录里的创建时刻；形状不对一律当"没有证据"。"""
    value = row.get("create_time")
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _mark_suspected(row: dict, verdict: str) -> None:
    row["status"] = STATUS_SUSPECTED
    row["last_verdict"] = verdict
    row["last_checked_at"] = time.time()


def _find_row(rows: list[dict], pid: int) -> dict | None:
    for row in rows:
        if row.get("pid") == pid:
            return row
    return None


# ---- 登记失败回滚 ----

def rollback(entry: BackgroundProcess) -> tuple[bool, str]:
    """终结一个"登记失败"的刚启动进程。返回 `(是否确认已结束, 原因)`。

    为什么要再比一次创建时刻：`register` 的探测与这里的回滚之间隔着一次失败的
    磁盘写，这段时间里进程完全可能已经退出、pid 被系统复用。凭 pid 直接
    `taskkill` 会杀到无关进程——台账在别处一直在防这件事，回滚路径不能例外。
    """
    identity = _inspect(entry.pid)
    if not identity.alive:
        return (True, "already_exited")
    if entry.create_time is None or not entry.executable:
        # 没有身份证据：无论进程是否还在，都不能动手。
        return (False, "identity_unconfirmed")
    return terminate_verified(
        entry.pid, create_time=entry.create_time, image=entry.executable
    )


def record_rollback_failure(entry: BackgroundProcess, owner: Owner, *, detail: str) -> None:
    """回滚失败：进程起来了、登记没落盘、还杀不掉。这是优先级最高的残留。

    单独一个函数而不是顺手 `logger.warning`：这条记录是事故复盘时唯一能还原
    "哪个进程、什么命令、哪次运行、为什么没清掉"的地方。
    """
    process_store.audit(
        {
            "severity": "high",
            "action": "rollback_failed",
            "reason": detail,
            "pid": entry.pid,
            "create_time": entry.create_time,
            "executable": entry.executable,
            "cmd": entry.cmd,
            "cwd": entry.cwd,
            "started_at": entry.started_at,
            "instance_id": entry.instance_id,
            "user_id": owner[0],
            "session_id": owner[1],
        }
    )
    logger.error(
        "sandbox_background_rollback_failed pid=%s reason=%s cmd=%r",
        entry.pid,
        detail,
        entry.cmd,
    )


# ---- 台账 ----

@dataclass(frozen=True)
class RecoveryReport:
    """一次启动恢复的结果，按**实际发生了什么**分开计数。

    这五个字段不能合并成一个"处理了多少条"：

    - `recovered`：确认归属并**真的终结**了进程；
    - `gone`：进程早已退出，只删了记录；
    - `reused`：pid 已被系统复用给别的进程，只删了记录（**没杀**）；
    - `unknown`：身份证明不足或终结失败，记录保留（**没杀**）；
    - `acknowledged`：人工已确认不处理，本轮完全跳过（不改状态、不告警）。

    `recovered` 之外都不能算"恢复成功"。把 `unknown` 计入成功恢复是最容易犯的
    错——那等于把"没处理"报告成"处理完了"。
    """

    recovered: tuple[int, ...] = ()
    gone: tuple[int, ...] = ()
    reused: tuple[int, ...] = ()
    unknown: tuple[int, ...] = ()
    acknowledged: tuple[int, ...] = ()
    #: 登记文件的读取状态（`ok` / `missing` / `corrupt` / `unreadable` / `shape_error`）。
    #: 非 `ok`/`missing` 时**没有执行任何恢复**——报告里全是空集合，但含义是
    #: "无法判断"，不是"没有遗留进程"。
    registry_status: str = "ok"

    @property
    def touched(self) -> bool:
        return bool(
            self.recovered or self.gone or self.reused or self.unknown or self.acknowledged
        )

    # 旧字段名保留为只读别名：调用点与既有测试用的是这三个词。
    @property
    def reclaimed(self) -> tuple[int, ...]:
        return self.recovered

    @property
    def stale(self) -> tuple[int, ...]:
        return self.gone + self.reused

    @property
    def suspected(self) -> tuple[int, ...]:
        return self.unknown

    def to_dict(self) -> dict:
        return {
            "registry_status": self.registry_status,
            "recovered": list(self.recovered),
            "gone": list(self.gone),
            "reused": list(self.reused),
            "unknown": list(self.unknown),
            "acknowledged": list(self.acknowledged),
        }


class BackgroundRegistry:
    """pid 台账。按归属分桶，跨 `LocalSandbox` 实例共享。

    **不是跨进程共享**：它是本 Python 进程的内存表。跨进程（后端被强杀后重启）
    那一层由 `process_store` 的登记文件 + `recover()` 负责，两者职责不重叠。

    用线程锁而不是事件循环原语：终结进程是同步阻塞调用（Windows 的 `taskkill`
    最长 10 秒），且 HTTP 层（删会话）与工具层可能并发触发。锁只在改字典时持有，
    **终结动作在锁外执行**——否则一次慢 kill 会卡住同一时刻的 list。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_owner: dict[Owner, dict[int, BackgroundProcess]] = {}
        #: 本进程实例的 id。写进每一条登记，只用于诊断"这批记录是哪次运行留下的"。
        self.instance_id = uuid.uuid4().hex

    # ---- 持久化 ----

    def _rows_locked(self) -> list[dict]:
        rows: list[dict] = []
        for (user_id, session_id), bucket in self._by_owner.items():
            for entry in bucket.values():
                rows.append(
                    {
                        "pid": entry.pid,
                        "create_time": entry.create_time,
                        "executable": entry.executable,
                        "cmd": entry.cmd,
                        "cwd": entry.cwd,
                        "started_at": entry.started_at,
                        "instance_id": entry.instance_id,
                        "user_id": user_id,
                        "session_id": session_id,
                        # 运行期登记的条目状态是 active。suspected / acknowledged
                        # 只会出现在"上次运行留下的记录"里（见 `recover`）。
                        "status": STATUS_ACTIVE,
                    }
                )
        return rows

    def _persist_locked(self) -> None:
        """调用方必须持锁：快照与落盘之间不能插入别的变更。

        写盘失败只记 warning（见 `process_store.save`）：进程已经起来了，
        "下次启动恢复不到它"是能力降级，"现在让命令失败"是功能故障。
        """
        process_store.save(self._rows_locked(), instance_id=self.instance_id)

    def _forget_pid(self, pid: int) -> None:
        """把某个 pid 从内存台账摘掉（恢复流程用）。"""
        with self._lock:
            for owner, bucket in list(self._by_owner.items()):
                bucket.pop(pid, None)
                if not bucket:
                    self._by_owner.pop(owner, None)

    # ---- 运行时 ----

    def register(self, owner: Owner, *, pid: int, cmd: str, cwd: str) -> BackgroundProcess:
        """登记后台进程。**落盘失败就抛 `PersistenceFailed`，不允许静默放行。**

        为什么不能"先返回成功、后台再补写"：工具会立刻告诉模型"已启动"，
        而模型接下来可能依赖"重启后它还在"。写不进去就必须当场回滚，
        否则用户拿到的是一个只在当前进程内成立的假象。

        内存条目在失败时会一并撤销：留下的条目既不在磁盘上、也不该出现在
        `sandbox_list_processes` 里——那会让用户以为它被管理着。
        """
        identity = _inspect(pid)
        entry = BackgroundProcess(
            pid=pid,
            cmd=cmd,
            cwd=cwd,
            started_at=time.time(),
            create_time=identity.create_time if identity.alive else None,
            executable=identity.image,
            instance_id=self.instance_id,
        )
        with self._lock:
            self._by_owner.setdefault(owner, {})[pid] = entry
            ok = process_store.save(
                self._rows_locked(), instance_id=self.instance_id
            )
            if not ok:
                bucket = self._by_owner.get(owner) or {}
                bucket.pop(pid, None)
                if not bucket:
                    self._by_owner.pop(owner, None)
                raise PersistenceFailed(entry, owner, "process_ledger_write_failed")
        return entry

    def list(self, owner: Owner) -> list[BackgroundProcess]:
        """存活中的登记项；顺手摘掉已退出 / pid 已被复用的条目。"""
        with self._lock:
            bucket = self._by_owner.get(owner)
            if not bucket:
                return []
            alive: list[BackgroundProcess] = []
            removed = False
            for pid, entry in list(bucket.items()):
                if not _still_same(entry):
                    bucket.pop(pid, None)
                    removed = True
                    continue
                alive.append(entry)
            if not bucket:
                self._by_owner.pop(owner, None)
            if removed:
                # 摘除必须落盘，否则"进程已经退出"这件事只活在内存里，
                # 下次启动会把它当成可疑孤儿重新报一遍。
                self._persist_locked()
            return alive

    def kill(self, owner: Owner, pid: int) -> BackgroundProcess:
        """终结本会话登记的 pid。未登记或有归属问题一律拒绝，**不做盲杀**。"""
        with self._lock:
            bucket = self._by_owner.get(owner) or {}
            entry = bucket.get(pid)
            if entry is None:
                raise ProcessNotOwned(
                    f"pid {pid} 不属于当前会话的沙箱：它可能从未由 "
                    "sandbox_start_process 启动，或所属会话已经被回收。"
                    "请先用 sandbox_list_processes 查看仍存活的进程。"
                )
            bucket.pop(pid, None)
            if not bucket:
                self._by_owner.pop(owner, None)
            stale = not _still_same(entry)
            self._persist_locked()
        if stale:
            raise ProcessNotOwned(
                f"pid {pid} 已经退出（或该 pid 已被系统复用给另一个进程），未执行结束操作。"
                "请先用 sandbox_list_processes 确认当前进程再操作。"
            )
        ok, reason = terminate_verified(
            pid, create_time=entry.create_time, image=entry.executable
        )
        if not ok and reason != "identity_mismatch":
            # 终结失败（权限/句柄）：把记录放回去。否则用户再也看不到这个进程，
            # 却又杀不掉它——比"看得见但杀不掉"更难排查。
            with self._lock:
                self._by_owner.setdefault(owner, {})[pid] = entry
                self._persist_locked()
            raise ProcessNotOwned(
                f"pid {pid} 无法结束（可能权限不足或进程已受保护），记录已保留。"
            )
        if reason == "identity_mismatch":
            # 核验通过之后、终结之前身份变了：原进程已退出且 pid 被复用。
            # 记录保持删除状态（原进程不存在了），但明确告诉调用方"没杀"。
            raise ProcessNotOwned(
                f"pid {pid} 在终结前身份已变化（进程退出或 pid 被复用），未执行结束操作。"
            )
        return entry

    def release(self, owner: Owner) -> int:
        """回收一个会话的全部后台进程，返回处理条数。"""
        with self._lock:
            bucket = self._by_owner.pop(owner, None) or {}
            entries = list(bucket.values())
            if entries:
                self._persist_locked()
        for entry in entries:
            if _still_same(entry):
                terminate_verified(
                    entry.pid, create_time=entry.create_time, image=entry.executable
                )
        if entries:
            # 回收是一个有后果的动作（进程被杀、文件句柄释放）。删会话时如果
            # 进程数量对不上，这条日志是唯一能回答"是谁带走了它"的地方。
            logger.info(
                "sandbox_background_released owner=%s count=%d", owner, len(entries)
            )
        return len(entries)

    def release_all(self) -> int:
        """回收所有会话的后台进程（后端退出时调用）。"""
        with self._lock:
            owners = list(self._by_owner)
        return sum(self.release(owner) for owner in owners)

    # ---- 启动恢复 ----

    def recover(self) -> RecoveryReport:
        """处理上一次运行留下的登记（后端被强杀时 `lifespan` 没跑到）。

        三种结果的边界：

        - `gone` / `reused`：记录必然过期（进程不在，或 pid 上已是别人），**只删记录**；
        - `unknown`：拿不到创建时刻或镜像路径、或镜像对不上，**不杀**，记录保留；
        - `confirmed`：创建时刻精确匹配 + 镜像一致，才终结进程树。

        `unknown` 保留记录是有意的：恢复是幂等的（再次启动会得到同样的报告，
        不会产生额外副作用），而误杀是不可逆的。宁可让用户看到一条"可疑孤儿"，
        也不要替他把一个无关进程干掉。

        没有记录时直接返回，**不写文件**——避免每次启动都在磁盘上留下空账本。
        """
        state = process_store.read_ledger()
        if not state.readable:
            # 读不出来就**不能**假装"没有遗留进程"：既不杀，也不返回空成功。
            # 返回空集合 + 一个非 ok 的状态，让启动日志与运维入口都能说出真相。
            process_store.audit(
                {
                    "severity": "high",
                    "action": "recover_blocked",
                    "reason": f"registry_{state.status}",
                    "detail": state.detail,
                    "backup": state.backup,
                    "instance_id": self.instance_id,
                }
            )
            logger.error(
                "sandbox_process_recovery_blocked status=%s backup=%s detail=%s",
                state.status,
                state.backup,
                state.detail,
            )
            return RecoveryReport(registry_status=state.status)

        rows = state.entries
        if not rows:
            return RecoveryReport(registry_status=state.status)

        recovered: list[int] = []
        gone: list[int] = []
        reused: list[int] = []
        unknown: list[int] = []
        acknowledged: list[int] = []
        remaining: list[dict] = []

        for row in rows:
            pid = int(row["pid"])
            status = str(row.get("status") or STATUS_ACTIVE)
            context = _row_context(row)

            if status == STATUS_ACKNOWLEDGED:
                # 人工已经确认"这条不处理"。恢复流程不再核验、不告警、不改状态。
                # 尤其**不能**因为 pid 不在了就把它记成 gone：acknowledged 是人的
                # 决定，不是进程状态，把两者混在一起等于在报告里制造假事实。
                acknowledged.append(pid)
                remaining.append(row)
                continue

            recorded_time = _recorded_time(row)
            identity = _inspect(pid)
            verdict = _identity_verdict(
                recorded_time, str(row.get("executable") or ""), identity
            )

            if verdict in ("gone", "reused"):
                (gone if verdict == "gone" else reused).append(pid)
                self._forget_pid(pid)
                process_store.audit({"action": "drop", "reason": verdict, **context})
                continue

            if verdict == "unknown":
                unknown.append(pid)
                _mark_suspected(row, verdict)
                remaining.append(row)
                logger.warning(
                    "sandbox_orphan_suspected pid=%s user=%s session=%s reason=identity_unconfirmed",
                    pid,
                    context["user_id"],
                    context["session_id"],
                )
                process_store.audit(
                    {"action": "keep", "reason": "identity_unconfirmed", **context}
                )
                continue

            # confirmed：创建时刻与镜像都对得上，交给"核验+终结"绑定路径
            ok, reason = terminate_verified(
                pid,
                create_time=recorded_time,
                image=str(row.get("executable") or ""),
            )
            if ok:
                recovered.append(pid)
                self._forget_pid(pid)
                process_store.audit(
                    {"action": "terminate", "reason": "confirmed", **context}
                )
            elif reason == "identity_mismatch":
                # 核验之后、动手之前身份变了：原进程已退出且 pid 被复用。
                # 这不是"恢复了"，也不是"未知"——单列 reused，且**确实没杀**。
                reused.append(pid)
                self._forget_pid(pid)
                process_store.audit(
                    {"action": "drop", "reason": "identity_changed", **context}
                )
            else:
                unknown.append(pid)
                _mark_suspected(row, "terminate_failed")
                remaining.append(row)
                logger.warning(
                    "sandbox_orphan_suspected pid=%s user=%s session=%s reason=terminate_failed",
                    pid,
                    context["user_id"],
                    context["session_id"],
                )
                process_store.audit(
                    {"action": "keep", "reason": "terminate_failed", **context}
                )

        # 只保留"杀不掉/不敢杀/人工确认不处理"的条目。确认已回收的和过期记录一并消失。
        # 这些遗留记录属于**上一个实例**，合并写默认不会动它们，所以必须显式
        # 声明 drop（已处理）与 patch（本次更新了 last_verdict 的 suspected 行）。
        process_store.save(
            remaining,
            instance_id=self.instance_id,
            patch_pids={int(row["pid"]) for row in remaining
                        if row.get("status") == STATUS_SUSPECTED},
            drop_pids=set(recovered) | set(gone) | set(reused),
        )
        report = RecoveryReport(
            tuple(recovered),
            tuple(gone),
            tuple(reused),
            tuple(unknown),
            tuple(acknowledged),
            registry_status=state.status,
        )
        if report.touched:
            process_store.audit(
                {"action": "recover_summary", "instance_id": self.instance_id, **report.to_dict()}
            )
            logger.info(
                "sandbox_process_recovery recovered=%s gone=%s reused=%s unknown=%s acknowledged=%s",
                list(report.recovered),
                list(report.gone),
                list(report.reused),
                list(report.unknown),
                list(report.acknowledged),
            )
        return report

    # ---- 人工处置（CLI 入口） ----

    def describe(self) -> list[dict]:
        """只读快照：每条遗留记录 + 实时核验结果（不含登记状态）。"""
        return self._describe_rows(process_store.load())

    def describe_registry(self) -> dict:
        """带状态的只读快照（CLI `inspect` 用）。

        `status != ok/missing` 时 `records` 为空，但那是"读不出来"，不是"没有"。
        运维入口据此返回非零退出码，避免把解析失败读成"系统干净"。
        """
        state = process_store.read_ledger()
        return {
            "status": state.status,
            "path": str(process_store.ledger_path()),
            "backup": state.backup,
            "backups": list(state.backups),
            "detail": state.detail,
            "records": self._describe_rows(state.entries),
        }

    def _describe_rows(self, rows: list[dict]) -> list[dict]:
        """**不写文件、不杀进程**。运维需要先看清楚"它是什么、凭什么判归属"，
        再决定 retry 还是 acknowledge；把核验和处置混在一个动作里，就没法在
        动手前留证。
        """
        described: list[dict] = []
        for row in rows:
            pid = int(row["pid"])
            identity = _inspect(pid)
            recorded_time = _recorded_time(row)
            described.append(
                {
                    "pid": pid,
                    "status": str(row.get("status") or STATUS_ACTIVE),
                    "verdict": _identity_verdict(
                        recorded_time, str(row.get("executable") or ""), identity
                    ),
                    "user_id": row.get("user_id", ""),
                    "session_id": row.get("session_id", ""),
                    "instance_id": row.get("instance_id", ""),
                    "cmd": row.get("cmd", ""),
                    "cwd": row.get("cwd", ""),
                    "started_at": row.get("started_at"),
                    "recorded_create_time": recorded_time,
                    "recorded_executable": row.get("executable", ""),
                    "alive": identity.alive,
                    "current_create_time": identity.create_time,
                    "current_executable": identity.image,
                    "last_verdict": row.get("last_verdict"),
                    "last_checked_at": row.get("last_checked_at"),
                    "acknowledged_at": row.get("acknowledged_at"),
                    "acknowledged_reason": row.get("acknowledged_reason"),
                    "acknowledged_result": row.get("acknowledged_result"),
                }
            )
        return described

    def retry(self, pid: int) -> dict:
        """重新核验一条记录。**只有 confirmed 才终结**，unknown 原地不动。"""
        rows = process_store.load()
        row = _find_row(rows, pid)
        if row is None:
            raise LookupError(f"没有 pid={pid} 的遗留进程记录")
        if str(row.get("status") or STATUS_ACTIVE) == STATUS_ACKNOWLEDGED:
            # acknowledged 是人的决定，retry 不能替人翻案。要处理就先 revoke
            # （当前不提供）——否则"人工确认"就成了没有任何约束力的标签。
            raise PermissionError(
                f"pid={pid} 已被人工确认不处理（acknowledged），retry 不会改变它的状态"
            )

        context = _row_context(row)
        identity = _inspect(pid)
        verdict = _identity_verdict(
            _recorded_time(row), str(row.get("executable") or ""), identity
        )

        if verdict in ("gone", "reused"):
            # 遗留记录属于上一个实例，删除必须显式声明（见 `process_store.save`）
            process_store.save(
                [], instance_id=self.instance_id, drop_pids={pid}
            )
            process_store.audit({"action": "retry_drop", "reason": verdict, **context})
            return {**context, "result": verdict, "terminated": False}

        if verdict == "unknown":
            _mark_suspected(row, verdict)
            process_store.save(
                [row], instance_id=self.instance_id, patch_pids={pid}
            )
            process_store.audit({"action": "retry_keep", "reason": verdict, **context})
            return {**context, "result": "unknown", "terminated": False}

        ok, reason = terminate_verified(
            pid,
            create_time=_recorded_time(row),
            image=str(row.get("executable") or ""),
        )
        if ok:
            process_store.save(
                [], instance_id=self.instance_id, drop_pids={pid}
            )
            process_store.audit(
                {"action": "retry_terminate", "reason": "confirmed", **context}
            )
            return {**context, "result": "recovered", "terminated": True}

        if reason == "identity_mismatch":
            # 核验之后身份变了：原进程已退出、pid 被复用。删记录，但不记 recovered。
            process_store.save(
                [], instance_id=self.instance_id, drop_pids={pid}
            )
            process_store.audit(
                {"action": "retry_drop", "reason": "identity_changed", **context}
            )
            return {**context, "result": "reused", "terminated": False}

        _mark_suspected(row, "terminate_failed")
        process_store.save([row], instance_id=self.instance_id, patch_pids={pid})
        process_store.audit(
            {"action": "retry_keep", "reason": "terminate_failed", **context}
        )
        return {**context, "result": "terminate_failed", "terminated": False}

    def acknowledge(self, pid: int, reason: str) -> dict:
        """人工确认"知道它存在，暂不处理"。**不杀进程，也不删记录与审计。**

        幂等：重复调用不会覆盖首次的处置时间与原因——那样会让"谁在什么时候
        做的决定"变成最后一次调用者的说法。
        """
        note = (reason or "").strip()
        if not note:
            raise ValueError("acknowledge 必须给出原因（会写进记录与审计）")
        rows = process_store.load()
        row = _find_row(rows, pid)
        if row is None:
            raise LookupError(f"没有 pid={pid} 的遗留进程记录")

        context = _row_context(row)
        if str(row.get("status") or STATUS_ACTIVE) == STATUS_ACKNOWLEDGED:
            process_store.audit({"action": "acknowledge_noop", **context})
            return {
                **context,
                "result": STATUS_ACKNOWLEDGED,
                "already_acknowledged": True,
                "acknowledged_at": row.get("acknowledged_at"),
                "acknowledged_reason": row.get("acknowledged_reason"),
            }

        now = time.time()
        row["status"] = STATUS_ACKNOWLEDGED
        row["acknowledged_at"] = now
        row["acknowledged_reason"] = note
        # 处置结果是"没有终结"。写清楚，避免以后被读成"已回收"。
        row["acknowledged_result"] = "not_terminated"
        # 修改的是上一个实例登记的行，必须显式 patch；否则合并写会把它当"别人的"跳过。
        process_store.save([row], instance_id=self.instance_id, patch_pids={pid})
        process_store.audit(
            {"action": "acknowledge", "operator_reason": note, **context}
        )
        return {
            **context,
            "result": STATUS_ACKNOWLEDGED,
            "already_acknowledged": False,
            "acknowledged_at": now,
            "acknowledged_reason": note,
        }


#: 模块级单例。工具层、HTTP 层、lifespan 共用同一份台账——这也是"跨实例可见"的
#: 实现所在，不是每个沙箱各一份。
registry = BackgroundRegistry()


#: 等待子进程退出的 task 必须有人持有，否则会被 GC 掉（asyncio 文档的标准要求）。
_reapers: set[asyncio.Task] = set()


async def _wait_quietly(proc: object) -> None:
    try:
        await proc.wait()  # type: ignore[attr-defined]
    except asyncio.CancelledError:
        raise
    except (RuntimeError, OSError):
        # 跨事件循环 / 句柄已失效：这个对象已经不代表进程了，忽略即可。
        pass


def reap(proc: object) -> None:
    """后台监听进程退出。

    目的不是拿退出码（台账只认 pid），而是修两个真实问题：

    1. POSIX 上父进程不 `wait` 会留下 zombie，进程数随会话增长；
    2. `asyncio.Process` 的 transport 只有在 `wait()` 返回后才正常收尾。不等待的
       话对象会活过事件循环，GC 时拿着已失效的句柄报警（WinError 6）——
       这正是旧实现只留一个字典、没有 `_reap` 所留下的空洞。
    """
    task = asyncio.ensure_future(_wait_quietly(proc))
    _reapers.add(task)
    task.add_done_callback(_reapers.discard)


def register(owner: Owner, *, pid: int, cmd: str, cwd: str) -> BackgroundProcess:
    return registry.register(owner, pid=pid, cmd=cmd, cwd=cwd)


def list_processes(owner: Owner) -> list[BackgroundProcess]:
    return registry.list(owner)


def kill(owner: Owner, pid: int) -> BackgroundProcess:
    return registry.kill(owner, pid)


def release(owner: Owner) -> int:
    return registry.release(owner)


def release_session(user_id: str, session_id: str) -> int:
    """删除会话时调用。幂等：没有登记过就返回 0。"""
    return registry.release(owner_of(user_id, session_id))


def release_all() -> int:
    return registry.release_all()


def recover() -> RecoveryReport:
    """处理上一次运行遗留的后台进程（`lifespan` 启动时调用一次）。"""
    return registry.recover()


def describe() -> list[dict]:
    """只读查看遗留记录（不带登记状态）。"""
    return registry.describe()


def describe_registry() -> dict:
    """只读查看登记状态与遗留记录（CLI `inspect`）。"""
    return registry.describe_registry()


def retry(pid: int) -> dict:
    """重新核验一条记录；确认归属才终结（CLI `retry`）。"""
    return registry.retry(pid)


def acknowledge(pid: int, reason: str) -> dict:
    """人工确认暂不处理（CLI `acknowledge`）：不杀进程、不删记录与审计。"""
    return registry.acknowledge(pid, reason)
