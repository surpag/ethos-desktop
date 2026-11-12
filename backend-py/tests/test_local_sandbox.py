"""本地沙箱原语测试：覆盖 E2B `files` / `commands` 的 11 个方法 + 路径边界。

约定与其它测试一致：pytest 收集的是同步函数，async 主体用 `asyncio.run` 包起来
（项目没有 pytest-asyncio，不引入新依赖）。

每个用例都用 `ETHOS_DATA_DIR` 指向 tmp_path：`data_root()` 每次调用都读环境变量，
所以 monkeypatch 立即生效，不会污染真实的 `%APPDATA%\\ETHOS`。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from edith_backend.internal.sandbox.local import (
    LocalSandbox,
    SandboxPathError,
)

SANDBOX_PATH_ERROR = SandboxPathError


@pytest.fixture
def sandbox(tmp_path, monkeypatch) -> LocalSandbox:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    return LocalSandbox.open("user-1", "session-1")


# ---- 生命周期 ----


def test_open_creates_layout_dirs(sandbox: LocalSandbox) -> None:
    """首次打开就建好 uploads/work/artifacts/workspace：模型第一次 ls 不该看到目录缺失。"""
    for name in ("uploads", "work", "artifacts", "workspace"):
        assert (sandbox.root / name).is_dir()


def test_attach_does_not_create(tmp_path, monkeypatch) -> None:
    """只读路径不能凭空造沙箱目录。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        with pytest.raises(FileNotFoundError):
            LocalSandbox.attach("user-x", "session-x")
        assert not (tmp_path / "sandbox" / "user-x").exists()

    asyncio.run(body())


def test_reopen_keeps_files(tmp_path, monkeypatch) -> None:
    """跨 run 文件必须还在（这是本地模式相对 E2B 的核心优势）。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        first = LocalSandbox.open("u", "s")
        await first.files.write("/home/user/work/a.txt", "persisted")
        await first.kill()

        second = LocalSandbox.open("u", "s")
        assert await second.files.read("/home/user/work/a.txt") == "persisted"

    asyncio.run(body())


# ---- 文件原语 ----


def test_write_read_roundtrip(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/hello.txt", "你好\nworld")
        assert await sandbox.files.read("/home/user/work/hello.txt") == "你好\nworld"
        # 相对路径以沙箱根为基准
        assert await sandbox.files.read("work/hello.txt") == "你好\nworld"

    asyncio.run(body())


def test_write_creates_parent_dirs(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/deep/nested/x.txt", "ok")
        assert (sandbox.root / "work" / "deep" / "nested" / "x.txt").is_file()

    asyncio.run(body())


def test_get_info_size(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/s.txt", "12345")
        info = await sandbox.files.get_info("/home/user/work/s.txt")
        assert info.size == 5
        assert info.name == "s.txt"

    asyncio.run(body())


def test_list_depth_and_type(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/a.txt", "a")
        await sandbox.files.make_dir("/home/user/work/sub")
        await sandbox.files.write("/home/user/work/sub/b.txt", "b")

        shallow = await sandbox.files.list("/home/user/work", depth=1)
        names = {e.name for e in shallow}
        assert names == {"a.txt", "sub"}

        deep = await sandbox.files.list("/home/user/work", depth=2)
        assert {e.name for e in deep} == {"a.txt", "sub", "b.txt"}

        entry = next(e for e in shallow if e.name == "sub")
        from e2b import FileType

        assert entry.type == FileType.DIR
        # 回给模型的路径必须是虚拟路径，不能泄漏本机磁盘路径
        assert entry.path.startswith("/home/user/")

    asyncio.run(body())


def test_make_dir_rename_remove(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/old.txt", "x")
        await sandbox.files.make_dir("/home/user/work/dir")
        await sandbox.files.rename("/home/user/work/old.txt", "/home/user/work/dir/new.txt")

        assert not (sandbox.root / "work" / "old.txt").exists()
        assert await sandbox.files.read("/home/user/work/dir/new.txt") == "x"

        await sandbox.files.remove("/home/user/work/dir")
        assert not (sandbox.root / "work" / "dir").exists()

    asyncio.run(body())


def test_remove_dir_is_recursive(sandbox: LocalSandbox) -> None:
    """与 E2B 语义一致：删目录是递归的（调用方负责先 list 确认为空）。"""
    async def body() -> None:
        await sandbox.files.write("/home/user/work/d/inner.txt", "x")
        await sandbox.files.remove("/home/user/work/d")
        assert not (sandbox.root / "work" / "d").exists()

    asyncio.run(body())


def test_read_missing_file_raises(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        with pytest.raises(FileNotFoundError):
            await sandbox.files.read("/home/user/work/nope.txt")

    asyncio.run(body())


# ---- 路径边界 ----


@pytest.mark.parametrize(
    "bad",
    [
        "/home/user/../etc/passwd",
        "/home/user/work/../../outside.txt",
        "/etc/passwd",
        "/tmp/x",
        "..",
        "../escape.txt",
    ],
)
def test_path_outside_root_rejected(sandbox: LocalSandbox, bad: str) -> None:
    async def body() -> None:
        with pytest.raises(SANDBOX_PATH_ERROR):
            await sandbox.files.read(bad)

    asyncio.run(body())


def test_symlink_escape_rejected(sandbox: LocalSandbox, tmp_path) -> None:
    """沙箱内指向外部的软链必须被拒——否则路径校验形同虚设。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = sandbox.root / "work" / "link"

    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    async def body() -> None:
        with pytest.raises(SANDBOX_PATH_ERROR):
            await sandbox.files.read("/home/user/work/link/secret.txt")

    asyncio.run(body())


def test_workspace_path_maps_to_volume(sandbox: LocalSandbox, tmp_path) -> None:
    """`workspace/{id}` 直接落到权威副本目录：本地模式不需要任何同步。"""
    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-1/readme.md", "# hi")
        volume_file = tmp_path / "workspace-volumes" / "ws-1" / "readme.md"
        assert volume_file.is_file(), "文件必须落在 workspace-volumes/{id} 下"
        assert volume_file.read_text(encoding="utf-8") == "# hi"

        info = await sandbox.files.get_info("/home/user/workspace/ws-1/readme.md")
        assert info.path == "/home/user/workspace/ws-1/readme.md", "反向映射不能泄漏磁盘路径"

    asyncio.run(body())


def test_workspace_path_traversal_rejected(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        with pytest.raises(SANDBOX_PATH_ERROR):
            await sandbox.files.read("/home/user/workspace/ws-1/../../escape.txt")

    asyncio.run(body())


# ---- 命令原语 ----


def test_command_run_success(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        result = await sandbox.commands.run("echo hello")
        assert result.exit_code == 0
        assert "hello" in result.stdout

    asyncio.run(body())


def test_command_nonzero_exit_is_result_not_exception(sandbox: LocalSandbox) -> None:
    """非零退出是命令的业务结果，不是系统故障——不能让工具层误判成 failure。"""
    async def body() -> None:
        result = await sandbox.commands.run(
            "exit 3" if sys.platform != "win32" else "cmd /c exit 3"
        )
        assert result.exit_code == 3

    asyncio.run(body())


def test_command_cwd_is_work_dir(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/marker.txt", "x")
        cmd = "dir /b" if sys.platform == "win32" else "ls"
        result = await sandbox.commands.run(cmd)
        assert "marker.txt" in result.stdout

    asyncio.run(body())


def test_command_env_passthrough(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        cmd = "echo %ETHOS_T% > env.txt" if sys.platform == "win32" else "echo $ETHOS_T > env.txt"
        await sandbox.commands.run(cmd, envs={"ETHOS_T": "sentinel"})
        content = await sandbox.files.read("/home/user/work/env.txt")
        assert "sentinel" in content

    asyncio.run(body())


def test_command_timeout_is_killed(sandbox: LocalSandbox) -> None:
    """超时必须真的结束进程，不能只把 future 取消掉留下孤儿。"""
    async def body() -> None:
        cmd = "ping -n 10 127.0.0.1 > nul" if sys.platform == "win32" else "sleep 10"
        result = await sandbox.commands.run(cmd, timeout=1)
        assert result.exit_code == 124
        assert "超时" in result.stderr

    asyncio.run(body())


def test_command_background_list_and_kill(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        cmd = "ping -n 20 127.0.0.1 > nul" if sys.platform == "win32" else "sleep 20"
        handle = await sandbox.commands.run(cmd, background=True)
        assert handle.pid > 0

        listed = await sandbox.commands.list()
        assert any(p.pid == handle.pid for p in listed)

        await sandbox.commands.kill(handle.pid)
        await asyncio.sleep(0.5)
        remaining = await sandbox.commands.list()
        assert all(p.pid != handle.pid for p in remaining)

    asyncio.run(body())


def test_kill_does_not_delete_files(sandbox: LocalSandbox) -> None:
    """结束沙箱只停后台进程，**不删目录**——工作区文件是用户的。"""
    async def body() -> None:
        await sandbox.files.write("/home/user/work/keep.txt", "keep")
        await sandbox.kill()
        assert await sandbox.files.read("/home/user/work/keep.txt") == "keep"

    asyncio.run(body())


# ---- Service 层：会话 → 沙箱的登记与复用 ----


def test_service_local_backend_registers_and_reuses(tmp_path, monkeypatch) -> None:
    """`Service.workspace()` 在 local 后端下返回 LocalSandbox，并登记 user_sandboxes。

    `user_sandboxes` 不是可有可无的旁路：`existing_workspace`（文件面板预览）
    和 UI 的沙箱状态都读它。没有这一行，工具能跑但面板永远是空的。
    """
    import aiosqlite

    from edith_backend.internal.sandbox.schema import create_schema as create_sandbox_schema
    from edith_backend.internal.sandbox.service import Service

    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_sandbox_schema(db)
        service = Service(db, "unused-template", None, backend="local")  # type: ignore[arg-type]

        first = await service.workspace("u1", "s1")
        assert isinstance(first, LocalSandbox)

        cursor = await db.execute(
            "SELECT sandbox_id FROM user_sandboxes WHERE user_id = ? AND session_id = ?",
            ("u1", "s1"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row["sandbox_id"] == first.sandbox_id

        # 再次调用复用同一目录（跨 run 文件必须还在）
        second = await service.workspace("u1", "s1")
        assert second.root == first.root

        # 只读路径也能打开
        existing = await service.existing_workspace("u1", "s1")
        assert isinstance(existing, LocalSandbox)
        assert existing.root == first.root

        await db.close()

    asyncio.run(body())


def test_service_local_existing_without_open_raises(tmp_path, monkeypatch) -> None:
    """没建过沙箱时 `existing_workspace` 必须报"找不到"，而不是凭空造一个。

    否则一个只读预览请求（比如打开文件面板）会产生一个空沙箱目录，
    之后 `workspace()` 会把它当成"已有沙箱"复用，掩盖掉真正的初始化问题。
    """
    import aiosqlite

    from edith_backend.internal.sandbox.schema import create_schema as create_sandbox_schema
    from edith_backend.internal.sandbox.service import Service

    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_sandbox_schema(db)
        service = Service(db, "unused-template", None, backend="local")  # type: ignore[arg-type]

        with pytest.raises(FileNotFoundError):
            await service.existing_workspace("ghost", "session")

        assert not (tmp_path / "sandbox" / "ghost").exists()
        await db.close()

    asyncio.run(body())
