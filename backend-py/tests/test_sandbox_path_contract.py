"""P0-1 之一：沙箱路径契约 + 两处误拒（D-5 / D-6）+ 边界反向验证。

本文件是**回归**，不是新功能。每条用例对应一个已实测的缺陷，修复前必须是 RED。

| 组 | 覆盖 | 对应缺陷 |
| --- | --- | --- |
| A 契约 | 挂载函数与工作区根解析返回「绝对 + 已归一 + 存在的目录」 | §11.5 返回值契约不一致 |
| B D-5 | `workspace` 挂载根本身可读可列 | D-5（现状 403） |
| C D-6 | `workspace/./{id}/…` 等合法写法被接受 | D-6（现状 403） |
| D 边界 | 修复不得放宽：跨工作区穿越仍拒绝 | 反向验证 |
| F 兜底 | 未绑定工作区仍走 `workspace-volumes/{id}` | 回归，不得破坏 |

约定与其它测试一致：pytest 收集同步函数，async 主体用 `asyncio.run` 包住；
每个用例都用 `ETHOS_DATA_DIR` 指向 `tmp_path`，不碰真实的 `%APPDATA%\\ETHOS`。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from edith_backend.internal.sandbox.local import (
    POSIX_SHELL,
    LocalSandbox,
    SandboxPathError,
    _is_link,
    _make_link,
)

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def sandbox(data_dir: Path) -> LocalSandbox:
    return LocalSandbox.open("user-1", "session-1")


def _remove_link(path: Path) -> None:
    """删连接本身（不穿透目标）。junction 只能 rmdir，symlink 用 unlink。"""
    try:
        os.rmdir(path)
    except OSError:
        path.unlink(missing_ok=True)


def make_dir_link(link: Path, target: Path) -> None:
    """建目录联接；环境不允许时跳过（Windows 需 mklink，POSIX 需 symlink 权限）。"""
    target.mkdir(parents=True, exist_ok=True)
    if _is_link(link) or link.exists():
        _remove_link(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _make_link(link, target):
        pytest.skip("当前环境无法创建目录联接/符号链接（需要 mklink 或 symlink 权限）")


# ---------------------------------------------------------------------------
# A 契约：返回值语义统一
# ---------------------------------------------------------------------------


def test_workspace_mount_root_is_resolved(sandbox: LocalSandbox) -> None:
    """`workspace` 挂载根必须直接返回**已归一的真实目录**。

    契约（两个 `_ensure_*_mount` 共用）：返回「绝对 + 已解析 + 存在」的目录，
    调用方拿它做 `is_relative_to(base)` 时不需要再自己 resolve。
    修复前 `_ensure_workspace_mount` 需要一个 workspace_id 且返回未归一的子目录，
    与 `_ensure_skills_mount`（返回已解析目标）不是同一种语义。
    """
    root = sandbox._ensure_workspace_mount()
    assert root.is_dir()
    assert root.is_absolute()
    assert str(root) == str(root.resolve())


def test_skills_mount_root_is_resolved(sandbox: LocalSandbox) -> None:
    """对照组：skills 分支本来就正确，改契约不能把它弄坏。"""
    root = sandbox._ensure_skills_mount()
    assert root.is_dir()
    assert str(root) == str(root.resolve())


def test_linked_volume_root_is_resolved(sandbox: LocalSandbox, data_dir: Path) -> None:
    """工作区根是联接时（= 将来绑定本地目录的形态），解析结果必须是真实目录。

    现状：`base` 未解析、`target` 已解析 → 两个操作数不在同一规范化平面 →
    子路径判定失败（403）。这条缺陷与 junction 本身的设计无关。
    """
    external = data_dir / "external-ws-root"
    external.mkdir()
    make_dir_link(data_dir / "workspace-volumes" / "ws-linked", external)

    root = sandbox._resolve_workspace_volume(
        "ws-linked", [], "/home/user/workspace/ws-linked"
    )
    assert str(root) == str(root.resolve())
    assert root == external.resolve()


def test_junction_volume_is_reachable_by_file_tools(
    sandbox: LocalSandbox, data_dir: Path
) -> None:
    """工作区根是联接时，文件原语必须能用（写入要真的落到目标目录）。"""
    external = data_dir / "external-ws"
    external.mkdir()
    (external / "seed.txt").write_text("seed", encoding="utf-8")
    make_dir_link(data_dir / "workspace-volumes" / "ws-linked", external)

    async def body() -> None:
        assert await sandbox.files.read("/home/user/workspace/ws-linked/seed.txt") == "seed"
        await sandbox.files.write("/home/user/workspace/ws-linked/new.txt", "hello")
        assert (external / "new.txt").read_text(encoding="utf-8") == "hello"
        entries = await sandbox.files.list("/home/user/workspace/ws-linked")
        assert {"seed.txt", "new.txt"} <= {e.name for e in entries}

    asyncio.run(body())


@pytest.mark.skipif(POSIX_SHELL is None, reason="本机没有 POSIX shell")
def test_junction_volume_is_shared_with_shell(sandbox: LocalSandbox, data_dir: Path) -> None:
    """文件工具与 shell 必须看同一片字节（绑定的工作区就是这种形态）。

    这条是 P0 的真正验收目标：**补绑定之后两种工具操作同一个根**。
    文件 → shell、shell → 文件 两个方向都验，且都落在联接目标目录里。
    """
    external = data_dir / "external-shell-ws"
    external.mkdir()
    make_dir_link(data_dir / "workspace-volumes" / "ws-shell", external)

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-shell/from-tool.txt", "tool")
        seen = await sandbox.commands.run("ls /home/user/workspace/ws-shell")
        assert "from-tool.txt" in seen.stdout, f"shell 看不到文件工具写的文件: {seen.stderr}"

        await sandbox.commands.run(
            "echo from-shell > /home/user/workspace/ws-shell/from-shell.txt"
        )
        assert (external / "from-shell.txt").is_file(), "shell 写入没有落到联接目标"
        assert await sandbox.files.read("/home/user/workspace/ws-shell/from-shell.txt") == (
            "from-shell\n"
        )

    asyncio.run(body())


# ---------------------------------------------------------------------------
# B D-5：`workspace` 挂载根本身
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["workspace", "/home/user/workspace", "workspace/", "/home/user/workspace/"]
)
def test_workspace_mount_root_is_reachable(sandbox: LocalSandbox, path: str) -> None:
    """`ls /home/user/workspace` 是模型第一条探查命令，不能 403。

    现状：`resolve()` 只在 `parts[0]=="workspace" and len(parts)>=2` 时分流，
    `workspace` 只有一段 → 落到沙箱根分支 → 联接解析到 `workspace-volumes`
    → 不在沙箱根下 → 403。`skills` 是无条件分流，所以没这个问题。
    """

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-root/a.txt", "x")
        entries = await sandbox.files.list(path)
        assert "ws-root" in {e.name for e in entries}

    asyncio.run(body())

    assert sandbox.resolve(path) == sandbox.resolve("/home/user/workspace/ws-root").parent


# ---------------------------------------------------------------------------
# C D-6：`./` 等合法写法
# ---------------------------------------------------------------------------

DOT_CASES = [
    ("/home/user/workspace/./ws-dot/a.txt", "/home/user/workspace/ws-dot/a.txt"),
    ("/home/user/workspace//ws-dot//a.txt", "/home/user/workspace/ws-dot/a.txt"),
    ("/home/user/workspace/ws-dot/./sub/../b.txt", "/home/user/workspace/ws-dot/b.txt"),
    ("workspace/./ws-dot/a.txt", "/home/user/workspace/ws-dot/a.txt"),
    ("/home/user/workspace/ws-dot/", "/home/user/workspace/ws-dot"),
]


@pytest.mark.parametrize("path,canonical", DOT_CASES)
def test_dot_segments_inside_workspace_allowed(
    sandbox: LocalSandbox, path: str, canonical: str
) -> None:
    """归一化前后必须比较**同一组片段**。

    现状守卫拿「未归一化的原始片段」对照「归一化后的片段」：`workspace/./ws-dot/a.txt`
    的 `original[1]` 是 `.`，`normalized_parts[1]` 是 `ws-dot`，两者不等 → 误判跳界。
    """
    assert sandbox.resolve(path) == sandbox.resolve(canonical)


# ---------------------------------------------------------------------------
# D 反向验证：修复不得放宽边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/home/user/workspace/ws-a/../ws-b/x.txt",       # 跨工作区改名
        "/home/user/workspace/./ws-a/../../work/x.txt",  # 归一化后跳出工作区树
        "/home/user/workspace/ws-a/../..",               # 跳回沙箱根
        "/home/user/workspace/../work/x.txt",            # 挂载根上溯
        "/home/user/work/../../outside.txt",
        "/etc/passwd",
        "..",
    ],
)
def test_boundary_still_enforced(sandbox: LocalSandbox, bad: str) -> None:
    """放宽 D-6 不能以放过跨工作区穿越为代价。"""
    with pytest.raises(SandboxPathError):
        sandbox.resolve(bad)


def test_workspace_files_stay_inside_their_volume(sandbox: LocalSandbox, data_dir: Path) -> None:
    """两个工作区互不可见：`ws-a` 的文件不得落在 `ws-b` 或沙箱根下。"""

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-a/only-a.txt", "a")

    asyncio.run(body())

    assert (data_dir / "workspace-volumes" / "ws-a" / "only-a.txt").is_file()
    assert not (data_dir / "workspace-volumes" / "ws-b" / "only-a.txt").exists()
    assert not (sandbox.root / "only-a.txt").exists()


# ---------------------------------------------------------------------------
# F 兜底：未绑定工作区仍走 Volume
# ---------------------------------------------------------------------------


def test_unbound_workspace_still_uses_volume(sandbox: LocalSandbox, data_dir: Path) -> None:
    """P0-1 只统一契约，不改变落点：未绑定工作区仍然写 `workspace-volumes/{id}`。"""

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-plain/a.txt", "x")
        assert await sandbox.files.read("/home/user/workspace/ws-plain/a.txt") == "x"

    asyncio.run(body())

    assert (data_dir / "workspace-volumes" / "ws-plain" / "a.txt").is_file()
