"""cronadapter：把定时任务转换为 Gateway 消息并消费结果。

对应 Go 版 internal/cronadapter/adapter.go。
"""

from __future__ import annotations

from uuid import uuid4

from ..cronjob.types import Job
from ..gateway.service import Service as GatewayService
from ..gateway.types import CRON_CHANNEL, IncomingMessage


class Adapter:
    """Cron 渠道执行器；不负责轮询和任务存储。"""

    def __init__(self, gateway: GatewayService) -> None:
        self._gateway = gateway

    async def run_job(self, job: Job) -> None:
        """把一个已抢占任务交给 Gateway，并消费事件直到 AgentRun 收尾。

        执行身份是**任务所有者**（`job.user_id`），不是「创建时那个 run 的身份」；
        `permission_level` 传的是创建时写下的快照，service 层会与用户当前档位取
        更严者。这两条合起来回答同一个问题：这条任务凭什么在这里做这件事。
        """
        stream, run_error = await self._gateway.run(
            IncomingMessage(
                channel=CRON_CHANNEL,
                external_user_id=job.user_id,
                session_key=f"cron:{job.id}",
                request_id=uuid4().hex,
                message=job.prompt,
                permission_level=job.permission_level,
                # 无人值守：审批档下需要确认的调用**不会**等人（没人可问），
                # 而是直接拒绝。让它挂起等于把会话锁死到一个永远不会来的点击上。
                interactive=False,
            )
        )
        if run_error is not None:
            raise RuntimeError(f"start cron agent run: {run_error.message}")
        execution_error: Exception | None = None
        # 事件流现在是可重放的日志（订阅式），仍然读到 None 哨兵为止；
        # 定时任务没有「重连」需求，但它必须与 Web 渠道消费同一种流，
        # 否则两边对「流结束」的理解会悄悄分叉。
        async for event in stream.open(0):
            if event is None:
                break
            if event.type == "run.error" and event.error is not None:
                execution_error = RuntimeError(event.error.message)
        if execution_error is not None:
            raise execution_error
