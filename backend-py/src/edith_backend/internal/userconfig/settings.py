"""用户 Agent 设置的读写能力。

对应 Go 版 internal/userconfig/settings.go。
"""

from __future__ import annotations

import aiosqlite

from .store import ensure_user
from .types import AgentSettings


class Settings:
    """提供用户 Agent 设置的读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def load(self, user_id: str) -> AgentSettings:
        """读取用户设置；首次读取会建立空白用户记录。"""
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await ensure_user(self._db, user_id)
        cursor = await self._db.execute(
            "SELECT personality, default_model_id, timezone, git_author_name, "
            "git_author_email, permission_level "
            "FROM user_agents WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return AgentSettings()
        return AgentSettings(
            personality=row["personality"] or "",
            default_model_id=row["default_model_id"] or "",
            timezone=row["timezone"] or "",
            git_author_name=row["git_author_name"] or "",
            git_author_email=row["git_author_email"] or "",
            permission_level=row["permission_level"] or "",
        )

    async def save(self, user_id: str, settings: AgentSettings) -> None:
        """保存人格、默认模型、时区、git 身份与执行档位，不处理供应商密钥。

        `permission_level` 为空时**保留原值**（`COALESCE(NULLIF(...))`）：
        其它设置页保存时不会带上这个字段，若按「空 = 默认值」写入，
        用户调过的档位会被一次无关的保存悄悄重置。
        """
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await self._db.execute(
            """
            INSERT INTO user_agents
                (user_id, personality, default_model_id, timezone, git_author_name,
                 git_author_email, permission_level)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                personality = excluded.personality,
                default_model_id = excluded.default_model_id,
                timezone = excluded.timezone,
                git_author_name = excluded.git_author_name,
                git_author_email = excluded.git_author_email,
                permission_level = COALESCE(
                    NULLIF(excluded.permission_level, ''), user_agents.permission_level
                )
            """,
            (
                user_id,
                settings.personality.strip(),
                settings.default_model_id.strip(),
                settings.timezone.strip(),
                settings.git_author_name.strip(),
                settings.git_author_email.strip(),
                settings.permission_level.strip(),
            ),
        )
        await self._db.commit()

    async def load_permission_level(self, user_id: str) -> str:
        """当前档位（原始字符串；空 = 未配置，解析交给 permissions.policy）。"""
        settings = await self.load(user_id)
        return settings.permission_level

    async def save_permission_level(self, user_id: str, level: str) -> None:
        settings = await self.load(user_id)
        settings.permission_level = level
        await self.save(user_id, settings)

    async def load_personality(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.personality

    async def load_default_model_id(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.default_model_id

    async def load_timezone(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.timezone

    async def save_timezone(self, user_id: str, timezone: str) -> None:
        settings = await self.load(user_id)
        settings.timezone = timezone
        await self.save(user_id, settings)
