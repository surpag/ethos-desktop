"""ASRT（`@anthropic-ai/sandbox-runtime`）后端：把命令交给 `srt` 在沙箱内执行。

**为什么是 CLI 而不是 SDK**：ASRT 是 Node 库，本项目的执行链在 Python。用 `srt`
可执行文件是最小耦合点——不需要在 Python 里嵌一个 Node 运行时，也不需要让
Electron 的 Node（版本可能低于 ASRT 要求）参与执行。代价是拿不到库级 API
（比如 per-command 策略热更新），这一层用 settings 文件静态表达策略即可。

**失败语义**：`srt` 起不来、settings 校验失败、Windows 未执行 `windows-install`
——都以非零退出码返回。这里**不重试、不回落**，结果原样交给上层（fail-closed）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from ....config import data_root
from ..policy import SandboxPolicy

logger = logging.getLogger(__name__)

#: settings 落点：**必须在工作区之外**。放进工作区等于让被隔离的进程有机会
#: 改自己脚下的策略文件（下次执行就按被改过的策略跑）。
_SETTINGS_DIR_NAME = "isolation"

#: Windows 下命令脚本的落点（相对沙箱根）。必须在**可写根之内**：沙箱用户要能
#: 读它，而 `data_root()` 整个在 `denyRead` 里。
_SCRIPT_DIR_NAME = ".isolation-cmd"


def settings_dir() -> Path:
    return data_root() / _SETTINGS_DIR_NAME / "policies"


def ensure_settings(policy: SandboxPolicy) -> Path:
    """按策略指纹缓存 settings 文件，返回绝对路径。

    内容变化（策略语义没变但用户改了白名单）时重写：以**内容**为准而不是
    文件存在性，避免"文件在、内容旧"这种最难查的状态。
    """
    payload = json.dumps(policy.to_asrt_settings(), ensure_ascii=False, indent=2)
    target = settings_dir() / f"{policy.digest()[:16]}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != payload:
        tmp = target.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # 策略里含用户绝对路径，不必公开
        except OSError:  # pragma: no cover - Windows 语义不同，失败不影响功能
            pass
        os.replace(tmp, target)
    return target


def write_command_script(sandbox_root: Path, command: str) -> Path:
    """把要执行的命令写成脚本文件，返回路径（**仅 Windows 用**）。

    为什么不把命令直接塞进 `srt -c <cmdline>`：Windows 上 `srt` 把 `-c` 的字符串
    交给 `cmd.exe /d /s /c`，而 cmd 不是 POSIX shell —— 命令里的 `|`、`&`、`>`、
    `%VAR%` 会被 cmd 抢先解析（实测 `ls / | head -3` 变成 cmd 的管道，报
    "`head` 不是内部或外部命令"），引号规则也与 bash 完全不同。

    落成文件后，命令行里只剩两个路径，命令内容一个字节都不经过 cmd。
    """
    script_dir = Path(sandbox_root) / _SCRIPT_DIR_NAME
    script_dir.mkdir(parents=True, exist_ok=True)
    # 顺手清掉上次崩溃留下的脚本（超过 1 天才算陈旧，避免误删并发执行的）
    cutoff = time.time() - 86400
    for stale in script_dir.glob("*.sh"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:  # pragma: no cover - 并发删除/占用都不影响功能
            pass
    target = script_dir / f"{uuid.uuid4().hex}.sh"
    # newline="\n"：CRLF 会让 bash 把 `\r` 当命令的一部分（"command not found: xxx\r"）
    target.write_text(command, encoding="utf-8", newline="\n")
    return target


def _win_quote(token: str) -> str:
    """把单个参数变成 cmd / MSVCRT 都能原样传递的形式。

    只在含空白或引号时加引号：裸 token 走 cmd 的常规解析，最不容易出意外。
    结尾的连续反斜杠要加倍，否则 MSVCRT 会把它当成转义下一个引号的字符。
    """
    if token and not any(ch in token for ch in ' \t"'):
        return token
    trailing = len(token) - len(token.rstrip("\\"))
    body = token[: len(token) - trailing] + "\\" * (trailing * 2)
    return '"' + body.replace('"', '\\"') + '"'


def _windows_argv(
    *, srt_exe: str, settings_path: Path, shell_exe: str | None, script: Path | None, argv: list[str] | None
) -> list[str]:
    """Windows 的 `-c` 形态。

    **不能用 `-- <prog> <args...>`**：ASRT 的 CLI 在 Windows 上仍用 POSIX 的
    `quote()` 拼命令串（`dist/utils/shell-quote.js`），含空格或反斜杠的参数会被
    单引号包起来，交给 `cmd /d /s /c` 后单引号只是普通字符 —— 于是
    `"C:\\Program Files\\Git\\bin\\bash.exe"` 变成 cmd 眼里的 `'C:\\Program`，
    报 "The filename, directory name, or volume label syntax is incorrect"
    （极具误导性，曾被误判为权限问题）。`-c` 模式原样使用调用方给的字符串，
    引号由我们自己按 cmd 的规则拼。
    """
    head = [srt_exe, "--settings", str(settings_path), "-c"]
    if argv:
        return head + [" ".join(_win_quote(tok) for tok in argv)]
    if shell_exe:
        if script is None:
            raise ValueError("Windows shell 执行需要 script（见 write_command_script）")
        return head + [f"{_win_quote(shell_exe)} {_win_quote(str(script))}"]
    raise ValueError("build_argv 需要 argv 或 shell_exe 之一")  # pragma: no cover


def build_argv(
    *,
    srt_exe: str,
    settings_path: Path,
    shell_exe: str | None,
    command: str,
    argv: list[str] | None = None,
    script: Path | None = None,
) -> list[str]:
    """组装 `srt` 的调用参数。

    POSIX：`srt --settings <file> -- <program> [args...]`。`--` 是必须的：ASRT 用
    commander 的 `[command...]` 接收命令，没有分隔符时命令里的 `--debug` 之类会被
    srt 自己吃掉，表现为"命令参数神秘消失"。

    Windows：`srt --settings <file> -c <cmdline>`，原因见 `_windows_argv`。
    `script` 是 `write_command_script` 的产物（shell 执行时必需）。

    传 `argv` 时直接执行该程序（例如 `git push`），不经 shell：git 的推送流程
    不需要 shell 语义，少一层解析就少一类引号注入问题。
    """
    if sys.platform == "win32":
        return _windows_argv(
            srt_exe=srt_exe,
            settings_path=settings_path,
            shell_exe=shell_exe,
            script=script,
            argv=argv,
        )
    head = [srt_exe, "--settings", str(settings_path), "--"]
    if argv:
        return head + list(argv)
    if shell_exe:
        return head + [shell_exe, "-c", command]
    raise ValueError("build_argv 需要 argv 或 shell_exe 之一")  # pragma: no cover


def srt_win_exe(srt_exe: str | Path) -> Path | None:
    """从 `srt.cmd`（npm shim）推导 vendored 的 `srt-win.exe`。

    只在清理 ACL 时直接调它：`acl forget <PATH>` 是 per-root 的，不需要管理员
    （0.1s/个），可以放在测试 teardown 里，避免把持久授权留在会被删掉的临时目录上。
    """
    base = Path(srt_exe).resolve().parent
    for candidate in (
        base / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "vendor" / "srt-win" / "x64" / "srt-win.exe",
        base / "vendor" / "srt-win" / "x64" / "srt-win.exe",
        base.parent / "vendor" / "srt-win" / "x64" / "srt-win.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def install_hint() -> str:
    """给用户看的安装指引（tool 错误信息里直接用这段）。"""
    return (
        "安装并初始化 ASRT 后重试：\n"
        "  npm i -g @anthropic-ai/sandbox-runtime\n"
        "  npx @anthropic-ai/sandbox-runtime windows-install   # Windows：UAC 一次，创建本地账户 + WFP 过滤\n"
        "# 若之前**反复超时**过（尤其命令被强制结束），srt 的 ACL 状态会卡在死循环里，\n"
        "# 之后每条命令都停在初始化。用**管理员**终端执行一次即可恢复：\n"
        "  <node 全局目录>\\@anthropic-ai\\sandbox-runtime\\vendor\\srt-win\\x64\\srt-win.exe "
        "acl forget --all --force\n"
        "注意：沙箱不可用时命令会被**拒绝**执行，不会回落到宿主直接运行。"
    )
