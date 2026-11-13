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
    POSIX_SHELL,
    LocalSandbox,
    SandboxPathError,
    _decode_output,
)

SANDBOX_PATH_ERROR = SandboxPathError


@pytest.fixture
def sandbox(tmp_path, monkeypatch) -> LocalSandbox:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))
    return LocalSandbox.open("user-1", "session-1")


# ---- 生命周期 ----


def test_open_creates_layout_dirs(sandbox: LocalSandbox) -> None:
    """首次打开就建好 uploads/work/artifacts：模型第一次 ls 不该看到目录缺失。

    `workspace` 是挂载点（指向 workspace-volumes 的联接），首次解析工作区路径时
    才建立——它不是一个"空目录"，提前建一个空目录反而会让 shell 看不到工作区文件。
    """
    for name in ("uploads", "work", "artifacts"):
        assert (sandbox.root / name).is_dir()

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-x/a.txt", "x")
        assert (sandbox.root / "workspace").is_dir()

    asyncio.run(body())


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
        from edith_backend.internal.sandbox.types import FileType

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


def test_list_root_returns_virtual_paths_for_mount_points(sandbox: LocalSandbox) -> None:
    """根目录列表里 `skills` / `workspace` 必须回虚拟路径，不能是磁盘绝对路径。

    这两个是联接（junction），它们的**联接点本身**是回归点：`resolve()` 会把
    `<root>/skills` 解析成 `<data_root>/skills`、把 `<root>/workspace` 解析成
    `workspace-volumes`，旧实现「等于根就跳过」的分支把两者都排除了，兜底返回
    `C:\\...`。文件面板拿这个 path 当节点 key，再展开时原样回传：
    `workspace_path()` 第一段不是白名单目录 → 400「top-level directory must be
    one of ...」，树上的节点就一直显示错误、点不动。
    """

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-1/readme.md", "# hi")
        root_entries = await sandbox.files.list("/home/user", depth=1)
        paths = {e.name: e.path for e in root_entries}

        assert paths["skills"] == "/home/user/skills"
        assert paths["workspace"] == "/home/user/workspace"
        for name, path in paths.items():
            assert path.startswith("/home/user/"), f"{name} 泄漏了磁盘路径：{path}"

    asyncio.run(body())


def test_virtual_maps_mount_roots_and_authoritative_copies(sandbox: LocalSandbox, tmp_path) -> None:
    """`virtual()` 两类输入都要正确：沙箱树里的路径、权威副本的真实路径。"""

    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-1/readme.md", "# hi")

        # 1) 沙箱树（未解析路径）——联接点本身
        assert sandbox.virtual(sandbox.root / "skills") == "/home/user/skills"
        assert sandbox.virtual(sandbox.root / "workspace") == "/home/user/workspace"
        assert sandbox.virtual(sandbox.root / "work" / "a.txt") == "/home/user/work/a.txt"

        # 2) 权威副本的真实路径（workspace 存储 / agent_input 直接给的）
        volume = tmp_path / "workspace-volumes"
        assert sandbox.virtual(volume) == "/home/user/workspace"
        assert sandbox.virtual(volume / "ws-1" / "readme.md") == "/home/user/workspace/ws-1/readme.md"
        skills = tmp_path / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        assert sandbox.virtual(skills) == "/home/user/skills"
        assert sandbox.virtual(skills / "demo" / "SKILL.md") == "/home/user/skills/demo/SKILL.md"

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
        result = await sandbox.commands.run("exit 3")
        assert result.exit_code == 3

    asyncio.run(body())


def test_command_cwd_is_work_dir(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.files.write("/home/user/work/marker.txt", "x")
        result = await sandbox.commands.run("ls")
        assert "marker.txt" in result.stdout
        pwd = await sandbox.commands.run("pwd")
        assert pwd.stdout.strip().replace("\\", "/").endswith("/work")

    asyncio.run(body())


def test_command_env_passthrough(sandbox: LocalSandbox) -> None:
    async def body() -> None:
        await sandbox.commands.run("echo $ETHOS_T > env.txt", envs={"ETHOS_T": "sentinel"})
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
    from edith_backend.internal.sandbox.service import SandboxNotFound, Service

    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_sandbox_schema(db)
        service = Service(db)

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
    from edith_backend.internal.sandbox.service import SandboxNotFound, Service

    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path))

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_sandbox_schema(db)
        service = Service(db)

        # 必须抛 SandboxNotFound 而不是 FileNotFoundError：HTTP 层只把前者
        # 映射成 404，后者会漏成 500——同一个原因两种状态码。
        with pytest.raises(SandboxNotFound):
            await service.existing_workspace("ghost", "session")

        assert not (tmp_path / "sandbox" / "ghost").exists()
        await db.close()

    asyncio.run(body())


# ---- 命令执行环境：四条等价性要求 ----
#
# 模型是按 Linux 习惯写命令的（`/home/user`、`ls`、`mkdir -p`、管道）。
# E2B 里这些都成立，本机不成立，所以本地后端要自己补齐。
# 下面每个用例对应一种"看起来能用、实际不能用"的失败方式。


def test_shell_sees_workspace_volume(sandbox: LocalSandbox) -> None:
    """文件原语与 shell 必须看同一片字节。

    少了 workspace/{id} 的联接，文件工具写的是权威副本、shell 看到的是沙箱里
    那个空目录——模型"刚写完就 ls 不到"，于是开始疯狂重试
    （2026-11-12 实测一轮 21 次工具调用里 15 次在找同一个文件）。
    """
    async def body() -> None:
        ws = "ws-mount"
        await sandbox.files.write(f"/home/user/workspace/{ws}/a.txt", "hello")

        seen = await sandbox.commands.run(f"ls /home/user/workspace/{ws}")
        assert "a.txt" in seen.stdout, f"shell 看不到工作区文件: {seen.stderr}"

        # 反方向：shell 写的文件，文件原语也要读得到（同一个目录，不是副本）
        await sandbox.commands.run(f"echo from-shell > /home/user/workspace/{ws}/b.txt")
        assert await sandbox.files.read(f"/home/user/workspace/{ws}/b.txt") == "from-shell\n"

        # 相对路径同样成立（cwd 是 work/，与 E2B 一致）
        rel = await sandbox.commands.run(f"ls ../workspace/{ws}")
        assert "a.txt" in rel.stdout

    asyncio.run(body())


@pytest.mark.skipif(POSIX_SHELL is None, reason="本机没有 POSIX shell")
def test_virtual_root_is_resolvable_in_shell(sandbox: LocalSandbox) -> None:
    """`/home/user` 既要能被命令解析，也要能在输出里原样回给模型。"""
    async def body() -> None:
        result = await sandbox.commands.run("pwd")
        assert result.exit_code == 0
        # 输出侧必须还原成虚拟路径：模型会拿它原样再传给文件工具，
        # 宿主真实路径会被 resolve() 判为越界。
        assert result.stdout.strip().replace("\\", "/").startswith("/home/user")
        assert "AppData" not in result.stdout

        listed = await sandbox.commands.run("ls /home/user")
        assert listed.exit_code == 0, listed.stderr
        assert "work" in listed.stdout

    asyncio.run(body())


@pytest.mark.skipif(POSIX_SHELL is None, reason="本机没有 POSIX shell")
def test_command_output_paths_are_virtual(sandbox: LocalSandbox) -> None:
    """`find` 的输出必须能直接喂回文件工具（虚拟路径，不是宿主路径）。"""
    async def body() -> None:
        await sandbox.files.write("/home/user/workspace/ws-out/deep/x.py", "print(1)")
        found = await sandbox.commands.run(
            'find /home/user/workspace/ws-out -name "*.py"'
        )
        path = found.stdout.strip().splitlines()[0]
        assert path == "/home/user/workspace/ws-out/deep/x.py"
        # 回读：输出里的路径是可直接使用的
        assert await sandbox.files.read(path) == "print(1)"

    asyncio.run(body())


@pytest.mark.skipif(POSIX_SHELL is None, reason="本机没有 POSIX shell")
def test_posix_habits_work(sandbox: LocalSandbox) -> None:
    """`mkdir -p`、管道、`sed` 都不该报"找不到命令"。

    这些在 E2B 里天然存在，本地若缺，模型每条命令都要试错一次。
    """
    async def body() -> None:
        made = await sandbox.commands.run("mkdir -p deep/a/b && echo ok")
        assert made.exit_code == 0, made.stderr

        piped = await sandbox.commands.run("printf 'x\\ny\\n' | wc -l")
        assert piped.stdout.strip() == "2"

    asyncio.run(body())


def test_gbk_output_is_decoded() -> None:
    """中文 Windows 的控制台程序按 GBK 输出；只按 UTF-8 解会变成乱码。

    乱码的后果不是"显示难看"：模型会把 `ϵͳ�Ҳ���ָ����·����` 当成"命令语法错了"，
    换着语法重试，而不是"这个路径不存在"。
    """
    gbk = "系统找不到指定的路径".encode("gbk")
    assert _decode_output(gbk) == "系统找不到指定的路径"
    assert _decode_output("正常 UTF-8".encode()) == "正常 UTF-8"
