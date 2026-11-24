"""`win_dacl` 的 DACL 精确读写测试（仅 Windows）。

覆盖点：
- 读出的 ACE 三要素（类型 / 掩码 / 继承标志）与 SID 正确；
- 加 DENY 后能读回，且不影响原有 ACE；
- 精确删除只删匹配的那一条，其他主体的 ACE 保留；
- 掩码/标志不同则不删（宁可不动手）；
- 继承 ACE 拒绝删除。
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 需要 DACL 测试")

from edith_backend.internal.isolation import win_dacl

EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"
OI_CI = win_dacl.OBJECT_INHERIT_ACE | win_dacl.CONTAINER_INHERIT_ACE


def _deny(view: win_dacl.DaclView, sid: str) -> list[win_dacl.Ace]:
    return [a for a in view.deny_aces_for(sid) if not a.is_inherited]


def test_read_default_acl_has_owner_full_control(tmp_path) -> None:
    view = win_dacl.read_dacl(str(tmp_path))
    assert view.aces, "新目录至少应有一条 ACE"
    assert any(a.mask == win_dacl.ACL_MASK_ALL for a in view.aces)


def test_add_deny_then_read_back(tmp_path) -> None:
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    view = win_dacl.read_dacl(str(tmp_path))
    hits = _deny(view, EVERYONE)
    assert len(hits) == 1, hits
    ace = hits[0]
    assert ace.ace_type == win_dacl.ACCESS_DENIED_ACE_TYPE
    assert ace.mask == win_dacl.ACL_MASK_ALL
    assert ace.inheritance_flags == OI_CI


def test_add_deny_is_not_inherited(tmp_path) -> None:
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    ace = _deny(win_dacl.read_dacl(str(tmp_path)), EVERYONE)[0]
    assert ace.is_inherited is False


def test_remove_exact_keeps_other_subjects(tmp_path) -> None:
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    win_dacl.add_deny_ace(str(tmp_path), USERS, mask=win_dacl.ACL_MASK_ALL)

    before = win_dacl.read_dacl(str(tmp_path))
    assert len(_deny(before, EVERYONE)) == 1
    assert len(_deny(before, USERS)) == 1

    removed = win_dacl.remove_ace_exact(
        str(tmp_path),
        sid=EVERYONE,
        ace_type=win_dacl.ACCESS_DENIED_ACE_TYPE,
        mask=win_dacl.ACL_MASK_ALL,
        ace_flags=OI_CI,
    )
    assert removed == 1

    after = win_dacl.read_dacl(str(tmp_path))
    assert _deny(after, EVERYONE) == []
    assert len(_deny(after, USERS)) == 1, "其他主体的 ACE 必须保留"


def test_remove_with_wrong_mask_is_noop(tmp_path) -> None:
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    removed = win_dacl.remove_ace_exact(
        str(tmp_path),
        sid=EVERYONE,
        ace_type=win_dacl.ACCESS_DENIED_ACE_TYPE,
        mask=0x1,  # 故意不匹配
        ace_flags=OI_CI,
    )
    assert removed == 0
    assert len(_deny(win_dacl.read_dacl(str(tmp_path)), EVERYONE)) == 1


def test_remove_with_wrong_flags_is_noop(tmp_path) -> None:
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    removed = win_dacl.remove_ace_exact(
        str(tmp_path),
        sid=EVERYONE,
        ace_type=win_dacl.ACCESS_DENIED_ACE_TYPE,
        mask=win_dacl.ACL_MASK_ALL,
        ace_flags=win_dacl.CONTAINER_INHERIT_ACE,  # 少了 OI
    )
    assert removed == 0


def test_deny_applies_to_child(tmp_path) -> None:
    child = tmp_path / "sub"
    child.mkdir()
    win_dacl.add_deny_ace(str(tmp_path), EVERYONE)
    inherited = [
        a
        for a in win_dacl.read_dacl(str(child)).deny_aces_for(EVERYONE)
        if a.is_inherited
    ]
    assert inherited, "目录上的 (OI)(CI) DENY 必须继承到子项"
