"""OS 隔离层：策略、环境收口、审计脱敏，以及最重要的——**fail-closed**。

这组用例守的是一条产品级约束：**没有任何配置组合能让命令在没有后端的情况下被执行**。
`off` 不是"关掉沙箱直接跑"，是"关掉执行能力"；`host` 需要两个变量同时在场。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from edith_backend.internal import isolation
from edith_backend.internal.isolation import audit
from edith_backend.internal.isolation.backends import asrt
from edith_backend.internal.isolation.capabilities import BackendCapability
from edith_backend.internal.isolation.errors import SandboxUnavailable
from edith_backend.internal.isolation.manager import plan, resolve
from edith_backend.internal.isolation.policy import (
    build_policy,
    windows_acl_denied_paths,
)
from edith_backend.internal.tools import tool_error_result


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


def _unavailable(name: str) -> BackendCapability:
    return BackendCapability(
        name=name, kind=name, available=False, detail=f"{name} 未安装"
    )


@pytest.fixture(autouse=True)
def _clean_backend(monkeypatch):
    """每个用例都从"没有后端"的干净状态开始，避免进程级缓存串味。"""
    monkeypatch.delenv(isolation.UNSAFE_HOST_ENV, raising=False)
    isolation.reset()
    yield
    isolation.reset()


# ---- fail-closed ----


def test_no_backend_refuses_execution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(isolation.BACKEND_ENV, "auto")
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_asrt",
        lambda: _unavailable("asrt"),
    )
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_appcontainer",
        lambda: _unavailable("appcontainer"),
    )

    with pytest.raises(SandboxUnavailable) as excinfo:
        plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo hi",
            env={"PATH": "x"},
            shell_exe="bash",
        )
    assert excinfo.value.reason == SandboxUnavailable.NOT_INSTALLED
    # 文案要给出可执行动作，否则用户只能看到"不能执行"
    assert "npm i -g" in str(excinfo.value)


def test_installed_but_broken_backend_reports_init_failed(monkeypatch, tmp_path: Path) -> None:
    """`srt --version` 通过 ≠ 能用：真机实测过它会卡在 acl grant 上。

    这种情况必须报 `init_failed`（去修安装/提权），而不是 `not_installed`
    （去装一遍）——两者的修复动作完全不同，混在一起会让人反复装同一个包。
    """
    monkeypatch.setenv(isolation.BACKEND_ENV, "auto")
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_asrt",
        lambda: BackendCapability(
            name="asrt", kind="asrt", available=True, location="srt", version="0.0.83"
        ),
    )
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_appcontainer",
        lambda: _unavailable("appcontainer"),
    )
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.verify_asrt",
        lambda cap: (False, "srt 卡在 srt-win acl grant --persistent"),
    )

    with pytest.raises(SandboxUnavailable) as excinfo:
        plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo hi",
            env={},
            shell_exe="bash",
        )
    assert excinfo.value.reason == SandboxUnavailable.INIT_FAILED
    assert "acl grant" in str(excinfo.value)


def test_off_disables_execution_rather_than_running_unprotected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(isolation.BACKEND_ENV, "off")
    with pytest.raises(SandboxUnavailable) as excinfo:
        plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo hi",
            env={},
            shell_exe="bash",
        )
    assert excinfo.value.reason == SandboxUnavailable.DISABLED


def test_host_backend_needs_both_switches(monkeypatch, tmp_path: Path) -> None:
    """只写 `backend=host` 不够：`ALLOW_UNSAFE_HOST` 是第二道确认。"""
    monkeypatch.setenv(isolation.BACKEND_ENV, "host")
    with pytest.raises(SandboxUnavailable):
        plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo hi",
            env={},
            shell_exe="bash",
        )


def test_missing_shell_refuses(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(isolation.BACKEND_ENV, "host")
    monkeypatch.setenv(isolation.UNSAFE_HOST_ENV, "1")
    with pytest.raises(SandboxUnavailable) as excinfo:
        plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo hi",
            env={},
            shell_exe=None,
        )
    assert excinfo.value.reason == SandboxUnavailable.NOT_SUPPORTED


def test_refusal_reaches_tool_result_as_non_retryable() -> None:
    """工具层必须把它标成不可重试：换命令/重试永远不会成功。"""
    payload = json.loads(
        tool_error_result(
            "sandbox_run_command",
            SandboxUnavailable("没有可用的隔离后端", reason=SandboxUnavailable.NOT_INSTALLED),
        )
    )
    assert payload["kind"] == "tool_failure"
    assert payload["retryable"] is False
    assert payload["reason"] == "sandbox_not_installed"
    # 不能出现"系统故障"——模型会当成偶发问题重试到熔断
    assert "系统故障" not in payload["error"]


def test_sandbox_unavailable_stops_process_before_spawn(monkeypatch, client) -> None:
    """真正的证明：后端不可用时 `commands.run` 抛错，**命令没有被执行**。

    用一个"写了文件就说明跑过"的命令：只要文件不存在，就说明进程根本没起来。
    """
    from edith_backend.internal.sandbox.local import LocalSandbox

    monkeypatch.setenv(isolation.BACKEND_ENV, "auto")
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_asrt",
        lambda: _unavailable("asrt"),
    )
    monkeypatch.setattr(
        "edith_backend.internal.isolation.manager.probe_appcontainer",
        lambda: _unavailable("appcontainer"),
    )
    isolation.reset()

    sandbox = LocalSandbox.open("isolation-user", "isolation-session")
    marker = Path(sandbox.root) / "never-created.txt"
    with pytest.raises(SandboxUnavailable):
        awaitable = sandbox.commands.run(f"touch {marker}".replace("\\", "/"))
        _run(awaitable)
    assert not marker.exists()


# ---- 策略 ----


def test_policy_binds_only_the_authorized_workspace(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sandbox"
    workspace = tmp_path / "my-project"
    workspace.mkdir()
    sandbox_root.mkdir()

    policy = build_policy(sandbox_root=sandbox_root, cwd=workspace, bound_root=str(workspace))
    assert str(workspace.resolve()) in policy.writable_roots
    assert policy.denied_read  # SSH / 云凭据 / 应用数据必须出现在拒绝列表里
    # Windows 上 `.ssh` 等由 ACL 加固承担，ASRT 策略里会去重掉（双重拒绝会让
    # `srt acl` 与 `denyRead` 互相干扰，且对主目录子项 stamp 会空转）。
    # 所以这条断言要看的是"被覆盖"，而不是"必须出现在 denyRead 里"。
    acl_handled = {path.casefold() for path in windows_acl_denied_paths()}
    assert any(".ssh" in item.casefold() for item in policy.denied_read) or any(
        path.endswith(".ssh") for path in acl_handled
    )


def test_policy_sets_are_stable_and_digest_depends_on_content(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    base = build_policy(sandbox_root=sandbox_root, cwd=sandbox_root)
    again = build_policy(sandbox_root=sandbox_root, cwd=sandbox_root)
    assert base.digest() == again.digest()

    wider = build_policy(
        sandbox_root=sandbox_root, cwd=sandbox_root, allowed_domains=("example.com",)
    )
    assert wider.digest() != base.digest()


def test_policy_defaults_to_no_network(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    policy = build_policy(sandbox_root=sandbox_root, cwd=sandbox_root)
    assert policy.allowed_domains == ()
    assert policy.allow_local_network is False
    assert policy.to_asrt_settings()["network"]["allowedDomains"] == []


def test_settings_file_is_written_outside_the_workspace(tmp_path: Path, client) -> None:
    """策略文件不能落在被隔离的进程脚下：否则它下次可以改自己的规则。"""
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    policy = build_policy(sandbox_root=sandbox_root, cwd=sandbox_root)

    first = asrt.ensure_settings(policy)
    second = asrt.ensure_settings(policy)
    assert first == second  # 同策略复用同一份文件
    assert not first.is_relative_to(sandbox_root.resolve())

    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["filesystem"]["allowWrite"]
    assert payload["filesystem"]["denyWrite"]
    assert payload["network"]["allowedDomains"] == []


def test_asrt_argv_has_separator_before_command(tmp_path: Path) -> None:
    """POSIX 用 `--` 分隔命令；Windows 用 `-c` 拼命令串。两者防的是不同的坑。"""
    if sys.platform == "win32":
        # Windows 不能走 `--`：ASRT 仍用 POSIX quote 拼命令串，含空格的路径会被
        # cmd 拆成 `'C:\Program`（见 `_windows_argv`）。所以 shell 执行必须落成脚本。
        script = tmp_path / "run.sh"
        script.write_text("echo hi", encoding="utf-8")
        argv = asrt.build_argv(
            srt_exe="C:/npm/srt.cmd",
            settings_path=tmp_path / "s.json",
            shell_exe="C:/Program Files/Git/bin/bash.exe",
            command="echo hi",
            script=script,
        )
        assert argv[:4] == [
            "C:/npm/srt.cmd",
            "--settings",
            str(tmp_path / "s.json"),
            "-c",
        ]
        # 含空格的解释器路径必须被整体加引号（否则 cmd 会拆成 `'C:/Program`）
        assert argv[4].startswith('"C:/Program Files/Git/bin/bash.exe"')
        assert argv[4].endswith(str(script))
        return

    argv = asrt.build_argv(
        srt_exe="C:/npm/srt.cmd",
        settings_path=tmp_path / "s.json",
        shell_exe="/bin/bash",
        command="echo hi",
    )
    assert argv[:4] == ["C:/npm/srt.cmd", "--settings", str(tmp_path / "s.json"), "--"]
    assert argv[4:] == ["/bin/bash", "-c", "echo hi"]


# ---- 环境变量收口与审计 ----


def test_env_filter_drops_credentials_and_proxies() -> None:
    env = {
        "PATH": "C:/bin",
        "SystemRoot": "C:/Windows",
        "OPENAI_API_KEY": "sk-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "HTTP_PROXY": "http://proxy",
        "EDITH_RUNTIME_TOKEN": "runtime-token",
        "GITHUB_TOKEN": "ghp_x",
    }
    kept, dropped = audit.filter_env(env)
    assert kept == {"PATH": "C:/bin", "SystemRoot": "C:/Windows"}
    assert set(dropped) == {
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HTTP_PROXY",
        "EDITH_RUNTIME_TOKEN",
        "GITHUB_TOKEN",
    }
    # 审计只记键名：任何一个值都不该出现在 dropped 里
    assert all("secret" not in item for item in dropped)


def test_command_summary_redacts_inline_secrets() -> None:
    summary = audit.command_summary(
        "curl -H 'Authorization: Bearer abc123' https://x && export API_KEY=sk-live-1"
    )
    assert "abc123" not in summary
    assert "sk-live-1" not in summary
    assert "Bearer ***" in summary


def test_command_summary_truncates_long_commands() -> None:
    summary = audit.command_summary("echo " + "a" * 500)
    assert len(summary) <= 200


# ---- 端到端：host 后端（测试专用）能跑通，证明交接没有把正常路径弄坏 ----


def test_host_backend_still_executes_in_tests(monkeypatch, client) -> None:
    from edith_backend.internal.sandbox.local import LocalSandbox

    # autouse fixture 会清掉这两个变量（用来验证 fail-closed），这里显式打开
    monkeypatch.setenv(isolation.BACKEND_ENV, "host")
    monkeypatch.setenv(isolation.UNSAFE_HOST_ENV, "1")
    isolation.reset()

    sandbox = LocalSandbox.open("isolation-user", "isolation-ok")
    result = _run(sandbox.commands.run("echo hello-isolation"))
    assert "hello-isolation" in (result.stdout or "")


def test_resolve_reports_unsafe_host_only_with_switch(monkeypatch) -> None:
    monkeypatch.setenv(isolation.BACKEND_ENV, "host")
    monkeypatch.setenv(isolation.UNSAFE_HOST_ENV, "1")
    isolation.reset()

    backend = resolve()
    assert backend.kind == "host"
    assert "没有任何 OS 级隔离" in " ".join(backend.limitations)
