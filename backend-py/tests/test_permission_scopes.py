"""档位只有一个控制点：用户设置。

这一组盯的不是功能，而是**控制点的唯一性**：

- 生效档位只由用户设置决定；定时任务的快照是授权记录，只能压低、不能抬高；
- 会话与工作区不再持有档位——列、HTTP 路由、响应字段都已经撤掉。

为什么值得单独立一组：把层级加回来只需要加一个字段，加回来之后两边各存一份
档位、谁也不报错，直到某天用户发现「我在设置里改成只读了，这个对话还是照样写」。
那条链路上没有任何异常可以观测，只有这张测试能挡住。

（历史：曾经有过工作区级 / 会话级上限，已回退。
见 docs/archive/permissions-p3-scopes.md。）
"""

from __future__ import annotations

import asyncio

import aiosqlite

from edith_backend.internal.permissions.chain import resolve_user_level
from edith_backend.internal.permissions.policy import resolve_chain
from edith_backend.internal.permissions.types import PermissionLevel
from edith_backend.internal.session import create_schema as create_session_schema
from edith_backend.internal.userconfig.schema import create_schema as create_user_schema
from edith_backend.internal.userconfig.settings import Settings
from edith_backend.internal.workspace import create_schema as create_workspace_schema

READ_ONLY = PermissionLevel.READ_ONLY
APPROVAL = PermissionLevel.APPROVAL
FULL = PermissionLevel.FULL


async def _settings() -> tuple[aiosqlite.Connection, Settings]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_user_schema(db)
    return db, Settings(db)


# ── 一、解析：用户设置是唯一来源，快照只能压低 ────────────────────────


def test_default_is_full_when_nothing_is_set() -> None:
    """两个来源都空 = 默认档。这是全新用户的实际状态。"""
    resolution = resolve_chain()
    assert resolution.level == FULL
    assert resolution.binding == ""
    assert resolution.invalid == ()


def test_user_level_is_the_effective_level() -> None:
    resolution = resolve_chain(user="read_only")
    assert resolution.level == READ_ONLY
    assert resolution.binding == "user", "压下来的是用户设置，界面要能说出来"


def test_cron_snapshot_only_tightens() -> None:
    """快照 = 创建任务时用户的授权上限，永远不能超过此刻的设置。

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


def test_invalid_user_value_fails_closed() -> None:
    resolution = resolve_chain(user="god_mode")
    assert resolution.level == READ_ONLY, "脏数据不能变成提权"
    assert resolution.invalid == ("user",)


def test_user_level_reader_failure_falls_back_to_read_only() -> None:
    """读失败 ≠ 没设置。数据库抖一下不能把只读变成完全。"""

    async def broken(_: str) -> object:
        raise RuntimeError("db is locked")

    resolution = asyncio.run(resolve_user_level(broken, "u1"))
    assert resolution.level == READ_ONLY
    assert resolution.errors == ("user",)


# ── 二、取数：真的读用户设置，且把 ceiling 传下去 ─────────────────────


def test_resolve_user_level_reads_the_user_setting() -> None:
    async def body() -> None:
        db, settings = await _settings()
        try:
            await settings.save_permission_level("u1", "approval")
            resolution = await resolve_user_level(
                settings.load_permission_level, "u1"
            )
            assert resolution.level == APPROVAL
            assert resolution.binding == "user"
        finally:
            await db.close()

    asyncio.run(body())


def test_resolve_user_level_keeps_ceiling() -> None:
    async def body() -> None:
        db, settings = await _settings()
        try:
            await settings.save_permission_level("u1", "full")
            resolution = await resolve_user_level(
                settings.load_permission_level, "u1", ceiling="read_only"
            )
            assert resolution.level == READ_ONLY
            assert resolution.binding == "ceiling"
        finally:
            await db.close()

    asyncio.run(body())


# ── 三、契约：会话与工作区不再持有档位 ───────────────────────────────


def test_new_database_has_no_level_columns() -> None:
    """新库不该再有这两列：留着就会有人以为它还在生效。"""

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

            assert "permission_level" not in await columns("session_meta")
            assert "permission_level" not in await columns("workspaces")
        finally:
            await db.close()

    asyncio.run(body())


def test_removed_level_routes_are_gone(client) -> None:
    """两个写入接口都必须消失：留着就多一个「谁能改档位」的答案。"""
    conversation = client.put(
        "/internal/conversations/s1/permission-level",
        json={"userId": "u1", "permissionLevel": "read_only"},
    )
    assert conversation.status_code == 404

    workspace = client.put(
        "/internal/workspaces/w1/permission-level",
        json={"userId": "u1", "permissionLevel": "read_only"},
    )
    assert workspace.status_code == 404


def test_workspace_response_has_no_permission_level(client) -> None:
    user = "test-scope-workspace"
    created = client.post("/internal/workspaces", json={"userId": user, "name": "proj"})
    assert created.status_code == 200, created.text
    assert "permissionLevel" not in created.json()

    listed = client.get("/internal/workspaces", params={"userId": user})
    assert listed.status_code == 200, listed.text
    for item in listed.json()["workspaces"]:
        assert "permissionLevel" not in item
