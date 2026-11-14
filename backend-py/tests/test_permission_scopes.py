"""P3：会话级 / 工作区级档位上限。

这一组测试盯的是**一个判断**：子作用域只能收紧。
其余（存储往返、HTTP 校验、读失败降级）都是它的配套，各自独立成因。

为什么它必须是一条测试而不是一句注释：把 `min` 改成「后者胜」只需要改一个词，
改完代码照样跑、界面照样显示，只有安全边界悄悄消失了。
"""

from __future__ import annotations

import asyncio

import aiosqlite

from edith_backend.internal.permissions.chain import LevelChain
from edith_backend.internal.permissions.policy import (
    InvalidLevelValue,
    parse_level_input,
    resolve_chain,
)
from edith_backend.internal.permissions.types import PermissionLevel
from edith_backend.internal.session import create_schema as create_session_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.workspace import create_schema as create_workspace_schema
from edith_backend.internal.workspace.store import WorkspaceStore

READ_ONLY = PermissionLevel.READ_ONLY
APPROVAL = PermissionLevel.APPROVAL
FULL = PermissionLevel.FULL


# ── 一、解析链：只能收紧 ─────────────────────────────────────────────


def test_empty_chain_keeps_default_level() -> None:
    """四个来源都没设置 = 不收紧。这是绝大多数用户的实际状态。"""
    resolution = resolve_chain()
    assert resolution.level == FULL
    assert resolution.binding == ""
    assert resolution.invalid == ()


def test_session_can_tighten_but_never_loosen() -> None:
    """核心断言：会话级只能往下压，不能把用户级的只读放宽回完全。

    这是整个 P3 的立论所在。允许它放宽，用户级档位就只是一个建议，
    而"只读档"的用处恰恰是「我确认这段对话不会写东西」。
    """
    tightened = resolve_chain(user="full", session="read_only")
    assert tightened.level == READ_ONLY
    assert tightened.binding == "session"

    loosened = resolve_chain(user="read_only", session="full")
    assert loosened.level == READ_ONLY, "会话级不得放宽用户级"
    assert loosened.binding == "user"


def test_workspace_can_tighten_and_is_combined_with_session() -> None:
    """两级同时存在时取更严者，没有「谁覆盖谁」。"""
    resolution = resolve_chain(user="full", workspace="approval", session="read_only")
    assert resolution.level == READ_ONLY
    # 命中同一档时，binding 记第一个（确定性，便于界面解释）
    assert resolution.binding in ("workspace", "session")


def test_invalid_value_falls_back_to_strictest() -> None:
    """脏数据按最严档处理，并单独记下来源：改坏一个字段不能提权。"""
    resolution = resolve_chain(user="full", workspace="god_mode")
    assert resolution.level == READ_ONLY
    assert resolution.invalid == ("workspace",)


def test_ceiling_still_applies_on_top_of_scopes() -> None:
    """定时任务快照与两级上限叠加，不是互斥的。"""
    resolution = resolve_chain(
        user="full", workspace="full", session="full", ceiling="approval"
    )
    assert resolution.level == APPROVAL
    assert resolution.binding == "ceiling"


def test_sources_recorded_even_when_empty() -> None:
    """空值也要出现在 sources 里：排查时要能分清「没设置」和「设成了空」。"""
    resolution = resolve_chain(user="")
    assert resolution.sources == (
        ("user", ""),
        ("workspace", ""),
        ("session", ""),
        ("ceiling", ""),
    )


def test_binding_is_empty_when_nobody_tightens() -> None:
    """全员 equal 默认档时没有元凶，别把第一个来源记成它。"""
    assert resolve_chain(user="full", workspace="full").binding == ""


# ── 二、写入侧校验（与读侧的容错解析分工）─────────────────────────────


def test_write_side_validation_rejects_instead_of_downgrading() -> None:
    """写侧非法值必须报错：静默改成最严档会让界面与行为不一致。"""
    assert parse_level_input("") == ""
    assert parse_level_input("READ_ONLY") == "read_only"
    assert parse_level_input(" approval ") == "approval"
    try:
        parse_level_input("god_mode")
    except InvalidLevelValue as exc:
        assert exc.raw == "god_mode"
    else:  # pragma: no cover - 只有校验失效时才会走到
        raise AssertionError("非法档位必须被拒绝")


# ── 三、存储往返 ────────────────────────────────────────────────────


async def _fresh_db() -> tuple[aiosqlite.Connection, SessionStore, WorkspaceStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_session_schema(db)
    await create_workspace_schema(db)
    return db, SessionStore(db), WorkspaceStore(db)


def test_session_level_round_trip_and_clear() -> None:
    async def body() -> None:
        db, sessions, _ = await _fresh_db()
        try:
            # 未设置 = 空串，而不是「完全档」：前者中性，后者是一次授权
            assert await sessions.get_permission_level("u1", "s1") == ""

            await sessions.set_permission_level("u1", "s1", "read_only")
            assert await sessions.get_permission_level("u1", "s1") == "read_only"

            # 显式清除
            await sessions.set_permission_level("u1", "s1", "")
            assert await sessions.get_permission_level("u1", "s1") == ""
        finally:
            await db.close()

    asyncio.run(body())


def test_session_level_is_scoped_per_user_and_session() -> None:
    """两个用户的同名 session_id 不能互相影响；同一用户的另一会话也互不影响。"""

    async def body() -> None:
        db, sessions, _ = await _fresh_db()
        try:
            await sessions.set_permission_level("u1", "s1", "read_only")
            assert await sessions.get_permission_level("u2", "s1") == ""
            assert await sessions.get_permission_level("u1", "s2") == ""
        finally:
            await db.close()

    asyncio.run(body())


def test_workspace_level_round_trip_and_missing_workspace() -> None:
    async def body() -> None:
        db, _, workspaces = await _fresh_db()
        try:
            wid = await workspaces.create("u1", "proj")
            assert await workspaces.get_permission_level("u1", wid) == ""

            assert await workspaces.set_permission_level("u1", wid, "approval") is True
            assert await workspaces.get_permission_level("u1", wid) == "approval"

            # 不存在的 workspace 不插入空壳
            assert await workspaces.set_permission_level("u1", "nope", "full") is False
            # 拒绝后既没写库，也没读出一个值
            assert await workspaces.get_permission_level("u1", "nope") == ""

            # 列表与详情都带上限，前端不必为每个工作区再发一次请求
            listed = await workspaces.list("u1")
            assert [w["permissionLevel"] for w in listed] == ["approval"]
            detail = await workspaces.get("u1", wid)
            assert detail is not None and detail["permissionLevel"] == "approval"
        finally:
            await db.close()

    asyncio.run(body())


def test_legacy_tables_get_the_column_backfilled_as_unset() -> None:
    """老库补列后必须是空串（未设置），不能是 'full'。

    DEFAULT '' 而不是 DEFAULT 'full' 是刻意的：给历史数据补一个默认值，
    等于替用户做了一次授权决定，而且他永远不会知道。
    """

    async def body() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        try:
            # 造两张「P3 之前」的表
            await db.execute(
                "CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                "name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT)"
            )
            await db.execute(
                "CREATE TABLE session_meta (user_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                "archived INTEGER NOT NULL DEFAULT 0, archived_at TEXT, PRIMARY KEY (user_id, session_id))"
            )
            await db.execute(
                "INSERT INTO workspaces (workspace_id, user_id, name) VALUES ('w1', 'u1', 'old')"
            )
            await db.commit()

            await create_workspace_schema(db)
            await create_session_schema(db)

            workspaces = WorkspaceStore(db)
            sessions = SessionStore(db)
            assert await workspaces.get_permission_level("u1", "w1") == ""
            assert await sessions.get_permission_level("u1", "s1") == ""
        finally:
            await db.close()

    asyncio.run(body())


# ── 四、LevelChain：取数端 ──────────────────────────────────────────


def test_chain_folds_all_three_sources() -> None:
    async def body() -> None:
        db, sessions, workspaces = await _fresh_db()
        try:
            wid = await workspaces.create("u1", "proj")
            await workspaces.set_permission_level("u1", wid, "approval")
            await sessions.set_permission_level("u1", "s1", "read_only")
            chain = LevelChain(
                user_level=lambda _uid: _value("full"),
                workspace_level=workspaces.get_permission_level,
                session_level=sessions.get_permission_level,
            )
            resolution = await chain.resolve("u1", workspace_id=wid, session_id="s1")
            assert resolution.level == READ_ONLY
            assert resolution.sources == (
                ("user", "full"),
                ("workspace", "approval"),
                ("session", "read_only"),
                ("ceiling", ""),
            )
        finally:
            await db.close()

    asyncio.run(body())


def test_chain_skips_scope_that_does_not_apply() -> None:
    """普通对话（无工作区）不去查工作区档位：查也只会得到空串，还多一次 IO。"""

    async def body() -> None:
        calls: list[str] = []

        async def workspace_level(_uid: str, _wid: str) -> str:
            calls.append(_wid)
            return "read_only"

        chain = LevelChain(
            user_level=lambda _uid: _value("full"),
            workspace_level=workspace_level,
        )
        resolution = await chain.resolve("u1", workspace_id="")
        assert calls == []
        assert resolution.level == FULL
        await chain.resolve("u1", workspace_id="w1")
        assert calls == ["w1"]

    asyncio.run(body())


def test_chain_read_failure_degrades_to_strictest() -> None:
    """读失败 = 按只读，并单独记 errors。

    不能当「没设置」：那等于「数据库抖一下就能提权」。也不能让异常穿出去：
    那会让整轮 run 挂掉，比降级更糟。
    """

    async def body() -> None:
        async def broken(_uid: str) -> str:
            raise RuntimeError("database is locked")

        chain = LevelChain(user_level=lambda _uid: _value("full"), workspace_level=broken)
        resolution = await chain.resolve("u1", workspace_id="w1")
        assert resolution.level == READ_ONLY
        assert resolution.errors == ("workspace",)
        # errors 与 invalid 是两回事：一个是没读到，一个是值坏了
        assert resolution.invalid == ()

    asyncio.run(body())


async def _value(raw: str) -> str:
    return raw


# ── 五、HTTP 边界 ───────────────────────────────────────────────────


def test_session_level_api_round_trip(client) -> None:
    resp = client.put(
        "/internal/conversations/s-api/permission-level",
        json={"userId": "u-api", "permissionLevel": "read_only"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sessionId"] == "s-api"
    assert body["permissionLevel"] == "read_only"
    # 生效值：用户级未设置（默认完全）+ 会话只读 → 只读，元凶是 session
    assert body["effectivePermissionLevel"] == "read_only"
    assert body["permissionBinding"] == "session"

    cleared = client.put(
        "/internal/conversations/s-api/permission-level",
        json={"userId": "u-api", "permissionLevel": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["permissionLevel"] == ""
    assert cleared.json()["effectivePermissionLevel"] == "full"


def test_session_level_api_rejects_bad_value(client) -> None:
    resp = client.put(
        "/internal/conversations/s-api/permission-level",
        json={"userId": "u-api", "permissionLevel": "god_mode"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["type"] == "invalid_permission_level"


def test_session_level_api_requires_user(client) -> None:
    resp = client.put(
        "/internal/conversations/s-api/permission-level",
        json={"userId": "  ", "permissionLevel": "full"},
    )
    assert resp.status_code == 400


def test_workspace_level_api_round_trip(client) -> None:
    created = client.post(
        "/internal/workspaces", json={"userId": "u-ws-level", "name": "ws-level"}
    )
    assert created.status_code == 200, created.text
    wid = created.json()["workspaceId"]
    assert created.json()["permissionLevel"] == ""

    resp = client.put(
        f"/internal/workspaces/{wid}/permission-level",
        json={"userId": "u-ws-level", "permissionLevel": "approval"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["permissionLevel"] == "approval"

    listed = client.get("/internal/workspaces", params={"userId": "u-ws-level"})
    assert listed.status_code == 200
    assert listed.json()["workspaces"][0]["permissionLevel"] == "approval"


def test_workspace_level_api_404_for_foreign_workspace(client) -> None:
    """不属于你的工作区：404 而不是 403，避免变成「这个 id 存不存在」的探测器。"""
    resp = client.put(
        "/internal/workspaces/nope/permission-level",
        json={"userId": "u-ws-level", "permissionLevel": "full"},
    )
    assert resp.status_code == 404
