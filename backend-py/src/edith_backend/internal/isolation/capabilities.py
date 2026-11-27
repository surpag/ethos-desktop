"""隔离后端的能力探测。

探测结果决定**能不能执行**，不是"用哪种更舒服"：探测不到后端就拒绝执行。
所以这里的每条结论都要能被复现——记下可执行文件的绝对路径与版本号，
而不是一个布尔值。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: ASRT 的 npm 包名（诊断文案里引用，避免文档与代码各写一份）
ASRT_PACKAGE = "@anthropic-ai/sandbox-runtime"
#: ASRT 的 Node 版本下限（见包的 engines 字段）。低于它 `srt` 启动即失败。
ASRT_MIN_NODE = (22, 12)
#: 探测命令的超时：`srt --version` 要起一个 Node 进程，冷启动可能上秒级
PROBE_TIMEOUT_SECONDS = 15
#: 行为验证的超时。**不是"容忍卡死"的预算，而是"覆盖首次建授权"的预算**：
#: 实测同一条 `echo` 首次 23.8s（`acl grant --persistent`）、授权建好后 3.9s。
#: 定成 25s 会让通过与否取决于当时的机器负载——那是最糟的一类失败：同一台机器
#: 时好时坏，用户只能反复重装。120s 是 4.7 倍余量，真卡死（600s 那个 bug）仍会被
#: 判失败，只是要等久一点。
VERIFY_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class BackendCapability:
    """一个后端在某台机器上的**事实**（不是配置意图）。"""

    name: str
    #: `asrt` / `appcontainer` / `unsafe-host`
    kind: str
    available: bool
    #: 可执行文件绝对路径或后端标识
    location: str = ""
    version: str = ""
    #: 不可用的原因（可用时为空）
    detail: str = ""
    #: 该后端**做不到**的事（能力缺口；如实上报，不假设已防护）
    limitations: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "location": self.location,
            "version": self.version,
            "detail": self.detail,
            "limitations": list(self.limitations),
        }


class ProbeError(RuntimeError):
    """探测命令本身跑不起来（不是"后端不可用"，是"这台机器连探测都做不到"）。"""


def _node_version() -> tuple[int, int] | None:
    """`node -v` 的主次版本。探测失败返回 None（不抛错：Node 缺失是常见状态）。"""
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace").strip().lstrip("v")
    parts = text.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def find_srt() -> str | None:
    """定位 `srt`。先看 PATH，再看 npm 全局前缀（桌面端 PATH 往往不含它）。"""
    found = shutil.which("srt")
    if found:
        return found
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        proc = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not prefix:
        return None
    for name in ("srt.cmd", "srt.exe", "srt"):
        candidate = Path(prefix) / name
        if candidate.is_file():
            return str(candidate)
    return None


def probe_asrt() -> BackendCapability:
    """探测 ASRT：可执行文件 + Node 版本 + `--version` 是否真的能跑。"""
    limitations = (
        "Windows 后端为 Alpha；schannel 类工具（PowerShell Invoke-WebRequest、"
        "curl.exe、.NET HttpClient、dotnet restore、winget、schannel 后端的 git）在沙箱内不可用",
        "Windows 首次使用需执行 `npx @anthropic-ai/sandbox-runtime windows-install`（UAC 一次，"
        "创建本地账户并安装机器级 WFP 过滤器）；升级后必须重跑",
        "allowWrite 的根授权跨会话保留：同机其他沙箱会话可读这些根（写仍受限）",
        "域名白名单在代理层判定，非 HTTP(S)/SOCKS 感知的程序可能被阻断或绕过",
    )
    exe = find_srt()
    if not exe:
        return BackendCapability(
            name=ASRT_PACKAGE,
            kind="asrt",
            available=False,
            detail=f"未找到 srt 可执行文件（npm i -g {ASRT_PACKAGE}）",
            limitations=limitations,
        )
    node = _node_version()
    if node is None:
        return BackendCapability(
            name=ASRT_PACKAGE,
            kind="asrt",
            available=False,
            location=exe,
            detail="未找到 node：srt 是 Node CLI，无法启动",
            limitations=limitations,
        )
    if node < ASRT_MIN_NODE:
        return BackendCapability(
            name=ASRT_PACKAGE,
            kind="asrt",
            available=False,
            location=exe,
            detail=(
                f"node {node[0]}.{node[1]} 低于 srt 要求的 "
                f"{ASRT_MIN_NODE[0]}.{ASRT_MIN_NODE[1]}"
            ),
            limitations=limitations,
        )
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BackendCapability(
            name=ASRT_PACKAGE,
            kind="asrt",
            available=False,
            location=exe,
            detail=f"srt --version 无法执行：{exc}",
            limitations=limitations,
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        return BackendCapability(
            name=ASRT_PACKAGE,
            kind="asrt",
            available=False,
            location=exe,
            detail=f"srt --version 退出码 {proc.returncode}：{stderr}",
            limitations=limitations,
        )
    version = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return BackendCapability(
        name=ASRT_PACKAGE,
        kind="asrt",
        available=True,
        location=exe,
        version=version[0] if version else "",
        limitations=limitations,
    )


def _kill_tree(pid: int) -> None:
    """连同孙子进程一起结束。

    只 kill 父进程不够：`srt` 会拉起 `srt-win`，孙子进程仍持有 stdout 管道，
    `communicate()` 会一直等到管道关闭——实测超时设 25s 却能卡 190s。
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


def _run_capture(argv: list[str], timeout: int) -> tuple[int, bytes, bytes, bool]:
    """带**进程树**清理的超时执行，返回 (returncode, stdout, stderr, timed_out)。"""
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode or 0, out, err, False
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            proc.kill()
        return proc.returncode or -1, b"", b"", True


def _shell_exe() -> str | None:
    """挑一个与生产路径一致的 shell（返回可执行文件路径）。

    顺序必须与 `sandbox/local.py::_find_posix_shell` 一致：**先看 Git for Windows
    的固定安装路径**，最后才查 PATH。原因：Windows 上 `shutil.which("bash")` 会先
    命中 `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe`——那是 WSL 的应用执行别名
    桩，在沙箱里跑不起来。用它做验证会得出"后端坏了"的错误结论。

    找不到 POSIX shell 时返回 None：生产路径会以 NOT_SUPPORTED 拒绝执行，验证
    也应当失败，而不是退到 `cmd` 验出一个假阳性。
    """
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(candidate).is_file():
                return candidate
        found = shutil.which("bash")
        if found and "windowsapps" not in found.lower() and "system32" not in found.lower():
            return found
        return None
    return shutil.which("bash") or "/bin/sh"


def _default_probe_root() -> Path:
    """验证用的根必须落在**应用真实数据区**，不能用系统临时目录。

    实测教训：ASRT 在本机对 `%TEMP%` 下的根会跳过持久 ACL 授权（直接可用），
    而对真实工作区根会卡在 `srt-win acl grant --persistent`。如果用临时目录做
    验证，会得到"可用"的假阳性，用户随后每条命令都吊死——比不验证更糟。
    """
    from ...config import data_root

    root = data_root() / "isolation" / "probe"
    root.mkdir(parents=True, exist_ok=True)
    return root


def verify_asrt(
    capability: BackendCapability,
    *,
    timeout: int = VERIFY_TIMEOUT_SECONDS,
    probe_root: Path | None = None,
) -> tuple[bool, str]:
    """**行为验证**：真在沙箱里跑一条 `echo`，看它是否返回。

    为什么不能只信 `--version`：实测（Windows 10 19045，未提权）`srt --version`
    正常返回 0.0.83，但真正执行时卡在 `srt-win acl grant --persistent` 上，
    直到它自己的 600 秒超时才失败。`available=true` 在这种机器上是**假阳性**，
    会让用户以为隔离生效，实际每条命令都吊死 10 分钟。

    **超时阈值必须覆盖"首次建授权"的正常开销**：实测同一条 `echo`，首次
    （要 `acl grant --persistent`）23.8s，授权建好后 3.9s。曾经把阈值定成 25s，
    结果验证通过与否取决于机器负载——应用启动时 Electron/DB 一起抢 IO 就翻过
    25s，被误判成"后端坏了"，再被 `_failure` 缓存放大成"整个会话不能执行"。
    所以阈值取 `VERIFY_TIMEOUT_SECONDS`，**靠近但不超过它不算失败**。

    所以：探测分两级——`--version` 决定"装没装"，这里决定"能不能用"。
    验证失败即视为不可用（fail-closed），并且把真实原因带进错误信息。
    """
    # **必须用真实执行链的形态**：生产路径在 Windows 上是
    # `srt -c '"<bash>" "<脚本>"'`（见 `backends/asrt.py`）。这里直接复用
    # `build_argv`，否则"验证通过但生产失败"会成为常态——两种形态在 cmd 的
    # 引号解析上完全不同。
    root = Path(probe_root) if probe_root else _default_probe_root()
    root.mkdir(parents=True, exist_ok=True)
    settings = root / "settings.json"
    # allowRead 只放验证根：**系统工具链不能进 allowRead**（普通用户对
    # Program Files / System32 没有 WRITE_DAC，`acl grant` 会整批失败，srt
    # 直接起不来）。详见 `policy.READ_ONLY_ROOTS_WARNING`。
    payload = {
        "network": {"allowedDomains": [], "deniedDomains": []},
        "filesystem": {
            "allowWrite": [str(root)],
            "denyWrite": [],
            "denyRead": [],
            "allowRead": [str(root)],
        },
    }
    settings.write_text(json.dumps(payload), encoding="utf-8")

    from .backends import asrt

    probe_command = "echo ethos-sandbox-probe"
    shell_exe = _shell_exe()
    if shell_exe is None:
        return False, "未找到 POSIX shell（Windows 需要 Git for Windows 的 bash）"
    script = None
    if sys.platform == "win32":
        script = asrt.write_command_script(root, probe_command)
    argv = asrt.build_argv(
        srt_exe=capability.location,
        settings_path=settings,
        shell_exe=shell_exe,
        command=probe_command,
        script=script,
    )
    try:
        returncode, out, err, timed_out = _run_capture(argv, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"沙箱行为验证无法执行：{exc}"

    if timed_out:
        return False, (
            f"沙箱行为验证超时（>{timeout}s）：srt 在初始化阶段没有返回。"
            "注意 Windows 上**首次**建目录授权（`srt-win acl grant --persistent`）"
            "本身就要 20s 以上，这属于正常开销；超过 "
            f"{timeout}s 才说明真的卡住。常见原因：工作区/数据目录落在受保护位置"
            "（Program Files、OneDrive 同步目录），或以普通权限首次建持久授权失败。"
            "**不要**一上来就 `acl forget`——那会清掉已建好的授权，下次再慢一遍；"
            "只有每条命令都这样、持续多轮之后才按 ACL 恢复步骤处理。"
        )
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace").strip()
    if returncode != 0:
        return False, f"沙箱内 echo 退出码 {returncode}：{stderr[:400]}"
    if "ethos-sandbox-probe" not in stdout:
        return False, f"沙箱内 echo 没有返回预期输出：{stdout[:200]!r}"
    return True, ""


def probe_appcontainer() -> BackendCapability:
    """自研 AppContainer 后端的能力探测（尚未实现，如实报不可用）。

    宁可报"没有"，也不要报"有"然后回落到宿主执行——那正是 spec 里禁止的
    静默降级。实现之后这里会改成真实的 API 可用性探测。
    """
    return BackendCapability(
        name="ethos-appcontainer",
        kind="appcontainer",
        available=False,
        detail="AppContainer 后端尚未实现（下一阶段）",
        limitations=(
            "不支持域名白名单（无代理层，只有内核级默认拒网）",
            "Windows 10 上无法限制 CPU 配额以外的 IO 优先级",
        ),
    )


def unsupported_platform() -> BackendCapability:
    return BackendCapability(
        name="none",
        kind="none",
        available=False,
        detail=f"当前平台 {sys.platform} 没有可用的隔离后端",
    )


def report() -> dict:
    """给设置页 / 健康检查用的完整报告（不含任何策略内容与用户路径）。"""
    return {
        "platform": sys.platform,
        "backends": [probe_asrt().as_dict(), probe_appcontainer().as_dict()],
        "node": os.environ.get("ETHOS_NODE_VERSION", ""),
    }
