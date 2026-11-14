"""档位有两个作用域，一条收紧规则。

这一组盯的不是功能，而是**作用域之间的语义边界**。四个不变量，缺一个就会
出现「界面上的设置不生效」或「设置生效得过头」：

1. 项目级覆盖用户级（不是取最严）——否则项目里的选择形同摆设；
2. 没有工作区的对话回落用户级——否则普通对话全变成默认档；
3. 定时任务快照只能压低——否则用户降权后历史任务成了例外；
4. **只有这两级**，会话不持有档位——再加一层就没人说得清哪一层在生效。

为什么值得单独立一组：把某一级删掉或改回 min，只需要改一行，改完没有任何
异常可以观测，直到某天用户发现「我在项目里选了完全，它还是只读」。
`binding` 是唯一能观测到的痕迹，所以每个用例都断言它。

（历史：P3 做过「用户 + 工作区 + 会话」三层上限，全部只能收紧，随后整体回退；
现在只有用户级与项目级，且项目级是覆盖。见 docs/permissions-p4-project.md。）
"""

from __future__ import annotations

import asyncio

import aiosqlite

from edith_backend.internal.permissions.chain import resolve_effective_level
from edith_backend.internal.permissions.policy import resolve_chain
from edith_backend.internal.permissions.types import PermissionLevel
from edith_backend.internal.session import create_schema as create_session_schema
from edith_backend.internal.userconfig.schema import create_schema as create_user_schema
from edith_backend.internal.userconfig.settings import Settings
from edith_backend.internal.workspace import create_schema as create_workspace_schema
from edith_backend.internal.workspace.store import WorkspaceStore

READ_ONLY = PermissionLevel.READ_ONLY
APPROVAL = PermissionLevel.APPROVAL
FULL = PermissionLevel.FULL


async def _settings() -> tuple[aiosqlite.Connection, Settings]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_user_schema(db)
    return db, Settings(db)


async def _workspaces() -> tuple[aiosqlite.Connection, WorkspaceStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_workspace_schema(db)
    return db, WorkspaceStore(db)


# ── 一、解析：项目覆盖用户，快照只能压低 ─────────────────────────────


def test_default_is_full_when_nothing_is_set() -> None:
    """三处都空 = 默认档。这是全新用户的实际状态。"""
    resolution = resolve_chain()
    assert resolution.level == FULL
    assert resolution.binding == "user", "没有项目时，生效值来自用户级（哪怕是默认值）"
    assert resolution.invalid == ()


def test_user_level_is_the_effective_level() -> None:
    resolution = resolve_chain(user="read_only")
    assert resolution.level == READ_ONLY
    assert resolution.binding == "user", "界面要能说出来这一档是谁定的"


def test_project_overrides_user_in_both_directions() -> None:
    """**覆盖**，不是取最严——两个方向都要成立。

    只测「项目更严」的话，min 也能通过；真正区分两种语义的是宽松方向：
    用户全局只读、这个项目设完全 → 完全。这正是上一版做成「上限」时做不到的事。
    """
    tightened = resolve_chain(user="full", workspace="read_only")
    assert tightened.level == READ_ONLY
    assert tightened.binding == "workspace"

    loosened = resolve_chain(user="read_only", workspace="full")
    assert loosened.level == FULL, "项目级放宽必须生效，否则项目设置只是摆设"
    assert loosened.binding == "workspace"


def test_empty_project_level_falls_back_to_user() -> None:
    """空串 = 没设，不是「设成了最严」。"""
    resolution = resolve_chain(user="approval", workspace="")
    assert resolution.level == APPROVAL
    assert resolution.binding == "user"


def test_invalid_values_fail_closed_per_source() -> None:
    dirty_user = resolve_chain(user="god_mode")
    assert dirty_user.level == READ_ONLY, "脏数据不能变成提权"
    assert dirty_user.invalid == ("user",)

    dirty_project = resolve_chain(user="full", workspace="god_mode")
    assert dirty_project.level == READ_ONLY, "项目级脏值同样按最严，不能回落用户级"
    assert dirty_project.invalid == ("workspace",)


def test_cron_snapshot_only_tightens() -> None:
    """ceiling = 创建定时任务时记下的授权，永远不能超过当时的上限。

    两个方向都要有：
    - 用户降权后，历史任务跟着降（否则自动执行成了例外）；
    - 用户提权后，旧快照仍压着（宁可少做，不可多做）。
    """
    downgraded = resolve_chain(user="read_only", ceiling="full")
    assert downgraded.level == READ_ONLY
    assert downgraded.binding == "user"

    capped = resolve_chain(user="full", ceiling="approval")
    assert capped.level == APPROVAL
    assert capped.binding == "ceiling"


def test_cron_snapshot_caps_project_level_too() -> None:
    """快照对项目级同样生效：项目设完全，但这次自动执行的授权只有审批。"""
    resolution = resolve_chain(user="read_only", workspace="full", ceiling="approval")
    assert resolution.level == APPROVAL
    assert resolution.binding == "ceiling"


def test_project_level_survives_global_downgrade() -> None:
    """用户把全局调低，不会顺带改写已经显式设过的项目档位。

    这是覆盖语义的固有结果，刻意保留：项目级是用户**更具体**的一次授权。
    代价是「改全局」不等于「全盘收紧」，所以界面上必须显示项目自己的值
    （`WorkspaceResponse.permissionLevel`），不能让用户以为全局一改就都变了。
    """
    resolution = resolve_chain(user="read_only", workspace="full")
    assert resolution.level == FULL


# ── 二、取数：真的读两级设置，读失败按最严 ───────────────────────────


def test_reader_reads_both_levels() -> None:
    async def body() -> None:
        user_db, settings = await _settings()
        ws_db, workspaces = await _workspaces()
        try:
            await settings.save_permission_level("u1", "read_only")
            workspace_id = await workspaces.create("u1", "proj")
            await workspaces.set_permission_level("u1", workspace_id, "full")

            resolution = await resolve_effective_level(
                settings.load_permission_level,
                "u1",
                load_workspace=workspaces.get_permission_level,
                workspace_id=workspace_id,
            )
            assert resolution.level == FULL
            assert resolution.binding == "workspace"
        finally:
            await ws_db.close()
            await user_db.close()

    asyncio.run(body())


def test_no_workspace_context_skips_project_level() -> None:
    """没有工作区（普通对话、定时任务）时不读项目级，回落用户级。"""

    async def body() -> None:
        user_db, settings = await _settings()
        try:
            await settings.save_permission_level("u1", "approval")
            resolution = await resolve_effective_level(
                settings.load_permission_level, "u1", workspace_id=None
            )
            assert resolution.level == APPROVAL
            assert resolution.binding == "user"
        finally:
            await user_db.close()

    asyncio.run(body())


def test_missing_workspace_falls_back_instead_of_locking() -> None:
    """工作区已被删（查不到行）→ 空的档位 → 回落用户级，不是只读。

    删掉一个项目不该把它变成一个永久的只读陷阱：用户此刻的意图仍由用户级表达。
    与「读失败」区分开——那条路径必须是只读。
    """

    async def body() -> None:
        user_db, settings = await _settings()
        ws_db, workspaces = await _workspaces()
        try:
            await settings.save_permission_level("u1", "full")
            resolution = await resolve_effective_level(
                settings.load_permission_level,
                "u1",
                load_workspace=workspaces.get_permission_level,
                workspace_id="does-not-exist",
            )
            assert resolution.level == FULL
            assert resolution.errors == ()
        finally:
            await ws_db.close()
            await user_db.close()

    asyncio.run(body())


def test_user_reader_failure_falls_back_to_read_only() -> None:
    """读失败 ≠ 没设置。数据库抖一下不能把只读变成完全。"""

    async def broken(_: str) -> object:
        raise RuntimeError("db is locked")

    resolution = asyncio.run(resolve_effective_level(broken, "u1"))
    assert resolution.level == READ_ONLY
    assert resolution.errors == ("user",)


def test_project_reader_failure_falls_back_to_read_only() -> None:
    """项目级读失败也不能回落用户级：那等于「数据一抖就放宽」。"""

    async def user_full(_: str) -> object:
        return "full"

    async def broken(_: str, __: str) -> object:
        raise RuntimeError("db is locked")

    resolution = asyncio.run(
        resolve_effective_level(
            user_full,
            "u1",
            load_workspace=broken,
            workspace_id="w1",
        )
    )
    assert resolution.level == READ_ONLY
    assert resolution.errors == ("workspace",)


# ── 三、契约：两级就是两级，会话不许有第三份 ─────────────────────────


def test_new_database_has_project_column_but_no_session_column() -> None:
    """项目级有列，会话级没有。"""

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        try:
            await create_session_schema(db)
            await create_workspace_schema(db)

            async def columns(table: str) -> list[str]:
                cursor = await db.execute(f"PRAGMA table_info({table})")
                rows = await cursor.fetchall()
                await cursor.close()
                return [row[1] for row in rows]

            assert "permission_level" in await columns("workspaces")
            assert "permission_level" not in await columns("session_meta"), (
                "会话级档位不该复活：第三份档位会让「哪一层在生效」无法回答"
            )
        finally:
            await db.close()

    asyncio.run(body())


def test_conversation_level_route_stays_removed(client) -> None:
    """会话级写入接口必须一直是 404。"""
    response = client.put(
        "/internal/conversations/s1/permission-level",
        json={"userId": "u1", "permissionLevel": "read_only"},
    )
    assert response.status_code == 404


def test_project_level_route_round_trip(client) -> None:
    """项目级：写入 → 读回 → 清除。三档都能存，空串能清。"""
    user = "test-scope-project"
    created = client.post("/internal/workspaces", json={"userId": user, "name": "proj"})
    assert created.status_code == 200, created.text
    workspace_id = created.json()["workspaceId"]
    assert created.json()["permissionLevel"] == ""

    for level in ("read_only", "approval", "full"):
        saved = client.put(
            f"/internal/workspaces/{workspace_id}/permission-level",
            json={"userId": user, "permissionLevel": level},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["permissionLevel"] == level

        listed = client.get("/internal/workspaces", params={"userId": user})
        matched = [
            w for w in listed.json()["workspaces"] if w["workspaceId"] == workspace_id
        ]
        assert matched and matched[0]["permissionLevel"] == level

    cleared = client.put(
        f"/internal/workspaces/{workspace_id}/permission-level",
        json={"userId": user, "permissionLevel": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["permissionLevel"] == ""


def test_project_level_route_rejects_invalid_value(client) -> None:
    """非法值 400，不静默改成最严档——否则用户会以为没点对，然后反复点。"""
    user = "test-scope-project-invalid"
    created = client.post("/internal/workspaces", json={"userId": user, "name": "proj"})
    workspace_id = created.json()["workspaceId"]

    response = client.put(
        f"/internal/workspaces/{workspace_id}/permission-level",
        json={"userId": user, "permissionLevel": "god_mode"},
    )
    assert response.status_code == 400


def test_project_level_route_404_for_unknown_workspace(client) -> None:
    response = client.put(
        "/internal/workspaces/nope/permission-level",
        json={"userId": "u1", "permissionLevel": "full"},
    )
    assert response.status_code == 404


# ── 六、全局档位专用写入路由（聊天页档位菜单在没有工作区时用它）─────


def test_user_level_route_changes_only_the_level(client) -> None:
    """改全局档位不许顺手冲掉别的设置。

    这条路径存在的原因就是「只为改一个下拉，不该回写整份设置」——所以用例也
    必须盯住这一点：先写一套人格/默认模型，改档位之后它们必须原封不动。
    如果哪天有人把它实现成「PUT 整份设置」，这个断言会立刻红。
    """
    user = "test-scope-user-level"
    saved = client.put(
        "/internal/user-settings",
        json={
            "userId": user,
            "personality": "简洁",
            "defaultModelId": "qwen3.7-flash",
            "timezone": "Asia/Shanghai",
            "permissionLevel": "approval",
            "providers": [],
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.put(
        "/internal/user-settings/permission-level",
        json={"userId": user, "permissionLevel": "read_only"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["permissionLevel"] == "read_only"

    settings = client.get("/internal/user-settings", params={"userId": user}).json()
    assert settings["permissionLevel"] == "read_only"
    assert settings["personality"] == "简洁"
    assert settings["defaultModelId"] == "qwen3.7-flash"
    assert settings["timezone"] == "Asia/Shanghai"


def test_user_level_route_rejects_invalid_value(client) -> None:
    response = client.put(
        "/internal/user-settings/permission-level",
        json={"userId": "test-scope-user-invalid", "permissionLevel": "god_mode"},
    )
    assert response.status_code == 400

    # 非法值没写进去：读回来仍是默认档，不是「最严档」也不是那个非法值
    settings = client.get(
        "/internal/user-settings", params={"userId": "test-scope-user-invalid"}
    ).json()
    assert settings["permissionLevel"] == "full"


def test_user_level_route_requires_user_id(client) -> None:
    response = client.put(
        "/internal/user-settings/permission-level",
        json={"userId": "   ", "permissionLevel": "full"},
    )
    assert response.status_code == 400
