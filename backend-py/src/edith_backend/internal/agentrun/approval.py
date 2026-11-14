"""审批挂起：让一次工具调用**停在原地**等人。

## 为什么是原地等待

「先结束 run、批准后再续跑」看起来更省事，但它把审批变成了空白支票：
续跑 = 重新调用模型，而用户批准的是「这一次调用」。模型第二次可能去干别的。
要把支票收回来，只能把「已批准的那一条」钉死成续跑的第一条——那等价于
「原地等待 + 一次进程重启」，复杂度更高、收益只有不占内存。

代价如实承认：挂起期间该 run 占着 `session_busy`，同会话发不了新消息。
这是刻意的（有一条待批操作悬着时，让用户继续下指令只会制造两个真相），
由超时兜底。

## 与存储的分工

- 本模块管**等待与唤醒**（进程内，`asyncio.Event`）。
- `session/approval_store.py` 管**审计与展示**（落库）。
- 谁赢竞态由数据库的条件更新裁决，不由内存决定：两个标签页同时点是常态。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..agentstream.types import ApprovalSnapshot, Event
from ..permissions.policy import (
    REASON_APPROVAL_CANCELED,
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_TIMEOUT,
    REASON_APPROVAL_UNAVAILABLE,
    Verdict,
    approval_refusal,
)
from ..permissions.types import APPROVAL_REQUIRED
from ..session import approval_store as store_status

if TYPE_CHECKING:
    from ..session.approval_store import ApprovalStore

logger = logging.getLogger(__name__)

#: 等多久算「用户不在」。10 分钟：够一次走开的咖啡，又不至于把会话锁一整天。
APPROVAL_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class ApprovalDecision:
    """一次挂起的结局。`status` 恒为终态之一（不会回到 pending）。"""

    status: str
    scope: str = "once"
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.status == store_status.APPROVED


@dataclass
class _Ticket:
    """在等的那个调用。一个 approval_id 一张票，票只在内存里。

    `request_id` 也记着：取消是**按 run** 的，不能把别的会话的待批一起唤醒。
    """

    request_id: str
    wake: asyncio.Event
    decision: ApprovalDecision | None = None

    def set(self, decision: ApprovalDecision) -> None:
        if self.decision is None:  # 第一个到达的胜出，后续点击不影响结果
            self.decision = decision
            self.wake.set()


def _expires_at(timeout: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=timeout)).isoformat(timespec="seconds")


def _decision_from_row(row: dict) -> ApprovalDecision:
    """把库里的终态还原成决策（竞态兜底：内存票被别处抢先决定时用）。"""
    return ApprovalDecision(
        status=row.get("status") or store_status.EXPIRED,
        scope=row.get("scope") or "once",
        reason=row.get("reason") or "",
    )


class ApprovalDesk:
    """审批的等待台。一个 Service 一个实例，跨 run 共享（按 approval_id 分键）。"""

    def __init__(
        self, store: ApprovalStore, timeout: float = APPROVAL_TIMEOUT_SECONDS
    ) -> None:
        self._store = store
        self._timeout = timeout
        self._tickets: dict[str, _Ticket] = {}

    @property
    def timeout(self) -> float:
        return self._timeout

    async def request(
        self,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: str,
        capabilities: list[str],
        emit,
    ) -> ApprovalDecision:
        """挂起一次调用，直到用户决策 / 超时 / 被取消。"""
        approval_id = uuid.uuid4().hex[:12]
        await self._store.create(
            approval_id=approval_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            capabilities=capabilities,
            arguments=arguments,
        )
        snapshot = ApprovalSnapshot(
            approval_id=approval_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=(arguments or "")[:4000],
            capabilities=capabilities,
            status=store_status.PENDING,
            expires_at=_expires_at(self._timeout),
        )
        ticket = _Ticket(request_id=request_id, wake=asyncio.Event())
        self._tickets[approval_id] = ticket
        logger.info(
            "approval_requested approval_id=%s request_id=%s tool=%s caps=%s timeout=%.0fs",
            approval_id,
            request_id,
            tool_name,
            ",".join(capabilities) or "-",
            self._timeout,
        )
        emit(Event(type="approval.requested", request_id=request_id, approval=snapshot))
        try:
            await asyncio.wait_for(ticket.wake.wait(), self._timeout)
            decision = ticket.decision or ApprovalDecision(
                store_status.EXPIRED, reason=REASON_APPROVAL_TIMEOUT
            )
        except (TimeoutError, asyncio.TimeoutError):
            decision = await self._expire(approval_id)
        finally:
            self._tickets.pop(approval_id, None)

        emit(
            Event(
                type="approval.resolved",
                request_id=request_id,
                approval=snapshot.model_copy(update={"status": decision.status}),
            )
        )
        logger.info(
            "approval_resolved approval_id=%s status=%s scope=%s reason=%s",
            approval_id,
            decision.status,
            decision.scope,
            decision.reason or "-",
        )
        return decision

    async def decide(
        self, user_id: str, approval_id: str, *, approve: bool, scope: str = "once"
    ) -> str:
        """记录一次用户决策并唤醒等待者。

        返回 `"ok"` / `"not_found"`（不存在或不属于该用户）/ `"previous"`（已被处理）。
        `"previous"` 不是错误：两个标签页同时点是常态，HTTP 层映射成 409，
        前端静默关闭即可。
        """
        row = await self._store.get(approval_id)
        if row is None or row["user_id"] != user_id:
            return "not_found"
        status = store_status.APPROVED if approve else store_status.DENIED
        changed = await self._store.decide(
            approval_id,
            status,
            scope=scope if approve else "once",
            reason="" if approve else REASON_APPROVAL_DENIED,
            by=user_id,
        )
        if not changed:
            return "previous"
        ticket = self._tickets.get(approval_id)
        if ticket is not None:
            ticket.set(
                ApprovalDecision(
                    status, scope if approve else "once", "" if approve else REASON_APPROVAL_DENIED
                )
            )
        return "ok"

    def cancel_run(self, request_id: str) -> None:
        """唤醒该 run 下所有等待者（用户点了停止）。

        只动内存：库里的 pending 由 `close_run` 收尾，两者分开是因为这里的
        调用者（cancel）是同步入口，不该为一次收尾去等一次磁盘写。

        **必须按 request_id 过滤**：取消一个 run 不能顺手唤醒别的会话的待批
        ——那会让另一个正在等的 run 拿到一个「已取消」的假决定。
        """
        for ticket in list(self._tickets.values()):
            if ticket.request_id != request_id:
                continue
            ticket.set(
                ApprovalDecision(store_status.CANCELED, reason=REASON_APPROVAL_CANCELED)
            )

    async def close_run(self, request_id: str, reason: str = REASON_APPROVAL_CANCELED):
        return await self._store.close_run(request_id, store_status.CANCELED, reason)

    async def pending(
        self, user_id: str, *, request_id: str = "", session_id: str = ""
    ) -> list[dict]:
        return await self._store.pending(
            user_id, request_id=request_id, session_id=session_id
        )

    async def expire_stale(self) -> int:
        """启动时把上一个进程遗留的 pending 置为 expired。

        它们已经没有等待者了：不让它们变成 expired，界面上就会出现一个
        「永远在等你确认」的幽灵卡片。
        """
        return await self._store.expire_stale()

    async def _expire(self, approval_id: str) -> ApprovalDecision:
        """超时：抢 pending。抢输了说明用户刚好在这一刻点了，尊重用户的决定。"""
        won = await self._store.decide(
            approval_id,
            store_status.EXPIRED,
            reason=REASON_APPROVAL_TIMEOUT,
            by="system",
        )
        if won:
            return ApprovalDecision(store_status.EXPIRED, reason=REASON_APPROVAL_TIMEOUT)
        row = await self._store.get(approval_id)
        return _decision_from_row(row) if row is not None else ApprovalDecision(
            store_status.EXPIRED, reason=REASON_APPROVAL_TIMEOUT
        )


@dataclass(frozen=True)
class ApprovalOutcome:
    """一次裁决经过审批流程之后的最终结论。"""

    verdict: Verdict
    #: 非 None = 这次调用被拒，直接把这段结果交给模型
    refusal: str | None = None
    #: 进 trace 的诊断字段（谁放行的、决策状态）；空表示这一轮没走审批
    meta: dict[str, str] = field(default_factory=dict)


async def resolve_approval(
    verdict: Verdict,
    *,
    tool_call_id: str,
    arguments: str,
    grants: set[str],
    desk: ApprovalDesk | None,
    interactive: bool,
    user_id: str,
    session_id: str,
    request_id: str,
    emit,
) -> ApprovalOutcome:
    """审批档的挂起入口。返回最终裁决与（可能的）拒绝结果。

    三条分支的顺序有讲究：

    1. **无人在场 → 直接拒**，排在「已授权」之前：没人在场时连授权都无从谈起，
       而且这个判断取决于 run 的来源，与先前的授权状态无关；
    2. **本 run 已授权该工具 → 放行**，但仍返回 `verdict.granted()` 而不是绕过
       裁决——授权只能解除档位这一层，不能让这次调用逃过别的检查；
    3. 其余才挂起等人。

    挂起期间并发是安全的：写工具整批串行（`_tc_readonly` 为 False 时不并发），
    所以一个 run 同一时刻最多一条待批。
    """
    if verdict.allowed or verdict.kind != APPROVAL_REQUIRED:
        return ApprovalOutcome(verdict)
    if desk is None or not interactive:
        return ApprovalOutcome(
            verdict, approval_refusal(verdict, REASON_APPROVAL_UNAVAILABLE)
        )
    if verdict.tool in grants:
        return ApprovalOutcome(verdict.granted(), meta={"grantedBy": "run"})
    decision = await desk.request(
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        tool_call_id=tool_call_id,
        tool_name=verdict.tool,
        arguments=arguments,
        capabilities=sorted(str(cap) for cap in verdict.needed),
        emit=emit,
    )
    meta = {
        "status": decision.status,
        "scope": decision.scope,
        "reason": decision.reason or "-",
    }
    if decision.approved:
        if decision.scope == "run":
            grants.add(verdict.tool)
        return ApprovalOutcome(verdict.granted(), meta=meta)
    return ApprovalOutcome(
        verdict,
        approval_refusal(verdict, decision.reason or REASON_APPROVAL_DENIED),
        meta,
    )
