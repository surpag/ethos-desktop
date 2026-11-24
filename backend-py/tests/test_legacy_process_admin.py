"""遗留进程的人工处置：inspect 只读、retry 只认确认、acknowledge 不杀不删。

核心不变量：

- `inspect` 不产生任何副作用（不改文件、不杀进程）；
- `retry` 只有"创建时刻 + 镜像"都对得上才终结，`unknown` 原地保留；
- `acknowledge` 不杀进程、不删记录、不删审计，且重复执行幂等；
- `acknowledged` 在启动恢复里**不是** gone、也**不是** recovered。
"""

from __future__ import annotations

import json
import time

import pytest

from edith_backend.internal.sandbox import process_store, processes
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


def _orphans(count: int = 1) -> list[int]:
    """起 count 个游离进程并登记，最后清空内存台账（模拟上次运行的遗留）。

    必须先全部登记再清内存：`register` 每次写的是**内存全量快照**，
    清空之后再登记会把磁盘上早先的记录冲掉。
    """
    pids: list[int] = []
    for _ in range(count):
        proc = spawn_detached()
        processes.register(OWNER, pid=proc.pid, cmd=LONG_RUNNING, cwd="/home/user/work")
        pids.append(proc.pid)
    processes.registry._by_owner.clear()
    return pids


def _orphan() -> int:
    return _orphans(1)[0]


def _row(pid: int) -> dict:
    return next(row for row in process_store.load() if row["pid"] == pid)


def _audit() -> list[dict]:
    path = process_store.audit_path()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def _alive(pid: int) -> bool:
    return processes._probe(pid)[0]


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_inspect_is_read_only() -> None:
    pid = _orphan()
    before = process_store.ledger_path().read_bytes()

    records = processes.describe()

    assert process_store.ledger_path().read_bytes() == before, "inspect 修改了登记文件"
    assert _alive(pid), "inspect 终结了进程"
    entry = next(item for item in records if item["pid"] == pid)
    assert entry["verdict"] == "confirmed"
    assert entry["status"] == "active"
    assert entry["recorded_create_time"] is not None
    assert entry["current_create_time"] is not None
    assert entry["recorded_executable"] == entry["current_executable"]


def test_retry_terminates_confirmed_record() -> None:
    pid = _orphan()

    result = processes.retry(pid)

    assert result["result"] == "recovered"
    assert result["terminated"] is True
    assert result["session_id"] == "session-1"
    assert _wait_dead(pid), "确认归属后没有真的终结"
    assert all(row["pid"] != pid for row in process_store.load())


def test_retry_keeps_unknown_record_without_kill() -> None:
    pid = _orphan()
    rows = process_store.load()
    for row in rows:
        if row["pid"] == pid:
            row["executable"] = "C:\\Windows\\System32\\notepad.exe"
    process_store.save(rows)

    result = processes.retry(pid)

    assert result["result"] == "unknown"
    assert result["terminated"] is False
    assert _alive(pid), "unknown 条目不允许执行终结"
    row = _row(pid)
    assert row["status"] == "suspected"
    assert row["last_verdict"] == "unknown"


def test_retry_refuses_acknowledged_record() -> None:
    pid = _orphan()
    processes.acknowledge(pid, "用户确认这是自己的 dev server")

    with pytest.raises(PermissionError):
        processes.retry(pid)

    row = _row(pid)
    assert row["status"] == "acknowledged"
    assert _alive(pid)


def test_acknowledge_is_idempotent() -> None:
    pid = _orphan()

    first = processes.acknowledge(pid, "先留着观察")
    second = processes.acknowledge(pid, "第二次点击")

    assert first["already_acknowledged"] is False
    assert second["already_acknowledged"] is True
    # 幂等：首次的处置时间与原因不能被后来的调用覆盖
    assert second["acknowledged_at"] == first["acknowledged_at"]
    assert second["acknowledged_reason"] == "先留着观察"
    row = _row(pid)
    assert row["status"] == "acknowledged"
    assert row["acknowledged_result"] == "not_terminated"
    assert _alive(pid), "acknowledge 不应该杀进程"


def test_acknowledged_is_not_reported_as_gone_or_recovered() -> None:
    """即使进程自己退出了，acknowledged 也不能被写成 gone。"""
    pid = _orphan()
    processes.acknowledge(pid, "确认过，不处理")
    processes.terminate_tree(pid)  # 模拟进程自己退出
    assert _wait_dead(pid)

    report = processes.recover()

    assert report.acknowledged == (pid,)
    assert pid not in report.gone
    assert pid not in report.recovered
    row = _row(pid)
    assert row["status"] == "acknowledged"
    assert row["acknowledged_reason"] == "确认过，不处理"


def test_recovery_report_separates_unknown_from_recovered() -> None:
    recoverable, suspected = _orphans(2)
    rows = process_store.load()
    for row in rows:
        if row["pid"] == suspected:
            row["executable"] = "C:\\Windows\\System32\\notepad.exe"
    process_store.save(rows)

    report = processes.recover()

    assert report.recovered == (recoverable,)
    assert report.unknown == (suspected,)
    assert suspected not in report.recovered
    assert _alive(suspected)
    assert [row["pid"] for row in process_store.load()] == [suspected]
    assert report.to_dict()["recovered"] == [recoverable]


def test_audit_history_is_never_removed() -> None:
    pid = _orphan()
    processes.acknowledge(pid, "第一次处置")
    baseline = _audit()
    assert baseline, "acknowledge 必须留下审计"

    processes.acknowledge(pid, "第二次处置")
    after = _audit()

    assert len(after) > len(baseline)
    # 原有行原样保留（追加写，不改写历史）
    assert after[: len(baseline)] == baseline
    ack_rows = [row for row in after if row["action"] == "acknowledge"]
    assert ack_rows[0]["operator_reason"] == "第一次处置"
    assert ack_rows[0]["pid"] == pid
    assert ack_rows[0]["create_time"] is not None
    assert ack_rows[0]["executable"]
    assert ack_rows[0]["session_id"] == "session-1"
