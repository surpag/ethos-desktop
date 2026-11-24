"""结构化审计与 git 执行旁路的收口。

守两件事：

1. **每一次执行都有可查询的记录**：哪个后端、什么策略指纹、归属哪条 tool_call、
   退出码与耗时；被拒绝的执行同样留痕。
2. **git 不再有宿主旁路**：`push` / `ls-remote` 的 git 进程与 Shell 走同一个
   隔离入口；后端不可用时直接拒绝，绝不回落宿主直执行。
"""

from __future__ import annotations

import asyncio

import pytest

from edith_backend.internal import isolation
from edith_backend.internal.gitops import push as push_mod
from edith_backend.internal.gitops.push import PushError, _remote_domains, _run_git
from edith_backend.internal.isolation.capabilities import BackendCapability
from edith_backend.internal.sandbox.local import LocalSandbox


def _unavailable(name: str) -> BackendCapability:
    return BackendCapability(name=name, kind=name, available=False, detail=f"{name} 未安装")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_backend(monkeypatch):
    monkeypatch.delenv(isolation.UNSAFE_HOST_ENV, raising=False)
    isolation.reset()
    yield
    isolation.reset()


def _host_backend(monkeypatch) -> None:
    monkeypatch.setenv(isolation.BACKEND_ENV, "host")
    monkeypatch.setenv(isolation.UNSAFE_HOST_ENV, "1")
    isolation.reset()


def _no_backend(monkeypatch) -> None:
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


# ---- 审计落库 ----


def test_execution_writes_queryable_audit_row(monkeypatch, client) -> None:
    _host_backend(monkeypatch)
    sandbox = LocalSandbox.open("audit-user", "audit-session")

    token = isolation.audit.bind_tool_call("call_abc123")
    try:
        _run(sandbox.commands.run("echo audit-me"))
    finally:
        isolation.audit.reset_tool_call(token)

    response = client.get(
        "/internal/sandbox/audit", params={"sessionId": "audit-session"}
    )
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert entries, "执行过就必须有一条审计记录"
    row = entries[0]
    assert row["tool_call_id"] == "call_abc123"
    assert row["tool_name"] == ""  # 未经过工具层时为空，不伪造
    assert row["backend"] == "host"
    assert row["outcome"] == "exited"
    assert row["exit_code"] == 0
    assert row["duration_ms"] is not None
    assert row["command"] == "echo audit-me"
    assert row["policy_version"].startswith("ethos-sandbox-policy/")
    assert len(row["policy_digest"]) == 64


def test_refused_execution_is_audited(monkeypatch, client) -> None:
    """被拒绝的执行也要有记录：事后要能回答"什么时候开始拒绝的、为什么"。"""
    _no_backend(monkeypatch)
    sandbox = LocalSandbox.open("audit-user", "audit-refused")
    with pytest.raises(isolation.SandboxUnavailable):
        _run(sandbox.commands.run("echo never-runs"))

    entries = client.get(
        "/internal/sandbox/audit", params={"sessionId": "audit-refused"}
    ).json()["entries"]
    assert entries
    row = entries[0]
    assert row["outcome"] == "refused"
    assert row["reason"] == "not_installed"
    assert row["backend"] == ""


def test_audit_never_stores_credentials(monkeypatch, client) -> None:
    _host_backend(monkeypatch)
    sandbox = LocalSandbox.open("audit-user", "audit-secret")
    _run(
        sandbox.commands.run(
            "echo 'Authorization: Bearer sk-live-secret-123'",
            envs={"OPENAI_API_KEY": "sk-should-not-leak"},
        )
    )

    rows = client.get(
        "/internal/sandbox/audit", params={"sessionId": "audit-secret"}
    ).json()["entries"]
    assert rows
    blob = str(rows)
    assert "sk-live-secret-123" not in blob
    assert "sk-should-not-leak" not in blob
    # 键名可以出现（用于证明"确实被丢掉了"），值不行
    assert "OPENAI_API_KEY" in rows[0]["dropped_env"] or rows[0]["backend"] == "host"


def test_tool_call_binding_reaches_exec_plan(monkeypatch, tmp_path) -> None:
    _host_backend(monkeypatch)
    token = isolation.audit.bind_tool_call("call_bound")
    try:
        plan = isolation.plan(
            sandbox_root=tmp_path,
            cwd=tmp_path,
            command="echo x",
            env={"PATH": "x"},
            shell_exe="bash",
        )
    finally:
        isolation.audit.reset_tool_call(token)
    assert plan.tool_call_id == "call_bound"


# ---- git 旁路 ----


def test_remote_domains_extracts_host() -> None:
    assert _remote_domains("https://github.com/a/b.git") == ("github.com",)
    assert _remote_domains("ssh://git@example.com/a.git") == ("example.com",)
    assert _remote_domains("") == ()


def test_git_push_refuses_without_backend(monkeypatch) -> None:
    """最关键的一条：没有沙箱就没有 push，不存在"用户点的所以放行"。"""
    _no_backend(monkeypatch)
    with pytest.raises(PushError) as excinfo:
        _run(
            push_mod.push_bundle(
                b"fake-bundle", "https://github.com/a/b.git", "main", "token-123"
            )
        )
    assert "命令未执行" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_git_push_goes_through_isolation_with_credentials_in_env(
    monkeypatch, tmp_path
) -> None:
    """凭据只进环境变量：不进 argv、不进 settings、审计只记键名。"""
    captured: dict = {}

    def fake_plan(**kwargs):
        captured.update(kwargs)
        raise PushError("stop-before-spawn")

    monkeypatch.setattr(push_mod.isolation, "plan", fake_plan)

    with pytest.raises(PushError):
        _run(
            _run_git(
                ["push", "https://github.com/a/b.git", "refs/heads/x:refs/heads/x"],
                str(tmp_path),
                "token-abc",
                purpose="git-push",
                domains=("github.com",),
            )
        )

    assert captured["argv"][0] == "git"
    assert captured["argv"][1] == "push"
    assert "token-abc" not in " ".join(captured["argv"])
    assert captured["extra_domains"] == ("github.com",)
    assert captured["purpose"] == "git-push"
    assert captured["extra_writable"] == (str(tmp_path),)
    auth = captured["extra_env"]
    assert auth["GIT_CONFIG_COUNT"] == "1"
    assert auth["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")


def test_git_local_steps_get_no_network(monkeypatch, tmp_path) -> None:
    """init / fetch 不出网：不给任何域名，避免把"本地操作"也顺手放行。"""
    captured: dict = {}

    def fake_plan(**kwargs):
        captured.update(kwargs)
        raise PushError("stop-before-spawn")

    monkeypatch.setattr(push_mod.isolation, "plan", fake_plan)
    with pytest.raises(PushError):
        _run(_run_git(["init", "--bare", "-q"], str(tmp_path), None, purpose="git-init"))
    assert captured["extra_domains"] == ()
    assert captured["extra_env"] is None
