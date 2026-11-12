"""Skill 的两层预算。

两层各自堵一条路，缺一层等于没做：

- **L1 索引预算**：只限加载的话，首轮 system prompt 本身就可能已经超了——
  一个装了上百个 Skill 的目录，光清单就能吃掉几千 token。
- **L2 加载预算**：只限索引的话，模型拿到「还有 200 个 Skill」的线索后可以把
  正文一个个读进上下文，首轮很小、第三轮就爆。

这套策略与租户无关，所以桌面版原样保留。

L1 为什么是纯函数
----------------
截断必须**确定性**：同一份磁盘内容，两次 run 得到的注入文本必须逐字符相同，
否则 system prompt 前缀每次都在变，跨 run 的缓存复用直接归零（实测 94.8% → 0%）。
所以这里不接受任何「按使用频率 / 最近使用」的输入——那天然是跨 run 变化的。
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# L1：Discovery 索引预算（进 System Prompt 的摘要）
# --------------------------------------------------------------------------

_INDEX_BUDGET_ENV = "SKILL_INDEX_BUDGET_CHARS"
_DEFAULT_INDEX_BUDGET = 4000


def index_budget_chars() -> int:
    """索引预算总字符数（环境变量可覆盖；非法值退回默认而不是崩）。"""
    raw = (os.getenv(_INDEX_BUDGET_ENV) or "").strip()
    if not raw:
        return _DEFAULT_INDEX_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_INDEX_BUDGET
    return value if value > 0 else _DEFAULT_INDEX_BUDGET


@dataclass(frozen=True)
class IndexPlan:
    """L1 的裁决结果。

    `kept` / `omitted` 都是**原下标**：调用方据此拼文本，本模块不关心每行长什么样，
    也就不会与渲染格式耦合。
    """

    kept: list[int] = field(default_factory=list)
    omitted: list[int] = field(default_factory=list)

    @property
    def omitted_count(self) -> int:
        return len(self.omitted)


def plan_index(costs: list[int]) -> IndexPlan:
    """按顺序截断，返回保留项与被省略项的原下标。

    两点刻意设计：

    - **截断以条目为单位**：半个 Skill 的摘要比没有更糟，模型会以为描述就到那里
      为止，从而错过一个本该读的 Skill。
    - **超预算的单项直接计入 omitted 并继续尝试后面的**：单项成本超过总预算时，
      跳过它比「整份清单只留一条」更诚实——但这种情况本身说明描述写得太长，
      所以调用方会把 omitted 写进 trace。
    """
    budget = index_budget_chars()
    kept: list[int] = []
    omitted: list[int] = []
    used = 0
    for index, cost in enumerate(costs):
        cost = max(0, int(cost))
        if used + cost <= budget:
            used += cost
            kept.append(index)
        else:
            omitted.append(index)
    return IndexPlan(kept=kept, omitted=omitted)


# --------------------------------------------------------------------------
# L2：Skill 加载预算（单 run 内实际读的正文）
# --------------------------------------------------------------------------

#: 单 run 最多加载几个**不同的** Skill
MAX_SKILLS_PER_RUN = 3
#: 单 run 累计返回的正文字节上限（60KB）
MAX_SKILL_BYTES_PER_RUN = 60 * 1024
#: 预警线比例（60KB × 0.8 = 48KB）：到这条线开始提醒剩余额度，但仍然放行
WARN_RATIO = 0.8


@dataclass
class SkillLoadBudget:
    """单 run 的 Skill 加载账本。

    两个指标必须分开记：

    - **数量**按「不同的 Skill 数」计——否则分页读完一个 100KB 的 SKILL.md 会被
      当成 5 个 Skill 而误杀；
    - **体积**按「累计返回字节」计——否则反复读同一段就能绕过上下文预算。

    计量口径是**实际返回给模型的字节**，不是文件大小：`offset`/`limit` 分页读一个
    100KB 文件，累计必须是 100KB，也不能因为只读了片段就按整份文件先记 100KB。
    """

    max_skills: int = MAX_SKILLS_PER_RUN
    max_bytes: int = MAX_SKILL_BYTES_PER_RUN
    #: skill 标识（沙箱内目录，如 `skills/demo`）→ 累计返回字节
    loaded: dict[str, int] = field(default_factory=dict)
    #: 被硬拒绝的次数：用于发现「模型在硬撞配额」
    denied: int = 0

    @property
    def loaded_bytes(self) -> int:
        return sum(self.loaded.values())

    @property
    def loaded_skills(self) -> list[str]:
        return sorted(self.loaded)

    def charge(self, key: str, size: int) -> ChargeDecision:
        """尝试为一读取入账，返回「放行 / 拒绝」及提示。

        拒绝时不改账本：这次读取没有返回任何正文，计入体积会让模型在
        「被拒 → 重试 → 被拒」的循环里把预算越撞越小。
        """
        size = max(0, int(size))
        known = key in self.loaded
        if not known and len(self.loaded) >= self.max_skills:
            self.denied += 1
            return ChargeDecision(
                allowed=False,
                reason="skill_count",
                message=(
                    f"本轮已加载 {len(self.loaded)} 个 Skill（上限 {self.max_skills} 个），"
                    "不能再加载新的 Skill 正文。"
                ),
            )
        if self.loaded_bytes + size > self.max_bytes:
            self.denied += 1
            return ChargeDecision(
                allowed=False,
                reason="skill_bytes",
                message=(
                    f"本轮 Skill 正文累计 {self.loaded_bytes} 字节，"
                    f"再读 {size} 字节会超过上限 {self.max_bytes} 字节。"
                ),
            )

        self.loaded[key] = self.loaded.get(key, 0) + size
        warn_at = int(self.max_bytes * WARN_RATIO)
        warning = ""
        if self.loaded_bytes >= warn_at:
            warning = (
                f"本轮 Skill 正文已累计 {self.loaded_bytes} 字节"
                f"（预警线 {warn_at}，上限 {self.max_bytes}）。"
                "后续请优先读片段或依赖摘要，避免触顶。"
            )
        return ChargeDecision(allowed=True, reason="", message=warning)

    def snapshot(self) -> dict[str, object]:
        """trace 用的快照。"""
        return {
            "loadedSkills": self.loaded_skills,
            "loadedBytes": self.loaded_bytes,
            "denied": self.denied,
        }


@dataclass(frozen=True)
class ChargeDecision:
    """一次入账的结论。

    `allowed=False` 时必须转成**结构化 tool error**而不是抛异常：模型据此能自救
    （改读片段、或直接依赖 L1 摘要），抛异常只会让整轮工具调用失败。
    """

    allowed: bool
    reason: str = ""
    message: str = ""


def new_skill_load_budget() -> SkillLoadBudget:
    return SkillLoadBudget()


#: 当前 run 的账本。用 ContextVar 而不是进程级 dict：
#: - 作用域天然等于 run 所在的 task（`asyncio.create_task` 会复制上下文），
#:   并发 run 之间互不可见；
#: - run 结束随 task 消失，不需要 TTL / 回收（进程级 dict 必须自己处理泄漏，
#:   而漏一次就是「上个 run 的额度被下个 run 继承」这种极难复现的 bug）；
#: - 代价：不绑定时不计量（单测直接调工具）。这是刻意的——预算不是安全边界，
#:   它的绑定点只有一处（`agentrun.execution.chat`），由测试钉住。
_CURRENT: ContextVar[SkillLoadBudget | None] = ContextVar(
    "edith_skill_load_budget", default=None
)


def bind_skill_load_budget(budget: SkillLoadBudget) -> None:
    """在 run 入口绑定账本（每次 run 覆盖，天然幂等）。"""
    _CURRENT.set(budget)


def current_skill_load_budget() -> SkillLoadBudget | None:
    return _CURRENT.get()


def reset_skill_load_budget() -> None:
    """仅测试使用：把当前上下文的账本清空。"""
    _CURRENT.set(None)


def charge_skill_load(key: str, size: int) -> ChargeDecision | None:
    """按当前 run 的账本入账；没有绑定账本时返回 None（不计量）。"""
    budget = _CURRENT.get()
    if budget is None:
        return None
    return budget.charge(key, size)


__all__ = [
    "MAX_SKILLS_PER_RUN",
    "MAX_SKILL_BYTES_PER_RUN",
    "WARN_RATIO",
    "ChargeDecision",
    "IndexPlan",
    "SkillLoadBudget",
    "bind_skill_load_budget",
    "charge_skill_load",
    "current_skill_load_budget",
    "index_budget_chars",
    "new_skill_load_budget",
    "plan_index",
    "reset_skill_load_budget",
]
