"""P0-1 之二：工作区根目录绑定入口的归一化与校验。

设计约束（本轮定稿）：**所有 Workspace 根目录必须在绑定入口完成规范化**，
之后内部只使用规范化后的路径。理由不是洁癖——`is_relative_to` 比较的是路径字符串，
而磁盘上同一个目录有很多写法（`Proj` / `proj` / `PROJ~1` / 联接的真实目标）。
只要一侧是别名、另一侧是真实路径，判定就失败，表现为「文件明明在，却 403」。

| 用例 | 判据 |
| --- | --- |
| 归一化 | 入口输出的路径与磁盘真实路径逐字一致（本机 `tmp_path` 就是 8.3 短名形态） |
| 三类失败 | 不存在 / 不是目录 / 相对路径 → 三种可区分的错误，不糊成一种 |
| 校验顺序 | 校验在写库之前；失败不覆盖旧绑定、不产生半截记录 |
| 从库读回 | 旧绑定（可能是短名）也要归一 |
| 加载边界 | 归一化只影响注册表，**不改写数据库**（加载阶段不引入写操作） |
| 换绑 | 不搬、不删旧目录 |
| 兜底 | 未绑定工作区不注册 local_root |
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from pathlib import Path

import pytest

from edith_backend.internal.workspace.volume_backend import (
    LocalRootMissing,
    LocalRootNotAbsolute,
    LocalRootNotDirectory,
    LocalVolumeBackend,
    normalize_local_root,
)
from edith_backend.internal.workspace.volume_service import WorkspaceVolumeService


class _FakeStore:
    """只记录写入，避免单元测试依赖数据库。"""

    def __init__(self, root: str | None = None) -> None:
        self.root = root
        self.calls: list[tuple[str, str, str, str]] = []

    async def bind_local_root(
        self, user_id: str, workspace_id: str, local_root: str, prefix: str
    ) -> None:
        self.calls.append((user_id, workspace_id, local_root, prefix))
        self.root = local_root

    async def get_local_root(self, user_id: str, workspace_id: str) -> str | None:
        return self.root

    async def get_local_root_prefix(self, user_id: str, workspace_id: str) -> str:
        return ""


def _service(
    tmp_path: Path, store: _FakeStore
) -> tuple[WorkspaceVolumeService, LocalVolumeBackend]:
    backend = LocalVolumeBackend(tmp_path / "volumes")
    return WorkspaceVolumeService(store, backend), backend  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_matches_disk_real_path(tmp_path: Path) -> None:
    """判据是「与磁盘真实路径逐字一致」。

    `tmp_path` 在本机就是 8.3 短名形态（`C:\\Users\\WS2101~1\\...`），
    正好复现真实故障：库里存短名、沙箱层用长名比较 → 假 403。
    """
    local = tmp_path / "local-root"
    local.mkdir()
    normalized = normalize_local_root(str(local))
    assert normalized == local.resolve()
    assert normalized.is_absolute()


def test_normalize_is_idempotent(tmp_path: Path) -> None:
    """归一化两次结果不变（否则每次比较都要担心对面是哪一版）。"""
    local = tmp_path / "local-root"
    local.mkdir()
    once = normalize_local_root(str(local))
    assert normalize_local_root(str(once)) == once


def test_normalize_distinguishes_failure_kinds(tmp_path: Path) -> None:
    """不存在 / 不是目录 / 相对路径必须可区分——不能都糊成「绑定失败」。"""
    with pytest.raises(LocalRootMissing):
        normalize_local_root(str(tmp_path / "not-there"), strict=True)

    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(LocalRootNotDirectory):
        normalize_local_root(str(a_file), strict=True)

    with pytest.raises(LocalRootNotAbsolute):
        normalize_local_root("relative/path")


def test_normalize_lenient_keeps_missing_path_usable(tmp_path: Path) -> None:
    """从库里读回时用宽松模式：目录被删不该让会话起不来（错误在真实读写时冒出来）。"""
    missing = tmp_path / "gone"
    assert normalize_local_root(str(missing)) == missing.resolve()


# ---------------------------------------------------------------------------
# 服务层：注册 = 归一化 + 校验先于写库
# ---------------------------------------------------------------------------


def test_bind_registers_canonical_root(tmp_path: Path) -> None:
    """注册表里只允许规范化后的路径。"""
    service, backend = _service(tmp_path, _FakeStore())
    local = tmp_path / "local-root"
    local.mkdir()

    asyncio.run(service.bind_local_root("u1", "w1", str(local)))

    assert backend.local_root_of("w1") == local.resolve()


def test_bind_persists_canonical_root(tmp_path: Path) -> None:
    """库里存的也必须是归一后的值：否则重启后注册表与库不在同一平面。"""
    store = _FakeStore()
    service, _ = _service(tmp_path, store)
    local = tmp_path / "local-root"
    local.mkdir()

    asyncio.run(service.bind_local_root("u1", "w1", str(local)))

    assert len(store.calls) == 1
    assert store.calls[0][2] == str(local.resolve())


def test_bind_failure_keeps_previous_binding(tmp_path: Path) -> None:
    """校验失败：不写库、不覆盖已有绑定。"""
    store = _FakeStore()
    service, backend = _service(tmp_path, store)
    good = tmp_path / "good-root"
    good.mkdir()

    asyncio.run(service.bind_local_root("u1", "w1", str(good)))
    writes_before = len(store.calls)

    with pytest.raises(LocalRootMissing):
        asyncio.run(service.bind_local_root("u1", "w1", str(tmp_path / "gone")))

    assert len(store.calls) == writes_before, "校验失败却写了库"
    assert backend.local_root_of("w1") == good.resolve(), "失败覆盖了已有绑定"


def test_bind_rejects_file(tmp_path: Path) -> None:
    """绑定目标必须是目录。"""
    service, backend = _service(tmp_path, _FakeStore())
    a_file = tmp_path / "a.txt"
    a_file.write_text("x", encoding="utf-8")

    with pytest.raises(LocalRootNotDirectory):
        asyncio.run(service.bind_local_root("u1", "w1", str(a_file)))

    assert backend.local_root_of("w1") is None


def test_rebind_does_not_touch_previous_directory(tmp_path: Path) -> None:
    """换绑不搬、不删旧目录（用户数据不能因为换绑消失）。"""
    service, backend = _service(tmp_path, _FakeStore())
    first = tmp_path / "first-root"
    second = tmp_path / "second-root"
    first.mkdir()
    second.mkdir()
    (first / "keep.txt").write_text("user", encoding="utf-8")

    asyncio.run(service.bind_local_root("u1", "w1", str(first)))
    asyncio.run(service.bind_local_root("u1", "w1", str(second)))

    assert backend.local_root_of("w1") == second.resolve()
    assert (first / "keep.txt").read_text(encoding="utf-8") == "user"


def test_load_registers_canonical_root(tmp_path: Path) -> None:
    """从库读回的旧绑定同样归一（库里可能存着短名/大小写别名）。"""
    local = tmp_path / "loaded-root"
    local.mkdir()
    store = _FakeStore(root=str(local))
    service, backend = _service(tmp_path, store)

    asyncio.run(service.load_and_bind_local_root("u1", "w1"))

    assert backend.local_root_of("w1") == local.resolve()


def test_unbound_workspace_registers_nothing(tmp_path: Path) -> None:
    """未绑定工作区不得凭空出现在注册表里（否则 `_dir()` 会绕开 Volume 兜底）。"""
    store = _FakeStore()
    service, backend = _service(tmp_path, store)

    asyncio.run(service.load_and_bind_local_root("u1", "w1"))

    assert backend.local_root_of("w1") is None


def _short_path(path: Path) -> str | None:
    """取 8.3 短名；系统关闭短名生成或已经是短名时返回 None（用例跳过）。"""
    if not sys.platform == "win32":
        return None
    buffer = ctypes.create_unicode_buffer(600)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 600)
    if not length or not buffer.value:
        return None
    return None if buffer.value.lower() == str(path).lower() else buffer.value


def test_load_normalizes_registry_but_leaves_db_untouched(tmp_path: Path) -> None:
    """库里的旧值（8.3 短名）→ 注册表拿归一后的路径；**库不被加载阶段改写**。

    这是本轮刻意划的边界：加载是读路径，不在这里引入数据库写操作
    （写库会带来新的失败场景与事务边界）。所以 DB 与注册表**表示**可以不同，
    但**语义**必须一致——运行时权威值是注册表里那份归一结果。
    """
    local = tmp_path / "loaded-short"
    local.mkdir()
    short = _short_path(local)
    if short is None:
        pytest.skip("本机未启用 8.3 短名（或路径已是短名），无法复现该形态")

    store = _FakeStore(root=short)
    service, backend = _service(tmp_path, store)

    asyncio.run(service.load_and_bind_local_root("u1", "w1"))

    assert backend.local_root_of("w1") == local.resolve()
    assert str(backend.local_root_of("w1")) != short, "注册表仍是短名，比较会假 403"
    assert store.root == short, "加载阶段不应改写数据库（本轮边界：只读）"
    assert store.calls == [], "加载阶段不应发生写库调用"

