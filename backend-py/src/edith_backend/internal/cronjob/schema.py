"""cronjob 的表结构。

对应 Go 版 internal/cronjob/schema.go。
"""

from __future__ import annotations

import aiosqlite

# 执行结果列：都是「最近一次」而非历史——任务列表要回答的是
# 「我今天早上那条跑成功了吗」，不是「它这半年失败过几次」。
#
# 为什么失败必须落库而不是只打日志：
#   定时任务没有人在旁边看着。日志是给运维看的，用户看不到；一次连续 4 次
#   402 失败，在用户侧表现为「什么都没发生」——既没有产出，也没有报错，
#   只有沉默。把失败写进任务行，才有地方把它显示出来。
_RESULT_COLUMNS = {
    "last_run_at": "TEXT",
    "last_error": "TEXT",
    "last_error_at": "TEXT",
    "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
}


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS cron_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            schedule TEXT NOT NULL,
            prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,
            running INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_run_at TEXT,
            last_error TEXT,
            last_error_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await _migrate(db)
    await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """老库补列（幂等）。

    `consecutive_failures` 有 DEFAULT 0：历史任务确实没失败过（或者我们不知道），
    0 是它唯一合理的初值——与 `chat_messages.end_reason` 那种「NULL 表示未知」
    的列不同，这里的 0 不构成任何断言。
    """
    cursor = await db.execute("PRAGMA table_info(cron_jobs)")
    existing = {row["name"] for row in await cursor.fetchall()}
    await cursor.close()
    if "user_id" not in existing and "clerk_user_id" in existing:
        # 列改名：`user_id` 才是它实际的含义，旧名是上游多租户身份体系留下的。
        # SQLite 3.25+ 的 RENAME COLUMN 保留数据与 NOT NULL 约束，比「建新表
        # → 拷数据 → 删旧表」安全得多。新库不会有 `clerk_user_id`，因此这一步
        # 只在老库上是真的动作。
        await db.execute("ALTER TABLE cron_jobs RENAME COLUMN clerk_user_id TO user_id")
        existing.discard("clerk_user_id")
        existing.add("user_id")
    for column, ddl in _RESULT_COLUMNS.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE cron_jobs ADD COLUMN {column} {ddl}")
