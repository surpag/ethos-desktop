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

#: 审批的四种「没有拿到许可」——都是拒绝，但对模型说的话完全不同：
#: 前两个是「用户说了不行」，第三个是「这次运行被取消了」，第四个是「没人可问」。
REASON_APPROVAL_DENIED = "approval_denied"
REASON_APPROVAL_TIMEOUT = "approval_timeout"
REASON_APPROVAL_CANCELED = "approval_canceled"
REASON_APPROVAL_UNAVAILABLE = "approval_unavailable"


@dataclass(frozen=True)
class Verdict:
    """一次工具调用的裁决结果。"""

    allowed: bool
    level: PermissionLevel
    needed: frozenset[Capability] = field(default_factory=frozenset)
    kind: str = ""
    reason: str = ""
    tool: str = ""

    def granted(self) -> Verdict:
        """用户批准之后的那份裁决：只解除档位裁决，不改任何其它字段。

        `needed` / `level` 原样保留——它们是这次批准**覆盖了哪些能力**的记录，
        抹掉就等于丢掉审计线索。特别地，这里**不**触碰安全约束：路径检查不在
        这一层，批准也不会让它消失（见 docs/permissions-p2-approval.md §5）。
        """
        return Verdict(
            allowed=True,
            level=self.level,
            needed=self.needed,
            kind="",
            reason="",
            tool=self.tool,
        )

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
            f"（需要能力：{_caps_text(verdict.needed)}），本次执行没有完成确认"
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
            "不要原样重试。这次操作需要用户当场确认，而确认没有完成："
            "请把要执行的动作、目标路径和预期结果写清楚告诉用户，由用户决定。"
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


class InvalidLevelValue(ValueError):
    """写入侧的档位取值非法。"""

    def __init__(self, raw: object) -> None:
        self.raw = str(raw or "")
        super().__init__(f"invalid permission level: {self.raw!r}")


def parse_level_input(raw: object) -> str:
    """**写**侧严格校验：返回规范化字符串，空串表示「清除本作用域的限制」。

    与 `parse_level` 的分工必须分清，否则两处会互相污染：

    - `parse_level` 是**读**侧的容错解析：库里的值可能是历史脏数据，遇到非法值
      降级到最严档（fail-closed），因为「读不懂」不能变成「放行」；
    - 这里是**写**侧：值来自本次请求，非法就拒绝。静默改成最严档等于骗人——
      用户选了「完全」，存进去的是「只读」，界面显示和行为不一致，他会以为
      自己没点对，然后反复点。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    try:
        return str(PermissionLevel(text))
    except ValueError as exc:
        raise InvalidLevelValue(raw) from exc


@dataclass(frozen=True)
class Resolution:
    """一次 run 的档位解析结果：**生效值 + 它是被谁压下来的**。

    `binding` 不是装饰：界面上「本次会话选了完全，为什么实际是只读」必须有答案，
    否则用户只会认为开关坏了，然后去关掉它。
    """

    level: PermissionLevel
    #: 参与解析的来源（名称 → 原始值）。含空值，便于判断「未设置」与「设成了空」。
    sources: tuple[tuple[str, str], ...] = ()
    #: 把档位压到 `level` 的来源名；空 = 没有任何来源在收紧（生效值就是默认值）。
    binding: str = ""
    #: 值非法、已被按最严档处理的来源名。
    invalid: tuple[str, ...] = ()
    #: 读取失败的来源名（DB 异常等）。与 `invalid` 分开：前者是「库里的值坏了」，
    #: 这里是「根本没读到」，两种故障的排查方向完全不同。
    errors: tuple[str, ...] = ()


def resolve_chain(*, user: object = "", ceiling: object = "") -> Resolution:
    """把用户级设置与本次执行上限折成生效档位：**取最严者**。

    两个来源刻意用显式关键字参数而不是 `**sources`：拼错的来源名应当立刻
    `TypeError`，而不是被静默忽略掉——这条链是每轮 run 都跑的，一个打错的
    kwarg 静默等于「上限永远不生效」，而它在界面上看不出任何异常。

    为什么取最严而不是「后者胜」（这条是本节唯一的策略判断）：

    - `user` 是用户**此刻**的意图，`ceiling` 是一次**授权记录**（定时任务创建时
      用户允许它跑到几档）。授权记录不能比当前设置更宽：否则用户把档位调低之后，
      历史定时任务成了例外——而例外恰恰在没人盯着的时候跑；
    - 反方向是安全的：用户放宽之后，旧的严格快照仍会压住它，直到用户重新创建
      任务。这是刻意的不便：自动执行宁可少做，不可多做。

    没有第三、第四个来源：档位是「我允许 Agent 对我的机器做什么」，这个问题只有
    一个回答者（用户设置）。工作区级 / 会话级上限曾经做过，已回退，见
    `docs/archive/permissions-p3-scopes.md`。

    空值 = 该来源未设置 = 不参与收紧（等价于默认档）。非法值 = 最严档
    （`parse_level` 的既有约定：改坏一个字段不能提权）。
    """
    pairs = (
        ("user", user),
        ("ceiling", ceiling),
    )
    parsed: list[tuple[str, str, PermissionLevel]] = []
    invalid: list[str] = []
    for name, raw in pairs:
        text = str(raw or "").strip()
        source_level, was_invalid = parse_level(text)
        if was_invalid:
            invalid.append(name)
        parsed.append((name, text, source_level))

    level = DEFAULT_LEVEL
    for _, _, source_level in parsed:
        level = min_level(level, source_level)

    # binding 只在**确实有来源把它压下来**时才有值：全员等于默认档时没有元凶。
    binding = ""
    if level != DEFAULT_LEVEL:
        for name, _, source_level in parsed:
            if source_level == level:
                binding = name
                break
    return Resolution(
        level=level,
        sources=tuple((name, text) for name, text, _ in parsed),
        binding=binding,
        invalid=tuple(invalid),
    )


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


#: 四种「没拿到许可」的措辞。分开写而不是一个模板，因为模型要靠它决定下一步：
#: 被拒 = 换个做法；超时/取消 = 报告现状；没人可问 = 说明这是自动任务。
_APPROVAL_TEXT: dict[str, tuple[str, str]] = {
    REASON_APPROVAL_DENIED: (
        "用户拒绝了这次操作，{tool} 未执行",
        "不要重试同一个调用。请问用户希望怎么改，或改用不需要该权限的做法。",
    ),
    REASON_APPROVAL_TIMEOUT: (
        "等待用户确认超时（{tool} 未执行）",
        "用户可能没看到确认请求。请先汇报当前进展，并说明还需要这一步才能继续。",
    ),
    REASON_APPROVAL_CANCELED: (
        "等待确认期间本次运行被取消，{tool} 未执行",
        "本次运行已结束，不要再发起该操作。",
    ),
    REASON_APPROVAL_UNAVAILABLE: (
        "这是无人值守的自动执行，{tool} 需要用户当场确认，因此未执行",
        "自动任务不能弹确认框。请改用不需要写入/执行的方案；"
        "确实需要时，请让用户手动执行或把该任务的权限档位调高。",
    ),
}


def approval_refusal(verdict: Verdict, reason: str) -> str:
    """没拿到许可时返回给模型的工具结果。

    `kind` 恒为 `permission_denied`：四种原因都是**确定性**拒绝，重试同一个调用
    必然还是同样的结局，因此不计熔断（`kind_counts_toward_breaker` 只认
    `tool_failure`）。原因放在 `reason` 里，让模型读到「为什么」而不是「不许」。
    """
    message, suggestion = _APPROVAL_TEXT.get(
        reason, ("{tool} 未执行：没有拿到用户确认", "不要重试同一个调用。")
    )
    return json.dumps(
        {
            "error": message.format(tool=verdict.tool),
            "kind": PERMISSION_DENIED,
            "retryable": False,
            "level": str(verdict.level),
            "needed": sorted(str(cap) for cap in verdict.needed),
            "reason": reason,
            "suggestion": suggestion,
        },
        ensure_ascii=False,
    )
