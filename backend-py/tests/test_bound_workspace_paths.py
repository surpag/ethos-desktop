"""S2：绑定态的路径落点。

这一层要证明的是**落点**，不是状态机（状态机在 `test_workspace_context.py`）。
三条断言缺一不可：

1. 绑定态的 `workspace/{id}/…` 落在用户目录（文件工具与 Git 共用同一个 `resolve()`）；
2. 落过去之后**不创建** `workspace-volumes/{id}`——执行链不是这个目录的拥有者；
3. 根不可用时**失败关闭**，绝不回退到旧副本、也不在原路径重建空目录。

第 3 条是本文件最重要的：静默回退比报错危险得多，因为用户会以为自己在看绑定目录。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from edith_backend.config import data_root
from edith_backend.internal.sandbox.local import LocalSandbox, SandboxPathError
from edith_backend.internal.workspace.context import (
    BindingRecord,
    ProbeReason,
    RootProbe,
    WorkspaceUnavailableError,
    build_context,
)

WS = "ws1"


def _context(
    root: Path | str,
    *,
    workspace_id: str = WS,
    prefix: str = "",
    probe: RootProbe | None = None,
):
    """按"探测通过"构造一个绑定态快照；传 `probe` 可模拟任意探测结论。"""
    return build_context(
        BindingRecord(
            workspace_id=workspace_id, local_root=str(root), prefix=prefix
        ),
        RootProbe(ProbeReason.OK) if probe is None else probe,
    )


@pytest.fixture
def bound_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "my-project"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root


@pytest.fixture
def sandbox(bound_root: Path) -> LocalSandbox:
    return LocalSandbox.open("u1", "s1", context=_context(bound_root))


# ---- 1. 落点 ----


def test_bound_path_lands_in_local_root(sandbox: LocalSandbox, bound_root: Path) -> None:
    real = sandbox.resolve(f"workspace/{WS}/app.py")
    assert real == (bound_root / "app.py").resolve()


def test_bound_workspace_root_is_local_root(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """`workspace/{id}` 本身就是绑定目录，不是它的上级也不是副本。"""
    assert sandbox.resolve(f"workspace/{WS}") == bound_root.resolve()


def test_unbound_context_matches_no_context(tmp_path, monkeypatch) -> None:
    """未绑定上下文与"完全没上下文"必须逐字节同构（S1 基线守的就是这条）。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    with_ctx = LocalSandbox.open(
        "u1", "s1", context=build_context(BindingRecord(WS, None), None)
    )
    # 同一会话的同一沙箱根，只有"有没有上下文"这一个变量不同
    without = LocalSandbox.open("u1", "s1")
    for virtual in (f"workspace/{WS}", f"workspace/{WS}/a.txt", "work/a.txt", "."):
        assert with_ctx.resolve(virtual) == without.resolve(virtual)


# ---- 2. 不创建副本 ----


def test_bound_does_not_create_volume_copy(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """绑定态下执行链只是使用者：`workspace-volumes/{id}` 不该被建出来。"""
    sandbox.resolve(f"workspace/{WS}/app.py")
    assert not (data_root() / "workspace-volumes" / WS).exists()


def test_bound_write_never_touches_volume_copy(
    bound_root: Path, sandbox: LocalSandbox
) -> None:
    async def go() -> None:
        await sandbox.files.write(f"workspace/{WS}/new.txt", "data")

    asyncio.run(go())
    assert (bound_root / "new.txt").read_text(encoding="utf-8") == "data"
    assert not (data_root() / "workspace-volumes" / WS).exists()


# ---- 3. 失败关闭 ----


def test_bound_root_removed_mid_run_fails_closed(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """快照时 ready，执行期间目录被删：本次执行失败，且**不重建**。

    `LocalFiles.write` 与 `LocalCommands.run` 都会对解析出的路径 `mkdir` 父目录，
    所以这道检查必须发生在 `resolve()` 里——晚一步就会在旧路径造出空目录树。
    """
    shutil.rmtree(bound_root)

    with pytest.raises(WorkspaceUnavailableError) as excinfo:
        sandbox.resolve(f"workspace/{WS}/app.py")

    assert excinfo.value.context.workspace_id == WS
    assert "本次执行期间" in str(excinfo.value)
    assert not bound_root.exists(), "解析失败时不得在原路径重建目录"


def test_unavailable_state_fails_closed(tmp_path, monkeypatch) -> None:
    """探测就失败（快照非 ready）：一次都不该进入执行。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "gone"
    root.mkdir()
    context = _context(root, probe=RootProbe(ProbeReason.NOT_EXISTS, "stat: nope"))
    sandbox = LocalSandbox.open("u1", "s1", context=context)

    with pytest.raises(WorkspaceUnavailableError):
        sandbox.resolve(f"workspace/{WS}/a.txt")
    assert not (data_root() / "workspace-volumes" / WS).exists()


def test_bound_entry_path_is_virtual(sandbox: LocalSandbox) -> None:
    """文件工具回给模型的是虚拟路径。

    给宿主绝对路径（`C:\\…\\my-project\\app.py`）的后果是模型下一次把它原样传回来
    会被 `resolve()` 判成越界——表现成"刚写完就读不到"，然后反复重试同一个路径。
    """

    async def go():
        return await sandbox.files.get_info(f"workspace/{WS}/app.py")

    assert asyncio.run(go()).path == f"/home/user/workspace/{WS}/app.py"


def test_tool_layer_writes_to_bound_root(bound_root: Path, monkeypatch) -> None:
    """端到端：工具层（`SandboxToolSet`）的写操作落在绑定目录，读回来还是同一个文件。"""
    import json

    from edith_backend.internal.sandbox.tools import SandboxToolSet

    monkeypatch.setenv("ETHOS_DATA_DIR", str(bound_root.parent / "data"))
    context = _context(bound_root)
    sandbox = LocalSandbox.open("u1", "s1", context=context)

    class _Service:
        async def workspace(self, user_id, session_id, context=None):  # noqa: ANN001
            return sandbox

    tools = SandboxToolSet(_Service())

    async def go():
        written = await tools.execute(
            "sandbox_write_file",
            {"path": "report.md", "content": "# hi"},
            "u1",
            "s1",
            WS,
            context=context,
        )
        listed = await tools.execute(
            "sandbox_list_files", {"path": ""}, "u1", "s1", WS, context=context
        )
        return json.loads(written), json.loads(listed)

    written, listed = asyncio.run(go())
    assert (bound_root / "report.md").read_text(encoding="utf-8") == "# hi"
    # 工具层对外给的是相对形式（`relative_path` 的既有契约），关键是它没变成宿主绝对路径
    assert written["path"] == f"workspace/{WS}/report.md"
    assert [e["name"] for e in listed["entries"]].count("report.md") == 1
    assert not (data_root() / "workspace-volumes" / WS).exists()


def test_denied_state_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "denied"
    root.mkdir()
    context = _context(root, probe=RootProbe(ProbeReason.PERMISSION_DENIED, "denied"))
    sandbox = LocalSandbox.open("u1", "s1", context=context)

    with pytest.raises(WorkspaceUnavailableError):
        sandbox.resolve(f"workspace/{WS}/a.txt")


# ---- 4. 拒绝的两种边界 ----


def test_prefix_binding_is_rejected(tmp_path, monkeypatch) -> None:
    """带前缀层时虚拟路径与本地路径不是一一对应——半映射会写错位置，所以停住。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "Data"
    root.mkdir()
    sandbox = LocalSandbox.open("u1", "s1", context=_context(root, prefix="Data"))

    with pytest.raises(SandboxPathError) as excinfo:
        sandbox.resolve(f"workspace/{WS}/a.txt")
    assert "前缀" in str(excinfo.value)


def test_other_workspace_is_rejected(tmp_path, monkeypatch) -> None:
    """一次执行固定在一个工作区上，路径指向别处必须拒绝（不许静默落到副本）。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "my-project"
    root.mkdir()
    sandbox = LocalSandbox.open("u1", "s1", context=_context(root))

    with pytest.raises(SandboxPathError) as excinfo:
        sandbox.resolve("workspace/other-ws/a.txt")
    assert "本次执行固定为" in str(excinfo.value)


# ---- 5. 运行期落点（Git 走的就是这条路） ----


def test_command_cwd_follows_bound_root(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """shell 进程的 cwd 也来自同一个 `resolve()`——Git 的 `cwd=workspace/{id}` 同理。

    这里不跑 git（需要仓库和可执行文件），只验证"以工作区为 cwd 起进程"这一层：
    相对路径写出的文件必须出现在绑定目录里。
    """
    import asyncio

    async def go():
        return await sandbox.commands.run(
            f'"{sys.executable}" -c "open(\'probe.txt\',\'w\',encoding=\'utf-8\').write(\'ok\')"',
            cwd=f"workspace/{WS}",
        )

    result = asyncio.run(go())
    assert result.exit_code == 0, result.stderr
    assert (bound_root / "probe.txt").read_text(encoding="utf-8") == "ok"
    assert not (data_root() / "workspace-volumes" / WS).exists()
