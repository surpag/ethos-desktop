"""S1 第一阶段：工作区上下文的探测层与解析层。

判据（与 docs/p0-2-s1-plan.md §0 三条裁决一一对应）：

| 用例 | 判据 |
| --- | --- |
| 五状态映射 | 每种探测结论只映射到一个状态；`NOT_DIRECTORY` 与 `NOT_EXISTS` 同归 missing |
| `BOUND_READY` 的边界 | 只代表"当前探测通过"，不承诺后续写入/删除 |
| 异常态不得回退 | 任何 `BOUND_*` 的 `root` 都是绑定路径；`UNBOUND` 之外的 `root` 绝不会是 Volume |
| 调用方错误 | 未绑定却给探测 / 有绑定却漏探测 → `BOUND_OFFLINE` + `root=None`，不静默降级 |
| 两段探测 | `stat` 通过但 `scandir` 无权限 → denied（只测第一步会漏掉这一格） |
| 不可变 | 快照赋值即抛 `FrozenInstanceError` |
| 不建目录 | `UNBOUND` 的 `root` 指向 Volume，但构建快照不创建它 |
"""

from __future__ import annotations

import errno
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from edith_backend.internal.workspace import context as ctx


class _FakeOSError(OSError):
    """带上任意 winerror 的 OSError：分类逻辑按属性判断，不依赖所在平台。"""

    def __init__(self, winerror: int) -> None:
        super().__init__("simulated")
        self.winerror = winerror


class _FakeBadNetPath(FileNotFoundError):
    """不可达网络路径的真实形状：`FileNotFoundError` + `winerror=53`。

    2026-11-20 对 `\\\\192.0.2.1\\share\\nothing` 实测得到（errno 被映射成 2）。
    只按异常类型分类会把"盘不见了"报成"目录不存在"。
    """

    def __init__(self) -> None:
        super().__init__(2, "bad net path")
        self.winerror = 53


# ---- 状态枚举 ----


def test_state_values_are_stable_strings() -> None:
    """值会进日志、事件与错误 payload，改动等于破坏外部契约。"""
    assert {s.value for s in ctx.BindingState} == {
        "unbound",
        "bound_ready",
        "bound_missing",
        "bound_denied",
        "bound_offline",
    }


# ---- 解析层（纯函数） ----


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (ctx.ProbeReason.OK, ctx.BindingState.BOUND_READY),
        (ctx.ProbeReason.NOT_EXISTS, ctx.BindingState.BOUND_MISSING),
        (ctx.ProbeReason.NOT_DIRECTORY, ctx.BindingState.BOUND_MISSING),
        (ctx.ProbeReason.PERMISSION_DENIED, ctx.BindingState.BOUND_DENIED),
        (ctx.ProbeReason.DEVICE_UNAVAILABLE, ctx.BindingState.BOUND_OFFLINE),
        (ctx.ProbeReason.UNEXPECTED, ctx.BindingState.BOUND_OFFLINE),
    ],
)
def test_state_mapping(reason: ctx.ProbeReason, expected: ctx.BindingState) -> None:
    record = ctx.BindingRecord("ws1", local_root="C:/bound")
    assert ctx.resolve_state(record, ctx.RootProbe(reason)) is expected


def test_unbound_needs_no_probe() -> None:
    assert (
        ctx.resolve_state(ctx.BindingRecord("ws1"), None) is ctx.BindingState.UNBOUND
    )


def test_unbound_with_probe_fails_closed() -> None:
    """未绑定却有探测结果 = 调用方搞错。不能返回 UNBOUND——那会给出 Volume 根。"""
    record = ctx.BindingRecord("ws1")
    probe = ctx.RootProbe(ctx.ProbeReason.OK)
    assert ctx.resolve_state(record, probe) is ctx.BindingState.BOUND_OFFLINE
    assert ctx.build_context(record, probe, now=1.0).root is None


def test_bound_without_probe_fails_closed() -> None:
    record = ctx.BindingRecord("ws1", local_root="C:/bound")
    assert ctx.resolve_state(record, None) is ctx.BindingState.BOUND_OFFLINE
    snapshot = ctx.build_context(record, None, now=1.0)
    assert snapshot.state is ctx.BindingState.BOUND_OFFLINE
    # 有绑定记录时 root 仍是绑定路径（供诊断），但不是 Volume
    assert snapshot.root == "C:/bound"


# ---- 探测层（真实文件系统） ----


def test_probe_directory(tmp_path: Path) -> None:
    probe = ctx.probe_root(tmp_path)
    assert probe.reason is ctx.ProbeReason.OK
    assert probe.detail == ""


def test_probe_missing_path(tmp_path: Path) -> None:
    probe = ctx.probe_root(tmp_path / "nope")
    assert probe.reason is ctx.ProbeReason.NOT_EXISTS


def test_probe_file_is_not_directory(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    probe = ctx.probe_root(target)
    assert probe.reason is ctx.ProbeReason.NOT_DIRECTORY
    assert "not a directory" in probe.detail


def test_probe_stat_permission_denied(tmp_path: Path, monkeypatch) -> None:
    def boom(_path):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(ctx, "_stat", boom)
    probe = ctx.probe_root(tmp_path)
    assert probe.reason is ctx.ProbeReason.PERMISSION_DENIED
    assert probe.detail.startswith("stat:")


def test_probe_scandir_permission_denied(tmp_path: Path, monkeypatch) -> None:
    """stat 通过、scandir 无权限：只测第一步会把它误判成 BOUND_READY。"""

    def boom(_path):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(ctx, "_scandir", boom)
    probe = ctx.probe_root(tmp_path)
    assert probe.reason is ctx.ProbeReason.PERMISSION_DENIED
    assert probe.detail.startswith("scandir:")


def test_probe_network_error_is_offline(tmp_path: Path, monkeypatch) -> None:
    def boom(_path):
        raise _FakeOSError(53)  # ERROR_BAD_NETPATH：找不到网络路径

    monkeypatch.setattr(ctx, "_stat", boom)
    assert ctx.probe_root(tmp_path).reason is ctx.ProbeReason.DEVICE_UNAVAILABLE


def test_probe_unreachable_network_path_is_offline_not_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """回归：winerror 必须优先于异常类型。

    `FileNotFoundError` + `winerror=53` 是网络盘掉线，不是目录被删；
    两者对用户是"去检查盘"和"去检查目录"的区别。
    """

    def boom(_path):
        raise _FakeBadNetPath()

    monkeypatch.setattr(ctx, "_stat", boom)
    probe = ctx.probe_root(tmp_path)
    assert probe.reason is ctx.ProbeReason.DEVICE_UNAVAILABLE
    assert probe.reason is not ctx.ProbeReason.NOT_EXISTS


def test_probe_winerror_not_found_maps_to_missing(tmp_path: Path, monkeypatch) -> None:
    def boom(_path):
        raise _FakeOSError(3)  # ERROR_PATH_NOT_FOUND

    monkeypatch.setattr(ctx, "_stat", boom)
    assert ctx.probe_root(tmp_path).reason is ctx.ProbeReason.NOT_EXISTS


def test_probe_unclassifiable_error_is_unexpected(tmp_path: Path, monkeypatch) -> None:
    def boom(_path):
        raise OSError(errno.EINVAL, "weird")

    monkeypatch.setattr(ctx, "_stat", boom)
    assert ctx.probe_root(tmp_path).reason is ctx.ProbeReason.UNEXPECTED


# ---- 快照 ----


def test_snapshot_is_immutable() -> None:
    snapshot = ctx.build_context(ctx.BindingRecord("ws1"), None, now=1.0)
    with pytest.raises(FrozenInstanceError):
        snapshot.state = ctx.BindingState.BOUND_READY  # type: ignore[misc]


def test_unbound_root_is_volume_and_not_created(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    snapshot = ctx.build_context(ctx.BindingRecord("ws1"), None, now=1.0)

    assert snapshot.state is ctx.BindingState.UNBOUND
    assert snapshot.root is not None
    assert snapshot.root.replace("\\", "/").endswith("workspace-volumes/ws1")
    # 权威根由执行链按需创建（S1 不动这条），快照只拼字符串
    assert not Path(snapshot.root).exists()


@pytest.mark.parametrize(
    "reason",
    [
        ctx.ProbeReason.NOT_EXISTS,
        ctx.ProbeReason.NOT_DIRECTORY,
        ctx.ProbeReason.PERMISSION_DENIED,
        ctx.ProbeReason.DEVICE_UNAVAILABLE,
        ctx.ProbeReason.UNEXPECTED,
    ],
)
def test_abnormal_bound_state_never_gets_volume_root(
    reason: ctx.ProbeReason, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    record = ctx.BindingRecord("ws1", local_root="C:/bound")
    snapshot = ctx.build_context(record, ctx.RootProbe(reason, "detail"), now=1.0)

    assert snapshot.state is not ctx.BindingState.UNBOUND
    assert snapshot.root == "C:/bound"
    assert "workspace-volumes" not in (snapshot.root or "")
    assert snapshot.detail == "detail"


def test_snapshot_carries_virtual_root_and_prefix() -> None:
    record = ctx.BindingRecord("ws1", local_root="C:/bound", prefix="Data")
    snapshot = ctx.build_context(
        record, ctx.RootProbe(ctx.ProbeReason.OK), now=123.0
    )
    assert snapshot.virtual_root == "/home/user/workspace/ws1"
    assert snapshot.prefix == "Data"
    assert snapshot.captured_at == 123.0
    assert snapshot.state is ctx.BindingState.BOUND_READY
