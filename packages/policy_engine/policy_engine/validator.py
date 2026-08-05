"""语义验证器（Schema 层由 Pydantic 承担，这里是第二层）。

输出结构化错误码 —— Agent 的修复循环靠错误码而不是自然语言，
这也是评测中"参数正确率/修复成功率"可度量的前提。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dsl import Policy, WetSensorCountCondition, ZoneInCondition


@dataclass(frozen=True)
class Inventory:
    """验证所需的资源清单快照（由 ZoneService/EmployeeService 提供）。"""

    zone_ids: frozenset[int]
    sensor_ids: frozenset[int]
    roles_present: frozenset[str]  # 当前系统里真实存在员工的角色


@dataclass
class ValidationIssue:
    code: str  # e.g. "E_UNKNOWN_ZONE"
    path: str  # e.g. "conditions[1].zone_ids"
    message: str  # 面向修复循环的、可执行的说明
    hint: str | None = None  # 可选：合法取值提示


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


MIN_COOLDOWN_WITH_NOTIFY_S = 300  # 含通知动作的策略,冷却下限更高,防邮件风暴


def validate(policy: Policy, inv: Inventory) -> ValidationResult:
    r = ValidationResult()

    # 1. 引用存在性 -------------------------------------------------------- #
    for i, cond in enumerate(policy.conditions):
        if isinstance(cond, ZoneInCondition):
            unknown = set(cond.zone_ids) - inv.zone_ids
            if unknown:
                r.issues.append(
                    ValidationIssue(
                        code="E_UNKNOWN_ZONE",
                        path=f"conditions[{i}].zone_ids",
                        message=f"未知 zone id: {sorted(unknown)}",
                        hint=f"合法取值: {sorted(inv.zone_ids)}",
                    )
                )

    for i, action in enumerate(policy.actions):
        role = getattr(action, "target_role", None)
        if role is not None and role not in inv.roles_present:
            r.issues.append(
                ValidationIssue(
                    code="E_ROLE_NOT_STAFFED",
                    path=f"actions[{i}].target_role",
                    message=f"角色 {role!r} 当前无在册员工，通知将无人接收",
                    hint=f"有人的角色: {sorted(inv.roles_present)}",
                )
            )

    # 2. 逻辑合理性 -------------------------------------------------------- #
    has_notify = any(a.type == "notify" for a in policy.actions)
    if has_notify and policy.cooldown_s < MIN_COOLDOWN_WITH_NOTIFY_S:
        r.issues.append(
            ValidationIssue(
                code="E_COOLDOWN_TOO_SHORT",
                path="cooldown_s",
                message=(
                    f"包含通知动作时 cooldown_s 不得低于 {MIN_COOLDOWN_WITH_NOTIFY_S}"
                ),
            )
        )

    seen: set[str] = set()
    for i, action in enumerate(policy.actions):
        key = action.model_dump_json()
        if key in seen:
            r.issues.append(
                ValidationIssue(
                    code="E_DUPLICATE_ACTION",
                    path=f"actions[{i}]",
                    message="重复的动作",
                )
            )
        seen.add(key)

    for i, cond in enumerate(policy.conditions):
        if (
            isinstance(cond, WetSensorCountCondition)
            and cond.op == "<="
            and cond.value >= len(inv.sensor_ids)
        ):
            r.issues.append(
                ValidationIssue(
                    code="E_ALWAYS_TRUE_CONDITION",
                    path=f"conditions[{i}]",
                    message="条件恒为真，策略将对每个触发事件生效",
                )
            )

    # TODO(W3): 跨策略冲突检测（同触发同区域的已发布策略动作互斥性）
    # TODO(W3): 自触发环检测（action 引发的状态变更能否再次命中本策略的 trigger）
    return r
