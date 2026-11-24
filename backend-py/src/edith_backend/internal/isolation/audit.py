"""沙箱执行的审计与环境变量收口。

两条铁律：

1. **审计里不出现凭据**。命令文本先做敏感模式脱敏（`--token xxx`、`KEY=value`），
   环境变量只记**键名**不记值，策略只记指纹不记绝对路径。
2. **环境变量按白名单传递**。沙箱进程默认继承 `os.environ` 的老做法会把
   `OPENAI_API_KEY`、`AWS_SECRET_ACCESS_KEY`、`HTTP_PROXY` 一起带进去——
   那等于把宿主的身份交给被隔离的进程。

request_id 不在这里显式记录：它由 `main.py` 的日志过滤器挂到每条日志上，
审计走同一个 logger，自动带上，不必也不该由调用方拼字符串。
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

logger = logging.getLogger("edith.isolation.audit")

#: 当前正在执行的工具调用 id。由 `SandboxToolSet.execute()` 绑定，
#: 隔离层在 `record_*` 里读取——工具层不需要把 id 一路当参数传到 command 层，
#: 也不会因为漏传而让审计里出现"不知道是哪次调用"的记录。
_TOOL_CALL: ContextVar[str] = ContextVar("ethos_tool_call_id", default="")


def bind_tool_call(tool_call_id: str):
    """在 `execute()` 进入时绑定，返回的 token 交给 `reset_tool_call` 复原。"""
    return _TOOL_CALL.set(tool_call_id or "")


def reset_tool_call(token) -> None:
    _TOOL_CALL.reset(token)


def current_tool_call() -> str:
    return _TOOL_CALL.get()

#: 允许透传给沙箱进程的环境变量。**必须显式列出**：漏掉的只是"功能不可用"，
#: 多带一个就是凭据泄漏，两种错误的代价不对称。
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "HOME",
        "USERPROFILE",
        # 这几个是**路径**不是凭据，且 `srt` 自身的初始化需要（缺 LOCALAPPDATA
        # 时 `srt-win acl grant` 直接失败）。沙箱内的可读范围由文件策略决定，
        # 传进路径变量不构成授权。
        "LOCALAPPDATA",
        "APPDATA",
        "ProgramData",
        "ProgramFiles",
        "SystemDrive",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
    }
)

#: 名字命中即丢弃：即使将来有人把它加进白名单，也不该带凭据进去。
_ENV_SECRET_PATTERN = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"^AWS_|^OPENAI_|^ANTHROPIC_|^EDITH_|^ETHOS_|PROXY)",
    re.IGNORECASE,
)

#: 命令文本里的凭据形态：`--token xxx`、`Authorization: Bearer xxx`、`KEY=xxx`。
#: `((?:bearer|basic|token)\s+)?` 这一段不能省：`Authorization: Bearer abc` 里
#: 紧跟冒号的是**认证方案**而不是凭据，漏掉它会把 `abc` 原样留在审计里。
_COMMAND_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization|bearer)\b"
    r"(\s*[=:]\s*|\s+)((?:bearer|basic|token)\s+)?(\S+)"
)

_MAX_COMMAND_CHARS = 200


def filter_env(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """按白名单裁剪环境变量，返回 (保留的 env, 被丢弃的键名)。

    丢弃的**只有键名**进审计——值本身可能就是一个密钥。

    **白名单比较必须大小写不敏感**：Windows 上 `os.environ` 的键是 `Path` /
    `SystemRoot` / `windir` / `ProgramData`，而白名单写的是 `PATH` / `SYSTEMROOT`。
    按大小写精确比较的后果实测过：`Path` 被当成敏感变量丢掉，沙箱里连 `git`
    都找不到——功能整体不可用，但日志上只是"丢了一个变量"，极难发现。
    """
    allow = {key.casefold() for key in _ENV_ALLOWLIST}
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in env.items():
        if key.casefold() in allow and not _ENV_SECRET_PATTERN.search(key):
            kept[key] = value
        else:
            dropped.append(key)
    return kept, tuple(sorted(dropped))


def command_summary(command: str) -> str:
    """命令的一行摘要（脱敏 + 截断），用于审计与工具日志。"""
    text = (command or "").strip().splitlines()
    first = text[0] if text else ""
    redacted = _COMMAND_SECRET_PATTERN.sub(r"\1\2\3***", first)
    if len(redacted) > _MAX_COMMAND_CHARS:
        redacted = redacted[: _MAX_COMMAND_CHARS - 1] + "…"
    return redacted


def record_start(
    *,
    backend: str,
    backend_version: str,
    policy_digest: str,
    policy_version: str,
    command: str,
    cwd: str,
    workspace_root: str,
    session_id: str = "",
    workspace_id: str = "",
    dropped_env: tuple[str, ...] = (),
    injected_env: tuple[str, ...] = (),
    purpose: str = "shell",
    background: bool = False,
) -> None:
    logger.info(
        "sandbox_exec_start backend=%s backend_version=%s policy=%s/%s purpose=%s "
        "session=%s workspace=%s tool_call=%s cwd=%s root=%s command=%r "
        "dropped_env=%s injected_env=%s background=%s",
        backend,
        backend_version or "-",
        policy_version,
        policy_digest[:12],
        purpose,
        session_id or "-",
        workspace_id or "-",
        current_tool_call() or "-",
        cwd,
        workspace_root or "-",
        command_summary(command),
        ",".join(dropped_env) or "-",
        ",".join(injected_env) or "-",
        background,
    )


def record_finish(
    *,
    backend: str,
    command: str,
    exit_code: int,
    duration_ms: float,
    outcome: str,
) -> None:
    """`outcome` 取 `exited` / `timeout` / `canceled` / `failed`。"""
    logger.info(
        "sandbox_exec_end backend=%s outcome=%s exit=%s duration_ms=%.1f command=%r",
        backend,
        outcome,
        exit_code,
        duration_ms,
        command_summary(command),
    )


def record_refusal(*, reason: str, detail: str, command: str = "") -> None:
    """拒绝执行（后端缺失 / 策略非法 / 初始化失败）。这是**安全事件**，用 warning。"""
    logger.warning(
        "sandbox_refused reason=%s detail=%s command=%r",
        reason,
        detail[:400],
        command_summary(command),
    )


def record_cleanup(*, pid: int, ok: bool, detail: str = "") -> None:
    logger.info("sandbox_cleanup pid=%s ok=%s detail=%s", pid, ok, detail[:200])
