"""S3：文件面板的数据来源必须与执行链同源。

绑定态下，面板**不许**在本地读不到时回落去读 `workspace-volumes/{id}`——
那是另一份数据，用户会以为自己看的是绑定目录。这条链的消费者是 HTTP 接口，
所以这里用真正的 `TestClient` 走一遍，而不是只测纯函数。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

USER = "panel-source-user"


def _create_bound_workspace(client: TestClient, root: Path, session: str) -> str:
    created = client.post(
        "/internal/workspaces",
        json={
            "userId": USER,
            # 同一用户下工作区名唯一：用会话名区分，避免测试之间互相撞名
            "name": f"proj-{session}",
            "localRoot": str(root),
            "sessionId": session,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["workspaceId"]


def test_panel_lists_bound_directory(client: TestClient, tmp_path: Path) -> None:
    bound = tmp_path / "proj"
    bound.mkdir()
    (bound / "a.txt").write_text("hi", encoding="utf-8")
    workspace_id = _create_bound_workspace(client, bound, "sess-list")

    response = client.get(
        "/internal/sandbox/files",
        params={
            "userId": USER,
            "sessionId": "sess-list",
            "path": f"workspace/{workspace_id}",
        },
    )
    assert response.status_code == 200, response.text
    assert [entry["name"] for entry in response.json()["entries"]] == ["a.txt"]


def test_panel_409_when_bound_root_disappears(
    client: TestClient, tmp_path: Path
) -> None:
    """绑定目录被移动/删除：面板报错，**不显示旧副本**。

    改造前这里返回 200 + `workspace-volumes/{id}` 的内容。那条静默回落
    正是 P0-2 要消灭的：用户看到的是旧数据，而且没有任何提示。
    """
    bound = tmp_path / "gone-proj"
    bound.mkdir()
    (bound / "old.txt").write_text("stale", encoding="utf-8")
    workspace_id = _create_bound_workspace(client, bound, "sess-gone")

    shutil.rmtree(bound)

    response = client.get(
        "/internal/sandbox/files",
        params={
            "userId": USER,
            "sessionId": "sess-gone",
            "path": f"workspace/{workspace_id}",
        },
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"] == "workspace_unavailable"
    assert "不可用" in body["message"]


def test_panel_404_when_bound_file_missing(client: TestClient, tmp_path: Path) -> None:
    """绑定态下文件不存在就是不存在——回落到沙箱只会读到另一份数据。"""
    bound = tmp_path / "proj-404"
    bound.mkdir()
    (bound / "a.txt").write_text("hi", encoding="utf-8")
    workspace_id = _create_bound_workspace(client, bound, "sess-404")

    response = client.get(
        "/internal/sandbox/files/content",
        params={
            "userId": USER,
            "sessionId": "sess-404",
            "path": f"workspace/{workspace_id}/nope.txt",
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["type"] == "file_not_found"


def test_panel_reads_bound_file_content(client: TestClient, tmp_path: Path) -> None:
    bound = tmp_path / "proj-read"
    bound.mkdir()
    (bound / "note.md").write_text("# hello", encoding="utf-8")
    workspace_id = _create_bound_workspace(client, bound, "sess-read")

    response = client.get(
        "/internal/sandbox/files/content",
        params={
            "userId": USER,
            "sessionId": "sess-read",
            "path": f"workspace/{workspace_id}/note.md",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "# hello"
