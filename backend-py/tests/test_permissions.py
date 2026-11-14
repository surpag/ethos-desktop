"""P1 权限系统：能力声明、档位裁决、安全约束的回归测试。

这张测试文件就是 `docs/permissions-p1.md` §9「副作用入口覆盖矩阵」的可执行版本。
每一行都对应矩阵里的一条，assert 写的是「预期结果」那一列。

刻意不测的东西：
- 不驱使真实模型跑一次 run（要 mock 流式 SDK，测的是 mock 而不是权限）；
- 不重复测各工具的业务语义（那是各模块自己的测试）。

所以这里直接驱动「裁决函数」和「工具集入口」这两层——它们是权限的**全部**判定点，
其余代码只消费它们的结论。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime

import aiosqlite
import pytest

from edith_backend.internal.agentrun.plan_tools import PlanToolSet
from edith_backend.internal.cronjob.schema import create_schema as create_cronjob_schema
from edith_backend.internal.cronjob.store import Store as CronStore
from edith_backend.internal.cronjob.tools import CronToolSet
from edith_backend.internal.cronjob.types import JobInput
from edith_backend.internal.permissions import catalog
from edith_backend.internal.permissions.paths import check_path
from edith_backend.internal.permissions.policy import (
    REASON_APPROVAL_REQUIRED,
    REASON_CAPABILITY_UNKNOWN,
    REASON_INVALID_ARGUMENTS,
    REASON_LEVEL_DENIED,
    REASON_TOOL_UNKNOWN,
    evaluate_call,
    min_level,
    parse_level,
    resolve_level,
)
from edith_backend.internal.permissions.types import (
    APPROVAL_REQUIRED,
    COMMAND_ERROR,
    PERMISSION_DENIED,
    Capability,
    PermissionLevel,
)
from edith_backend.internal.sandbox.local import LocalSandbox
from edith_backend.internal.sandbox.tools import SandboxToolSet
from edith_backend.internal.systemtools import SystemToolSet
from edith_backend.internal.tools import ToolRegistry
from edith_backend.internal.userconfig.schema import create_schema as create_user_schema
from edith_backend.internal.userconfig.settings import Settings

READ_ONLY = PermissionLevel.READ_ONLY
APPROVAL = PermissionLevel.APPROVAL
FULL = PermissionLevel.FULL


# ── 一、声明完整性 ────────────────────────────────────────────────────


def _all_tool_names() -> set[str]:
    sets = [
        SystemToolSet(),
        SandboxToolSet(None),
        CronToolSet(None),
        PlanToolSet(None, None),
    ]
    return {d["function"]["name"] for s in sets for d in s.tool_definitions()}


def test_every_registered_tool_is_declared() -> None:
    """注册表里每个工具都必须在能力表里有声明。

    这条测试是「默认拒绝」的构建期版本：新增工具忘记声明，运行时会被拒（安全），
    但只有测试报红才能让作者在合并之前发现，而不是等用户发现「这个工具用不了」。
    反向也查（声明了但工具不存在），否则表会变成一堆没人删的尸体。
    """
    registered = _all_tool_names()
    declared = set(catalog.TOOL_CAPABILITIES)
    assert registered - declared == set(), "有工具没声明能力（运行时会被拒绝）"
    assert declared - registered == set(), "声明表里有不存在的工具"


def test_capabilities_are_sets_not_levels() -> None:
    """shell 必须同时是 EXEC/WRITE/EXTERNAL。

    这不是「多打两个标签」：只要漏一个，将来某个只检查 WRITE 的策略就会从 shell
    这里漏过去。EXEC 不能理解成「WRITE 的高一级」——两者是并列的。
    """
    caps = catalog.capabilities_of("sandbox_run_command")
    assert caps == {
        Capability.EXEC,
        Capability.WRITE,
        Capability.EXTERNAL,
    }


def test_mcp_capabilities_are_never_read_only() -> None:
    assert Capability.READ not in catalog.MCP_CAPABILITIES
    assert catalog.is_readonly("github__list_issues", is_mcp=True) is False


def test_readonly_derives_from_declaration() -> None:
    """只读判定不再来自手写清单：查一下就是只读，git 要看子命令。"""
    assert catalog.is_readonly("sandbox_list_processes") is True
    assert catalog.is_readonly("git", {"subcommand": "status"}) is True
    assert catalog.is_readonly("git", {"subcommand": "commit"}) is False
    assert catalog.is_readonly("sandbox_write_file", {"path": "a.txt"}) is False


# ── 二、档位 × 能力（矩阵 1~15 行）────────────────────────────────────


def _verdict(tool: str, arguments: dict | None = None, level=FULL, **kwargs):
    return evaluate_call(level, tool, arguments or {}, **kwargs)


READ_TOOLS = [
    "sandbox_read_file",
    "sandbox_list_files",
    "sandbox_list_processes",
    "get_current_time",
]
WRITE_TOOLS = [
    ("sandbox_write_file", {"path": "docs/a.md", "content": "x"}),
    ("sandbox_make_directory", {"path": "docs/new"}),
    ("sandbox_move_path", {"from": "docs/a.md", "to": "docs/b.md"}),
    ("sandbox_delete_path", {"path": "docs/a.md"}),
]
EXTERNAL_TOOLS = [
    ("git_push", {}),
    ("git_remote_branches", {}),
    ("create_cron_job", {"name": "n", "taskType": "once", "schedule": "s", "prompt": "p"}),
]


def test_read_tools_allowed_at_every_level() -> None:
    for level in (READ_ONLY, APPROVAL, FULL):
        for tool in READ_TOOLS:
            verdict = _verdict(tool, level=level)
            assert verdict.allowed, (tool, level, verdict.reason)


@pytest.mark.parametrize("tool,arguments", WRITE_TOOLS)
def test_write_tools_matrix(tool: str, arguments: dict) -> None:
    assert _verdict(tool, arguments, level=READ_ONLY).reason == REASON_LEVEL_DENIED
    denied = _verdict(tool, arguments, level=APPROVAL)
    assert denied.reason == REASON_APPROVAL_REQUIRED
    assert denied.kind == APPROVAL_REQUIRED
    assert _verdict(tool, arguments, level=FULL).allowed


@pytest.mark.parametrize("tool,arguments", EXTERNAL_TOOLS)
def test_external_tools_matrix(tool: str, arguments: dict) -> None:
    assert _verdict(tool, arguments, level=READ_ONLY).allowed is False
    assert _verdict(tool, arguments, level=APPROVAL).kind == APPROVAL_REQUIRED
    assert _verdict(tool, arguments, level=FULL).allowed


def test_shell_is_full_only() -> None:
    """命令、起进程、杀进程：只在完全档可达。

    只读档**不**做「看起来只读的命令就放行」这种事——命令层无法可靠证明
    一条命令没有副作用（`ls` 之后接上 `&& rm -rf` 仍是同一条命令）。
    """
    for tool in ("sandbox_run_command", "sandbox_start_process", "sandbox_kill_process"):
        assert _verdict(tool, {"command": "ls"}, level=READ_ONLY).allowed is False
        assert _verdict(tool, {"command": "ls"}, level=APPROVAL).allowed is False
        assert _verdict(tool, {"command": "ls"}, level=FULL).allowed is True


def test_git_capabilities_follow_subcommand() -> None:
    read_only = ["status", "log", "diff", "show"]
    for sub in read_only:
        assert _verdict("git", {"subcommand": sub}, level=READ_ONLY).allowed, sub
    for sub in ("add", "commit"):
        args = {"subcommand": sub, "args": ["-m", "x"]}
        assert _verdict("git", args, level=READ_ONLY).allowed is False, sub
        assert _verdict("git", args, level=FULL).allowed, sub
    for sub in ("clone", "push", "pull", "fetch"):
        args = {"subcommand": sub, "args": []}
        assert _verdict("git", args, level=READ_ONLY).allowed is False, sub
        assert _verdict("git", args, level=APPROVAL).allowed is False, sub
        assert _verdict("git", args, level=FULL).allowed, sub
    # 未知子命令按最宽假设（只可能在 full 走到白名单那一步被拒）
    assert _verdict("git", {"subcommand": "rebase"}, level=APPROVAL).allowed is False


def test_skills_write_is_external() -> None:
    """写 Skill 的效果跨 run，必须比普通写入更严。"""
    plain = catalog.capabilities_of("sandbox_write_file", {"path": "docs/a.md"})
    skill = catalog.capabilities_of(
        "sandbox_write_file", {"path": "skills/demo/SKILL.md"}
    )
    assert plain == {Capability.WRITE}
    assert skill == {Capability.WRITE, Capability.EXTERNAL}
    # 换成分隔符 / 绝对路径写法，结论不变
    for path in ("skills\\demo\\SKILL.md", "/home/user/skills/demo/SKILL.md"):
        assert Capability.EXTERNAL in catalog.capabilities_of(
            "sandbox_write_file", {"path": path}
        )
    # 移到/删掉 Skill 同样是跨 run 影响
    for tool, args in (
        ("sandbox_delete_path", {"path": "skills/demo"}),
        ("sandbox_move_path", {"from": "skills/demo/SKILL.md", "to": "docs/x.md"}),
    ):
        assert Capability.EXTERNAL in catalog.capabilities_of(tool, args)
        assert _verdict(tool, args, level=APPROVAL).allowed is False


def test_metadata_allowed_at_every_level() -> None:
    """计划更新只写我们自己的元数据：三档都放行。

    判成 WRITE 的后果很具体：只读档下模型连计划都建不起来，而「计划丢失」正是
    续跑断裂的起点。
    """
    for level in (READ_ONLY, APPROVAL, FULL):
        assert _verdict("plan_update", {"mode": "set", "items": []}, level=level).allowed


# ── 三、未知情形（全部 fail-closed）──────────────────────────────────


def test_unknown_tool_denied_at_every_level() -> None:
    for level in (READ_ONLY, APPROVAL, FULL):
        verdict = _verdict("sandbox_magic", level=level, registered=False)
        assert verdict.allowed is False
        assert verdict.reason == REASON_TOOL_UNKNOWN
        assert verdict.kind == PERMISSION_DENIED


def test_undeclared_tool_denied() -> None:
    """已注册但没声明能力：同样是拒绝（只是排查方向不同）。"""
    verdict = _verdict("sandbox_magic", level=FULL, registered=True)
    assert verdict.allowed is False
    assert verdict.reason == REASON_CAPABILITY_UNKNOWN


def test_mcp_tools_are_never_allowed_below_full() -> None:
    for level in (READ_ONLY, APPROVAL):
        verdict = _verdict("github__create_issue", level=level, is_mcp=True)
        assert verdict.allowed is False
    assert _verdict("github__list_issues", level=FULL, is_mcp=True).allowed


def test_invalid_arguments_denied() -> None:
    """参数不是合法 JSON 时不再退化成 `{}` 继续执行。"""
    verdict = _verdict("sandbox_write_file", level=FULL, arguments_invalid=True)
    assert verdict.allowed is False
    assert verdict.reason == REASON_INVALID_ARGUMENTS
    payload = json.loads(verdict.error_payload())
    assert payload["retryable"] is True  # 模型可以把 JSON 写对，属于可自救


def test_denied_payload_tells_model_not_to_retry() -> None:
    payload = json.loads(_verdict("sandbox_write_file", level=READ_ONLY).error_payload())
    assert payload["kind"] == PERMISSION_DENIED
    assert payload["retryable"] is False
    assert payload["level"] == "read_only"
    assert payload["needed"] == ["write"]
    assert "重试" in payload["suggestion"]


def test_approval_payload_is_a_different_kind() -> None:
    """P2 接真审批时只改这张表，不改错误体系——所以两种拒绝必须现在就分开。"""
    payload = json.loads(_verdict("sandbox_write_file", level=APPROVAL).error_payload())
    assert payload["kind"] == APPROVAL_REQUIRED


# ── 四、档位解析 ──────────────────────────────────────────────────────


def test_parse_level_fails_closed() -> None:
    assert parse_level("") == (FULL, False)  # 未配置 = 保持既有行为
    assert parse_level("read_only") == (READ_ONLY, False)
    assert parse_level("APPROVAL") == (APPROVAL, False)
    # 脏数据降级为最严档位，而不是「不认识就当默认」
    assert parse_level("superuser") == (READ_ONLY, True)


def test_resolve_level_takes_the_stricter_one() -> None:
    assert resolve_level("full", "full") == FULL
    assert resolve_level("read_only", "full") == READ_ONLY  # 用户后来调低 → 更严生效
    assert resolve_level("full", "approval") == APPROVAL  # 快照是上限 → 升级不回溯
    assert resolve_level("approval", "") == APPROVAL  # 无上限 → 只看当前
    assert min_level(FULL, READ_ONLY) == READ_ONLY


# ── 五、敏感路径（安全约束，三档皆拦）────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "work/.env",
        "work/.env.local",
        "docs/.env.production",
        "home/.ssh/id_rsa",
        "keys/server.pem",
        "keys/client.key",
        "certs/a.p12",
        "certs/a.pfx",
        "config/.aws/credentials",
        "config/.config/gcloud/application_default_credentials.json",
        "work/credentials.json",
        "work/.netrc",
    ],
)
def test_sensitive_paths_are_matched(path: str) -> None:
    assert check_path(path), path


@pytest.mark.parametrize(
    "path",
    [
        "docs/env.md",
        "docs/environment.txt",
        "src/credentials_loader.py",
        "work/id_rsa.pub",
        "docs/config/gcloud.md",
    ],
)
def test_innocent_paths_are_not_matched(path: str) -> None:
    """不能退化成「子串包含」：`docs/env.md` 不该因为含 env 被拦。"""
    assert check_path(path) == "", path


def test_sensitive_paths_normalize_separators_and_case() -> None:
    assert check_path("C:\\Users\\me\\.SSH\\id_rsa")
    assert check_path("WORK\\.ENV")
    assert check_path("/home/u/.Ssh/Id_Rsa")


def test_sensitive_paths_reject_traversal_and_nul() -> None:
    """`..` 与 NUL 都算「无法可靠判定落点」→ 拒绝。"""
    assert check_path("work/../.env")
    assert check_path("work/a\x00b") == "路径含无法解析的字符"


class _FakeService:
    """只提供 `workspace()` 的沙箱服务替身（工具集只需要这一个方法）。"""

    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sandbox = sandbox

    async def workspace(self, user_id: str, session_id: str) -> LocalSandbox:
        return self._sandbox


@pytest.fixture
def sandbox_tools(tmp_path, monkeypatch) -> SandboxToolSet:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    return SandboxToolSet(_FakeService(LocalSandbox.open("u1", "s1")))


def test_sensitive_path_enforced_without_workspace(sandbox_tools, tmp_path) -> None:
    """F1 回归：没有绑定工作区的会话也不能写 `work/.env`。

    旧实现把敏感检查挂在 `if name in _FILE_TOOLS and workspace_id` 上，于是
    「没选工作区」这条完全正常的使用路径成了检查的盲区。
    """

    async def body() -> None:
        result = json.loads(
            await sandbox_tools.execute(
                "sandbox_write_file",
                {"path": "work/.env", "content": "TOKEN=abc"},
                "u1",
                "s1",
                "",  # 关键：没有工作区
            )
        )
        assert result["kind"] == PERMISSION_DENIED
        assert result["reason"] == "sensitive_path"
        assert result["retryable"] is False
        assert not (tmp_path / "sandbox" / "u1" / "s1" / "work" / ".env").exists()

    asyncio.run(body())


def test_normal_write_still_works_without_workspace(sandbox_tools, tmp_path) -> None:
    """安全约束不能顺手把正常写入也拦掉（否则等于用「全拒绝」冒充安全）。"""

    async def body() -> None:
        raw = await sandbox_tools.execute(
            "sandbox_write_file", {"path": "work/ok.txt", "content": "hello"}, "u1", "s1", ""
        )
        assert "error" not in json.loads(raw), raw
        assert (tmp_path / "sandbox" / "u1" / "s1" / "work" / "ok.txt").read_text(
            encoding="utf-8"
        ) == "hello"

    asyncio.run(body())


def test_git_add_sensitive_path(sandbox_tools) -> None:
    """`git add .env && commit && push` 是一条完整的密钥外送链路，必须拦在第一步。"""

    async def body() -> None:
        result = json.loads(
            await sandbox_tools.execute(
                "git", {"subcommand": "add", "args": [".env"]}, "u1", "s1", ""
            )
        )
        assert result["kind"] == PERMISSION_DENIED
        assert result["reason"] == "sensitive_path"

    asyncio.run(body())


def test_delete_path_checks_entries(sandbox_tools, tmp_path) -> None:
    """递删是**容器操作**：`work/archive` 的路径串干净，但 `files.remove` 删的是里面每一条。

    P1 只判目标路径，于是 `sandbox_delete_path work/archive` 会把目录里的 `.env`
    一起删掉——路径检查保护的是「这次操作指向哪个路径」，不是「这个路径里面每条」。
    """
    archive = tmp_path / "sandbox" / "u1" / "s1" / "work" / "archive"
    (archive / "deep").mkdir(parents=True)
    (archive / "notes.md").write_text("ok", encoding="utf-8")
    (archive / "deep" / ".env").write_text("TOKEN=1", encoding="utf-8")

    async def body() -> None:
        result = json.loads(
            await sandbox_tools.execute(
                "sandbox_delete_path", {"path": "work/archive"}, "u1", "s1", ""
            )
        )
        assert result["kind"] == PERMISSION_DENIED
        assert result["reason"] == "sensitive_path"
        # 报的是目录内**相对**条目，模型据此能自己判断该怎么改
        assert "deep/.env" in result["error"]
        assert (archive / "deep" / ".env").exists()
        assert (archive / "notes.md").exists()

    asyncio.run(body())


def test_delete_path_allows_clean_tree(sandbox_tools, tmp_path) -> None:
    """按条目判定不能顺带把正常目录删不掉（全拒绝不是安全，是无能）。"""
    archive = tmp_path / "sandbox" / "u1" / "s1" / "work" / "archive"
    (archive / "sub").mkdir(parents=True)
    (archive / "sub" / "a.md").write_text("ok", encoding="utf-8")

    async def body() -> None:
        raw = await sandbox_tools.execute(
            "sandbox_delete_path", {"path": "work/archive"}, "u1", "s1", ""
        )
        assert "error" not in json.loads(raw), raw
        assert not archive.exists()

    asyncio.run(body())


def _init_repo(repo, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=30)


def test_git_add_directory_checks_expansion(tmp_path, monkeypatch) -> None:
    """`git add <目录>` 会按条目展开：路径串里没有敏感段，暂存集合里有。

    这是完整的外送链路第一步——`add` 目录 → `commit` → `git_push`。
    """
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u1", "s1")
    tools = SandboxToolSet(_FakeService(sandbox))
    repo = sandbox.resolve("workspace/w1")
    _init_repo(repo, {"sub/a.txt": "ok", "sub/.env": "TOKEN=1"})

    async def body() -> None:
        result = json.loads(
            await tools.execute(
                "git", {"subcommand": "add", "args": ["sub"]}, "u1", "s1", "w1"
            )
        )
        assert result["kind"] == PERMISSION_DENIED, result
        assert result["reason"] == "sensitive_path"
        assert "sub/.env" in result["error"]
        # 拒绝发生在**写索引之前**：不是先暂存再撤回
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert staged.stdout.strip() == ""

    asyncio.run(body())


def test_git_add_explicit_file_still_works(tmp_path, monkeypatch) -> None:
    """目录里有 `.env` 时，add 明确的具体文件仍然可行（否则等于把 git 工具废掉）。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u1", "s1")
    tools = SandboxToolSet(_FakeService(sandbox))
    repo = sandbox.resolve("workspace/w1")
    _init_repo(repo, {"sub/a.txt": "ok", "sub/.env": "TOKEN=1"})

    async def body() -> None:
        result = json.loads(
            await tools.execute(
                "git", {"subcommand": "add", "args": ["sub/a.txt"]}, "u1", "s1", "w1"
            )
        )
        assert result.get("exitCode") == 0, result
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert staged.stdout.strip() == "sub/a.txt"

    asyncio.run(body())


def test_git_add_directory_ignores_gitignored_secrets(tmp_path, monkeypatch) -> None:
    """被 `.gitignore` 忽略的 `.env` 根本不会进暂存集合，就不该被拒。

    这条是「问 git 而不是自己遍历目录」的理由：自己走一遍会把忽略规则算错，
    于是 `git add src/` 只要目录里有个 `.env` 就永久失败。误拒比漏判安全，
    但一条会误拒的规则会被下一个人注释掉。
    """
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    sandbox = LocalSandbox.open("u1", "s1")
    tools = SandboxToolSet(_FakeService(sandbox))
    repo = sandbox.resolve("workspace/w1")
    _init_repo(repo, {"sub/a.txt": "ok", "sub/.env": "TOKEN=1", ".gitignore": ".env\n"})

    async def body() -> None:
        result = json.loads(
            await tools.execute(
                "git", {"subcommand": "add", "args": ["sub"]}, "u1", "s1", "w1"
            )
        )
        assert result.get("exitCode") == 0, result
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert staged.stdout.strip() == "sub/a.txt"

    asyncio.run(body())


def test_approval_cannot_bypass_sensitive_paths(sandbox_tools, tmp_path) -> None:
    """批准只解除**档位**那一层；安全约束在工具内部，与批准无关。

    这条是结构性的：`policy` 不认识路径，`sandbox/tools.py` 不认识档位。
    写成「批准后重新裁决一次」就守不住了——那时批准会把两者一起放开。
    """
    verdict = evaluate_call(
        APPROVAL, "sandbox_write_file", {"path": "work/.env", "content": "T=1"}
    )
    assert verdict.kind == APPROVAL_REQUIRED
    granted = verdict.granted()
    assert granted.allowed is True
    assert granted.needed == verdict.needed  # 批准覆盖了哪些能力，原样留作审计

    async def body() -> None:
        result = json.loads(
            await sandbox_tools.execute(
                "sandbox_write_file",
                {"path": "work/.env", "content": "T=1"},
                "u1",
                "s1",
                "",
            )
        )
        assert result["kind"] == PERMISSION_DENIED
        assert result["reason"] == "sensitive_path"
        assert not (tmp_path / "sandbox" / "u1" / "s1" / "work" / ".env").exists()

    asyncio.run(body())


def _make_link(link, target, *, directory: bool) -> bool:
    """在 `link` 处建一个指向 `target` 的链接/联接；不支持时返回 False。

    Windows 上普通符号链接需要开发者模式或管理员权限，目录**联接（junction）**
    不需要——而 `os.path.realpath` 对两者的解析结果一样。所以这里优先符号链接，
    失败后退到 junction，两者都不行才跳过（跳过而不是假装通过）。
    """
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except (OSError, NotImplementedError):
        # Windows 上 symlink 失败（缺权限）时会留下一个空的占位文件/目录，
        # 不清掉的话下一步 mklink 会报「文件已存在」。占位类型不一定是哪一种，
        # 所以两种删法都试一遍。
        for cleanup in (link.unlink, link.rmdir):
            try:
                cleanup()
            except OSError:
                pass
        if link.exists() or link.is_symlink():
            return False
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J" if directory else "/H", str(link), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and os.path.exists(link)


def test_symlink_escape_to_ssh_key(sandbox_tools, tmp_path) -> None:
    """逻辑路径干净、落点敏感：只有真实路径复核拦得住。

    `sandbox_run_command ln -s ~/.ssh work/k`（或 Windows 上的 `mklink /J`）在完全档
    是允许的；之后 `sandbox_read_file work/k/config` 的字面路径里没有任何敏感段。
    """
    secret_dir = tmp_path / "home" / ".ssh"
    secret_dir.mkdir(parents=True)
    (secret_dir / "config").write_text("PRIVATE KEY", encoding="utf-8")

    sandbox_root = tmp_path / "sandbox" / "u1" / "s1"
    link = sandbox_root / "work" / "k"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _make_link(link, secret_dir, directory=True):
        pytest.skip("当前环境不允许创建符号链接或目录联接")

    async def body() -> None:
        result = json.loads(
            await sandbox_tools.execute(
                "sandbox_read_file", {"path": "work/k/config"}, "u1", "s1", ""
            )
        )
        assert result["kind"] == PERMISSION_DENIED, result
        assert "PRIVATE KEY" not in json.dumps(result, ensure_ascii=False)

    asyncio.run(body())


# ── 六、Cron 权限快照 ────────────────────────────────────────────────


async def _cron_store() -> tuple[aiosqlite.Connection, CronStore, Settings]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_cronjob_schema(db)
    await create_user_schema(db)
    settings = Settings(db)
    return db, CronStore(db, settings), settings


def test_cron_job_records_permission_snapshot() -> None:
    async def body() -> None:
        db, store, _ = await _cron_store()
        try:
            job = await store.create(
                "u1",
                JobInput("nightly", "once", "2030-01-01T09:00:00+08:00", "写日报"),
                permission_level="approval",
            )
            assert job.permission_level == "approval"
            listed = await store.list("u1")
            assert listed[0].permission_level == "approval"
        finally:
            await db.close()

    asyncio.run(body())


def test_legacy_cron_job_gets_snapshot_backfilled() -> None:
    """该列引入前创建的任务：启动时用「此刻的档位」补一条授权记录。

    留空会变成「每次执行都临时读当前设置」，那正是权限漂移本身。
    """

    async def body() -> None:
        db, store, settings = await _cron_store()
        try:
            await settings.save_permission_level("u1", "read_only")
            await db.execute(
                "INSERT INTO cron_jobs (id, user_id, name, task_type, schedule, prompt, "
                "enabled, next_run_at, running, created_at, permission_level) "
                "VALUES ('j1', 'u1', 'old', 'once', '2030-01-01T09:00:00+00:00', 'p', "
                "1, NULL, 0, '2026-01-01T00:00:00+00:00', '')"
            )
            await db.commit()
            await store.initialize_next_runs(datetime.now(UTC))
            job = (await store.list("u1"))[0]
            assert job.permission_level == "read_only"
        finally:
            await db.close()

    asyncio.run(body())


def test_cron_effective_level_is_min_of_snapshot_and_current() -> None:
    """三条现实场景：降权、提权、无权。"""
    # 创建时 full，用户后来调成只读 → 任务降级
    assert resolve_level("read_only", "full") == READ_ONLY
    # 创建时只读，用户后来调成 full → 任务不自动升级
    assert resolve_level("full", "read_only") == READ_ONLY
    # 两边都是 full → 照旧
    assert resolve_level("full", "full") == FULL


# ── 七、Registry 的错误分类 ──────────────────────────────────────────


def test_registry_unknown_tool_is_business_error() -> None:
    """模型幻觉出的工具名 → 可自救的业务错误，不能计熔断。"""

    async def body() -> None:
        payload = json.loads(await ToolRegistry([]).execute("nope", {}, "u", "s"))
        assert payload["kind"] == COMMAND_ERROR
        assert payload["retryable"] is True

    asyncio.run(body())


# ── 八、HTTP 边界（档位可配置，且不会被无关保存冲掉）──────────────────


def test_settings_api_round_trips_permission_level(client) -> None:
    from edith_backend.internal.models.catalog import DEFAULT_MODEL_ID

    user = "test-perm-settings"
    base = {"userId": user, "defaultModelId": DEFAULT_MODEL_ID}

    saved = client.put(
        "/internal/user-settings", json={**base, "permissionLevel": "read_only"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["permissionLevel"] == "read_only"
    assert client.get("/internal/user-settings", params={"userId": user}).json()[
        "permissionLevel"
    ] == "read_only"

    # 前端还没做这个开关：保存其它设置时**不能**把档位冲掉
    again = client.put("/internal/user-settings", json=base)
    assert again.status_code == 200
    assert again.json()["permissionLevel"] == "read_only"

    # 非法档位在边界就被拒（否则只能落到库里，再靠读时降级兜底）。
    # 全局异常处理器把 pydantic 校验错误统一映射成 400 invalid_request。
    bad = client.put(
        "/internal/user-settings", json={**base, "permissionLevel": "god_mode"}
    )
    assert bad.status_code == 400
    assert bad.json()["type"] == "invalid_request"


def test_cron_http_creation_records_snapshot(client) -> None:
    """网页端创建的任务同样要留授权记录（两条创建入口不能只有一条留痕）。"""
    from edith_backend.internal.models.catalog import DEFAULT_MODEL_ID

    user = "test-perm-cron"
    client.put(
        "/internal/user-settings",
        json={
            "userId": user,
            "defaultModelId": DEFAULT_MODEL_ID,
            "permissionLevel": "approval",
        },
    )
    created = client.post(
        "/internal/cron-jobs",
        json={
            "userId": user,
            "name": "夜间整理",
            "taskType": "once",
            "schedule": "2030-01-01T09:00:00+08:00",
            "prompt": "整理下载目录",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["permissionLevel"] == "approval"


# ── 九、老库迁移 ─────────────────────────────────────────────────────


def test_legacy_user_agents_table_gets_permission_level_column() -> None:
    """老库补列后必须能读到一个**不变**的行为（默认 full），而不是锁死或提权。"""

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        try:
            await db.execute(
                "CREATE TABLE user_agents (user_id TEXT PRIMARY KEY, personality TEXT NOT NULL DEFAULT '', "
                "default_model_id TEXT NOT NULL DEFAULT '', timezone TEXT NOT NULL DEFAULT '')"
            )
            await db.execute(
                "INSERT INTO user_agents (user_id, personality) VALUES ('u1', 'old')"
            )
            await db.commit()

            await create_user_schema(db)
            cursor = await db.execute("PRAGMA table_info(user_agents)")
            columns = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            assert "permission_level" in columns

            settings = Settings(db)
            assert await settings.load_permission_level("u1") == "full"
            # 其它字段没被迁移弄丢
            assert (await settings.load("u1")).personality == "old"
        finally:
            await db.close()

    asyncio.run(body())
