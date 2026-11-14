"""档位裁决：`Decision = decide(level, name, capabilities, arguments)`。

四个输入缺一不可，理由逐个说明：

- `level`：用户想让我做多少事（唯一可配置项）；
- `name`：诊断与提示要说清「哪个工具」；
- `capabilities`：**做什么**——能力是集合，判定是集合包含，不做大小比较；
- `arguments`：**这一次做什么**——`git status` 与 `git commit` 同名不同权，
  `sandbox_write_file` 写 `docs/x.md` 与写 `skills/x/SKILL.md` 不同权。

## 判定顺序（不可交换）

1. 参数能否解析 → 不能则拒绝（`invalid_arguments`）；
2. 工具是否有能力声明 → 没有则拒绝（`capability_unknown` / `tool_unknown`）；
3. 能力集合是否被当前档位允许 → 不允许时，审批档报 `approval_required`，
   其余报 `permission_denied`。

顺序本身就是策略：**先确认「我知不知道它要干什么」，再问「用户允不允许」。**
反过来会出现「一个未知工具因为档位是 full 而被放行」——那等于未知即授权。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import catalog
from .types import (
    APPROVAL_REQUIRED,
    DEFAULT_LEVEL,
    FALLBACK_LEVEL,
    INVALID_ARGUMENTS,
    LEVEL_ORDER,
    PERMISSION_DENIED,
    Capability,
    PermissionLevel,
    kind_is_retryable,
)

#: 每个档位允许的能力集合。「审批」在 P1 与「只读」放行面相同——差别只在
#: 拒绝的**理由**（`approval_required` vs `permission_denied`），因为 P1 还没有
#: 审批基础设施。P2 接入后只改这张表，不改判定逻辑与错误体系。
LEVEL_ALLOWED: dict[PermissionLevel, frozenset[Capability]] = {
    PermissionLevel.READ_ONLY: frozenset({Capability.READ, Capability.METADATA}),
    PermissionLevel.APPROVAL: frozenset({Capability.READ, Capability.METADATA}),
    PermissionLevel.FULL: frozenset(Capability),
}

LEVEL_LABELS = {
    PermissionLevel.READ_ONLY: "只读",
    PermissionLevel.APPROVAL: "审批",
    PermissionLevel.FULL: "完全",
}

#: 拒绝原因（写进结果与 trace，供排障与矩阵核对）
REASON_LEVEL_DENIED = "level_denied"
REASON_APPROVAL_REQUIRED = "approval_required"
REASON_CAPABILITY_UNKNOWN = "capability_unknown"
REASON_TOOL_UNKNOWN = "tool_unknown"
REASON_INVALID_ARGUMENTS = "invalid_arguments"
REASON_LEVEL_UNKNOWN = "level_unknown"


@dataclass(frozen=True)
class Verdict:
    """一次工具调用的裁决结果。"""

    allowed: bool
    level: PermissionLevel
    needed: frozenset[Capability] = field(default_factory=frozenset)
    kind: str = ""
    reason: str = ""
    tool: str = ""

    def error_payload(self) -> str:
        """拒绝时返回给模型的工具结果（结构化、可自救且明确「重试无用」）。"""
        return json.dumps(
            {
                "error": _message(self),
                "kind": self.kind,
                "retryable": kind_is_retryable(self.kind),
                "level": str(self.level),
                "needed": sorted(str(cap) for cap in self.needed),
                "reason": self.reason,
                "suggestion": _suggestion(self),
            },
            ensure_ascii=False,
        )


def _caps_text(caps: frozenset[Capability]) -> str:
    return "、".join(sorted(str(cap) for cap in caps)) or "未知"


def _message(verdict: Verdict) -> str:
    label = LEVEL_LABELS.get(verdict.level, str(verdict.level))
    if verdict.reason == REASON_TOOL_UNKNOWN:
        return f"工具 {verdict.tool} 不存在，已拒绝执行"
    if verdict.reason == REASON_CAPABILITY_UNKNOWN:
        return (
            f"工具 {verdict.tool} 没有声明能力，无法判断它的副作用，已按默认拒绝处理"
        )
    if verdict.reason == REASON_INVALID_ARGUMENTS:
        return f"工具 {verdict.tool} 的参数不是合法 JSON，已拒绝执行"
    if verdict.reason in (REASON_APPROVAL_REQUIRED,):
        return (
            f"当前档位「{label}」要求人工确认才能执行 {verdict.tool}"
            f"（需要能力：{_caps_text(verdict.needed)}），而审批功能尚未启用"
        )
    if verdict.reason == REASON_LEVEL_UNKNOWN:
        return (
            "权限档位配置值非法，已按最严档位（只读）处理；"
            f"因此 {verdict.tool} 被拒绝"
        )
    return (
        f"当前档位「{label}」不允许执行 {verdict.tool}"
        f"（需要能力：{_caps_text(verdict.needed)}）"
    )


def _suggestion(verdict: Verdict) -> str:
    if verdict.reason == REASON_TOOL_UNKNOWN:
        return "这个工具名不存在，重试无效。请只调用本轮工具列表里出现的工具。"
    if verdict.reason == REASON_CAPABILITY_UNKNOWN:
        return "这是平台的能力声明缺失，重试无效。请改用其它工具，或告知用户。"
    if verdict.reason == REASON_INVALID_ARGUMENTS:
        return "请重新生成这次调用，确保 arguments 是合法 JSON 对象。"
    if verdict.reason == REASON_APPROVAL_REQUIRED:
        return (
            "不要重试。请把要执行的动作、目标路径和预期结果写清楚告诉用户，"
            "由用户自己执行或调整权限档位。"
        )
    return (
        "这是用户的权限设置，不是你的参数问题，重试无效。"
        "请说明你需要做什么、为什么需要，由用户决定是否调整档位。"
    )


def parse_level(raw: object) -> tuple[PermissionLevel, bool]:
    """解析档位字符串，返回 (档位, 是否为非法值)。

    空值 → 默认档位（未配置）；非法值 → **最严档位**。把非法值当默认值处理，
    等于「改坏一个配置字段就能提权」。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return DEFAULT_LEVEL, False
    try:
        return PermissionLevel(text), False
    except ValueError:
        return FALLBACK_LEVEL, True


def min_level(left: PermissionLevel, right: PermissionLevel) -> PermissionLevel:
    """取更严的一档（序：read_only < approval < full）。"""
    return left if LEVEL_ORDER.index(left) <= LEVEL_ORDER.index(right) else right


def resolve_level(current_raw: object, ceiling_raw: object = "") -> PermissionLevel:
    """算出一次 run 的生效档位。

    `current_raw`：用户当前设置。
    `ceiling_raw`：本次执行的额外上限（定时任务创建时的快照）。

    为什么取更严的一档而不是直接信快照：
    - 只看当前值 → 「改一次全局设置」等于给所有历史任务批量重新授权，不用经过
      任何准入检查；
    - 只看快照 → 用户把档位调低之后，定时任务成了这条意图的例外。
    """
    current, _ = parse_level(current_raw)
    ceiling_text = str(ceiling_raw or "").strip()
    if not ceiling_text:
        return current
    ceiling, _ = parse_level(ceiling_text)
    return min_level(current, ceiling)


def allowed_capabilities(level: PermissionLevel) -> frozenset[Capability]:
    return LEVEL_ALLOWED.get(level, LEVEL_ALLOWED[FALLBACK_LEVEL])


def evaluate(
    level: PermissionLevel,
    name: str,
    capabilities: frozenset[Capability] | None,
    *,
    registered: bool | None = None,
    level_was_invalid: bool = False,
) -> Verdict:
    """裁决一次工具调用。`capabilities is None` 表示未声明 → 拒绝。"""
    if capabilities is None:
        reason = (
            REASON_TOOL_UNKNOWN if registered is False else REASON_CAPABILITY_UNKNOWN
        )
        return Verdict(
            allowed=False,
            level=level,
            kind=PERMISSION_DENIED,
            reason=reason,
            tool=name,
        )
    allowed = allowed_capabilities(level)
    if capabilities <= allowed:
        return Verdict(allowed=True, level=level, needed=capabilities, tool=name)
    reason = (
        REASON_LEVEL_UNKNOWN
        if level_was_invalid
        else (
            REASON_APPROVAL_REQUIRED
            if level is PermissionLevel.APPROVAL
            else REASON_LEVEL_DENIED
        )
    )
    kind = (
        APPROVAL_REQUIRED
        if reason == REASON_APPROVAL_REQUIRED
        else PERMISSION_DENIED
    )
    return Verdict(
        allowed=False,
        level=level,
        needed=capabilities,
        kind=kind,
        reason=reason,
        tool=name,
    )


def evaluate_call(
    level: PermissionLevel,
    name: str,
    arguments: dict | None,
    *,
    is_mcp: bool = False,
    registered: bool | None = None,
    arguments_invalid: bool = False,
    level_was_invalid: bool = False,
) -> Verdict:
    """按「档位 + 工具 + 能力 + 参数」裁决一次调用（执行层唯一入口）。"""
    if arguments_invalid:
        return Verdict(
            allowed=False,
            level=level,
            kind=INVALID_ARGUMENTS,
            reason=REASON_INVALID_ARGUMENTS,
            tool=name,
        )
    capabilities = catalog.capabilities_of(name, arguments, is_mcp=is_mcp)
    return evaluate(
        level,
        name,
        capabilities,
        registered=registered,
        level_was_invalid=level_was_invalid,
    )
