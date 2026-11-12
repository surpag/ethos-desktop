"""SQLite 连接管理。

对应 Go 版 cmd/server/database.go。
aiosqlite 底层是单连接线程模型，天然对应 Go 版 SetMaxOpenConns(1) 的写入串行语义。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite


async def open_database(path: str) -> aiosqlite.Connection:
    """打开一个 SQLite 连接，配置忙等待超时。

    注意：不用 WAL 模式。WAL 的 -shm 文件在 Windows 沙箱环境下若被进程
    （如被强杀的后端）遗留 mmap 锁，会导致后续连接写入挂起；且沙箱的
    safe-delete 机制会拦截 -wal 的清理。单连接场景下 DELETE 模式足够。
    """
    # SQLite 只创建文件，不创建父目录。桌面端首次启动时 `%APPDATA%\ETHOS`
    # 还不存在，直接 connect 会报 `unable to open database file`——报错里
    # 完全不提目录，很难定位。`":memory:"` 不是路径，跳过。
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA journal_mode = DELETE")
        await db.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        await db.close()
        raise
    return db
