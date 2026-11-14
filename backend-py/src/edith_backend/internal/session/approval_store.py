"""审批记录的持久化（`run_approvals`）。

这张表的用途只有两个：**审计**（谁在什么时候批了什么）与**刷新后展示**
（事件日志在进程内，重启就没了，但「有一条悬着的待批」需要能查）。

它**不是恢复依据**：进程死了，那次被批准的调用也已经不存在了。正确做法是把 pending
统一置成 `expired`，而不是让下一个进程去兑现一张过期的支票。这一点与 `run_plans`
正好相反——计划必须能恢复，因为不知道「做到第几步」会让模型重做；审批不需要恢复，
因为许可针对的是某一次具体调用。

## 为什么决策必须是条件更新

两个标签页同时点「允许」是常态而不是异常。`UPDATE ... WHERE status='pending'` 的
受影响行数就是那把锁：只有第一个到达的拿到 1，其余拿到 0 并被告知「来晚了」。
用「先查再写」在这里必然翻车——中间窗口足够两个请求都读到 pending。
"""

from __future__ import annotations

import json

import aiosqlite

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CANCELED = "canceled"

#: 已进入终态、不会再变的集合（用于「这次决策是不是第一次」的判断）
FINAL_STATUSES = frozenset({APPROVED, DENIED, EXPIRED, CANCELED})

#: 参数落库上限：审批卡片要给人看，但没必要把 200KB 文件内容塞进库里
MAX_ARGUMENTS_CHARS = 4000


def _row_to_dict(row: aiosqlite.Row) -> dict:
    try:
        capabilities = json.loads(row["capabilities"] or "[]")
    except (json.JSONDecodeError, TypeError):
        capabilities = []
    return {
        "approval_id": row["approval_id"],
        "user_id": row["user_id"],
        "session_id": row["session_id"],
        "request_id": row["request_id"],
        "tool_call_id": row["tool_call_id"],
        "tool_name": row["tool_name"],
        "capabilities": capabilities if isinstance(capabilities, list) else [],
        "arguments": row["arguments"] or "",
        "status": row["status"],
        "scope": row["scope"],
        "reason": row["reason"],
        "decided_by": row["decided_by"],
        "created_at": row["created_at"] or "",
        "decided_at": row["decided_at"] or "",
    }


class ApprovalStore:
    """`run_approvals` 的读写。等待与唤醒语义在 `agentrun/approval.py`。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create(
        self,
        *,
        approval_id: str,
        user_id: str,
        session_id: str,
        request_id: str,
        tool_call_id: str,
        tool_name: str,
        capabilities: list[str],
        arguments: str,
    ) -> dict:
        """建一条待批记录。同一 (request_id, tool_call_id) 重复建会抛 IntegrityError。

        重复插入说明有代码路径在重放裁决，应当失败而不是留下两条互相矛盾的待批。
        """
        await self._db.execute(
            """
            INSERT INTO run_approvals (
                approval_id, user_id, session_id, request_id,
                tool_call_id, tool_name, capabilities, arguments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                user_id,
                session_id,
                request_id,
                tool_call_id,
                tool_name,
                json.dumps(capabilities, ensure_ascii=False),
                (arguments or "")[:MAX_ARGUMENTS_CHARS],
            ),
        )
        await self._db.commit()
        row = await self.get(approval_id)
        assert row is not None  # 刚插入的行不可能读不到
        return row

    async def get(self, approval_id: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM run_approvals WHERE approval_id = ?", (approval_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_dict(row) if row is not None else None

    async def decide(
        self,
        approval_id: str,
        status: str,
        *,
        scope: str = "once",
        reason: str = "",
        by: str = "",
    ) -> bool:
        """把一条 pending 记录推向终态。返回是否**本次**决策生效（False = 来晚了）。

        条件更新是唯一的竞态闸门：受影响行数为 0 说明它已经是终态，调用方必须
        把这次点击当成「无效」处理，而不能再执行一遍对应工具。
        """
        cursor = await self._db.execute(
            """
            UPDATE run_approvals
            SET status = ?, scope = ?, reason = ?, decided_by = ?,
                decided_at = CURRENT_TIMESTAMP
            WHERE approval_id = ? AND status = ?
            """,
            (status, scope, reason, by, approval_id, PENDING),
        )
        changed = cursor.rowcount or 0
        await cursor.close()
        await self._db.commit()
        return changed > 0

    async def pending(
        self, user_id: str, *, request_id: str = "", session_id: str = ""
    ) -> list[dict]:
        """待批准列表（刷新兜底）。按 run 或按会话取，二者都给时按 run 取。"""
        sql = "SELECT * FROM run_approvals WHERE user_id = ? AND status = ?"
        params: list[object] = [user_id, PENDING]
        if request_id:
            sql += " AND request_id = ?"
            params.append(request_id)
        elif session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        cursor = await self._db.execute(
            sql + " ORDER BY created_at ASC, rowid ASC", tuple(params)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_dict(row) for row in rows]

    async def expire_stale(self, reason: str = "backend_restart") -> int:
        """把遗留的 pending 全部置为 expired（启动时调用）。返回处理条数。"""
        cursor = await self._db.execute(
            """
            UPDATE run_approvals
            SET status = ?, reason = ?, decided_at = CURRENT_TIMESTAMP
            WHERE status = ?
            """,
            (EXPIRED, reason, PENDING),
        )
        changed = cursor.rowcount or 0
        await cursor.close()
        await self._db.commit()
        return changed

    async def close_run(self, request_id: str, status: str, reason: str) -> int:
        """收尾一个 run 的全部 pending（用户取消 run 时用）。"""
        cursor = await self._db.execute(
            """
            UPDATE run_approvals
            SET status = ?, reason = ?, decided_at = CURRENT_TIMESTAMP
            WHERE request_id = ? AND status = ?
            """,
            (status, reason, request_id, PENDING),
        )
        changed = cursor.rowcount or 0
        await cursor.close()
        await self._db.commit()
        return changed
