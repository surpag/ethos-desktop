"""核验与终结之间的竞态（TOCTOU）。

旧流程是"先 `_inspect` 判断归属，再 `terminate_tree(pid)`"。两步之间原进程可能
退出、pid 被系统复用，于是杀到一个无关进程。

修复的关键不是"再多查一次"，而是**把核验与终结绑在同一个进程对象上**：

- Windows：`_open_verified` 返回的句柄让进程对象引用计数不为零，持有期间 pid
  不可能被复用；
- Linux：`pidfd` 绑定进程对象，信号不随 pid 复用而转向别人。

真实复现"pid 恰好被复用"不可控，所以这里的验收标准是：
1. 身份不符/证据缺失时**一定不杀**；
2. 终结路径内部**自己**复核，不信任调用方刚才那次探测；
3. 窗口内身份变化时，结果分类为 reused，而不是 recovered 或 unknown。
"""

from __future__ import annotations

import asyncio
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


def _alive(pid: int) -> bool:
    return processes._probe(pid)[0]


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_terminate_verified_kills_matching_process() -> None:
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)

    ok, reason = processes.terminate_verified(
        proc.pid, create_time=identity.create_time, image=identity.image
    )

    assert (ok, reason) == (True, "terminated")
    assert _wait_dead(proc.pid)


def test_terminate_verified_refuses_changed_create_time() -> None:
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)
    if identity.create_time is None:  # pragma: no cover
        pytest.skip("当前平台无法取得进程创建时刻")

    ok, reason = processes.terminate_verified(
        proc.pid,
        create_time=identity.create_time + 3600.0,
        image=identity.image,
    )

    assert (ok, reason) == (False, "identity_mismatch")
    assert _alive(proc.pid), "创建时刻不符时不允许终结"


def test_terminate_verified_refuses_changed_image() -> None:
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)

    ok, reason = processes.terminate_verified(
        proc.pid,
        create_time=identity.create_time,
        image="C:\\Windows\\System32\\notepad.exe",
    )

    assert (ok, reason) == (False, "identity_mismatch")
    assert _alive(proc.pid), "镜像不符时不允许终结"


def test_terminate_verified_refuses_missing_evidence() -> None:
    """拿不到创建时刻 = 没有证据 = 不杀（fail-closed）。"""
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)

    ok, reason = processes.terminate_verified(
        proc.pid, create_time=None, image=identity.image
    )

    assert (ok, reason) == (False, "identity_mismatch")
    assert _alive(proc.pid)


def test_terminate_verified_reports_already_exited() -> None:
    proc = spawn_detached()
    identity = processes._inspect(proc.pid)
    processes.terminate_tree(proc.pid)
    assert _wait_dead(proc.pid)

    ok, reason = processes.terminate_verified(
        proc.pid, create_time=identity.create_time, image=identity.image
    )

    assert (ok, reason) == (True, "already_exited")


def test_kill_goes_through_verified_path(monkeypatch) -> None:
    """工具层的 kill 必须走"核验+终结"绑定路径，并带上记录里的身份。"""
    proc = spawn_detached()
    entry = processes.register(OWNER, pid=proc.pid, cmd=LONG_RUNNING, cwd="/home/user/work")

    seen: list[tuple[int, float | None, str]] = []
    real = processes.terminate_verified

    def spy(pid: int, *, create_time, image):
        seen.append((pid, create_time, image))
        return real(pid, create_time=create_time, image=image)

    monkeypatch.setattr(processes, "terminate_verified", spy)

    asyncio.run(LocalSandbox.open(*OWNER).commands.kill(proc.pid))

    assert seen == [(proc.pid, entry.create_time, entry.executable)]
    assert _wait_dead(proc.pid)


def test_recover_classifies_identity_change_as_reused(monkeypatch) -> None:
    """核验通过、动手前身份变了：不杀，分类为 reused，而不是 recovered。"""
    proc = spawn_detached()
    processes.register(OWNER, pid=proc.pid, cmd=LONG_RUNNING, cwd="/home/user/work")
    processes.registry._by_owner.clear()

    # 模拟"这一刻 pid 上已经是另一个进程了"：终结路径内部的复核会失败
    monkeypatch.setattr(processes, "_identity_matches", lambda *args, **kwargs: False)

    report = processes.recover()

    assert proc.pid in report.reused
    assert report.recovered == ()
    assert report.unknown == ()
    assert _alive(proc.pid), "身份在窗口内变化时不允许终结"
    assert process_store.load() == []
