"""登记失败的原子性：要么"登记成功 + 进程在跑"，要么"进程已被终结 + 如实报错"。

这里防的是一类很难在日志里看出来的问题：进程起来了、工具返回"启动成功"，
但登记没写进磁盘。后端一旦被强杀，这个进程就彻底失去归属——没人知道它属于
哪个会话，也没人会在下次启动时处理它。

约束：
- 登记失败必须终结刚启动的进程（且只终结它）；
- 终结失败要留高优先级审计（pid / 创建时刻 / 镜像 / 命令 / 清理错误）；
- 不允许因为登记失败去杀"pid 上现在的那个进程"——身份不符就不杀。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from edith_backend.internal.sandbox import process_store, processes
from edith_backend.internal.sandbox.local import LocalSandbox
from process_helpers import LONG_RUNNING, cleanup_detached, spawn_detached

OWNER = ("user-1", "session-1")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    processes.registry._by_owner.clear()
    process_store.save([])
    try:
        yield
    finally:
        processes.registry._by_owner.clear()
        process_store.save([])
        cleanup_detached()


def _run_background():
    """走真实启动路径（spawn → register）。登记被 patch 失败时由代码自己回滚。"""
    sandbox = LocalSandbox.open(*OWNER)
    return asyncio.run(sandbox.commands.run(LONG_RUNNING, background=True))


def _audit() -> list[dict]:
    path = process_store.audit_path()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def test_registration_failure_terminates_started_process(monkeypatch) -> None:
    monkeypatch.setattr(process_store, "save", lambda entries, **kwargs: False)

    with pytest.raises(processes.PersistenceFailed) as caught:
        _run_background()

    exc = caught.value
    assert exc.terminated is True, exc.rollback_detail
    assert exc.rollback_detail in ("terminated", "already_exited")
    assert not processes._probe(exc.entry.pid)[0], "登记失败后进程仍在运行"
    # 内存台账与磁盘登记都不能留下这条：否则 list 会显示一个没人管的进程
    assert processes.describe() == []


def test_rollback_failure_writes_high_priority_audit(monkeypatch) -> None:
    real_terminate = processes.terminate_tree
    monkeypatch.setattr(process_store, "save", lambda entries, **kwargs: False)
    monkeypatch.setattr(
        processes, "terminate_verified", lambda *args, **kwargs: (False, "terminate_failed")
    )

    with pytest.raises(processes.PersistenceFailed) as caught:
        _run_background()

    exc = caught.value
    assert exc.terminated is False
    assert exc.rollback_detail == "terminate_failed"

    row = next(r for r in _audit() if r["action"] == "rollback_failed")
    assert row["severity"] == "high"
    assert row["pid"] == exc.entry.pid
    assert row["cmd"] == LONG_RUNNING
    assert row["executable"], "审计缺少镜像路径，事后无法判断是哪个程序"
    assert row["create_time"] is not None
    assert row["session_id"] == "session-1"
    assert row["instance_id"] == processes.registry.instance_id
    assert row["reason"] == "terminate_failed"

    real_terminate(exc.entry.pid)


def test_rollback_refuses_pid_with_different_identity() -> None:
    """身份不符就不杀：登记与回滚之间 pid 可能已被系统复用。"""
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)
    if identity.create_time is None:  # pragma: no cover - 平台取不到创建时刻
        pytest.skip("当前平台无法取得进程创建时刻")
    entry = processes.BackgroundProcess(
        pid=proc.pid,
        cmd=LONG_RUNNING,
        cwd="/home/user/work",
        started_at=time.time(),
        create_time=identity.create_time + 3600.0,
        # 镜像必须一起给：没有镜像属于"证据不足"，与"证据不符"要分开断言
        executable=identity.image,
    )

    killed, detail = processes.rollback(entry)

    assert (killed, detail) == (False, "identity_mismatch")
    assert processes._probe(proc.pid)[0], "身份不符时不应该终结任何进程"


def test_rollback_falls_back_to_no_blind_kill(monkeypatch) -> None:
    """身份在核验与终结之间变化时：不杀，也不退化成按 pid 的盲杀。"""
    blind: list[int] = []
    monkeypatch.setattr(process_store, "save", lambda entries, **kwargs: False)
    monkeypatch.setattr(processes, "terminate_tree", lambda pid: blind.append(pid) or False)
    monkeypatch.setattr(
        processes, "terminate_verified", lambda *args, **kwargs: (False, "identity_mismatch")
    )

    with pytest.raises(processes.PersistenceFailed) as caught:
        _run_background()

    assert blind == [], "身份不符时仍然调用了按 pid 的盲杀"
    assert caught.value.rollback_detail == "identity_mismatch"


def test_normal_registration_is_unaffected() -> None:
    info = _run_background()

    rows = process_store.load()
    assert [row["pid"] for row in rows] == [info.pid]
    assert rows[0]["status"] == "active"
    assert rows[0]["create_time"] is not None

    processes.release_all()
    assert process_store.load() == []
