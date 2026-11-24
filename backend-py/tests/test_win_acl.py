"""`win_acl` 的 ACL 生命周期测试（仅 Windows）。

重点覆盖本轮要修的三件事：
1. 目录「物化」之后必须补加固（`.azure` / `.gnupg` 漏保护的回归）；
2. 状态文件与真实 DACL 对账（崩溃在写状态之前也能收养）；
3. 只撤销自己创建的 ACE，其他主体一条不动；并发与异常中断不产生重复 ACE。
"""

from __future__ import annotations

import json
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 需要 ACL 测试")

from edith_backend.internal.isolation import win_acl, win_dacl

#: 用**不存在的伪造 SID** 当"沙箱账户"：
#: - 不能用 `S-1-1-0`（Everyone）或 `BUILTIN\Users`——它们包含测试进程自己，
#:   DENY 优先会把测试进程也挡住，读 DACL 直接 ACCESS_DENIED；
#: - 真实代码用的是 `srt-sandbox` 的 SID，同样不包含当前用户。
TEST_SID = "S-1-5-21-999999999-888888888-777777777-4321"
OTHER_SID = "S-1-5-21-999999999-888888888-777777777-4322"
OI_CI = win_dacl.OBJECT_INHERIT_ACE | win_dacl.CONTAINER_INHERIT_ACE


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ETHOS_DATA_DIR", str(data))
    protected = tmp_path / "protected"
    protected.mkdir()
    return tmp_path, protected


def _state() -> dict:
    return json.loads(win_acl.state_path().read_text(encoding="utf-8"))


def _our_deny(path) -> list[win_dacl.Ace]:
    view = win_dacl.read_dacl(str(path))
    return [a for a in view.deny_aces_for(TEST_SID) if not a.is_inherited]


def test_ensure_hardens_and_records(workspace) -> None:
    _, protected = workspace
    ok, detail = win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    assert ok, detail
    assert len(_our_deny(protected)) == 1
    entry = _state()["entries"][0]
    assert entry["path"] == str(protected)
    assert entry["sid"] == TEST_SID
    assert entry["mask"] == win_acl.DENY_MASK
    assert entry["ace_flags"] == win_acl.DENY_FLAGS


def test_ensure_is_idempotent(workspace) -> None:
    _, protected = workspace
    win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    assert len(_our_deny(protected)) == 1, "重复 ensure 不能叠加 ACE"


def test_missing_path_is_recorded_not_silently_safe(workspace) -> None:
    _, protected = workspace
    absent = protected / "not-yet"
    ok, detail = win_acl.ensure_deny((str(protected), str(absent)), sid=TEST_SID)
    assert ok, detail
    assert "不存在" in detail
    assert str(absent) in _state()["missing"]


def test_existing_but_unprotected_fails_verification(workspace) -> None:
    """核心回归：目录存在但没加固时，verify_all 必须失败，不能算安全。"""
    _, protected = workspace
    win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    late = protected / "late"
    late.mkdir()  # 模拟运行时才被创建出来的 .azure
    ok, detail = win_acl.verify_all((str(protected), str(late)), sid=TEST_SID)
    assert ok is False
    assert "late" in detail


def test_newly_created_dir_gets_hardened_on_next_ensure(workspace) -> None:
    """`.azure` 漏加固的直接回归：先 missing，物化后必须补齐。"""
    _, protected = workspace
    late = protected / "late"
    win_acl.ensure_deny((str(protected), str(late)), sid=TEST_SID)
    assert str(late) in _state()["missing"]

    late.mkdir()
    ok, detail = win_acl.ensure_deny((str(protected), str(late)), sid=TEST_SID)
    assert ok, detail
    assert len(_our_deny(late)) == 1
    assert str(late) not in _state()["missing"]


def test_reconcile_adopts_untracked_ace(workspace) -> None:
    """ACE 已写、状态没写（崩溃在中间）时，对账要收养而不是重复添加。"""
    _, protected = workspace
    win_dacl.add_deny_ace(str(protected), TEST_SID, mask=win_acl.DENY_MASK, flags=OI_CI)
    # 模拟"写完 ACE 之后、写状态之前"崩溃：状态文件还不存在
    assert not win_acl.state_path().exists()

    ok, detail = win_acl.reconcile((str(protected),), sid=TEST_SID)
    assert ok, detail
    assert len(_our_deny(protected)) == 1, "收养不能变成再加一条"
    assert _state()["entries"][0]["path"] == str(protected)


def test_cleanup_removes_only_own_ace(workspace) -> None:
    _, protected = workspace
    win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    # 模拟"别的程序"在同一目录上放的 DENY
    win_dacl.add_deny_ace(str(protected), OTHER_SID, mask=win_acl.DENY_MASK, flags=OI_CI)

    ok, detail = win_acl.cleanup(sid=TEST_SID)
    assert ok, detail
    assert _our_deny(protected) == [], "自己创建的必须撤销"
    view = win_dacl.read_dacl(str(protected))
    assert view.deny_aces_for(OTHER_SID), "其他主体的 ACE 一条都不能动"
    assert _state()["entries"] == []


def test_concurrent_ensure_produces_single_ace(workspace) -> None:
    _, protected = workspace
    errors: list[str] = []

    def worker() -> None:
        ok, detail = win_acl.ensure_deny((str(protected),), sid=TEST_SID)
        if not ok:
            errors.append(detail)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(_our_deny(protected)) == 1, "并发 ensure 只能留下一条 ACE"


def test_file_target_uses_zero_flags(workspace) -> None:
    """`.netrc` 这类目标是文件：文件 ACE 的 OI/CI 会被系统规范化成 0。"""
    _, protected = workspace
    creds = protected / "creds"
    creds.write_text("x", encoding="utf-8")
    ok, detail = win_acl.ensure_deny((str(creds),), sid=TEST_SID)
    assert ok, detail
    ours = [a for a in win_dacl.read_dacl(str(creds)).deny_aces_for(TEST_SID) if not a.is_inherited]
    assert len(ours) == 1
    assert ours[0].inheritance_flags == 0


def test_duplicate_aces_are_deduped(workspace) -> None:
    """历史遗留的重复 DENY 必须被收敛成一条，而不是继续叠加。"""
    _, protected = workspace
    creds = protected / "creds"
    creds.write_text("x", encoding="utf-8")
    win_dacl.add_deny_ace(str(creds), TEST_SID, mask=win_acl.DENY_MASK, flags=0)
    win_dacl.add_deny_ace(str(creds), TEST_SID, mask=win_acl.DENY_MASK, flags=0)
    before = [a for a in win_dacl.read_dacl(str(creds)).deny_aces_for(TEST_SID) if not a.is_inherited]
    assert len(before) == 2

    ok, detail = win_acl.ensure_deny((str(creds),), sid=TEST_SID)
    assert ok, detail
    after = [a for a in win_dacl.read_dacl(str(creds)).deny_aces_for(TEST_SID) if not a.is_inherited]
    assert len(after) == 1, "重复 ACE 必须被收敛"


def test_verify_all_reports_missing_count(workspace) -> None:
    _, protected = workspace
    win_acl.ensure_deny((str(protected),), sid=TEST_SID)
    ok, detail = win_acl.verify_all(
        (str(protected), str(protected / "absent")), sid=TEST_SID
    )
    assert ok, detail
    assert "1 个不存在" in detail
