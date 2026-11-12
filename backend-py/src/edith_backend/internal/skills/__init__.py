"""skills 模块：单层目录的扫描、内置 Skill 铺设、清单渲染。

桌面版的设计只有一句话：**一个目录，扫它**。
云端那套作用域 / 裁决 / 冲突 / 迁移在这里全部不存在，理由见 types.py。
"""

from .budget import (
    MAX_SKILLS_PER_RUN,
    MAX_SKILL_BYTES_PER_RUN,
    ChargeDecision,
    SkillLoadBudget,
    bind_skill_load_budget,
    charge_skill_load,
    current_skill_load_budget,
    new_skill_load_budget,
)
from .catalog import Catalog, ScanResult, load_skill, scan_skills, seed_builtin
from .http import HTTP
from .instruction import SkillInstruction, render_skill_instruction
from .types import SKILL_FILE, SKILLS_PATH, Skill, SkillListItem, SkillsResponse

__all__ = [
    "HTTP",
    "MAX_SKILLS_PER_RUN",
    "MAX_SKILL_BYTES_PER_RUN",
    "SKILL_FILE",
    "SKILLS_PATH",
    "Catalog",
    "ChargeDecision",
    "ScanResult",
    "Skill",
    "SkillInstruction",
    "SkillListItem",
    "SkillLoadBudget",
    "SkillsResponse",
    "bind_skill_load_budget",
    "charge_skill_load",
    "current_skill_load_budget",
    "load_skill",
    "new_skill_load_budget",
    "render_skill_instruction",
    "scan_skills",
    "seed_builtin",
]
