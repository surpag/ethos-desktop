"""S3：面板直读的分类契约。

这一层的核心不是"路径算得对不对"，而是**本地不可用时的处置**：

- `SANDBOX`：本地没有权威文件，交给沙箱（`skills/…`、未绑定、前缀之外）；
- `READY`：本地路径已解析，存在性由调用方判断；
- `UNAVAILABLE`：绑定目录没了，**必须报错**。

把三者压成 `Path | None` 的后果是面板在绑定目录被移动后静默显示旧副本 ——
用户会以为自己在看绑定目录。这组用例守的就是这条。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edith_backend.internal.workspace.volume_backend import (
    LocalLookupKind,
    LocalVolumeBackend,
)

WS = "ws1"


@pytest.fixture
def backend(tmp_path: Path) -> LocalVolumeBackend:
    return LocalVolumeBackend(tmp_path / "volumes")


@pytest.fixture
def bound(backend: LocalVolumeBackend, tmp_path: Path) -> Path:
    root = tmp_path / "my-project"
    root.mkdir()
    (root / "app.py").write_text("x", encoding="utf-8")
    backend.bind_local_root(WS, str(root))
    return root


# ---- 交给沙箱 ----


@pytest.mark.parametrize("relative", ["", ".", "skills/x/SKILL.md", "uploads/a.txt"])
def test_non_workspace_paths_go_to_sandbox(backend, relative: str) -> None:
    assert backend.lookup_workspace_path(relative).kind is LocalLookupKind.SANDBOX


def test_unbound_missing_copy_goes_to_sandbox(backend) -> None:
    """未绑定且副本目录还没建出来：正常状态，沙箱会按需初始化（同改造前）。"""
    lookup = backend.lookup_workspace_path(f"workspace/{WS}/a.txt")
    assert lookup.kind is LocalLookupKind.SANDBOX
    assert lookup.fallback_allowed is True


# ---- READY ----


def test_unbound_existing_copy_is_ready_and_may_fallback(backend, tmp_path: Path) -> None:
    copy = tmp_path / "volumes" / WS
    copy.mkdir(parents=True)
    lookup = backend.lookup_workspace_path(f"workspace/{WS}/a.txt")
    assert lookup.kind is LocalLookupKind.READY
    assert lookup.path == copy / "a.txt"
    # 未绑定：本地没有某个文件时仍然可以回落沙箱（两者是同一份数据）
    assert lookup.fallback_allowed is True


def test_bound_path_is_ready_and_must_not_fallback(backend, bound: Path) -> None:
    """绑定态的关键不是路径对不对，而是 fallback_allowed=False。"""
    lookup = backend.lookup_workspace_path(f"workspace/{WS}/app.py")
    assert lookup.kind is LocalLookupKind.READY
    assert lookup.path == bound / "app.py"
    assert lookup.fallback_allowed is False


def test_bound_root_itself_is_ready(backend, bound: Path) -> None:
    lookup = backend.lookup_workspace_path(f"workspace/{WS}")
    assert lookup.kind is LocalLookupKind.READY
    assert lookup.path == bound


# ---- UNAVAILABLE ----


def test_bound_root_removed_is_unavailable_not_none(backend, bound: Path) -> None:
    """绑定目录被移动/删除：必须能区分出来，而不是"本地没有，去读沙箱"。"""
    import shutil

    shutil.rmtree(bound)

    lookup = backend.lookup_workspace_path(f"workspace/{WS}/app.py")
    assert lookup.kind is LocalLookupKind.UNAVAILABLE
    assert lookup.fallback_allowed is False
    assert "不可用" in lookup.detail


def test_unbound_root_removed_is_still_sandbox(backend, tmp_path: Path) -> None:
    """未绑定时"目录不存在"是常态（新工作区），不能被当成故障。"""
    assert not (tmp_path / "volumes" / WS).exists()
    lookup = backend.lookup_workspace_path(f"workspace/{WS}/a.txt")
    assert lookup.kind is LocalLookupKind.SANDBOX


# ---- 前缀 ----


def test_prefix_maps_virtual_layer_to_local(tmp_path: Path) -> None:
    """`workspace/{id}/Data/a.txt` ↔ `local_root/a.txt`（前缀是虚拟视图多出来的一层）。"""
    root = tmp_path / "Data"
    root.mkdir()
    backend = LocalVolumeBackend(tmp_path / "volumes")
    backend.bind_local_root(WS, str(root), prefix="Data")

    inside = backend.lookup_workspace_path(f"workspace/{WS}/Data/a.txt")
    assert inside.kind is LocalLookupKind.READY
    assert inside.path == root / "a.txt"

    outside = backend.lookup_workspace_path(f"workspace/{WS}/other/a.txt")
    assert outside.kind is LocalLookupKind.SANDBOX


# ---- 安全 ----


def test_traversal_is_not_resolved_to_local(backend, bound: Path) -> None:
    """`..` 在剥离前缀之后出现时必须放弃本地直读，不能拼出根外路径。"""
    lookup = backend.lookup_workspace_path(f"workspace/{WS}/../../../etc/passwd")
    assert lookup.kind is LocalLookupKind.SANDBOX


# ---- 兼容视图 ----


def test_resolve_workspace_path_still_returns_path_or_none(backend, bound: Path) -> None:
    assert backend.resolve_workspace_path(f"workspace/{WS}/app.py") == bound / "app.py"
    assert backend.resolve_workspace_path("skills/x") is None
