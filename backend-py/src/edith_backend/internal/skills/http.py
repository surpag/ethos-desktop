"""skills 的 HTTP 边界。

只回答一个问题：现在磁盘上有哪些 Skill。没有作用域参数、没有覆盖裁决结果——
桌面端只有一份目录，「哪个生效」是伪问题。

`status` 三值：
- `ok`      —— 扫到了，且没有坏掉的条目
- `partial` —— 扫到了，但有 SKILL.md 解析失败（`reason` 里列出是谁、为什么）
- `missing` —— 目录还不存在（首次启动、用户全删）；这不是错误
"""

from __future__ import annotations

from fastapi import APIRouter

from .catalog import Catalog
from .types import SKILLS_PATH, SkillListItem, SkillsResponse


class HTTP:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/skills", response_model=SkillsResponse)
        async def list_skills() -> SkillsResponse:
            scan = self.catalog.scan()
            if not scan.exists:
                return SkillsResponse(
                    skills=[],
                    status="missing",
                    reason=f"{self.catalog.root} 尚未创建",
                )
            return SkillsResponse(
                skills=[
                    SkillListItem(
                        name=skill.name,
                        description=skill.description,
                        path=f"{SKILLS_PATH}/{skill.id}",
                    )
                    for skill in scan.skills
                ],
                status="partial" if scan.skipped else "ok",
                reason="; ".join(f"{name}: {why}" for name, why in scan.skipped),
            )

        return router
