"""异常退出恢复：后端被强杀后，下一次启动要能认出并处理自己遗留的后台进程。

这些用例模拟的正是 `lifespan` 跑不到的那条路径：

    启动 → 起后台进程 → 登记落盘 → 后端被 kill -9（内存台账消失）
         → 重新启动 → 读回登记 → 证明归属 → 终结 / 保留

"模拟重启"通过清空内存台账实现（`_simulate_restart`），磁盘登记保持不动。
这是唯一能在一个测试进程里复现"内存没了、文件还在"的办法。

进程用 `spawn_detached()` 起（`subprocess.Popen`）：`commands.run` 的 transport
绑在事件循环上，`asyncio.run` 返回时会替我们杀掉进程，测不出恢复逻辑。
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
    """独立的 data_root + 空台账；结束时清掉本次启动的所有进程。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    processes.registry._by_owner.clear()
    process_store.save([])
    try:
        yield
    finally:
        # 不走 release_all：它会把"清理"也写进登记，干扰下一个用例的断言
        processes.registry._by_owner.clear()
        process_store.save([])
        cleanup_detached()


def _simulate_restart() -> None:
    """后端被强杀后重启：内存台账消失，磁盘登记还在。"""
    processes.registry._by_owner.clear()


def _orphan(owner: tuple[str, str] = OWNER) -> int:
    """启动一个游离进程并登记它，返回 pid。"""
    proc = spawn_detached()
    processes.register(owner, pid=proc.pid, cmd=LONG_RUNNING, cwd="/home/user/work")
    return proc.pid


def _alive(pid: int) -> bool:
    return processes._probe(pid)[0]


def _audit() -> list[dict]:
    path = process_store.audit_path()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_register_persists_identity() -> None:
    """登记必须落盘，且带够判定归属所需的字段。"""
    pid = _orphan()

    rows = process_store.load()
    row = next(item for item in rows if item["pid"] == pid)
    assert row["user_id"] == "user-1"
    assert row["session_id"] == "session-1"
    assert row["create_time"] is not None
    assert row["executable"], "没有镜像路径就无法在重启后证明归属"
    assert row["instance_id"] == processes.registry.instance_id


def test_kill_removes_persisted_entry() -> None:
    pid = _orphan()
    sandbox = LocalSandbox.open(*OWNER)
    asyncio.run(sandbox.commands.kill(pid))

    assert _wait_dead(pid)
    assert all(item["pid"] != pid for item in process_store.load())


def test_recover_reclaims_confirmed_orphan() -> None:
    """创建时刻与镜像都对得上 → 确认归属，终结进程并清掉记录。"""
    pid = _orphan()
    _simulate_restart()

    report = processes.recover()

    assert pid in report.reclaimed
    assert _wait_dead(pid), "确认归属后没有真的结束进程"
    assert process_store.load() == []


def test_recover_drops_reused_pid_without_kill() -> None:
    """创建时刻对不上 = pid 已被系统复用：只删记录，绝不杀。"""
    pid = _orphan()
    rows = process_store.load()
    for row in rows:
        if row["pid"] == pid:
            row["create_time"] = (row["create_time"] or 0) + 3600.0
    process_store.save(rows)
    _simulate_restart()

    report = processes.recover()

    assert pid in report.stale
    assert report.reclaimed == ()
    assert _alive(pid), "pid 复用的记录不应该触发终结"
    assert process_store.load() == []


def test_recover_keeps_unconfirmed_without_kill() -> None:
    """镜像对不上 = 无法证明是同一个进程：保留记录，不杀。"""
    pid = _orphan()
    rows = process_store.load()
    for row in rows:
        if row["pid"] == pid:
            row["executable"] = "C:\\Windows\\System32\\notepad.exe"
    process_store.save(rows)
    _simulate_restart()

    report = processes.recover()

    assert pid in report.suspected
    assert report.reclaimed == ()
    assert _alive(pid), "归属无法确认时必须 fail-closed"
    remaining = process_store.load()
    assert [item["pid"] for item in remaining] == [pid], "可疑记录应保留供人工处理"


def test_recover_skips_entry_without_identity() -> None:
    """连创建时刻都没有（旧版本记录 / 平台取不到）：同样不杀。"""
    pid = _orphan()
    rows = process_store.load()
    for row in rows:
        if row["pid"] == pid:
            row["create_time"] = None
    process_store.save(rows)
    _simulate_restart()

    report = processes.recover()

    assert pid in report.suspected
    assert _alive(pid)
    assert [item["pid"] for item in process_store.load()] == [pid]


def test_recover_is_idempotent() -> None:
    """重复恢复不产生额外副作用：第二次看不到任何记录。"""
    pid = _orphan()
    _simulate_restart()

    first = processes.recover()
    second = processes.recover()

    assert pid in first.reclaimed
    assert first.touched
    assert not second.touched
    assert second.reclaimed == () and second.stale == () and second.suspected == ()


def test_corrupt_ledger_is_moved_aside() -> None:
    """损坏的登记不能拿来杀进程：备份留证，按空处理。"""
    _orphan()
    ledger = process_store.ledger_path()
    ledger.write_text("{not json", encoding="utf-8")

    assert process_store.load() == []
    report = processes.recover()
    assert not report.touched
    assert list(ledger.parent.glob("background-processes.json.corrupt-*"))


def test_recovery_writes_audit() -> None:
    """恢复动作要留痕，且记录足以回答"杀的是谁、为什么"。"""
    pid = _orphan()
    _simulate_restart()

    processes.recover()

    lines = process_store.audit_path().read_text("utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    terminate = [r for r in records if r.get("action") == "terminate"]
    assert terminate, records
    assert terminate[0]["pid"] == pid
    assert terminate[0]["reason"] == "confirmed"
    assert terminate[0]["session_id"] == "session-1"
    assert any(r.get("action") == "recover_summary" for r in records)


def test_corrupt_ledger_blocks_recovery() -> None:
    """登记损坏 = 无法判断。此时**不得**终结任何进程，也不得报成"没有遗留"。"""
    proc = spawn_detached()
    ledger = process_store.ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not json", encoding="utf-8")

    report = processes.recover()

    assert report.registry_status == "corrupt"
    assert not report.touched
    assert processes._probe(proc.pid)[0], "登记不可读时不得终结任何进程"

    records = _audit()
    blocked = [row for row in records if row["action"] == "recover_blocked"]
    assert blocked and blocked[0]["severity"] == "high"
    assert blocked[0]["backup"]


def test_missing_ledger_is_not_corrupt() -> None:
    process_store.ledger_path().unlink(missing_ok=True)

    report = processes.recover()

    assert report.registry_status == "missing"
    assert not report.touched
