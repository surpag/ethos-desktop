"""S6：P0-2 组合验收——一轮里所有执行入口看同一份数据。

S2/S3/S4/S5 各自证明了一个入口，但用户真正的体验是**它们同时存在**：

    文件工具写 → shell 读 → git 看到 → shell 写 → 文件工具读

四个入口里任意一个落错根，模型就会在「刚写完就读不到」里打转。这一层不引入
新实现，只把组合钉住，同时覆盖两种状态与两种失败：

| 场景 | 断言 |
| --- | --- |
| 绑定态 · 全部成功 | 三入口互相看见同一份文件，且不创建 `workspace-volumes/{id}` |
| 绑定态 · 运行中期目录消失 | 三入口**全部** fail-closed（retryable=False），无一回退 |
| 未绑定态 | 三入口落在 `workspace-volumes/{id}`，与改造前一致 |

面板入口由 `test_panel_workspace_source.py` 覆盖（HTTP 层），这里不重复。
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


def _bound_context(root: Path | str):
    return build_context(
        BindingRecord(workspace_id=WS, local_root=str(root)),
        RootProbe(ProbeReason.OK),
    )


def _unbound_context():
    return build_context(BindingRecord(workspace_id=WS, local_root=None), None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


class _StaticService:
    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sandbox = sandbox

    async def workspace(self, user_id, session_id, context=None):  # noqa: ANN001
        return self._sandbox


def _tool(sandbox: LocalSandbox, name: str, args: dict) -> dict:
    tools = SandboxToolSet(_StaticService(sandbox))

    async def go() -> dict:
        raw = await tools.execute(name, args, "u1", "s1", WS, context=sandbox.workspace_context)
        return json.loads(raw) if isinstance(raw, str) else raw

    return asyncio.run(go())


def _shell(sandbox: LocalSandbox, command: str) -> dict:
    return _tool(sandbox, "sandbox_run_command", {"command": command})


def _git(sandbox: LocalSandbox, *args: str) -> dict:
    return _tool(sandbox, "git", {"subcommand": args[0], "args": list(args[1:])})


def _has_git(sandbox: LocalSandbox) -> bool:
    return asyncio.run(sandbox.commands.run("git --version")).exit_code == 0


# ---- 场景 A：绑定态，一轮里三入口互相看见 ----


def test_bound_round_trip_across_all_entry_points(data_dir: Path) -> None:
    root = data_dir.parent / "proj"
    root.mkdir()
    sandbox = LocalSandbox.open("u1", "s1", context=_bound_context(root))

    # 1. 文件工具写
    written = _tool(sandbox, "sandbox_write_file", {"path": "from-tool.txt", "content": "hello"})
    assert "error" not in written, written

    # 2. shell 读到了（cwd 就是工作区，相对路径直接命中）
    cat = _shell(sandbox, "cat from-tool.txt")
    assert cat.get("exitCode") == 0, cat
    assert cat["stdout"].strip() == "hello"

    # 3. shell 写，文件工具读回来
    shell_write = _shell(sandbox, "printf from-shell > from-shell.txt")
    assert shell_write.get("exitCode") == 0, shell_write
    read_back = _tool(sandbox, "sandbox_read_file", {"path": "from-shell.txt"})
    assert "from-shell" in json.dumps(read_back, ensure_ascii=False)

    # 4. shell 用绝对虚拟路径写，落在同一个根
    abs_write = _shell(sandbox, f"printf abs > /home/user/workspace/{WS}/from-abs.txt")
    assert abs_write.get("exitCode") == 0, abs_write
    assert (root / "from-abs.txt").read_text(encoding="utf-8") == "abs"

    # 5. git 看到的也是这两个文件
    if _has_git(sandbox):
        # `init` 不在 git 工具的白名单里（那个白名单是给模型用的），
        # 这里直接走沙箱原语：测的是落点，不是策略。
        assert asyncio.run(sandbox.commands.run("git init", cwd=f"workspace/{WS}")).exit_code == 0
        status = _git(sandbox, "status", "--porcelain")
        assert status.get("exitCode") == 0, status
        assert "from-tool.txt" in status["stdout"]
        assert "from-shell.txt" in status["stdout"]

    # 6. 全程没有副本目录
    assert not (data_root() / "workspace-volumes" / WS).exists()


# ---- 场景 B：绑定态，运行中期目录消失 → 三入口全部 fail-closed ----


def test_bound_root_lost_mid_run_fails_closed_everywhere(data_dir: Path) -> None:
    import shutil

    root = data_dir.parent / "vanishing"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    sandbox = LocalSandbox.open("u1", "s1", context=_bound_context(root))

    # 先证明它是可用的，避免"本来就坏"掩盖问题
    assert _tool(sandbox, "sandbox_read_file", {"path": "a.txt"}).get("error") is None

    shutil.rmtree(root)

    results = {
        "file_tool": _tool(sandbox, "sandbox_write_file", {"path": "b.txt", "content": "x"}),
        "shell": _shell(sandbox, "ls"),
        "git": _git(sandbox, "status"),
    }
    for name, result in results.items():
        assert result.get("error"), f"{name} 应当失败关闭，实际: {result}"
        assert result.get("retryable") is False, f"{name} 不该被当成可重试的业务错误: {result}"
        assert "不可用" in result["error"], f"{name} 的文案要指向绑定目录: {result['error']}"

    assert not root.exists(), "失败关闭不得在原路径重建目录"
    assert not (data_root() / "workspace-volumes" / WS).exists(), "不得回退到旧副本"


# ---- 场景 C：未绑定态，三入口落在副本目录 ----


def test_unbound_round_trip_uses_volume_copy(data_dir: Path) -> None:
    sandbox = LocalSandbox.open("u1", "s9", context=_unbound_context())

    written = _tool(sandbox, "sandbox_write_file", {"path": "u.txt", "content": "u"})
    assert "error" not in written, written

    cat = _shell(sandbox, "cat u.txt")
    assert cat.get("exitCode") == 0, cat
    assert cat["stdout"].strip() == "u"

    volume = data_root() / "workspace-volumes" / WS
    assert (volume / "u.txt").read_text(encoding="utf-8") == "u"

    # 沙箱级的 work/ 仍然是绝对路径才能到，不会因为 cwd 变化被写进工作区
    work_write = _shell(sandbox, "printf w > /home/user/work/w.txt")
    assert work_write.get("exitCode") == 0, work_write
    assert (sandbox.root / "work" / "w.txt").read_text(encoding="utf-8") == "w"
    assert not (volume / "work").exists()
