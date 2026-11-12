"""阶段 5 验收测试：skills 目录、sandbox 路径校验与工具定义。"""

from __future__ import annotations

from edith_backend.internal.sandbox.tools import (
    SandboxToolSet,
    _complete_workspace_path,
)
from edith_backend.internal.sandbox.workspace import relative_path, workspace_path
from edith_backend.internal.skills.catalog import builtin_skill_dirs, load_skill


def test_builtin_skills_are_loadable() -> None:
    """内置 Skill 必须能被单独解析——它们是首次启动铺进数据目录的唯一来源。"""
    dirs = builtin_skill_dirs()
    names = {d.name for d in dirs}
    assert {"current-time", "skill-creator"} <= names
    for path in dirs:
        skill = load_skill(path)
        assert skill.name
        assert skill.description
        assert skill.body


def test_workspace_path_rejects_absolute() -> None:
    try:
        workspace_path("/etc/passwd", allow_root=False)
        assert False, "should reject absolute path"
    except ValueError:
        pass


def test_workspace_path_rejects_parent() -> None:
    try:
        workspace_path("../secret", allow_root=False)
        assert False, "should reject parent traversal"
    except ValueError:
        pass


def test_workspace_path_normalizes_backslash() -> None:
    # Windows 分隔符按 POSIX 处理，避免绕过校验
    try:
        workspace_path("..\\secret", allow_root=False)
        assert False, "should reject backslash traversal"
    except ValueError:
        pass


def test_workspace_path_joins_root() -> None:
    assert workspace_path("work/file.txt", allow_root=False) == "/home/user/work/file.txt"
    assert workspace_path("", allow_root=True) == "/home/user"


def test_relative_path_roundtrip() -> None:
    assert relative_path("/home/user/work/x.txt") == "work/x.txt"
    assert relative_path("/home/user") == "."


# 本次新增：工作区路径自动补全（Web 场景模型无当前目录概念，工具层补全到 workspace/{id}/）
def test_complete_workspace_path() -> None:
    assert _complete_workspace_path("", "abc123") == "workspace/abc123"
    assert _complete_workspace_path(".", "abc123") == "workspace/abc123"
    assert _complete_workspace_path("src/main.py", "abc123") == "workspace/abc123/src/main.py"
    # 显式顶层目录保持原样
    assert _complete_workspace_path("uploads/a.txt", "abc123") == "uploads/a.txt"
    assert _complete_workspace_path("work/tmp", "abc123") == "work/tmp"
    assert _complete_workspace_path("artifacts/out.md", "abc123") == "artifacts/out.md"
    assert _complete_workspace_path("skills/x", "abc123") == "skills/x"
    # 无 workspace_id 时原样返回
    assert _complete_workspace_path("", "") == ""
    assert _complete_workspace_path("src/main.py", "") == "src/main.py"


def test_tool_definitions_count() -> None:
    toolset = SandboxToolSet(None)  # service 仅在执行时用到
    defs = toolset.tool_definitions()
    assert len(defs) == 13
    names = {d["function"]["name"] for d in defs}
    assert names == {
        "sandbox_list_files",
        "sandbox_read_file",
        "sandbox_write_file",
        "sandbox_make_directory",
        "sandbox_move_path",
        "sandbox_delete_path",
        "sandbox_run_command",
        "sandbox_start_process",
        "sandbox_list_processes",
        "sandbox_kill_process",
        "git",
        "git_push",
        "git_remote_branches",
    }


def test_skills_http_lists_single_layer(client) -> None:
    """接口只回答「磁盘上有什么」。桌面端没有作用域，所以没有 system/custom 之分。"""
    response = client.get("/internal/skills")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"ok", "missing", "partial"}
    names = {s["name"] for s in data["skills"]}
    # 测试环境的数据目录是 tmp，没有 seed 过 → 空列表也是正确结果；
    # 这条断言的目的是钉住「返回形状」，不是钉住内容。
    if data["status"] == "ok":
        assert all(s["path"].startswith("skills/") for s in data["skills"])
    assert isinstance(names, set)
