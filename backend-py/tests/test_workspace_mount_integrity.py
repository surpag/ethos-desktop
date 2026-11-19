"""P0-1 之二：workspace / skills 挂载点的完整性自检。

背景：`_ensure_workspace_mount` 原先"见到联接就早退"，不校验联接指向。
联接还在、但目标是别的目录（或目标已被删）时，文件工具落到 `workspace-volumes`，
而 shell 走的是那个联接——两个视图再次分裂，且**没有任何报错**：
文件工具能读能写，`ls` 却看到别处，只有靠模型反复说"文件不见了"才能反推。

本轮的不变量（两条 mount 共用 `_ensure_mount`，语义逐条相同）：

| 情况 | 行为 |
| --- | --- |
| 联接指向预期目录（**回读确认**） | 复用，不重建（不产生任何写入） |
| 联接目标被删（损坏） | 重建；摘链接不得碰目标 |
| 联接指向错误目录 | 重建；**不得静默复用**，也不得删目标 |
| 摘链接失败 | 抛 `unlink_failed` |
| 重建失败 | 抛 `relink_failed` |
| 建完回读仍不符 | 抛 `verify_failed` |
| 空实体目录占位（无数据） | 删掉占位后建链接 |
| 非空目录 / 同名文件占位 | 抛 `occupied`，其中内容一个不动 |
| 占位目录因权限等原因删不掉 | 抛 `prepare_failed`（**不是** `occupied`） |
| 首次创建失败 | 抛 `create_failed` |

最后一行是 2026-11-18 的修正：原先"首次创建失败仅告警"，判据是"没有弄坏既有
状态"。这个判据站不住——挂载没建成时文件工具走 `workspace-volumes`、shell 走
`workspace`，两者是不同的目录，**这正是 P0-1 要消灭的视图分裂**，与它是不是
"刚建的"无关。所以现在只有两种结局：返回一个已验证一致的根，或者抛错。

"验证"是回读（`_link_target` + `_same_dir`），不是"我刚调用过 `_make_link`"：
建链接返回成功但落点不对，同样必须报错，否则函数就在骗调用方。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from edith_backend.internal.sandbox import local as local_mod
from edith_backend.internal.sandbox.local import (
    MOUNT_CREATE_FAILED,
    MOUNT_OCCUPIED,
    MOUNT_PREPARE_FAILED,
    MOUNT_RELINK_FAILED,
    MOUNT_UNLINK_FAILED,
    MOUNT_VERIFY_FAILED,
    LocalSandbox,
    SandboxMountError,
    _is_link,
    _link_target,
    _link_target_text,
    _make_link,
    _same_dir,
)


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


def _link(mount: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if _is_link(mount) or mount.exists():
        _remove_link(mount)
    mount.parent.mkdir(parents=True, exist_ok=True)
    if not _make_link(mount, target):
        pytest.skip("当前环境无法创建目录联接/符号链接（需要 mklink 或 symlink 权限）")


# ---------------------------------------------------------------------------
# 复用
# ---------------------------------------------------------------------------


def test_healthy_mount_is_reused_without_relinking(
    sandbox: LocalSandbox, monkeypatch
) -> None:
    """联接已指向正确目录 → 直接复用，不做任何重建。"""

    def boom(*_args, **_kwargs):
        raise AssertionError("健康挂载不该被重建")

    first = sandbox._ensure_workspace_mount()
    monkeypatch.setattr(local_mod, "_make_link", boom)
    assert sandbox._ensure_workspace_mount() == first


# ---------------------------------------------------------------------------
# 损坏：目标被删
# ---------------------------------------------------------------------------


def test_broken_mount_is_repaired(sandbox: LocalSandbox, data_dir: Path) -> None:
    """联接目标被删（用户移走/重命名了目录）→ 检测到损坏并重新挂到 volumes。"""
    gone = data_dir / "gone-target"
    mount = sandbox.root / "workspace"
    _link(mount, gone)
    os.rmdir(gone)

    assert _is_link(mount) and not mount.exists(), "前置条件：应是一个坏联接"

    root = sandbox._ensure_workspace_mount()

    assert root == (data_dir / "workspace-volumes").resolve()
    assert _is_link(mount)
    assert _same_dir(_link_target(mount), root), "重建后仍指向旧目标"
    assert mount.is_dir(), "重建后 shell 应当能进入"


# ---------------------------------------------------------------------------
# 指向错误目录：不得静默复用
# ---------------------------------------------------------------------------


def test_wrong_target_is_not_reused(sandbox: LocalSandbox, data_dir: Path) -> None:
    """联接指向别处（不是 workspace-volumes）→ 必须重建，不能早退。"""
    elsewhere = data_dir / "elsewhere"
    mount = sandbox.root / "workspace"
    _link(mount, elsewhere)
    assert _same_dir(_link_target(mount), elsewhere)

    root = sandbox._ensure_workspace_mount()

    assert not _same_dir(_link_target(mount), elsewhere), "坏挂载被静默复用了"
    assert _same_dir(_link_target(mount), root)
    assert elsewhere.is_dir(), "重建时删掉了目标目录"


def test_wrong_target_keeps_previous_contents(sandbox: LocalSandbox, data_dir: Path) -> None:
    """重建只摘链接：旧目标里的文件（含子目录）必须逐字节还在。

    只断言"链接换了"是不够的——真正要排除的是「顺手把旧目标清掉」这种
    破坏性修复。所以这里放嵌套文件，并在修复后核对内容与目录结构。
    """
    elsewhere = data_dir / "user-project"
    nested = elsewhere / "src" / "pkg"
    nested.mkdir(parents=True)
    keep = nested / "main.py"
    keep.write_text("print('user data')\n", encoding="utf-8")
    top = elsewhere / "README.md"
    top.write_text("readme", encoding="utf-8")

    _link(sandbox.root / "workspace", elsewhere)
    sandbox._ensure_workspace_mount()

    assert keep.is_file(), "修复挂载时删掉了旧目标里的文件"
    assert keep.read_text(encoding="utf-8") == "print('user data')\n"
    assert top.read_text(encoding="utf-8") == "readme"
    assert sorted(p.name for p in elsewhere.iterdir()) == ["README.md", "src"]


def test_wrong_target_keeps_shell_and_file_tools_in_sync(
    sandbox: LocalSandbox, data_dir: Path
) -> None:
    """修复后 shell 与文件工具看到同一片字节（这才是挂载自检的目的）。"""
    elsewhere = data_dir / "elsewhere-sync"
    elsewhere.mkdir()
    (elsewhere / "old.txt").write_text("old", encoding="utf-8")
    _link(sandbox.root / "workspace", elsewhere)

    sandbox._ensure_workspace_mount()

    written = sandbox.resolve("workspace/ws-sync/from-tool.txt")
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text("tool", encoding="utf-8")

    via_mount = sandbox.root / "workspace" / "ws-sync" / "from-tool.txt"
    assert via_mount.is_file(), "shell 侧（联接路径）看不到文件工具写的文件"
    assert via_mount.read_text(encoding="utf-8") == "tool"
    assert not (elsewhere / "ws-sync").exists(), "文件仍落在旧目标目录里"


# ---------------------------------------------------------------------------
# 修复失败 / 首次创建失败：一律明确报错
# ---------------------------------------------------------------------------


def test_rebuild_failure_raises(sandbox: LocalSandbox, data_dir: Path, monkeypatch) -> None:
    """摘掉坏链接后重建失败 → 抛错，不返回一个"看起来正常"的根。"""
    elsewhere = data_dir / "elsewhere-fail"
    _link(sandbox.root / "workspace", elsewhere)

    monkeypatch.setattr(local_mod, "_make_link", lambda *_a, **_k: False)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()
    assert excinfo.value.reason == MOUNT_RELINK_FAILED


def test_unlink_failure_raises(sandbox: LocalSandbox, data_dir: Path, monkeypatch) -> None:
    """坏链接摘不掉（被占用/权限不足）→ 抛错，不能降级成"文件工具能读就行"。"""
    elsewhere = data_dir / "elsewhere-locked"
    _link(sandbox.root / "workspace", elsewhere)

    monkeypatch.setattr(local_mod, "_unlink_mount", lambda *_a, **_k: False)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()
    assert excinfo.value.reason == MOUNT_UNLINK_FAILED


def test_first_creation_failure_raises(sandbox: LocalSandbox, monkeypatch) -> None:
    """首次创建就失败 → 同样抛错。

    "没有既有状态被破坏"不构成降级理由：此时 shell 侧根本没有 `workspace`，
    文件工具却已经能读写 `workspace-volumes`，两者从第一秒起就是分裂的。
    """
    mount = sandbox.root / "workspace"
    assert not _is_link(mount) and not mount.exists()

    monkeypatch.setattr(local_mod, "_make_link", lambda *_a, **_k: False)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()
    assert excinfo.value.reason == MOUNT_CREATE_FAILED


def test_sandbox_open_fails_when_skills_mount_cannot_be_built(
    data_dir: Path, monkeypatch
) -> None:
    """挂载建不出来时 `open()` 直接失败，而不是交出一个半可用的沙箱。

    skills 挂载在 `open()` 里就建立，所以这里也是"整个沙箱是否可用"的门槛。
    """
    monkeypatch.setattr(local_mod, "_make_link", lambda *_a, **_k: False)

    with pytest.raises(SandboxMountError) as excinfo:
        LocalSandbox.open("user-1", "session-broken")
    assert excinfo.value.reason == MOUNT_CREATE_FAILED


# ---------------------------------------------------------------------------
# 被实体目录占位
# ---------------------------------------------------------------------------


def test_empty_directory_placeholder_is_replaced(sandbox: LocalSandbox) -> None:
    """空实体目录（无数据）→ 删掉占位、建联接。"""
    mount = sandbox.root / "workspace"
    assert not _is_link(mount)
    mount.mkdir(parents=True, exist_ok=True)

    root = sandbox._ensure_workspace_mount()

    assert _is_link(mount), "空占位目录没有被替换成联接"
    assert _same_dir(_link_target(mount), root)


def test_occupied_directory_raises_and_keeps_files(sandbox: LocalSandbox, data_dir: Path) -> None:
    """非空实体目录占位（旧版本遗留 / 用户手建）→ 报错，且不删里面任何东西。

    这里刻意**不**自动迁移：里面可能是用户真实文件，也可能是旧版按工作区联接的
    布局，两种都只能由人判断。自动搬走的代价是某天搬错一次。
    """
    mount = sandbox.root / "workspace"
    assert not _is_link(mount)
    (mount / "legacy-ws").mkdir(parents=True)
    keep = mount / "legacy-ws" / "note.txt"
    keep.write_text("legacy", encoding="utf-8")

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()

    assert excinfo.value.reason == MOUNT_OCCUPIED
    assert keep.read_text(encoding="utf-8") == "legacy", "报错时删掉了占位目录里的文件"
    assert not _is_link(mount), "报错路径下不该出现半成品联接"


def test_file_placeholder_raises_occupied(sandbox: LocalSandbox) -> None:
    """同名**文件**占位：同样不能删，报 `occupied` 并说清是文件而不是目录。"""
    mount = sandbox.root / "workspace"
    mount.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()

    assert excinfo.value.reason == MOUNT_OCCUPIED
    assert "同名文件" in str(excinfo.value)
    assert mount.read_text(encoding="utf-8") == "not a directory"


def test_rmdir_permission_error_is_not_reported_as_occupied(
    sandbox: LocalSandbox, monkeypatch
) -> None:
    """`rmdir` 失败 ≠ 目录非空：权限/占用必须报 `prepare_failed`，否则排查方向全错。"""
    mount = sandbox.root / "workspace"
    mount.mkdir(parents=True)
    real_rmdir = Path.rmdir

    def fake_rmdir(self, *args, **kwargs):
        if self == mount:
            raise PermissionError(13, "Access is denied")
        return real_rmdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", fake_rmdir)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()

    assert excinfo.value.reason == MOUNT_PREPARE_FAILED
    assert "非空" not in str(excinfo.value).replace("「目录非空」", "")


def test_concurrently_deleted_placeholder_still_links(
    sandbox: LocalSandbox, monkeypatch
) -> None:
    """检查与删除之间占位目录被别人删掉 → 不是错误，继续建链接。"""
    mount = sandbox.root / "workspace"
    mount.mkdir(parents=True)
    real_rmdir = Path.rmdir

    def racing_rmdir(self, *args, **kwargs):
        if self == mount:
            real_rmdir(self)  # 另一个进程先删掉了，我们才收到 not-found
            raise FileNotFoundError(2, "No such file or directory")
        return real_rmdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    root = sandbox._ensure_workspace_mount()

    assert _is_link(mount), "并发删除后应当正常建好联接"
    assert _same_dir(_link_target(mount), root)


def test_link_created_at_wrong_target_is_rejected(
    sandbox: LocalSandbox, data_dir: Path, monkeypatch
) -> None:
    """`_make_link` 返回成功但落点不对 → `verify_failed`，不能当成功返回。

    这是"返回即承诺"的失败测试：只看 `_make_link` 的返回值，函数就会把
    "建好了但指错"当成健康状态交出去。
    """
    decoy = data_dir / "decoy"
    decoy.mkdir()
    real_make_link = local_mod._make_link

    def wrong_link(mount: Path, _target: Path) -> bool:
        return real_make_link(mount, decoy)  # 真的建了，只是指错

    monkeypatch.setattr(local_mod, "_make_link", wrong_link)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_workspace_mount()

    assert excinfo.value.reason == MOUNT_VERIFY_FAILED
    assert decoy.is_dir(), "验证失败时不该删掉链接指向的目标"


# ---------------------------------------------------------------------------
# skills 是同一个缺陷的孪生位置：失败语义必须逐条相同
# ---------------------------------------------------------------------------


def test_skills_mount_wrong_target_is_rebuilt(sandbox: LocalSandbox, data_dir: Path) -> None:
    elsewhere = data_dir / "not-skills"
    mount = sandbox.root / "skills"
    _link(mount, elsewhere)

    root = sandbox._ensure_skills_mount()

    assert _same_dir(root, data_dir / "skills")
    assert not _same_dir(_link_target(mount), elsewhere), "skills 坏挂载被静默复用了"
    assert elsewhere.is_dir(), "重建 skills 挂载时删掉了目标目录"


def test_skills_broken_mount_is_repaired(sandbox: LocalSandbox, data_dir: Path) -> None:
    """skills 联接目标被删 → 与 workspace 一样重建，而不是把空目录当成内容。"""
    gone = data_dir / "gone-skills"
    mount = sandbox.root / "skills"
    _link(mount, gone)
    os.rmdir(gone)
    assert _is_link(mount) and not mount.exists()

    root = sandbox._ensure_skills_mount()

    assert _same_dir(root, (data_dir / "skills").resolve())
    assert mount.is_dir(), "重建后 shell 应当能进入 skills"


def test_skills_mount_failure_raises(sandbox: LocalSandbox, monkeypatch) -> None:
    """skills 建不出来 → 抛错（与 workspace 同语义，不降级）。"""
    _remove_link(sandbox.root / "skills")
    monkeypatch.setattr(local_mod, "_make_link", lambda *_a, **_k: False)

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_skills_mount()
    assert excinfo.value.reason == MOUNT_CREATE_FAILED


def test_skills_occupied_directory_raises(sandbox: LocalSandbox) -> None:
    """skills 被非空实体目录占位 → 报错，不删里面的文件。"""
    mount = sandbox.root / "skills"
    _remove_link(mount)
    (mount / "mine").mkdir(parents=True)
    keep = mount / "mine" / "SKILL.md"
    keep.write_text("user skill", encoding="utf-8")

    with pytest.raises(SandboxMountError) as excinfo:
        sandbox._ensure_skills_mount()

    assert excinfo.value.reason == MOUNT_OCCUPIED
    assert keep.read_text(encoding="utf-8") == "user skill"


# ---------------------------------------------------------------------------
# `_link_target` 的路径形状：必须验证**最终返回的 Path**，不是中间字符串
# ---------------------------------------------------------------------------

# `os.readlink` 在 Windows 上可能的四种返回形态（本机实测 `is_absolute()` 均为 True）。
READLINK_FORMS = [
    ("\\\\?\\C:\\data\\ws", "C:\\data\\ws"),
    ("\\\\?\\UNC\\server\\share\\ws", "\\\\server\\share\\ws"),
    ("\\\\?\\Volume{0123-4567}\\ws", "\\\\?\\Volume{0123-4567}\\ws"),
    ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\ws", "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\ws"),
]


@pytest.mark.parametrize("raw,expected", READLINK_FORMS)
def test_link_target_text_shapes(raw: str, expected: str) -> None:
    """盘符剥前缀、UNC 还原、Volume GUID / GLOBALROOT 原样保留。

    `Volume{...}` 是**设备名**，不是相对路径片段：把扩展前缀剥掉之后，
    `Path("Volume{...}\\ws").is_absolute()` 为 False，会被当成相对路径拼到
    挂载点父目录下——一条凭空编造的路径，于是这种挂载每次检查都被判"指错"。
    """
    assert _link_target_text(raw) == expected


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="四种形态都是 Windows 扩展路径，判绝对用的是 `Path.is_absolute()`（随 OS 变）；"
    "POSIX 上反斜杠不是分隔符、`C:\\...` 也不算绝对路径，断言会假失败",
)
@pytest.mark.parametrize("raw,expected", READLINK_FORMS)
def test_link_target_never_invents_relative_path(
    tmp_path: Path, monkeypatch, raw: str, expected: str
) -> None:
    """`_link_target()` 的返回值必须保持绝对；相对目标才允许拼链接父目录。

    这一条是上一轮漏掉的：只测字符串变换看不出问题，因为出错的是
    "判绝对 → 拼接父目录"这一步。
    """
    mount = tmp_path / "workspace"
    monkeypatch.setattr(local_mod.os, "readlink", lambda _p: raw)

    target = _link_target(mount)

    assert target is not None
    assert target.is_absolute(), f"读回的目标被解析成了相对路径: {target}"
    assert str(target) == expected
    # 关键：不能出现「拼到挂载点父目录下」的伪造路径（Volume GUID 那条的老毛病）
    assert not str(target).startswith(str(mount.parent))


def test_link_target_resolves_posix_relative_against_link_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """相对符号链接按**链接所在目录**解析（POSIX 语义），不是进程 cwd。

    断言落在链接父目录之下：如果实现用 cwd 解析，结果会跑到仓库目录里，
    与这里的 `tmp_path` 无关。
    """
    mount = tmp_path / "sandbox" / "workspace"
    monkeypatch.setattr(local_mod.os, "readlink", lambda _p: "../shared/ws")

    target = _link_target(mount)

    assert target == mount.parent / "../shared/ws"
    assert str(target).startswith(str(mount.parent))


def test_link_target_on_real_junction(sandbox: LocalSandbox, data_dir: Path) -> None:
    """端到端：真实 junction 的读回结果与预期目标等价（8.3 短名由 `_same_dir` 吸收）。"""
    target = data_dir / "real-target"
    mount = sandbox.root / "workspace"
    _link(mount, target)

    read_back = _link_target(mount)

    assert read_back is not None and read_back.is_absolute()
    assert _same_dir(read_back, target)
