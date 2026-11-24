"""沙箱执行的**结构化**审计落库（不只是日志）。

为什么要落库而不是只写日志：验收要回答「这次 run 的每一条命令分别跑在哪个后端、
策略指纹是什么、退出码多少、有没有被拒绝」，日志只能人肉 grep，且滚动后就没了。
Trace 面板要的是可查询的行。

**不写凭据**：命令文本入库前已脱敏；环境变量只存键名。`workspace_root` 落库是
有意的（本地单用户，路径本身就是排障信息），但**不写**完整策略内容。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_audit (
    id              TEXT PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    request_id      TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    workspace_id    TEXT NOT NULL DEFAULT '',
    tool_call_id    TEXT NOT NULL DEFAULT '',
    tool_name       TEXT NOT NULL DEFAULT '',
    purpose         TEXT NOT NULL DEFAULT '',
    command         TEXT NOT NULL DEFAULT '',
    backend         TEXT NOT NULL DEFAULT '',
    backend_version TEXT NOT NULL DEFAULT '',
    policy_version  TEXT NOT NULL DEFAULT '',
    policy_digest   TEXT NOT NULL DEFAULT '',
    workspace_root  TEXT NOT NULL DEFAULT '',
    exit_code       INTEGER,
    duration_ms     REAL,
    outcome         TEXT NOT NULL DEFAULT '',
    pid             INTEGER,
    dropped_env     TEXT NOT NULL DEFAULT '[]',
    injected_env    TEXT NOT NULL DEFAULT '[]',
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sandbox_audit_session
    ON sandbox_audit(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sandbox_audit_request
    ON sandbox_audit(request_id);
"""


class SandboxAuditStore:
    """审计表读写。与其它 store 一样：单连接、由 `main.py` 注入。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        # 与仓库现有做法一致：幂等 DDL，每次启动执行一次。
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def record(self, entry: dict[str, Any]) -> None:
        await self._db.execute(
            """
            INSERT INTO sandbox_audit (
                id, created_at, request_id, session_id, workspace_id, tool_call_id,
                tool_name, purpose, command, backend, backend_version, policy_version,
                policy_digest, workspace_root, exit_code, duration_ms, outcome, pid,
                dropped_env, injected_env, reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entry.get("id") or uuid.uuid4().hex,
                entry.get("created_at") or 0,
                entry.get("request_id", ""),
                entry.get("session_id", ""),
                entry.get("workspace_id", ""),
                entry.get("tool_call_id", ""),
                entry.get("tool_name", ""),
                entry.get("purpose", ""),
                entry.get("command", ""),
                entry.get("backend", ""),
                entry.get("backend_version", ""),
                entry.get("policy_version", ""),
                entry.get("policy_digest", ""),
                entry.get("workspace_root", ""),
                entry.get("exit_code"),
                entry.get("duration_ms"),
                entry.get("outcome", ""),
                entry.get("pid"),
                json.dumps(entry.get("dropped_env") or [], ensure_ascii=False),
                json.dumps(entry.get("injected_env") or [], ensure_ascii=False),
                entry.get("reason", ""),
            ),
        )
        await self._db.commit()

    async def query(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
        workspace_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("request_id", request_id),
            ("workspace_id", workspace_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 50), 500)))
        cursor = await self._db.execute(
            f"SELECT * FROM sandbox_audit{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("dropped_env", "injected_env"):
                try:
                    item[key] = json.loads(item.get(key) or "[]")
                except (TypeError, ValueError):
                    item[key] = []
            result.append(item)
        return result


#: 进程级单例。隔离层是进程级服务（与 SandboxManager 同理），
#: 由 `main.py` 在启动时注入连接；未注入时审计只写日志，不报错。
_store: SandboxAuditStore | None = None


def configure(db: aiosqlite.Connection) -> SandboxAuditStore:
    global _store
    _store = SandboxAuditStore(db)
    return _store


def current() -> SandboxAuditStore | None:
    return _store


def reset() -> None:
    global _store
    _store = None
