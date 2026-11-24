"""P0-SEC-1：文件面板与 Agent 文件工具**同一套**敏感路径 / 越界语义。

改造前的事实：`check_path()` 只被 `sandbox/tools.py` 调用，面板直读本地磁盘
那条链完全没判——同一个 `.env`，Agent 读不到、面板能预览；再加上目录联接，
逻辑上干净的路径还能落到绑定根之外。这里把两侧的语义钉在一起。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from edith_backend.internal.permissions.paths import PathRejected, guard_real_path

USER = "panel-guard-user"


def _create_workspace(
    client: TestClient, name: str, session: str, root: Path | None = None
) -> str:
    payload: dict[str, object] = {"userId": USER, "name": name, "sessionId": session}
    if root is not None:
        payload["localRoot"] = str(root)
    created = client.post("/internal/workspaces", json=payload)
    assert created.status_code == 200, created.text
    return created.json()["workspaceId"]


def _read(client: TestClient, session: str, path: str):
    return client.get(
        "/internal/sandbox/files/content",
        params={"userId": USER, "sessionId": session, "path": path},
    )


def _list(client: TestClient, session: str, path: str):
    return client.get(
        "/internal/sandbox/files",
        params={"userId": USER, "sessionId": session, "path": path},
    )


# ---- 面板 HTTP 契约 ----


def test_panel_refuses_sensitive_file(client: TestClient, tmp_path: Path) -> None:
    bound = tmp_path / "proj-env"
    bound.mkdir()
    (bound / ".env").write_text("OPENAI_API_KEY=sk-real", encoding="utf-8")
    (bound / "app.py").write_text("print(1)", encoding="utf-8")
    workspace_id = _create_workspace(client, "guard-env", "sess-guard-env", bound)

    response = _read(client, "sess-guard-env", f"workspace/{workspace_id}/.env")
    assert response.status_code == 403, response.text
    assert response.json()["type"] == "path_forbidden"


def test_panel_listing_hides_sensitive_entries(client: TestClient, tmp_path: Path) -> None:
    """列表与读取同一套语义：点开会被拒的条目也不该出现在枚举结果里。"""
    bound = tmp_path / "proj-list"
    bound.mkdir()
    (bound / "app.py").write_text("print(1)", encoding="utf-8")
    (bound / ".env.local").write_text("X=1", encoding="utf-8")
    (bound / "id_rsa").write_text("key", encoding="utf-8")
    workspace_id = _create_workspace(client, "guard-list", "sess-guard-list", bound)

    response = _list(client, "sess-guard-list", f"workspace/{workspace_id}")
    assert response.status_code == 200, response.text
    names = [entry["name"] for entry in response.json()["entries"]]
    assert names == ["app.py"]


def test_panel_refuses_sensitive_path_without_local_binding(
    client: TestClient, tmp_path: Path
) -> None:
    """未绑定工作区（沙箱分支）同样受约束：判定发生在选路之前。"""
    workspace_id = _create_workspace(client, "guard-unbound", "sess-guard-unbound")
    response = _read(client, "sess-guard-unbound", f"workspace/{workspace_id}/.ssh/id_rsa")
    assert response.status_code == 403, response.text


def test_panel_refuses_uploading_sensitive_name(client: TestClient, tmp_path: Path) -> None:
    bound = tmp_path / "proj-upload"
    bound.mkdir()
    workspace_id = _create_workspace(client, "guard-upload", "sess-guard-upload", bound)

    response = client.post(
        "/internal/sandbox/files/upload",
        params={"userId": USER, "sessionId": "sess-guard-upload"},
        files={"file": (".env", b"SECRET=1", "text/plain")},
    )
    assert response.status_code == 403, response.text
    assert not (bound / ".env").exists()


def test_panel_still_reads_normal_file(client: TestClient, tmp_path: Path) -> None:
    bound = tmp_path / "proj-ok"
    bound.mkdir()
    (bound / "note.md").write_text("# hi", encoding="utf-8")
    workspace_id = _create_workspace(client, "guard-ok", "sess-guard-ok", bound)

    response = _read(client, "sess-guard-ok", f"workspace/{workspace_id}/note.md")
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "# hi"


# ---- guard_real_path（第二遍：真实落点） ----


def test_guard_allows_path_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / "a.txt"
    target.write_text("x", encoding="utf-8")
    assert guard_real_path(target, root=root)


def test_guard_blocks_sensitive_real_path(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / ".env"
    target.write_text("X=1", encoding="utf-8")
    with pytest.raises(PathRejected):
        guard_real_path(target, root=root)


def test_guard_blocks_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathRejected):
        guard_real_path(outside, root=root)


def test_guard_follows_link_out_of_root(tmp_path: Path) -> None:
    """逻辑路径干净、落点在根外：只有看真实路径才拦得住（junction / symlink）。

    Windows 上建符号链接需要开发者模式或管理员权限，建不出来就跳过——
    这条规则本身由上面两个用例覆盖，这里只验证"链接会被跟随"。
    """
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    link = root / "k"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows 上普通用户建不了符号链接；退回目录联接（工程里本来就用它挂工作区）
        from edith_backend.internal.sandbox.local import _make_link

        if not _make_link(link, outside):
            pytest.skip("当前环境既建不了符号链接也建不了目录联接")

    with pytest.raises(PathRejected):
        guard_real_path(link / "secret.txt", root=root)
