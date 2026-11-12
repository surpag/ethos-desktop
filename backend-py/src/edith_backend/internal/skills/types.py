"""skills 的类型定义（桌面单层版）。

与云端版的关键差别：**没有作用域**。
云端按 platform / user / project 三层存放并按优先级裁决，是因为多租户下
「谁覆盖谁」必须可解释；桌面端只有一个用户、一份目录，这三层退化成
「同一个目录里的三个子文件夹」，裁决链永远只有参与者一人。

所以这里只有一个路径常量。所有「这条 Skill 属于谁」的问题都消失了，
连带消失的还有 resolver、冲突快照、基线、迁移工具。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

#: Skill 目录在沙箱里的位置（相对 /home/user）。`LocalSandbox` 会把宿主上的
#: `<data_root>/skills` 直接 junction 到这里——**同一份文件**，不是副本。
SKILLS_PATH = "skills"

#: 每个 Skill 的入口文件名。
SKILL_FILE = "SKILL.md"


@dataclass
class Skill:
    """一个已经解析完成的 Skill。

    `id` 是磁盘上的**目录名**，`name` 是 frontmatter 里的 `name`。两者可以不同，
    而模型拿到的路径必须用目录名（否则读到一个不存在的路径，表现为「Skill 是空的」
    而不是报错）。
    """

    id: str
    name: str
    description: str
    body: str = ""


class SkillListItem(BaseModel):
    """浏览器展示的 Skill 摘要。"""

    name: str
    description: str
    #: 沙箱内相对路径，如 `skills/current-time`。前端拿它做「在哪」的提示。
    path: str = ""


class SkillsResponse(BaseModel):
    """Skills 列表 HTTP 输出。

    `status` / `reason` 必须存在：空列表既可能是「真的没有 Skill」，也可能是
    「目录读不了」。旧实现把两者压成同一个空数组，故障因此长期隐形。
    """

    skills: list[SkillListItem] = Field(default_factory=list)
    status: str = "ok"
    reason: str = ""
