"""故障注入：真实崩溃恢复 + 回滚的进程树验收。

**为什么不用 `asyncio.run()` 模拟崩溃**：stdlib 的 `BaseSubprocessTransport.close()`
在子进程仍运行时本身就会终结它。用关闭事件循环来"模拟后端死亡"，会把被测代码
的行为和被 stdlib 顺带杀掉的结果混在一起，证明不了恢复逻辑。

所以这里起一个**独立的后端进程**（`_crash_backend.py`），让它登记一个**独立游离
子进程**，然后用 `TerminateProcess` 强杀后端——登记留在磁盘上，进程还在跑，
与线上崩溃一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from edith_backend.internal.sandbox import process_store, processes
from edith_backend.internal.sandbox.local import LocalSandbox
from process_helpers import LONG_RUNNING, cleanup_detached

OWNER = ("user-1", "session-1")
TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
CRASH_SCRIPT = TESTS_DIR / "_crash_backend.py"


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


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_recover_after_backend_process_is_killed() -> None:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(SRC_DIR), os.environ.get("PYTHONPATH", "")]
        ).strip(os.pathsep),
    }
    backend = subprocess.Popen(
        [sys.executable, str(CRASH_SCRIPT), LONG_RUNNING],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = backend.stdout.readline() if backend.stdout else ""
        info = json.loads(line)
        orphan = int(info["pid"])
        assert info["ledger"] == str(process_store.ledger_path())
        assert _alive(orphan), "替身后端没有成功启动游离进程"
    finally:
        # 强杀：不走任何收尾逻辑，模拟后端被 TerminateProcess
        backend.kill()
        backend.wait(timeout=10)

    assert not _alive(backend.pid)
    assert _alive(orphan), "游离进程不应随登记者一起死（这正是需要恢复的场景）"

    report = processes.recover()

    assert orphan in report.recovered
    assert _wait_dead(orphan), "启动恢复没有真的结束遗留进程"
    assert process_store.load() == []

    audit_rows = [
        json.loads(row)
        for row in process_store.audit_path().read_text("utf-8").splitlines()
        if row.strip()
    ]
    terminate = [row for row in audit_rows if row["action"] == "terminate"]
    assert terminate and terminate[0]["pid"] == orphan
    assert terminate[0]["instance_id"] != processes.registry.instance_id


def _ping_count() -> int:
    output = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq PING.EXE", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return output.lower().count("ping.exe")


def _tree_command() -> str:
    if sys.platform == "win32":
        # 两个 ping 是 bash 的子进程，`wait` 让父进程活着，`taskkill /T` 才带得走整棵树
        return (
            "ping -n 60 127.0.0.1 >/dev/null 2>&1 & "
            "ping -n 60 127.0.0.1 >/dev/null 2>&1 & wait"
        )
    return "sleep 30 & sleep 30 & wait"


@pytest.mark.skipif(sys.platform != "win32", reason="进程计数依赖 tasklist")
def test_registration_failure_kills_the_whole_tree(monkeypatch) -> None:
    """回滚要带走子进程，不只是父进程。"""
    baseline = _ping_count()
    monkeypatch.setattr(process_store, "save", lambda entries, **kwargs: False)

    with pytest.raises(processes.PersistenceFailed) as caught:
        asyncio.run(
            LocalSandbox.open(*OWNER).commands.run(_tree_command(), background=True)
        )

    exc = caught.value
    assert exc.terminated is True
    assert _wait_dead(exc.entry.pid)
    # 父进程退出后子进程可能还没被回收完，给一点时间
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _ping_count() > baseline:
        time.sleep(0.1)
    assert _ping_count() <= baseline, "回滚只杀了父进程，子进程残留"


def _deep_tree_command() -> str:
    """两层子 shell：bash → 子 shell → ping（孙进程）。

    `taskkill /T` 走的是进程树遍历，层级越深越容易出现"父没了、孙还在"。
    """
    return "( ping -n 60 127.0.0.1 >/dev/null 2>&1 & wait ) & wait"


@pytest.mark.skipif(sys.platform != "win32", reason="进程计数依赖 tasklist")
def test_rollback_reaches_grandchildren(monkeypatch) -> None:
    baseline = _ping_count()
    monkeypatch.setattr(process_store, "save", lambda entries, **kwargs: False)

    with pytest.raises(processes.PersistenceFailed) as caught:
        asyncio.run(
            LocalSandbox.open(*OWNER).commands.run(_deep_tree_command(), background=True)
        )

    assert caught.value.terminated is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _ping_count() > baseline:
        time.sleep(0.1)
    assert _ping_count() <= baseline, "孙进程残留：树杀没有覆盖到深层子进程"
