"""git 执行器：在沙箱内以非交互环境执行白名单内的 git 命令，并脱敏输出。

只读 + clone + 本地写（add/commit）默认可用；远端写（push/pull/fetch）需要 runner 沙箱
（凭据只在那里可达，见 policy 的 allow_remote）。
"""

from __future__ import annotations

import shlex

from .env import git_identity_env, non_interactive_env
from .policy import GitCommandRejected, validate_git_command
from .redact import redact_git_output
from .ssrf import assert_safe_clone_url

# git 单次执行超时（秒）
DEFAULT_GIT_TIMEOUT = 60


async def run_git(
    sandbox,
    workspace_path: str,
    subcmd: str,
    args: list[str],
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    identity: tuple[str, str] | None = None,
    allow_remote: bool = False,
) -> dict:
    """执行一次白名单内的 git 调用，返回 {exitCode, stdout, stderr, command}。

    顺序：白名单校验 → clone 额外做 SSRF 校验 → 组装命令（shlex.quote 防注入）
    → 非交互 env（+ 提交身份）→ 执行 → 输出脱敏。

    identity=(name, email) 时注入 GIT_AUTHOR_*/GIT_COMMITTER_*（用 env 而非 `-c`，
    后者在 BLOCKED_COMMIT_FLAGS 内被禁）。
    allow_remote=True 才放行 push/pull/fetch —— 只应由 runner 沙箱的调用方设置。

    git 大量合法场景以**非零退出码**表达（非仓库=128、冲突=1…），所以这里
    **不能**把非零退出当异常：`commands.run` 返回带 exit_code 的结果对象，
    由上层按 exit_code 判断。
    """
    validate_git_command(subcmd, args, allow_remote=allow_remote)
    if subcmd == "clone" and args:
        assert_safe_clone_url(args[0])

    command = "git " + " ".join([subcmd, *(shlex.quote(str(a)) for a in args)])
    envs = non_interactive_env()
    if identity is not None:
        envs.update(git_identity_env(*identity))
    result = await sandbox.commands.run(
        command,
        cwd=workspace_path,
        envs=envs,
        timeout=timeout,
    )
    return {
        "exitCode": result.exit_code or 0,
        "stdout": redact_git_output(result.stdout or ""),
        "stderr": redact_git_output(result.stderr or ""),
        "command": command,
    }


async def plan_git_add(
    sandbox, workspace_path: str, args: list[str]
) -> tuple[list[str], str]:
    """问 git：这次 `add` 会暂存哪些文件（**不写索引**）。

    返回 `(仓库相对路径列表, 失败原因)`；失败原因为空串表示拿到了可信的计划。

    为什么问 git 而不是自己遍历目录：`.gitignore`、pathspec 展开、已跟踪文件的
    新增/修改，这三件事只有 git 自己算得准。自己 `os.walk` 会把被忽略的文件
    算进「会被暂存」，于是 `git add src/` 只要目录里有个被忽略的 `.env.bak`
    就误拒——误拒比漏判安全，但会让人开始怀疑这条规则到底在保护什么。

    参数校验与真实执行**完全同一套**（`validate_git_command`），否则
    「计划里没有、实际却暂存了」这种不对称会变成一个绕道。
    """
    validate_git_command("add", args)
    command = "git add -n -- " + " ".join(shlex.quote(str(a)) for a in args)
    result = await sandbox.commands.run(
        command,
        cwd=workspace_path,
        envs=non_interactive_env(),
        timeout=DEFAULT_GIT_TIMEOUT,
    )
    if (result.exit_code or 0) != 0:
        # 非仓库 / pathspec 非法等：把 git 自己的话原样交回去，
        # 不翻译成「敏感路径」——那会让人以为命中了密钥。
        return [], redact_git_output(result.stderr or "").strip() or "git add -n 执行失败"

    planned: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        # 两种行：`add 'path'`；大小写不敏感文件系统上可能出现的 `remove 'path'`
        for verb in ("add '", "remove '"):
            if line.startswith(verb) and line.endswith("'"):
                name = line[len(verb) : -1]
                if verb == "add '":
                    planned.append(name)
                break
        else:
            # 解析不了就等于「不知道会暂存什么」，按失败处理（fail-closed）
            return [], f"无法解析 git 的暂存计划：{line[:120]!r}"
    return planned, ""


__all__ = [
    "GitCommandRejected",
    "assert_safe_clone_url",
    "git_identity_env",
    "non_interactive_env",
    "plan_git_add",
    "redact_git_output",
    "run_git",
    "validate_git_command",
]
