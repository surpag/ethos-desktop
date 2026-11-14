"""P2-2 审批挂起与恢复：状态机、竞态、超时、取消、无人在场。

这张文件对应 `docs/permissions-p2-approval.md` §1.2「一次挂起的全部终止路径」——
每一行至少一个用例。刻意不驱动完整 chat()（要伪造模型流，测的是 mock 而不是审批）：
真正需要断言的判定集中在 `resolve_approval`，它是 `_run_one` 里唯一的分支入口。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from edith_backend.internal.agentrun.approval import (
    ApprovalDesk,
    resolve_approval,
)
from edith_backend.internal.permissions.policy import (
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_TIMEOUT,
    REASON_APPROVAL_UNAVAILABLE,
    evaluate_call,
)
from edith_backend.internal.permissions.types import (
    APPROVAL_REQUIRED,
    PERMISSION_DENIED,
    PermissionLevel,
    kind_counts_toward_breaker,
)
from edith_backend.internal.session import create_schema as create_session_schema
from edith_backend.internal.session import approval_store
from edith_backend.internal.session.approval_store import ApprovalStore

APPROVAL = PermissionLevel.APPROVAL
FULL = PermissionLevel.FULL


async def _fresh_store() -> tuple[aiosqlite.Connection, ApprovalStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_session_schema(db)
    return db, ApprovalStore(db)


async def _create(store: ApprovalStore, **overrides) -> dict:
    params = {
        "approval_id": "a1",
        "user_id": "u1",
        "session_id": "s1",
        "request_id": "r1",
        "tool_call_id": "call_1",
        "tool_name": "sandbox_write_file",
        "capabilities": ["WRITE"],
        "arguments": '{"path": "a.md"}',
    }
    params.update(overrides)
    return await store.create(**params)


async def _wait_pending(
    store: ApprovalStore, request_id: str = "r1", timeout: float = 2.0
) -> list[dict]:
    """等到 request() 真的把待批行建出来（用轮询而不是 sleep 固定时长的猜测）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rows = await store.pending("u1", request_id=request_id)
        if rows:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError("approval row was not created in time")


def _asking_verdict():
    """一个必然落在「需要审批」上的裁决（审批档 + 写工具）。"""
    verdict = evaluate_call(APPROVAL, "sandbox_write_file", {"path": "a.md"})
    assert verdict.kind == APPROVAL_REQUIRED, verdict
    return verdict


# ── 一、存储：状态机与竞态 ────────────────────────────────────────────


def test_store_records_and_decides_once() -> None:
    """条件更新是唯一的竞态闸门：第二次决策必须失败，而不是覆盖第一次。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            await _create(store)
            assert [r["status"] for r in await store.pending("u1")] == ["pending"]
            assert await store.decide("a1", approval_store.APPROVED, by="u1") is True
            assert await store.decide("a1", approval_store.DENIED, by="u2") is False
            row = await store.get("a1")
            assert row is not None
            assert row["status"] == approval_store.APPROVED
            assert row["decided_by"] == "u1"  # 后到的决策没有改写归属
            assert await store.pending("u1") == []
        finally:
            await db.close()

    asyncio.run(body())


def test_store_rejects_duplicate_tool_call() -> None:
    """(request_id, tool_call_id) 唯一：重复插 = 有人重放了裁决，应当失败。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            await _create(store)
            with pytest.raises(aiosqlite.IntegrityError):
                await _create(store, approval_id="a2")
        finally:
            await db.close()

    asyncio.run(body())


def test_store_expires_stale_on_startup() -> None:
    """进程重启：遗留的 pending 一律过期——它们已经没有等待者了。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            await _create(store)
            await _create(store, approval_id="a2", tool_call_id="call_2")
            assert await store.expire_stale() == 2
            assert await store.pending("u1") == []
            row = await store.get("a1")
            assert row is not None
            assert row["status"] == approval_store.EXPIRED
            assert row["reason"] == "backend_restart"
        finally:
            await db.close()

    asyncio.run(body())


def test_store_close_run_scoped_to_one_run() -> None:
    """取消只收尾自己这个 run，不能顺手把别的会话的待批也关掉。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            await _create(store)
            await _create(
                store,
                approval_id="a2",
                request_id="r2",
                session_id="s2",
                tool_call_id="call_2",
            )
            assert await store.close_run("r1", approval_store.CANCELED, "canceled") == 1
            assert len(await store.pending("u1", request_id="r2")) == 1
        finally:
            await db.close()

    asyncio.run(body())


# ── 二、等待台：等待、唤醒、超时、取消 ────────────────────────────────


def test_desk_waits_then_wakes_on_approve() -> None:
    """主路径：挂起 → 用户批准 → 唤醒 → 事件与库都到终态。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=5.0)
            events: list = []
            task = asyncio.create_task(
                desk.request(
                    user_id="u1",
                    session_id="s1",
                    request_id="r1",
                    assistant_id="assistant_r1",
                    tool_call_id="call_1",
                    tool_name="sandbox_write_file",
                    arguments='{"path": "a.md"}',
                    capabilities=["WRITE"],
                    emit=events.append,
                )
            )
            rows = await _wait_pending(store)
            assert events[0].type == "approval.requested"
            assert events[0].approval.tool_name == "sandbox_write_file"
            assert events[0].approval.capabilities == ["WRITE"]
            # 卡片要落进「发起它的那条回复」：归属由事件自己带，不靠前端猜最后一个块
            assert events[0].assistant_id == "assistant_r1"
            assert events[0].session_id == "s1"

            assert await desk.decide("u1", rows[0]["approval_id"], approve=True) == "ok"
            decision = await task
            assert decision.approved is True
            assert decision.scope == "once"
            assert events[-1].type == "approval.resolved"
            assert events[-1].approval.status == approval_store.APPROVED
            # 同一次挂起的两条事件必须共用 approvalId：前端靠它把卡片从「待确认」
            # 改成终态，而不是新开一张（那会留下一个点了没反应的按钮）。
            assert events[-1].approval.approval_id == events[0].approval.approval_id
            row = await store.get(rows[0]["approval_id"])
            assert row is not None and row["status"] == approval_store.APPROVED
        finally:
            await db.close()

    asyncio.run(body())


def test_desk_timeout_expires() -> None:
    """用户不理会：超时按拒绝处理，run 继续跑（不挂死）。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=0.02)
            decision = await desk.request(
                user_id="u1",
                session_id="s1",
                request_id="r1",
                assistant_id="assistant_r1",
                tool_call_id="call_1",
                tool_name="sandbox_write_file",
                arguments="{}",
                capabilities=["WRITE"],
                emit=lambda _e: None,
            )
            assert decision.status == approval_store.EXPIRED
            assert decision.reason == REASON_APPROVAL_TIMEOUT
            rows = await store.pending("u1")
            assert rows == []
        finally:
            await db.close()

    asyncio.run(body())


def test_desk_second_decision_is_previous() -> None:
    """两个标签页同时点：第二个必须拿到 previous（HTTP 409），且不再唤醒。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=5.0)
            task = asyncio.create_task(
                desk.request(
                    user_id="u1",
                    session_id="s1",
                    request_id="r1",
                    assistant_id="assistant_r1",
                    tool_call_id="call_1",
                    tool_name="sandbox_write_file",
                    arguments="{}",
                    capabilities=["WRITE"],
                    emit=lambda _e: None,
                )
            )
            rows = await _wait_pending(store)
            approval_id = rows[0]["approval_id"]
            assert await desk.decide("u1", approval_id, approve=False) == "ok"
            assert await desk.decide("u1", approval_id, approve=True) == "previous"
            decision = await task
            # 第一次是拒绝，第二次的「允许」不得翻盘
            assert decision.status == approval_store.DENIED
            assert decision.reason == REASON_APPROVAL_DENIED
        finally:
            await db.close()

    asyncio.run(body())


def test_desk_wrong_user_cannot_decide() -> None:
    """别人的 approval_id 一律 not_found：不泄露「这条存在」。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=0.05)
            await _create(store)
            assert await desk.decide("u2", "a1", approve=True) == "not_found"
            assert await desk.decide("u1", "missing", approve=True) == "not_found"
            assert len(await store.pending("u1")) == 1
        finally:
            await db.close()

    asyncio.run(body())


def test_cancel_run_only_wakes_its_own_tickets() -> None:
    """取消是**按 run** 的：不能把别的会话正在等的那张票一起作废。

    这是最容易写错的一处（遍历所有 ticket 而不看 request_id），后果是
    另一个 run 拿到一个凭空出现的「已取消」决定。
    """

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=5.0)
            first = asyncio.create_task(
                desk.request(
                    user_id="u1",
                    session_id="s1",
                    request_id="r1",
                    assistant_id="assistant_r1",
                    tool_call_id="call_1",
                    tool_name="sandbox_write_file",
                    arguments="{}",
                    capabilities=["WRITE"],
                    emit=lambda _e: None,
                )
            )
            second = asyncio.create_task(
                desk.request(
                    user_id="u1",
                    session_id="s2",
                    request_id="r2",
                    assistant_id="assistant_r2",
                    tool_call_id="call_2",
                    tool_name="sandbox_write_file",
                    arguments="{}",
                    capabilities=["WRITE"],
                    emit=lambda _e: None,
                )
            )
            await _wait_pending(store, "r1")
            await _wait_pending(store, "r2")
            desk.cancel_run("r1")
            assert (await first).status == approval_store.CANCELED
            assert not second.done()
            rows = await store.pending("u1", request_id="r2")
            assert len(rows) == 1
            await desk.decide("u1", rows[0]["approval_id"], approve=True)
            assert (await second).approved is True
        finally:
            await db.close()

    asyncio.run(body())


# ── 三、分支：谁问了、谁没问 ──────────────────────────────────────────


def _resolve(verdict, **overrides):
    params = {
        "tool_call_id": "call_1",
        "arguments": '{"path": "a.md"}',
        "grants": set(),
        "desk": None,
        "interactive": True,
        "user_id": "u1",
        "session_id": "s1",
        "request_id": "r1",
        "assistant_id": "assistant_r1",
        "emit": lambda _e: None,
    }
    params.update(overrides)
    return resolve_approval(verdict, **params)


def test_allowed_verdict_is_untouched() -> None:
    """不需要审批的调用必须原样返回（不能因为接了审批而多一次判断）。"""

    async def body() -> None:
        verdict = evaluate_call(FULL, "sandbox_write_file", {"path": "a.md"})
        outcome = await _resolve(verdict)
        assert outcome.verdict is verdict
        assert outcome.refusal is None
        assert outcome.meta == {}

    asyncio.run(body())


def test_headless_denies_without_asking() -> None:
    """定时任务没有人可问：直接拒绝，且**不建待批行**（否则界面上是幽灵卡片）。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=0.05)
            outcome = await _resolve(
                _asking_verdict(), desk=desk, interactive=False
            )
            assert outcome.verdict.allowed is False
            assert outcome.refusal is not None
            payload = json.loads(outcome.refusal)
            assert payload["kind"] == PERMISSION_DENIED
            assert payload["reason"] == REASON_APPROVAL_UNAVAILABLE
            assert payload["retryable"] is False
            assert await store.pending("u1") == []
        finally:
            await db.close()

    asyncio.run(body())


def test_run_grant_skips_asking() -> None:
    """本次运行内已授权该工具：不再问，但仍然返回 granted 裁决而不是绕过裁决。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=0.05)
            grants = {"sandbox_write_file"}
            outcome = await _resolve(_asking_verdict(), desk=desk, grants=grants)
            assert outcome.verdict.allowed is True
            assert outcome.meta == {"grantedBy": "run"}
            # 授权记录不被读取用于放行之外的用途：库里没有待批行
            assert await store.pending("u1") == []
        finally:
            await db.close()

    asyncio.run(body())


def test_timeout_is_a_deterministic_refusal() -> None:
    """超时的拒绝结果：permission_denied + 明确的 reason，且**不计熔断**。

    写成 command_error 的话，模型会按「重试」处理，而重试只会再等满一轮超时。
    """

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=0.02)
            outcome = await _resolve(_asking_verdict(), desk=desk)
            assert outcome.verdict.allowed is False
            payload = json.loads(outcome.refusal or "{}")
            assert payload["kind"] == PERMISSION_DENIED
            assert payload["reason"] == REASON_APPROVAL_TIMEOUT
            assert payload["retryable"] is False
            assert kind_counts_toward_breaker(payload["kind"]) is False
            assert outcome.meta["status"] == approval_store.EXPIRED
        finally:
            await db.close()

    asyncio.run(body())


def test_approval_flow_end_to_end_deny() -> None:
    """完整一轮：挂起 → 拒绝 → 拒绝结果（而不是执行）。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=5.0)
            task = asyncio.create_task(_resolve(_asking_verdict(), desk=desk))
            rows = await _wait_pending(store)
            await desk.decide("u1", rows[0]["approval_id"], approve=False)
            outcome = await task
            assert outcome.verdict.allowed is False
            payload = json.loads(outcome.refusal or "{}")
            assert payload["reason"] == REASON_APPROVAL_DENIED
            assert outcome.meta["status"] == approval_store.DENIED
        finally:
            await db.close()

    asyncio.run(body())


def test_run_scope_grant_is_added_after_approval() -> None:
    """批准时选「本次运行都允许」→ 该工具进 grants，下一次不再问。"""

    async def body() -> None:
        db, store = await _fresh_store()
        try:
            desk = ApprovalDesk(store, timeout=5.0)
            grants: set[str] = set()
            task = asyncio.create_task(
                _resolve(_asking_verdict(), desk=desk, grants=grants)
            )
            rows = await _wait_pending(store)
            await desk.decide("u1", rows[0]["approval_id"], approve=True, scope="run")
            outcome = await task
            assert outcome.verdict.allowed is True
            assert grants == {"sandbox_write_file"}

            # 第二次：不再建待批行（pending 为空，且没有新增记录）
            again = await _resolve(_asking_verdict(), desk=desk, grants=grants)
            assert again.verdict.allowed is True
            assert again.meta == {"grantedBy": "run"}
            assert await store.pending("u1") == []
        finally:
            await db.close()

    asyncio.run(body())


# ── 四、HTTP 契约 ─────────────────────────────────────────────────────


def test_approval_http_validates_and_404s(client) -> None:
    """接口边界：未知 id → 404；decision 非法 → 400；空列表 → 200。"""
    assert client.get("/internal/approvals?userId=u-approve-1").json() == {
        "approvals": []
    }
    assert (
        client.post(
            "/internal/approvals/nope",
            json={"userId": "u-approve-1", "decision": "approve"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/internal/approvals/nope",
            json={"userId": "u-approve-1", "decision": "maybe"},
        ).status_code
        == 400
    )
