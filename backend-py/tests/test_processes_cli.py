"""`edith-processes` 的异常输入行为。

CLI 是运维唯一能碰到的入口，它的"坏输入"必须可预测：不存在、非法、重复、
损坏文件都不能抛栈或静默成功。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edith_backend.internal.sandbox import process_store, processes
from edith_backend.internal.sandbox.processes_cli import main
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


def _orphan() -> int:
    proc = spawn_detached()
    processes.register(OWNER, pid=proc.pid, cmd=LONG_RUNNING, cwd="/home/user/work")
    processes.registry._by_owner.clear()
    return proc.pid


def test_inspect_on_empty_ledger(capsys) -> None:
    # fixture 会写一个空账本；这里要测"从没写过"的那一种
    process_store.ledger_path().unlink(missing_ok=True)

    assert main(["inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "missing"
    assert payload["records"] == []


def test_inspect_on_empty_entries_is_ok(capsys) -> None:
    """空账本（存在、但没有记录）与"没有账本"都读作成功，但状态不同。"""
    assert main(["inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["records"] == []


def test_retry_unknown_pid_returns_1(capsys) -> None:
    assert main(["retry", "999999"]) == 1
    assert "没有 pid=999999" in capsys.readouterr().err


def test_acknowledge_unknown_pid_returns_1(capsys) -> None:
    assert main(["acknowledge", "999999", "--reason", "x"]) == 1
    assert "没有 pid=999999" in capsys.readouterr().err


def test_non_numeric_pid_is_rejected_by_parser() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["retry", "not-a-pid"])
    assert caught.value.code == 2


def test_acknowledge_requires_reason() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["acknowledge", "1"])
    assert caught.value.code == 2


def test_corrupt_ledger_is_reported_not_hidden(capsys) -> None:
    """损坏 ≠ 空。空 records 必须配一个非 ok 的状态和非零退出码。"""
    process_store.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    process_store.ledger_path().write_text("{broken", encoding="utf-8")

    assert main(["inspect"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "corrupt"
    assert payload["records"] == []
    assert payload["backup"], "损坏时没有给出备份路径"
    assert Path(payload["backup"]).exists()
    assert payload["backups"]


def test_acknowledge_then_retry_is_refused(capsys) -> None:
    pid = _orphan()

    assert main(["acknowledge", str(pid), "--reason", "自己的 dev server"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["result"] == "acknowledged"
    assert first["already_acknowledged"] is False

    # 幂等：第二次不覆盖首次时间
    assert main(["acknowledge", str(pid), "--reason", "再次点击"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["already_acknowledged"] is True
    assert second["acknowledged_at"] == first["acknowledged_at"]

    assert main(["retry", str(pid)]) == 2
    assert "acknowledged" in capsys.readouterr().err


def test_inspect_shows_realtime_state_for_acknowledged(capsys) -> None:
    """acknowledged 不再告警，但 inspect 必须能看到真实状态。"""
    pid = _orphan()
    processes.acknowledge(pid, "先留着")
    processes.terminate_tree(pid)

    assert main(["inspect"]) == 0
    records = json.loads(capsys.readouterr().out)["records"]
    entry = next(item for item in records if item["pid"] == pid)
    assert entry["status"] == "acknowledged"
    assert entry["alive"] is False
    assert entry["verdict"] == "gone"
    assert entry["acknowledged_reason"] == "先留着"
