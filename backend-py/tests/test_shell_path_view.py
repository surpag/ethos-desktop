"""S4：Shell 的路径视图与文件工具同根。

S2/S3 之后文件工具与面板都落在绑定目录，但 shell 还留在旧视图：

- cwd 取自 `WORKSPACE.work_path()`（沙箱树里的 `work/`）；
- `_translate` 只把 `/home/user` 换成沙箱根，于是命令里的
  `/home/user/workspace/{id}/…` 经 `{root}/workspace` 联接落到 `workspace-volumes/{id}`。

同一轮里「文件工具写用户目录、shell 看到旧副本」，模型会把同一个路径重试到熔断。

这一层要证明四件事，缺一条就还是分裂的：

1. **cwd 同源**：工具层默认 cwd 就是工作区根（绑定态=用户目录）；
2. **绝对虚拟路径同源**：命令里的 `workspace/{id}/…` 与文件工具落到同一个根；
3. **边界不被前缀吃掉**：`workspace/ws-10` 不能因为存在 `ws-1` 绑定就被翻译错；
4. **`work/` 语义不变**：临时产物仍然落沙箱树，不跟着 cwd 一起漂。

第 4 条是刻意的：`work/` 与 `artifacts/` 是沙箱级目录，不属于工作区。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from edith_backend.config import data_root
from edith_backend.internal.sandbox.local import LocalSandbox
from edith_backend.internal.sandbox.tools import SandboxToolSet, _command_cwd
from edith_backend.internal.workspace.context import (
    BindingRecord,
    ProbeReason,
    RootProbe,
    build_context,
)

WS = "ws1"


def _context(root: Path | str, *, workspace_id: str = WS):
    return build_context(
        BindingRecord(workspace_id=workspace_id, local_root=str(root)),
        RootProbe(ProbeReason.OK),
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


def _write_probe(name: str) -> str:
    """一条「在当前目录写文件」的命令，不依赖 bash 专有语法。"""
    return (
        f'"{sys.executable}" -c '
        f'"open({name!r}, \'w\', encoding=\'utf-8\').write(\'ok\')"'
    )


class _StaticService:
    """只回同一个沙箱的 service 替身（工具层唯一依赖）。"""

    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sandbox = sandbox

    async def workspace(self, user_id, session_id, context=None):  # noqa: ANN001
        return self._sandbox


def _run_tool(sandbox: LocalSandbox, name: str, args: dict) -> dict:
    """走工具层（含 `_complete_arguments` / 授权 / cwd 决策）。"""
    tools = SandboxToolSet(_StaticService(sandbox))

    async def go() -> dict:
        raw = await tools.execute(name, args, "u1", "s1", WS, context=sandbox.workspace_context)
        return json.loads(raw) if isinstance(raw, str) else raw

    return asyncio.run(go())


# ---- 1. cwd 同源 ----


def test_command_cwd_is_workspace_root() -> None:
    """有工作区就落在工作区根；没有工作区（无项目对话）仍是 `work/`。"""
    assert _command_cwd(WS) == f"/home/user/workspace/{WS}"
    assert _command_cwd("") == "/home/user/work"


def test_tool_run_command_defaults_to_bound_root(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """工具层不传 cwd 时，命令的初始目录就是绑定目录。"""
    result = _run_tool(sandbox, "sandbox_run_command", {"command": _write_probe("probe.txt")})

    assert result.get("exitCode") == 0, result
    assert (bound_root / "probe.txt").read_text(encoding="utf-8") == "ok"
    assert not (data_root() / "workspace-volumes" / WS).exists()


# ---- 2. 绝对虚拟路径同源 ----


def test_absolute_virtual_workspace_path_reaches_bound_root(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """命令里写 `/home/user/workspace/{id}/…` 必须看到用户目录里的文件。

    这是模型最自然的写法（工具描述里就是 `/home/user/workspace/`），
    翻译错一步就会经联接读到旧副本——而旧副本可能根本是空的。
    """
    translated = sandbox.commands._translate(f"cat /home/user/workspace/{WS}/app.py")
    assert "workspace-volumes" not in translated.replace("\\", "/")
    assert sandbox.commands._shell_path_of(bound_root) in translated.replace("\\", "/")

    # 用 `cat`（MSYS 程序）而不是 `sys.executable`：翻译出来的 `/d/...` 是 shell 的
    # 原生形式，Windows 原生程序（python.exe）不认——那是既有局限，已在
    # docs/p0-2-s4-shell-cwd.md 记为已知项，不在这一层断言。
    result = asyncio.run(sandbox.commands.run(f"cat /home/user/workspace/{WS}/app.py"))
    assert result.exit_code == 0, result.stderr
    assert "print('hi')" in result.stdout


def test_translate_rejects_sibling_workspace_prefix_collision(
    bound_root: Path
) -> None:
    """`workspace/ws-10/…` 不能因为存在 `ws-1` 绑定就被翻译到用户目录。"""
    sandbox = LocalSandbox.open("u1", "s1", context=_context(bound_root, workspace_id="ws-1"))
    translated = sandbox.commands._translate("ls /home/user/workspace/ws-10").replace("\\", "/")

    assert "workspace-volumes/ws-10" in translated
    assert sandbox.commands._shell_path_of(bound_root) not in translated


# ---- 3. `work/` 语义不变 ----


def test_work_prefix_still_maps_into_sandbox(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """临时产物仍写沙箱树的 `work/`，不会跟着 cwd 漂到用户目录。"""
    translated = sandbox.commands._translate("ls /home/user/work").replace("\\", "/")

    assert sandbox.commands._shell_path_of(sandbox.root / "work") in translated
    assert sandbox.commands._shell_path_of(bound_root) not in translated
    assert not (bound_root / "work").exists()


# ---- 4. 输出归一化 ----


def test_normalize_maps_bound_path_back_to_virtual(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """输出里的宿主路径要还原成虚拟路径，且不留混合分隔符。

    否则模型拿到 `C:\\…\\my-project\\app.py` 再传回来，`resolve()` 会判越界，
    表现成「刚写完就读不到」。
    """
    raw = str(bound_root / "app.py")
    out = sandbox.commands._normalize(f"found {raw}")

    assert f"/home/user/workspace/{WS}/app.py" in out
    assert raw not in out
    assert "\\" not in out.split("/home/user", 1)[1]


# ---- 5. 端到端：shell 写、文件工具读 ----


def test_shell_write_is_visible_to_file_tools(
    sandbox: LocalSandbox, bound_root: Path
) -> None:
    """S4 真正要消灭的分裂：shell 写的文件，文件工具当轮就能读到。"""
    _run_tool(sandbox, "sandbox_run_command", {"command": _write_probe("from-shell.txt")})

    async def read_back() -> str:
        return await sandbox.files.read(f"workspace/{WS}/from-shell.txt")

    assert asyncio.run(read_back()) == "ok"
    assert (bound_root / "from-shell.txt").is_file()
