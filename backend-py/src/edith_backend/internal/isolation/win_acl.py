"""Windows：沙箱账户在家中敏感目录上的 DENY ACE 生命周期管理。

历史教训（本机实测，见 `docs/os-sandbox.md` 第 8 节）：

- `srt-win acl stamp` 给「parent 是用户主目录」的目标加 `FILE_DELETE_CHILD` DENY
  时会 CPU 空转，所以这些拒绝不能交给 ASRT 的 `denyRead`；
- 用 `icacls` 名字匹配做幂等判定不可靠：`.config\\gcloud` 明明有继承 DENY
  （显示成 `(DENY)(S,DC)`）却被判成"未加固"，会重复叠加 ACE；
- `ensure_deny` 只在 `resolve()` 跑一次，而 srt 会在 `--deny-read` 指向不存在路径时
  把目录"物化"出来（`.azure` / `.gnupg` 就是 23:38 这样出现的），于是这些新目录
  永远漏加固。

因此这里的原则：
1. **按 SID 精确匹配**（名称相同、SID 不同不算）；
2. **每次写都读回校验**，不信 API 返回码；
3. **状态文件与真实 DACL 对账**，`acl-state.json` 只是缓存，不是真相；
4. **路径不存在不静默跳过**：记入 `missing`，一旦它出现而没加固，验证就失败；
5. **只撤销自己创建的那一条**（`win_dacl.remove_ace_exact` 三要素匹配）。
"""

from __future__ import annotations

import json
import logging
import msvcrt
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ...config import data_root
from . import win_dacl

logger = logging.getLogger(__name__)

SANDBOX_ACCOUNT_NAME = "srt-sandbox"

#: 加固时写入的 ACE 形状：显式 DENY，全权掩码，容器+对象继承。
DENY_MASK = win_dacl.ACL_MASK_ALL
DENY_FLAGS = win_dacl.OBJECT_INHERIT_ACE | win_dacl.CONTAINER_INHERIT_ACE

_STATE_VERSION = 1
_LOCK_TIMEOUT = 10.0


def sandbox_account() -> str | None:
    computer = os.environ.get("COMPUTERNAME", "").strip()
    return f"{computer}\\{SANDBOX_ACCOUNT_NAME}" if computer else None


def resolve_sandbox_sid() -> tuple[str | None, str]:
    """沙箱账户的 SID。账户不存在时返回 `(None, 原因)`。"""
    if sys.platform != "win32":  # pragma: no cover
        return None, "非 Windows"
    account = sandbox_account()
    if account is None:
        return None, "取不到 COMPUTERNAME"
    sid = win_dacl.lookup_sid(account)
    if sid is None:
        return None, f"账户不存在或无法解析: {account}"
    return sid, "ok"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path() -> Path:
    return data_root() / "isolation" / "acl-state.json"


@contextmanager
def _state_lock() -> Iterator[None]:
    """跨进程独占锁：两个实例同时启动时不至于互相覆盖状态文件。"""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    handle = open(lock, "a+b")
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield
    finally:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - 解锁失败不影响正确性
            pass
        handle.close()


def _load_state(sid: str | None) -> dict:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
        raw = {"version": _STATE_VERSION, "entries": [], "missing": []}
    # SID 变了（账户重建）= 之前记录的 ACE 全部失效，不能沿用
    if sid and raw.get("sid") and raw["sid"] != sid:
        raw = {"version": _STATE_VERSION, "entries": [], "missing": []}
    raw.setdefault("entries", [])
    raw.setdefault("missing", [])
    if sid:
        raw["sid"] = sid
    return raw


def _save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def expected_flags(path: str) -> int:
    """目录用 `(OI)(CI)`；**文件只能是 0** —— 系统会把文件 ACE 的 OI/CI 规范化掉。

    早期版本对文件也写 `(OI)(CI)`，读回变成 0 → 匹配不上 → 重复叠加 ACE。
    """
    return DENY_FLAGS if Path(path).is_dir() else 0


def _has_deny(path: str, sid: str) -> bool:
    """真实 DACL 里是否有**我们这种形状**的显式 DENY（不看状态文件）。"""
    try:
        view = win_dacl.read_dacl(path)
    except win_dacl.WinDaclError:
        return False
    flags = expected_flags(path)
    return any(
        ace.is_deny
        and not ace.is_inherited
        and ace.sid.casefold() == sid.casefold()
        and ace.mask == DENY_MASK
        and ace.inheritance_flags == flags
        for ace in view.aces
    )


def _entry_for(path: str, sid: str) -> dict:
    return {
        "path": str(path),
        "sid": sid,
        "ace_type": win_dacl.ACCESS_DENIED_ACE_TYPE,
        "mask": DENY_MASK,
        "ace_flags": expected_flags(str(path)),
        "verified_at": _now(),
    }


def verify_all(paths: tuple[str, ...], *, sid: str | None = None) -> tuple[bool, str]:
    """**只读**校验：必须保护的路径要么已加固，要么确实不存在。

    这是 `plan()` 生成 ExecPlan 前的门禁——不能靠"启动时做过一次"就认为一直安全。
    """
    sid = sid or resolve_sandbox_sid()[0]
    if sid is None:
        return False, "取不到沙箱账户 SID"
    missing: list[str] = []
    unprotected: list[str] = []
    for raw in paths:
        if not Path(raw).exists():
            missing.append(raw)
            continue
        if not _has_deny(raw, sid):
            unprotected.append(raw)
    if unprotected:
        return False, f"以下敏感目录未加固：{', '.join(unprotected)}"
    return True, f"全部已加固（{len(paths) - len(missing)} 个，{len(missing)} 个不存在）"


def ensure_deny(paths: tuple[str, ...], *, sid: str | None = None) -> tuple[bool, str]:
    """确保每个存在的路径都有 DENY ACE；不存在的记入 `missing`。

    写入后**立即读回校验**；任何一条失败返回 `(False, …)`，由调用方 fail-closed。
    """
    if sys.platform != "win32":  # pragma: no cover
        return True, "非 Windows，跳过"
    sid = sid or resolve_sandbox_sid()[0]
    if sid is None:
        return False, "取不到沙箱账户 SID（srt-sandbox 账户不存在？）"

    problems: list[str] = []
    hardened = 0
    missing: list[str] = []

    with _state_lock():
        state = _load_state(sid)
        known = {e["path"]: e for e in state["entries"]}
        for raw in paths:
            path = Path(raw)
            if not path.exists():
                missing.append(str(path))
                continue
            try:
                flags = expected_flags(str(path))
                # 先收敛历史重复 ACE（文件上曾错误地用 (OI)(CI) 标志）
                win_dacl.dedupe_matching(
                    str(path),
                    sid=sid,
                    ace_type=win_dacl.ACCESS_DENIED_ACE_TYPE,
                    mask=DENY_MASK,
                    ace_flags=flags,
                )
                if not _has_deny(str(path), sid):
                    win_dacl.add_deny_ace(str(path), sid, mask=DENY_MASK, flags=flags)
                # 不信写入返回值：读回真实 DACL
                if not _has_deny(str(path), sid):
                    problems.append(f"{path}: 写入后校验失败")
                    continue
            except win_dacl.WinDaclError as exc:
                problems.append(f"{path}: {exc}")
                continue
            hardened += 1
            known[str(path)] = _entry_for(str(path), sid)
        state["entries"] = sorted(known.values(), key=lambda e: e["path"])
        state["missing"] = sorted(set(missing))
        state["updated_at"] = _now()
        _save_state(state)

    detail = f"已加固 {hardened} 个路径"
    if missing:
        detail += f"，{len(missing)} 个不存在（记入 missing，创建后会在下次执行前补加固）"
    if problems:
        return False, "；".join(problems)
    return True, detail


def reconcile(paths: tuple[str, ...], *, sid: str | None = None) -> tuple[bool, str]:
    """状态文件与真实 DACL 对账：补缺失、作废已消失的记录、收养未记录的 ACE。

    "收养"指 DACL 里已有我们这种形状的 DENY 但状态文件没有（上次写完 ACE 后崩在
    写状态之前）——补进状态，既不重复添加，也不在卸载时漏删。
    """
    sid = sid or resolve_sandbox_sid()[0]
    if sid is None:
        return False, "取不到沙箱账户 SID"
    with _state_lock():
        state = _load_state(sid)
        entries = {e["path"]: e for e in state["entries"]}
        adopted: list[str] = []
        dropped: list[str] = []
        for path_text in list(entries):
            if not _has_deny(path_text, sid):
                entries.pop(path_text, None)
                dropped.append(path_text)
        for raw in paths:
            if Path(raw).exists() and _has_deny(raw, sid) and raw not in entries:
                entries[raw] = _entry_for(raw, sid)
                adopted.append(raw)
        state["entries"] = sorted(entries.values(), key=lambda e: e["path"])
        state["updated_at"] = _now()
        _save_state(state)
    return True, f"对账完成（收养 {len(adopted)}，作废 {len(dropped)}）"


def _srt_running() -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq srt-win.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return True  # 判断不了就当作有活动任务，宁可不删
    return "srt-win.exe" in (proc.stdout or "").lower()


def cleanup(
    *, only_paths: tuple[str, ...] | None = None, sid: str | None = None
) -> tuple[bool, str]:
    """显式撤销 ETHOS 自己创建的 DENY ACE（卸载/修复用）。

    前置条件：没有活动沙箱进程。只删状态文件里记录、且真实 DACL 中三要素完全匹配
    的那一条；其他主体的 ACE 一律不动。
    """
    if _srt_running():
        return False, "仍有 srt 沙箱进程在运行，拒绝撤销 DENY"
    sid = sid or resolve_sandbox_sid()[0]
    if sid is None:
        return False, "取不到沙箱账户 SID"
    removed: list[str] = []
    failed: list[str] = []
    with _state_lock():
        state = _load_state(sid)
        keep: list[dict] = []
        for entry in state["entries"]:
            path = entry["path"]
            if only_paths is not None and path not in only_paths:
                keep.append(entry)
                continue
            try:
                count = win_dacl.remove_ace_exact(
                    path,
                    sid=sid,
                    ace_type=entry["ace_type"],
                    mask=entry["mask"],
                    ace_flags=entry["ace_flags"],
                    # 状态里记录的就是"我们创建的这条形状"；历史上可能重复叠加过，
                    # 撤销时一并清掉，其他 SID / 其他形状的 ACE 一条都不碰。
                    all_matches=True,
                )
            except win_dacl.WinDaclError as exc:
                failed.append(f"{path}: {exc}")
                keep.append(entry)
                continue
            if count:
                removed.append(path)
        state["entries"] = keep
        state["last_cleanup"] = _now()
        _save_state(state)
    detail = f"已撤销 {len(removed)} 条"
    if failed:
        return False, f"{detail}；失败：{'；'.join(failed)}"
    return True, detail
