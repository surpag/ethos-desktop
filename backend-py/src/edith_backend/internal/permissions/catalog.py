"""工具能力声明表：**唯一权威源**。

为什么集中在一张表，而不是散在四个 tool set 里：

1. 「哪些工具能做什么」是一个必须能**一次看全**的问题。分散后要回答同一个问题，
   读者得打开 `systemtools` / `sandbox` / `cronjob` / `agentrun` 四个文件。
2. 这张表同时被权限裁决、只读工具调度（并发/缓存）、完成证据分级三处消费。
   分散声明必然漂移成三个答案——上一版就出现过 `_READ_ONLY_TOOLS` 漏登记两个工具。
3. 「新增工具忘记声明」在两种布局下同样会发生。集中布局只需要一条测试
   （注册表 ↔ 声明表比对）就能在构建期报红，分散布局要写四条。

没有独立 tool set 的 MCP 工具不在表里：它们每次 run 现开，名字运行时才知道，
一律按 `MCP_CAPABILITIES`（最宽的风险假设）处理。
"""

from __future__ import annotations

from .types import Capability

_F = frozenset

#: MCP 工具的能力：**不可知**。服务端可以自报 `readOnlyHint`，但那是自述、不是
#: 可验证事实；按最宽假设处理意味着它只在 `full` 档可用。
MCP_CAPABILITIES = _F({Capability.WRITE, Capability.EXEC, Capability.EXTERNAL})

#: 文件类写工具涉及的路径参数名（判断「是否写进了 skills」）。
_PATH_KEYS = ("path", "from", "to")

#: 每个工具的基础能力。参数敏感的能力由 `_REFINERS` 追加/替换。
TOOL_CAPABILITIES: dict[str, frozenset[Capability]] = {
    # ---- systemtools ----
    "get_current_time": _F({Capability.READ}),
    # ---- sandbox：文件 ----
    "sandbox_list_files": _F({Capability.READ}),
    "sandbox_read_file": _F({Capability.READ}),
    "sandbox_write_file": _F({Capability.WRITE}),
    "sandbox_make_directory": _F({Capability.WRITE}),
    "sandbox_move_path": _F({Capability.WRITE}),
    "sandbox_delete_path": _F({Capability.WRITE}),
    # ---- sandbox：命令与进程 ----
    # Shell 的能力远超「执行一个命令」：它能读写删文件、能联网、能起长期进程。
    # 所以它不是「WRITE 的高一级」，而是三把刀同时在手——少了任何一项，将来某个
    # 只检查 WRITE 的策略都会从 shell 这里漏过去。
    "sandbox_run_command": _F(
        {Capability.EXEC, Capability.WRITE, Capability.EXTERNAL}
    ),
    "sandbox_start_process": _F(
        {Capability.EXEC, Capability.WRITE, Capability.EXTERNAL}
    ),
    "sandbox_list_processes": _F({Capability.READ}),
    "sandbox_kill_process": _F({Capability.EXEC}),
    # ---- sandbox：git ----
    # git 是唯一「看一眼子命令才知道能干什么」的工具，由 refine_git 定能力。
    "git": _F({Capability.WRITE}),
    # 推送改变远端仓库的状态（本地一个字节都没动，但「有没有发生变化」的答案是
    # 有），所以同时是 WRITE 与 EXTERNAL：证据分级看前者，档位裁决看后者。
    "git_push": _F({Capability.WRITE, Capability.EXTERNAL}),
    # 只读，但要带用户凭据出网：只读档不放行（只读档承诺的是「不碰外部」）。
    "git_remote_branches": _F({Capability.READ, Capability.EXTERNAL}),
    # ---- cronjob ----
    # 它本身只写一行数据，但它创建的是**未来某时刻的一次 Agent 执行**——
    # 效果不在本次 run 的边界内，这才是它的真实风险。
    "create_cron_job": _F({Capability.WRITE, Capability.EXTERNAL}),
    # ---- plan ----
    "plan_update": _F({Capability.METADATA}),
    # ---- browser ----
    # 导航与点击/填写会改变外部站点的状态（提交表单、下单、发消息），效果不在本次
    # run 的边界内，所以是 EXTERNAL 而不是 WRITE（本地一个字节都没动）。
    "browser_open_url": _F({Capability.EXTERNAL}),
    "browser_click": _F({Capability.EXTERNAL}),
    "browser_fill": _F({Capability.EXTERNAL}),
    "browser_press": _F({Capability.EXTERNAL}),
    # 下面三个是「看」，但**不能声明成纯 READ**：只读工具会进 run 级结果缓存
    # （key = 工具名 + 参数），`browser_get_page_snapshot` 第二次以 `{}` 调用会命中
    # 缓存、拿回上一份已经过期的 observation_id，紧接着的点击必然失败。
    # 加 METADATA 让它不进只读缓存，同时在只读档/审批档仍然放行。
    "browser_get_page_snapshot": _F({Capability.READ, Capability.METADATA}),
    "browser_get_url": _F({Capability.READ, Capability.METADATA}),
    # 滚动只改视口，但会触发懒加载（出网）。按 READ 处理是明示取舍：
    # 若算 EXTERNAL，审批档下每滚一屏都要用户点一次确认。
    "browser_scroll": _F({Capability.READ, Capability.METADATA}),
}


def _top_segment(raw: object) -> str:
    """取路径的第一段（把 `home/user/skills/x` 与 `/home/user/skills/x` 归一）。"""
    if not isinstance(raw, str):
        return ""
    path = raw.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if path.startswith("home/user/"):
        path = path[len("home/user/") :]
    return path.split("/")[0].lower()


def _refine_skills_paths(
    base: frozenset[Capability], arguments: dict
) -> frozenset[Capability]:
    """写 `skills/**` → 追加 `EXTERNAL`。

    写 Skill 不是普通写入：正文会进**未来每一次 run 的 system prompt**。普通写入
    的后果在本次 run 内就能被复核（下一步读一次即可），而 Skill 写入的后果本次 run
    永远看不见——它是跨 run 的持久化影响，与「创建定时任务」同级。
    """
    for key in _PATH_KEYS:
        if _top_segment(arguments.get(key)) == "skills":
            return base | {Capability.EXTERNAL}
    return base


def _refine_git(base: frozenset[Capability], arguments: dict) -> frozenset[Capability]:
    """按 git 子命令定能力。

    分类直接复用 `gitops.policy` 的四张白名单——那是「哪些子命令允许执行」的既有
    权威源。在这里另抄一份分类，等于让「能不能执行」和「算不算写」慢慢分叉。
    """
    from ..gitops.policy import (
        IMPORT_SUBCOMMANDS,
        READ_ONLY_SUBCOMMANDS,
        REMOTE_SUBCOMMANDS,
        WRITE_SUBCOMMANDS,
    )

    subcmd = str(arguments.get("subcommand") or "").strip()
    if subcmd in READ_ONLY_SUBCOMMANDS:
        return _F({Capability.READ})
    if subcmd in WRITE_SUBCOMMANDS:
        return _F({Capability.WRITE})
    if subcmd in IMPORT_SUBCOMMANDS or subcmd in REMOTE_SUBCOMMANDS:
        return _F({Capability.WRITE, Capability.EXTERNAL})
    # 未知子命令：白名单会在执行时拒绝它，但裁决发生在执行之前，
    # 所以这里按最宽假设处理（只有在 full 档才会走到白名单那一步）。
    return _F({Capability.WRITE, Capability.EXEC, Capability.EXTERNAL})


_REFINERS = {
    "sandbox_write_file": _refine_skills_paths,
    "sandbox_make_directory": _refine_skills_paths,
    "sandbox_move_path": _refine_skills_paths,
    "sandbox_delete_path": _refine_skills_paths,
    "git": _refine_git,
}


def capabilities_of(
    name: str, arguments: dict | None = None, *, is_mcp: bool = False
) -> frozenset[Capability] | None:
    """查询一个工具的能力集合；`None` 表示**未声明**（调用方必须拒绝）。

    `arguments` 参与判定只对少数工具成立——这正是「能力要按调用看」的体现：
    `git status` 与 `git commit` 是同一个工具名，但一件只读、一件写。
    """
    if is_mcp:
        return MCP_CAPABILITIES
    base = TOOL_CAPABILITIES.get(name)
    if base is None:
        return None
    refine = _REFINERS.get(name)
    if refine is None:
        return base
    return refine(base, arguments or {})


def is_readonly(name: str, arguments: dict | None = None, *, is_mcp: bool = False) -> bool:
    """纯只读工具（可用于并发调度与结果缓存）。

    未声明能力的工具返回 False：不知道它做什么，就当它什么都可能做。
    """
    caps = capabilities_of(name, arguments, is_mcp=is_mcp)
    return caps is not None and caps <= {Capability.READ}
