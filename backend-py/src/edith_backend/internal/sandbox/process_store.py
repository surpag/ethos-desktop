"""后台进程登记的持久化：给"后端被强杀"留一条可恢复的线索。

内存台账（`processes.BackgroundRegistry`）解决的是同一进程内多个 `LocalSandbox`
实例互相看不见的问题。它解决不了另一件事：**`lifespan` 没机会执行**。

    ETHOS 启动 → 模型起 dev server（登记 pid）→ 后端被 kill -9 / 蓝屏重启
              → 进程还在跑
    下次启动 → 内存台账是空的 → 没有任何人知道它存在

这份文件就是那条线索。它只记录**身份**（pid、创建时刻、镜像路径、归属会话），
不记录凭据、不记录输出。

**写文件失败不阻塞命令。** 进程已经起来了，让工具调用失败并不能把它收回去；
正确的降级是继续执行 + 记一条 warning——恢复能力的损失是"下次启动看不到它"，
而阻塞执行会让用户当场用不了命令。两种坏处里选小的。

**读文件失败一律按"没有记录"处理。** 半截 JSON 里可能有被杀错的风险，而少恢复
一个孤儿只是让用户手动清一次。恢复逻辑不允许"猜测"。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...config import data_root

logger = logging.getLogger(__name__)

#: 记录格式版本。将来加字段时用它分流，而不是猜旧文件长什么样。
VERSION = 1

_LEDGER_NAME = "background-processes.json"
_AUDIT_NAME = "process-recovery.jsonl"
_LOCK_NAME = ".processes.lock"

#: 等锁的上限。超过就放弃写入（返回 False），不阻塞调用方——写登记失败已经
#: 有回滚路径处理，卡住命令执行只会让故障表现得更糟。
_LOCK_TIMEOUT = 2.0


def _root() -> Path:
    return data_root() / "sandbox"


def _try_lock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _locked(timeout: float = _LOCK_TIMEOUT):
    """跨进程排他锁。`yield` 出是否拿到锁。

    为什么需要它：`save` 写的是"读-合并-整文件替换"。两个后端实例同时写时，
    没有锁就会后写覆盖先写——A 刚登记的进程在 B 的一次写里消失，而 B 并不知道
    自己抹掉了什么。锁文件独立于数据文件，因为数据文件会被 `os.replace` 换掉，
    锁在它上面会失效。
    """
    path = _root() / _LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        # Windows 的 msvcrt 要求锁定区域内存在字节；空锁文件可能让 LK_NBLCK 失败。
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                _try_lock(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                _unlock(handle)
            except OSError:
                pass
        handle.close()


def ledger_path() -> Path:
    return _root() / _LEDGER_NAME


def audit_path() -> Path:
    return _root() / _AUDIT_NAME


@dataclass(frozen=True)
class LedgerState:
    """登记文件的读取结果。**"没有记录"与"读不出来"是两件事。**

    `status` 取值：`ok` / `missing` / `corrupt` / `unreadable` / `shape_error`。

    为什么必须分开：`inspect` 若把损坏文件报成"空"，运维会得出"没有遗留进程"的
    结论——一个由解析失败伪造出来的安全结论。损坏时记录被移到 `.corrupt-*`
    备份，路径一并返回，供人工查看。
    """

    entries: list[dict] = field(default_factory=list)
    status: str = "ok"
    backup: str | None = None
    detail: str = ""
    backups: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        return self.status in ("ok", "missing")


def _backups() -> tuple[str, ...]:
    try:
        return tuple(sorted(str(path) for path in _root().glob(f"{_LEDGER_NAME}.corrupt-*")))
    except OSError:
        return ()


def read_ledger() -> LedgerState:
    """读取登记并如实描述状态。不执行任何终结。"""
    path = ledger_path()
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return LedgerState(status="missing", backups=_backups())
    except OSError as exc:
        logger.warning("process_ledger_unreadable path=%s error=%s", path, exc)
        return LedgerState(status="unreadable", detail=str(exc), backups=_backups())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 损坏的登记不能拿来杀进程：备份留证，然后当它不存在——但**要说明**它损坏过。
        backup_path = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        moved: str | None = None
        try:
            path.replace(backup_path)
            moved = str(backup_path)
            logger.warning("process_ledger_corrupt moved_to=%s", backup_path)
        except OSError:
            logger.warning("process_ledger_corrupt path=%s", path)
        return LedgerState(
            status="corrupt",
            backup=moved,
            detail=str(exc),
            backups=_backups(),
        )
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        logger.warning("process_ledger_unexpected_shape path=%s", path)
        return LedgerState(
            status="shape_error",
            detail="entries 不是列表",
            backups=_backups(),
        )
    rows = [
        item
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("pid"), int)
    ]
    return LedgerState(entries=rows, status="ok", backups=_backups())


def load() -> list[dict]:
    """只要记录本身（兼容旧调用）。需要区分损坏/缺失时用 `read_ledger`。"""
    return read_ledger().entries


def _read_rows() -> list[dict]:
    return read_ledger().entries


def _merge_with_disk(
    entries: list[dict],
    instance_id: str,
    patch_pids: frozenset[int],
    drop_pids: frozenset[int],
) -> list[dict]:
    """只替换本实例的条目，保留其他实例写的记录。

    没有这一步，两个后端实例会互相抹掉对方的登记：各写各的内存快照，后写的
    覆盖先写的，而先写的那一方并不知道自己的进程从登记里消失了。

    **删除和修改"别人的行"必须显式声明**（`drop_pids` / `patch_pids`）：遗留记录
    属于上一个实例，它的 `instance_id` 与当前实例不同，仅凭"本实例快照里没有它"
    无法区分"我要删它"和"它不是我的、别动"。这也是启动恢复删不掉记录的原因。
    """
    merged = {
        row["pid"]: row
        for row in _read_rows()
        if row.get("instance_id") != instance_id
    }
    for row in entries:
        if row.get("instance_id") == instance_id:
            merged[row["pid"]] = row
    patched = {row["pid"]: row for row in entries if row["pid"] in patch_pids}
    for pid, row in patched.items():
        if pid in merged:  # 只修改已存在的记录，不凭 patch 凭空造一条
            merged[pid] = row
    for pid in drop_pids:
        merged.pop(pid, None)
    return list(merged.values())


def save(
    entries: list[dict],
    *,
    instance_id: str = "",
    patch_pids: frozenset[int] | set[int] = frozenset(),
    drop_pids: frozenset[int] | set[int] = frozenset(),
) -> bool:
    """原子写入。返回是否成功；失败只记日志，由调用方决定是否继续。

    - `instance_id` 非空 → 合并写；为空 → 整文件覆盖（仅用于清空/测试）；
    - `patch_pids` → 允许修改其他实例登记的记录（运维处置）；
    - `drop_pids` → 允许删除其他实例登记的记录（启动恢复）。
    """
    path = ledger_path()
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked() as acquired:
            if not acquired:
                logger.warning("process_ledger_lock_timeout path=%s", path)
                return False
            payload = (
                _merge_with_disk(
                    entries, instance_id, frozenset(patch_pids), frozenset(drop_pids)
                )
                if instance_id
                else entries
            )
            # 先写临时文件再 replace：进程若在写入中途被强杀，磁盘上要么是旧文件、
            # 要么是新文件，不会出现半截 JSON——而恢复逻辑的第一件事就是解析它。
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), prefix=".processes-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": VERSION, "entries": payload},
                    handle,
                    ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("process_ledger_write_failed path=%s error=%s", path, exc)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def audit(record: dict) -> None:
    """恢复动作留痕：JSONL 追加。

    不走数据库：恢复发生在 `lifespan` 的早期，而审计表由隔离层自己初始化；
    为了让"恢复失败"不依赖另一个模块的初始化顺序，这里用独立文件。
    """
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 审计是追加写，但两个实例同时追加仍可能交错出半行。跟登记用同一把锁，
        # 代价是几毫秒，换来的是"日志永远可被逐行解析"。
        with _locked() as acquired:
            if not acquired:
                logger.warning("process_recovery_audit_lock_timeout")
                return
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"ts": time.time(), **record}, ensure_ascii=False) + "\n"
                )
    except OSError as exc:
        logger.warning("process_recovery_audit_failed error=%s", exc)
