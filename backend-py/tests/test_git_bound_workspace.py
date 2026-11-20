"""S5：Git 在绑定态落在用户目录。

S2 改的是 `resolve()`，Git 与文件工具共用它；S4 改的是 shell 的 cwd 与路径翻译。
这一层把 Git 单独钉一遍，因为它的调用链最长、最容易在某次改动里悄悄退回副本：

    `_op_git` → `run_git(cwd=workspace/{id})` → `commands.run` → `resolve()` → 绑定根

三个断言：

1. `git status` 在绑定目录里执行成功；
2. 文件工具写的文件，`git status` 当轮就能看到——**两个工具看同一个目录**；
3. 全程不创建 `workspace-volumes/{id}`——Git 不是副本目录的拥有者。

没有可执行 git 时跳过：这里测的是落点，不是 git 本身。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from edith_backend.config import data_root
from edith_backend.internal.sandbox.local import LocalSandbox
from edith_backend.internal.sandbox.tools import SandboxToolSet
from edith_backend.internal.workspace.context import (
    BindingRecord,
    ProbeReason,
    RootProbe,
    build_context,
)

WS = "ws1"


def _context(root: Path | str):
    return build_context(
        BindingRecord(workspace_id=WS, local_root=str(root)),
        RootProbe(ProbeReason.OK),
    )


@pytest.fixture
def bound_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root


@pytest.fixture
def sandbox(bound_root: Path) -> LocalSandbox:
    return LocalSandbox.open("u1", "s1", context=_context(bound_root))


def _git(sandbox: LocalSandbox, *args: str):
    return asyncio.run(
        sandbox.commands.run("git " + " ".join(args), cwd=f"workspace/{WS}")
    )


def _require_git(sandbox: LocalSandbox) -> None:
    probe = asyncio.run(sandbox.commands.run("git --version"))
    if probe.exit_code != 0:
        pytest.skip(f"环境没有可执行的 git: {probe.stderr.strip()[:120]}")


class _StaticService:
    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sandbox = sandbox

    async def workspace(self, user_id, session_id, context=None):  # noqa: ANN001
        return self._sandbox


def _run_tool(sandbox: LocalSandbox, name: str, args: dict) -> dict:
    tools = SandboxToolSet(_StaticService(sandbox))

    async def go() -> dict:
        raw = await tools.execute(name, args, "u1", "s1", WS, context=sandbox.workspace_context)
        return json.loads(raw) if isinstance(raw, str) else raw

    return asyncio.run(go())


def test_git_status_runs_in_bound_root(sandbox: LocalSandbox, bound_root: Path) -> None:
    """git 的 cwd 来自同一个 `resolve()`，绑定态就是用户目录。"""
    _require_git(sandbox)
    assert _git(sandbox, "init").exit_code == 0

    assert (bound_root / ".git").is_dir(), "仓库必须初始化在绑定目录里"
    result = _git(sandbox, "status", "--porcelain")
    assert result.exit_code == 0, result.stderr
    # 输出里不该出现宿主绝对路径：`_normalize` 负责还原成虚拟路径
    assert str(bound_root) not in result.stdout


def test_git_sees_file_written_by_file_tool(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """文件工具写、git 读：两个工具必须看同一个目录。"""
    _require_git(sandbox)
    assert _git(sandbox, "init").exit_code == 0

    written = _run_tool(sandbox, "sandbox_write_file", {"path": "new.txt", "content": "x"})
    assert "error" not in written, written

    result = _git(sandbox, "status", "--porcelain")
    assert result.exit_code == 0, result.stderr
    assert "new.txt" in result.stdout
    assert (bound_root / "new.txt").is_file()


def test_git_and_file_tools_never_touch_volume_copy(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """全程不创建 `workspace-volumes/{id}`：绑定态执行链只是使用者。"""
    _require_git(sandbox)
    _git(sandbox, "init")
    _run_tool(sandbox, "sandbox_write_file", {"path": "a.txt", "content": "x"})
    _git(sandbox, "status", "--porcelain")

    assert not (data_root() / "workspace-volumes" / WS).exists()
