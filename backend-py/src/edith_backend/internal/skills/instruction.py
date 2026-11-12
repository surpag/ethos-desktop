"""把 Skill 清单渲染进 System Prompt 末尾。

渐进式加载的 L1 层：这里只告诉模型「有哪些 Skill、在哪、怎么读」，
正文一个字都不进 prompt。模型需要时自己用 `sandbox_read_file` 去读。

注入位置固定在 System Prompt **末尾**：动态内容放前面会让前缀缓存每次失效，
实测 system 开头改 4 个字符就能把跨 run 的缓存命中率从 98% 打到 0%。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .budget import index_budget_chars, plan_index
from .types import SKILLS_PATH, Skill

#: 指令里对模型说明的读法。路径用**目录名**而不是 frontmatter 的 name：
#: 两者不一致时，用 name 拼出来的路径在磁盘上不存在，模型会把「读不到」
#: 理解成「这个 Skill 是空的」，而不是「路径错了」。
_HEADER = """## Skill

以下 Skill 提供特定任务的详细步骤。**按需读取**：先看描述判断是否相关，
需要时用 `sandbox_read_file` 读 `{path}/<目录名>/SKILL.md`。正文通常很长，
不要预先通读；`scripts/` 下的脚本按需单独读。

创建或修改 Skill：写 `{path}/<目录名>/SKILL.md`，frontmatter 必须含 `name` 和
`description`（模型只凭 description 决定要不要读它），写完必须读回确认。
`{path}/` 是平台目录，与用户项目里的 `workspace/` 是两个命名空间。"""


@dataclass(frozen=True)
class SkillInstruction:
    """一次注入的成品与它的账目。

    为什么要带账目而不是只返回字符串：`omitted` 必须同时出现在**指令文本**与
    **trace** 里（两者缺一即视为静默丢内容）。只返回字符串的话，调用方要么自己
    再算一遍预算（两处实现必然漂），要么就漏掉 trace 那一半。
    """

    text: str = ""
    #: 真正进了 prompt 的条目，形如 `skills/current-time`
    injected: list[str] = field(default_factory=list)
    #: 被 L1 预算截掉的条目，顺序 = 截断顺序
    omitted: list[str] = field(default_factory=list)
    budget_chars: int = 0


def _line(skill: Skill) -> str:
    """单条摘要。成本与这一行的实际长度严格一致（预算按字符算）。"""
    return f"- `{skill.name}`：{skill.description} — {SKILLS_PATH}/{skill.id}"


def render_skill_instruction(skills: list[Skill]) -> SkillInstruction:
    """渲染注入段落；没有 Skill 时返回空串（不往 prompt 里塞空标题）。"""
    if not skills:
        return SkillInstruction(text="", budget_chars=index_budget_chars())

    # 排序键用 (name, id)：name 相同时靠目录名再定一次序，避免依赖文件系统
    # 的返回顺序——那在不同机器上可能不同，会让注入文本跨机器不一致。
    ordered = sorted(skills, key=lambda s: (s.name, s.id))
    lines = [_line(skill) for skill in ordered]
    plan = plan_index([len(line) + 1 for line in lines])

    kept = [lines[i] for i in plan.kept]
    injected = [f"{SKILLS_PATH}/{ordered[i].id}" for i in plan.kept]
    omitted = [f"{SKILLS_PATH}/{ordered[i].id}" for i in plan.omitted]

    body = "\n".join(kept)
    if omitted:
        # 必须显式说出来：模型不知道「清单被截过」时，会认为目录里只有这些，
        # 于是「我想找的那个 Skill 不存在」——一个纯属虚构的结论。
        body += (
            f"\n\n还有 {len(omitted)} 个 Skill 因索引预算未列出："
            + "、".join(f"`{SKILLS_PATH}/{ordered[i].id}`" for i in plan.omitted)
        )

    text = _HEADER.format(path=SKILLS_PATH) + "\n\n" + body
    return SkillInstruction(
        text=text,
        injected=injected,
        omitted=omitted,
        budget_chars=index_budget_chars(),
    )
