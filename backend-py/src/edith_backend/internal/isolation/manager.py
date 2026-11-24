"""Sandbox Manager：后端选择、策略生成、执行计划与 fail-closed 判定。

**唯一的执行入口**。`LocalCommands` 不再自己决定怎么起进程——它只把"要跑什么、
在哪个目录、什么环境"交给这里，拿回一个 `ExecPlan`（argv + 裁剪后的 env）。

后端选择规则（`ETHOS_SANDBOX_BACKEND`）：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | ASRT → AppContainer → 允许时才用 unsafe-host → 否则**拒绝** |
| `asrt` | 必须可用，否则拒绝（不悄悄换别的） |
| `appcontainer` | 必须可用，否则拒绝 |
| `host` | 仅在 `ETHOS_SANDBOX_ALLOW_UNSAFE_HOST=1` 时可用（测试/开发），并打 warning |
| `off` | 一律拒绝执行 |

`off` 不是"关掉沙箱直接跑"，而是"关掉执行能力"——这是本模块最重要的一条语义：
**没有任何配置组合能让命令在无隔离的情况下被执行**，除非显式打开那个带
`ALLOW_UNSAFE` 字样的开关（它只服务于 pytest，文档里单独标注）。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import log_context
from . import audit, store, win_acl
from .backends import asrt
from .capabilities import (
    BackendCapability,
    probe_appcontainer,
    probe_asrt,
    verify_asrt,
)
from .errors import SandboxUnavailable
from .policy import SandboxPolicy, build_policy, windows_acl_denied_paths

logger = logging.getLogger(__name__)

BACKEND_ENV = "ETHOS_SANDBOX_BACKEND"
UNSAFE_HOST_ENV = "ETHOS_SANDBOX_ALLOW_UNSAFE_HOST"
ALLOWED_DOMAINS_ENV = "ETHOS_SANDBOX_ALLOWED_DOMAINS"

UNSAFE_HOST = "unsafe-host"


@dataclass(frozen=True)
class ExecPlan:
    """一次执行的最终计划。冻结：拿到之后没有入口能改 argv / env。"""

    backend: str
    backend_version: str
    policy_digest: str
    policy_version: str
    argv: list[str]
    env: dict[str, str]
    command: str = ""
    dropped_env: tuple[str, ...] = ()
    #: 服务端显式送入的环境变量**键名**（例如 git 凭据）。只记名字，值永不落盘。
    injected_env: tuple[str, ...] = ()
    workspace_root: str = ""
    session_id: str = ""
    workspace_id: str = ""
    tool_call_id: str = ""
    purpose: str = "shell"
    limitations: tuple[str, ...] = field(default=())
    #: Windows 下承载本次命令的脚本文件（`finish` 负责删除）
    script_path: str = ""

    @property
    def uses_shell(self) -> bool:
        return len(self.argv) >= 3 and self.argv[-2] == "-c"


#: 进程级缓存：探测一次就够。`reset()` 供测试与"装完 ASRT 后立刻生效"使用。
_resolved: BackendCapability | None = None
#: 失败也缓存：否则每次工具调用都会重新跑 `srt --version` / `npm prefix -g`
#: （每个命令多几百毫秒到几秒），而重装后端本来就要求重启应用。
_failure: tuple[str, str] | None = None


def reset() -> None:
    global _resolved, _failure
    _resolved = None
    _failure = None


def _requested() -> str:
    return os.environ.get(BACKEND_ENV, "auto").strip().lower() or "auto"


def _unsafe_host_allowed() -> bool:
    return os.environ.get(UNSAFE_HOST_ENV, "").strip() == "1"


def _unsafe_host_capability() -> BackendCapability:
    return BackendCapability(
        name=UNSAFE_HOST,
        kind="host",
        available=_unsafe_host_allowed(),
        location="host",
        detail=(
            "显式开放在宿主直接执行（仅测试/开发）"
            if _unsafe_host_allowed()
            else f"未设置 {UNSAFE_HOST_ENV}=1"
        ),
        limitations=(
            "没有任何 OS 级隔离：进程以当前用户身份运行，可读写整个用户目录",
            "仅用于自动化测试，禁止在分发包中开启",
        ),
    )


def resolve() -> BackendCapability:
    """选定后端并缓存。不可用时抛 `SandboxUnavailable`（而不是返回 None）。"""
    global _resolved, _failure
    if _resolved is not None:
        return _resolved
    if _failure is not None:
        message, reason = _failure
        raise SandboxUnavailable(message, reason=reason)

    requested = _requested()
    if requested in ("off", "none", "disabled"):
        raise SandboxUnavailable(
            f"{BACKEND_ENV}={requested}：执行能力已显式关闭",
            reason=SandboxUnavailable.DISABLED,
        )

    candidates: list[BackendCapability]
    if requested == "asrt":
        candidates = [probe_asrt()]
    elif requested == "appcontainer":
        candidates = [probe_appcontainer()]
    elif requested in ("host", UNSAFE_HOST):
        candidates = [_unsafe_host_capability()]
    else:
        # auto：按 spec 的优先级 —— 成熟原生实现优先，自研后端其次
        candidates = [probe_asrt(), probe_appcontainer()]
        if _unsafe_host_allowed():
            candidates.append(_unsafe_host_capability())

    failures: list[str] = []
    verify_failed = False
    for candidate in candidates:
        if not candidate.available:
            continue
        # 二级探测：**真跑一次**再认定可用。`srt --version` 通过但初始化卡死的
        # 机器是真实存在的（Windows 未提权时 acl grant 会挂到 600s 超时）。
        if candidate.kind == "asrt":
            ok, detail_text = verify_asrt(candidate)
            if not ok:
                audit.record_refusal(
                    reason=SandboxUnavailable.INIT_FAILED,
                    detail=f"ASRT 行为验证失败：{detail_text}",
                )
                failures.append(f"{candidate.name}: {detail_text}")
                verify_failed = True
                continue
            # 家目录敏感目录的读拒绝**不能**交给 ASRT：它要给 parent（用户主目录）
            # 加 ACE，而那一步在 srt-win 0.0.83 上会空转（见 win_acl 模块）。
            # 这里自己加固；加不上就拒绝执行——否则沙箱进程能读到 SSH 私钥。
            acl_ok, acl_detail = win_acl.ensure_deny(windows_acl_denied_paths())
            if not acl_ok:
                audit.record_refusal(
                    reason=SandboxUnavailable.INIT_FAILED,
                    detail=f"敏感目录 ACL 加固失败：{acl_detail}",
                )
                failures.append(f"{candidate.name}: {acl_detail}")
                verify_failed = True
                continue
            # 状态文件只是缓存：与真实 DACL 对一次账（收养上次崩在写状态之前的 ACE）
            rec_ok, rec_detail = win_acl.reconcile(windows_acl_denied_paths())
            if not rec_ok:
                logger.warning("sandbox_acl_reconcile_failed detail=%s", rec_detail)
        _resolved = candidate
        if candidate.kind == "host":  # pragma: no cover - 仅测试路径
            logger.warning(
                "sandbox backend=unsafe-host：命令将在宿主机直接执行（仅测试/开发）"
            )
        return candidate

    detail = "；".join(
        [*(f"{c.name}: {c.detail}" for c in candidates if c.detail), *failures]
    )
    platform_hint = "" if sys.platform == "win32" else f"（当前平台 {sys.platform}）"
    message = f"没有可用的隔离后端{platform_hint}，命令已拒绝执行。{detail}\n{asrt.install_hint()}"
    # 装是装了但验证不过 → 这是"初始化失败"，不是"没装"：两者的修复动作完全不同
    reason = SandboxUnavailable.INIT_FAILED if verify_failed else SandboxUnavailable.NOT_INSTALLED
    _failure = (message, reason)
    raise SandboxUnavailable(message, reason=reason)


def _allowed_domains() -> tuple[str, ...]:
    raw = os.environ.get(ALLOWED_DOMAINS_ENV, "")
    return tuple(
        item.strip() for item in raw.replace(";", ",").split(",") if item.strip()
    )


def health() -> dict:
    """健康检查：只报告能力与选择，不读策略内容、不暴露用户路径。"""
    requested = _requested()
    try:
        chosen = resolve()
        return {
            "requested": requested,
            "backend": chosen.kind,
            "name": chosen.name,
            "version": chosen.version,
            "available": True,
            "limitations": list(chosen.limitations),
        }
    except SandboxUnavailable as exc:
        return {
            "requested": requested,
            "backend": "",
            "available": False,
            "reason": exc.reason,
            "message": str(exc),
        }


def plan(
    *,
    sandbox_root: Path,
    cwd: Path,
    command: str,
    env: dict[str, str],
    shell_exe: str | None,
    argv: list[str] | None = None,
    bound_root: str | None = None,
    session_id: str = "",
    workspace_id: str = "",
    background: bool = False,
    extra_writable: tuple[str, ...] = (),
    extra_domains: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    purpose: str = "shell",
) -> ExecPlan:
    """生成本次执行计划；后端不可用时抛 `SandboxUnavailable`（fail-closed）。

    `argv` / `extra_writable` / `extra_domains` / `extra_env` 只给**服务端自己的**
    执行流程用（例如 git push 需要临时仓库可写、只对远端域名放行、且凭据必须进
    环境变量）。工具参数不参与这些值，模型无法借此扩大权限。
    """
    if not shell_exe and not argv:
        # 没有 shell 就没有可执行的命令形态。这里不能"退化成直接 exec 整串命令"
        # （那会把 `cmd1 && cmd2` 当成一个不存在的可执行文件），直接拒绝更诚实。
        raise SandboxUnavailable(
            "未找到可用的 shell（Windows 需要 Git for Windows 的 bash）",
            reason=SandboxUnavailable.NOT_SUPPORTED,
        )
    backend = resolve()

    if backend.kind == "asrt":
        # **每次生成 ExecPlan 前**都核对真实 DACL。目录可能在 resolve 之后才出现
        # （srt 的占位目录、用户新装 AWS CLI/Azure CLI），只在启动时加固一次不够。
        targets = windows_acl_denied_paths()
        acl_ok, acl_detail = win_acl.verify_all(targets)
        if not acl_ok:
            fixed, fix_detail = win_acl.ensure_deny(targets)
            acl_ok, acl_detail = (
                win_acl.verify_all(targets) if fixed else (False, fix_detail)
            )
        if not acl_ok:
            audit.record_refusal(
                reason=SandboxUnavailable.INIT_FAILED,
                detail=f"敏感目录 ACL 校验失败：{acl_detail}",
            )
            raise SandboxUnavailable(
                f"敏感目录 ACL 校验失败，命令已拒绝执行：{acl_detail}",
                reason=SandboxUnavailable.INIT_FAILED,
            )

    policy = build_policy(
        sandbox_root=sandbox_root,
        cwd=cwd,
        bound_root=bound_root,
        # 进程级白名单 + 本次执行的服务端追加（如 git 远端域名）
        allowed_domains=tuple(dict.fromkeys((*_allowed_domains(), *extra_domains))),
        extra_writable=extra_writable,
    )

    if backend.kind == "host":
        # 只有显式开关才会走到这里。**不裁剪 env**：测试依赖继承的环境变量，
        # 而这条路径的风险已经被开关本身承认了。
        plan_ = ExecPlan(
            backend=backend.kind,
            backend_version=backend.version,
            policy_digest=policy.digest(),
            policy_version=policy.policy_version,
            argv=list(argv) if argv else [shell_exe, "-c", command],
            env=dict(env),
            command=command,
            workspace_root=policy.workspace_root,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_call_id=audit.current_tool_call(),
            purpose=purpose,
            limitations=backend.limitations,
        )
    elif backend.kind == "asrt":
        settings = asrt.ensure_settings(policy)
        safe_env, dropped = audit.filter_env(env)
        # 沙箱内的临时目录：TEMP/TMP 指到工作区里的私有目录，别让构建缓存
        # 落到宿主用户目录（否则"清理临时资源"根本清不干净）。
        tmp = str(sandbox_root / ".isolation-tmp")
        safe_env["TEMP"] = tmp
        safe_env["TMP"] = tmp
        if extra_env:
            # 服务端显式送入的变量（目前只有 git 凭据）：绕过白名单但**只记键名**。
            # 值绝不进日志/审计，argv 里也不会出现。
            safe_env.update({str(k): str(v) for k, v in extra_env.items()})
        # Windows 上命令必须落成脚本文件：srt 的 `-c` 会交给 cmd 解析，命令里的
        # `|`/`&`/`%VAR%` 会被 cmd 抢走（见 asrt.write_command_script）。
        script = None
        if sys.platform == "win32" and shell_exe and not argv:
            script = asrt.write_command_script(Path(sandbox_root), command)
        plan_ = ExecPlan(
            backend=backend.kind,
            backend_version=backend.version,
            policy_digest=policy.digest(),
            policy_version=policy.policy_version,
            argv=asrt.build_argv(
                srt_exe=backend.location,
                settings_path=settings,
                shell_exe=shell_exe,
                command=command,
                argv=argv,
                script=script,
            ),
            script_path=str(script) if script is not None else "",
            env=safe_env,
            command=command,
            dropped_env=dropped,
            injected_env=tuple(sorted(extra_env)) if extra_env else (),
            workspace_root=policy.workspace_root,
            session_id=session_id,
            workspace_id=workspace_id,
            tool_call_id=audit.current_tool_call(),
            purpose=purpose,
            limitations=backend.limitations,
        )
    else:  # pragma: no cover - 目前只有上面两种 kind
        raise SandboxUnavailable(
            f"未知的隔离后端类型：{backend.kind}", reason=SandboxUnavailable.INIT_FAILED
        )

    audit.record_start(
        backend=plan_.backend,
        backend_version=plan_.backend_version,
        policy_digest=plan_.policy_digest,
        policy_version=plan_.policy_version,
        command=command,
        cwd=str(cwd),
        workspace_root=plan_.workspace_root,
        session_id=session_id,
        workspace_id=workspace_id,
        dropped_env=plan_.dropped_env,
        injected_env=plan_.injected_env,
        purpose=purpose,
        background=background,
    )
    return plan_


def finish(plan_: ExecPlan, *, exit_code: int, duration_ms: float, outcome: str) -> None:
    audit.record_finish(
        backend=plan_.backend,
        command=plan_.command,
        exit_code=exit_code,
        duration_ms=duration_ms,
        outcome=outcome,
    )
    # 命令脚本只在本次执行期间有意义；留着等于把用户命令长期写在沙箱树里
    if plan_.script_path:
        try:
            Path(plan_.script_path).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 删不掉只影响整洁，不影响结果
            logger.debug("sandbox_script_cleanup_failed path=%s", plan_.script_path)


async def record(
    plan_: ExecPlan,
    *,
    exit_code: int | None,
    duration_ms: float | None,
    outcome: str,
    pid: int | None = None,
    tool_name: str = "",
    reason: str = "",
) -> None:
    """落库一条审计记录。**不影响命令结果**：写库失败只记日志。

    反过来的取舍是错的：因为审计写不进去就把已经成功的命令判成失败，
    会让用户在磁盘满/库锁住时丢掉工作成果。
    """
    target = store.current()
    if target is None:
        return
    entry = {
        "created_at": int(time.time() * 1000),
        "request_id": log_context.current_request_id(),
        "session_id": plan_.session_id,
        "workspace_id": plan_.workspace_id,
        "tool_call_id": plan_.tool_call_id,
        "tool_name": tool_name,
        "purpose": plan_.purpose,
        "command": audit.command_summary(plan_.command),
        "backend": plan_.backend,
        "backend_version": plan_.backend_version,
        "policy_version": plan_.policy_version,
        "policy_digest": plan_.policy_digest,
        "workspace_root": plan_.workspace_root,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "outcome": outcome,
        "pid": pid,
        "dropped_env": list(plan_.dropped_env),
        "injected_env": list(plan_.injected_env),
        "reason": reason,
    }
    try:
        await target.record(entry)
    except Exception:  # noqa: BLE001 - 审计失败不能回滚已完成的执行
        logger.warning("sandbox_audit_write_failed", exc_info=True)


async def record_refusal(
    exc: SandboxUnavailable,
    *,
    command: str,
    session_id: str = "",
    workspace_id: str = "",
    tool_name: str = "",
) -> None:
    """记录一次**被拒绝**的执行。没有 plan，所以字段由这里自己拼。"""
    audit.record_refusal(reason=exc.reason, detail=str(exc), command=command)
    target = store.current()
    if target is None:
        return
    try:
        await target.record(
            {
                "created_at": int(time.time() * 1000),
                "request_id": log_context.current_request_id(),
                "session_id": session_id,
                "workspace_id": workspace_id,
                "tool_call_id": audit.current_tool_call(),
                "tool_name": tool_name,
                "purpose": "refused",
                "command": audit.command_summary(command),
                "outcome": "refused",
                "reason": exc.reason,
                "dropped_env": [],
                "injected_env": [],
            }
        )
    except Exception:  # noqa: BLE001
        logger.warning("sandbox_audit_write_failed", exc_info=True)


def refusal_detail(exc: SandboxUnavailable) -> str:
    """把拒绝原因整理成给用户/模型看的一句话（不含路径与 env）。"""
    audit.record_refusal(reason=exc.reason, detail=str(exc))
    return str(exc)


def policy_for(
    *, sandbox_root: Path, cwd: Path, bound_root: str | None = None
) -> SandboxPolicy:
    """供诊断/测试读取当前策略（不执行任何命令）。"""
    return build_policy(
        sandbox_root=sandbox_root,
        cwd=cwd,
        bound_root=bound_root,
        allowed_domains=_allowed_domains(),
    )
