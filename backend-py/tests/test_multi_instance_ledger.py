"""多实例写登记：不能互相覆盖。

旧的 `save` 是"内存全量快照 → 整文件替换"。两个后端实例各写各的时，后写的
会把先写的进程从登记里抹掉——而先写的那一方并不知道自己的进程已经"消失"。

修复后的语义：
- 带 `instance_id` 的写是**合并**：只替换本实例的条目；
- 不带 `instance_id` 的写是**覆盖**：运维处置要表达"这条没了"，合并不适用；
- 两条路径都在跨进程文件锁里完成。
"""

from __future__ import annotations

import os
import threading

import pytest

from edith_backend.internal.sandbox import process_store


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    process_store.save([])
    yield
    process_store.save([])


def _row(pid: int, instance: str) -> dict:
    return {
        "pid": pid,
        "instance_id": instance,
        "status": "active",
        "create_time": 1.0,
        "executable": "bash",
        "cmd": "sleep 1",
        "cwd": "/home/user/work",
        "started_at": 1.0,
        "user_id": "user-1",
        "session_id": "session-1",
    }


def test_two_instances_do_not_overwrite_each_other() -> None:
    process_store.save([_row(101, "A")], instance_id="A")
    process_store.save([_row(202, "B")], instance_id="B")

    assert {row["pid"] for row in process_store.load()} == {101, 202}


def test_instance_removal_only_touches_own_entries() -> None:
    process_store.save([_row(101, "A"), _row(102, "A")], instance_id="A")
    process_store.save([_row(303, "B")], instance_id="B")

    process_store.save([], instance_id="A")

    assert {row["pid"] for row in process_store.load()} == {303}


def test_concurrent_writes_are_serialized() -> None:
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(instance: str, pid: int) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(15):
                process_store.save([_row(pid, instance)], instance_id=instance)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("A", 101)),
        threading.Thread(target=writer, args=("B", 202)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert {row["pid"] for row in process_store.load()} == {101, 202}


def test_merge_reads_inside_the_lock(monkeypatch) -> None:
    """`读 → 合并 → 写`必须在同一把锁里完成。

    只在写文件时加锁是不够的：读到的旧快照可能在写之前就被别的实例改了，
    随后用旧快照覆盖回去，等于撤销了对方的登记。这里通过"合并读发生时，外层
    是否已经持锁"来证明顺序。
    """
    locked: list[bool] = []
    real = process_store._read_rows

    def spy() -> list[dict]:
        probe = (process_store._root() / process_store._LOCK_NAME).open("a+b")
        try:
            probe.seek(0, os.SEEK_END)
            if probe.tell() == 0:
                probe.write(b"\0")
                probe.flush()
            probe.seek(0)
            try:
                process_store._try_lock(probe)
                locked.append(False)  # 竟然能锁上 → 说明外层没持锁
            except OSError:
                locked.append(True)
        finally:
            probe.close()
        return real()

    monkeypatch.setattr(process_store, "_read_rows", spy)
    process_store.save([_row(101, "A")], instance_id="A")

    assert locked == [True], "合并写读取磁盘时没有持锁"


def test_save_fails_closed_when_lock_is_held() -> None:
    """锁等不到就放弃写入（返回 False），而不是绕过锁硬写。"""
    lock_path = process_store._root() / process_store._LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("a+b")
    holder.seek(0, os.SEEK_END)
    if holder.tell() == 0:
        holder.write(b"\0")
        holder.flush()
    holder.seek(0)
    process_store._try_lock(holder)
    try:
        assert process_store.save([_row(101, "A")], instance_id="A") is False
        assert process_store.load() == []
    finally:
        process_store._unlock(holder)
        holder.close()


def test_operator_write_replaces_whole_file() -> None:
    """运维路径（无 instance_id）是覆盖写：处置必须能真正删掉记录。"""
    process_store.save([_row(101, "A"), _row(202, "B")], instance_id="A")
    process_store.save([_row(202, "B")])

    assert {row["pid"] for row in process_store.load()} == {202}
