"""第一层: 静态验证器 (SPEC-001 第五节)。

Schema 层由 Pydantic 承担 (白名单外的 type 直接失败), 这一层是**语义**检查。
输出结构化错误码 + path + message + hint —— Agent 的修复循环靠错误码而不是
自然语言, 这也是 W5 评测里"参数正确率/修复成功率"可度量的前提。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .dsl import (
    ACTION_REQUIRED_CONTEXT,
    TRIGGER_CONTEXT,
    IncidentElapsedTrigger,
    OpenIncidentAction,
    Policy,
    Scope,
    WetSensorCountCondition,
)


@dataclass(frozen=True)
class Inventory:
    """验证所需的资源快照, 由 service 层提供 (引擎零 IO, 不自己查库)。

    roles_present 的事实源定死为 user_roles ("当前有哪些角色下挂着账号")。
    不能用 employees.role: 那一列是无约束的自由文本, 与 roles 表没有任何关联,
    拿它比对必然对不上 —— 那正是"三套角色名"问题的根源 (SPEC-001 第五节)。
    """

    zone_ids: frozenset[int]
    sensor_ids: frozenset[int]
    sensor_zone: dict[int, int]  # sensor_id -> zone_id, same_zone 检查要用
    roles_present: frozenset[str]


@dataclass
class ValidationIssue:
    code: str  # e.g. "E_UNKNOWN_ZONE"
    path: str  # e.g. "scope.ids"
    message: str  # 面向修复循环的、可执行的说明
    hint: str | None = None  # 合法取值提示


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


MIN_COOLDOWN_WITH_NOTIFY_S = 300  # 含通知动作的策略, 冷却下限更高, 防邮件风暴


def validate(policy: Policy, inv: Inventory) -> ValidationResult:
    r = ValidationResult()
    _check_scope(policy.scope, inv, r)
    _check_actions(policy, inv, r)
    _check_conditions(policy, inv, r)
    _check_context(policy, r)
    _check_self_trigger_loop(policy, r)
    return r


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #


def _check_scope(scope: Scope, inv: Inventory, r: ValidationResult) -> None:
    if scope.type == "global":
        if scope.ids:
            r.issues.append(
                ValidationIssue(
                    code="E_SCOPE_IDS_MISMATCH",
                    path="scope.ids",
                    message="type=global 时 ids 必须为空",
                    hint="去掉 ids, 或把 type 改成 zone/sensor",
                )
            )
        return
    if not scope.ids:
        r.issues.append(
            ValidationIssue(
                code="E_SCOPE_IDS_MISMATCH",
                path="scope.ids",
                message=f"type={scope.type} 时 ids 必须非空 (最多 16 个)",
                hint=(
                    f"合法取值: "
                    f"{sorted(inv.zone_ids if scope.type == 'zone' else inv.sensor_ids)}"
                ),
            )
        )
        return
    if scope.type == "zone":
        unknown = set(scope.ids) - inv.zone_ids
        if unknown:
            r.issues.append(
                ValidationIssue(
                    code="E_UNKNOWN_ZONE",
                    path="scope.ids",
                    message=f"未知 zone id: {sorted(unknown)}",
                    hint=f"合法取值: {sorted(inv.zone_ids)}",
                )
            )
    else:  # sensor
        unknown = set(scope.ids) - inv.sensor_ids
        if unknown:
            r.issues.append(
                ValidationIssue(
                    code="E_UNKNOWN_SENSOR",
                    path="scope.ids",
                    message=f"未知 sensor id: {sorted(unknown)}",
                    hint=f"合法取值: {sorted(inv.sensor_ids)}",
                )
            )


def _check_actions(policy: Policy, inv: Inventory, r: ValidationResult) -> None:
    for i, action in enumerate(policy.actions):
        role = getattr(action, "target_role", None)
        if role is not None and role not in inv.roles_present:
            r.issues.append(
                ValidationIssue(
                    code="E_ROLE_NOT_STAFFED",
                    path=f"actions[{i}].target_role",
                    message=f"角色 {role!r} 当前没有任何账号, 通知将无人接收",
                    hint=f"有账号的角色: {sorted(inv.roles_present)}",
                )
            )

    has_notify = any(a.type == "notify" for a in policy.actions)
    if has_notify and policy.cooldown_s < MIN_COOLDOWN_WITH_NOTIFY_S:
        r.issues.append(
            ValidationIssue(
                code="E_COOLDOWN_TOO_SHORT",
                path="cooldown_s",
                message=(
                    f"包含通知动作时 cooldown_s 不得低于 {MIN_COOLDOWN_WITH_NOTIFY_S}"
                ),
                hint=f"把 cooldown_s 提到 {MIN_COOLDOWN_WITH_NOTIFY_S} 以上",
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
                    message="动作数组里有完全相同的两项",
                    hint="删掉重复的那一项",
                )
            )
        seen.add(key)


def _check_conditions(policy: Policy, inv: Inventory, r: ValidationResult) -> None:
    for i, cond in enumerate(policy.conditions):
        if not isinstance(cond, WetSensorCountCondition):
            continue
        upper = _wet_count_upper_bound(cond, policy.scope, inv)
        if cond.op == "<=" and cond.value >= upper:
            r.issues.append(
                ValidationIssue(
                    code="E_ALWAYS_TRUE_CONDITION",
                    path=f"conditions[{i}]",
                    message=(
                        f"条件恒为真: 计数上限是 {upper} "
                        f"(count_within={cond.count_within}, 按 scope 内单区最大传感器数算), "
                        f"count <= {cond.value} 永远成立"
                    ),
                    hint=f"把 value 降到 {upper} 以下, 或改用 >=",
                )
            )


def _wet_count_upper_bound(
    cond: WetSensorCountCondition, scope: Scope, inv: Inventory
) -> int:
    """count_within=same_zone 时该比的是**该区的**传感器数, 不是全部
    (SPEC-001 第五节对既有缺陷的修正)。"""
    if cond.count_within == "any_zone":
        return len(inv.sensor_ids)
    per_zone = Counter(inv.sensor_zone.values())
    zones: set[int]
    if scope.type == "zone":
        zones = set(scope.ids)
    elif scope.type == "sensor":
        zones = {inv.sensor_zone[s] for s in scope.ids if s in inv.sensor_zone}
    else:
        zones = set(per_zone)
    return max((per_zone[z] for z in zones), default=0)


def _check_context(policy: Policy, r: ValidationResult) -> None:
    """E_CONTEXT_UNAVAILABLE: 对照 trigger 的"必定提供/条件性提供"两列与
    action 的必需上下文。判定规则写死 (SPEC-001 第二节):
    落在"必定提供" → 通过; 落在"条件性提供" → 静态通过, 运行时为空则该 Effect
    不产出并记 skipped: missing_context; 两列都没有 → 在提交草稿那一刻拦住。"""
    always, conditional = TRIGGER_CONTEXT[policy.trigger.type]
    available = always | conditional
    for i, action in enumerate(policy.actions):
        missing = ACTION_REQUIRED_CONTEXT[action.type] - available
        if missing:
            r.issues.append(
                ValidationIssue(
                    code="E_CONTEXT_UNAVAILABLE",
                    path=f"actions[{i}]",
                    message=(
                        f"动作 {action.type} 需要 {sorted(missing)}, "
                        f"而 trigger {policy.trigger.type} 提供不了 —— "
                        f"这条策略在运行时必然无事可做"
                    ),
                    hint=(
                        f"{policy.trigger.type} 必定提供 {sorted(always)}, "
                        f"条件性提供 {sorted(conditional)}; "
                        f"换一个能提供所缺上下文的 trigger, 或换动作"
                    ),
                )
            )


def _check_self_trigger_loop(policy: Policy, r: ValidationResult) -> None:
    """E_SELF_TRIGGER_LOOP: open_incident 会产生 incident_opened 事件, 若本策略的
    trigger 是 incident_elapsed, 就构成自触发环。v1 只做这一层直接环检测;
    跨策略的间接环放 SPEC-006 的发布前检查 (需要读其它已发布策略, 超出纯函数边界)。"""
    if not isinstance(policy.trigger, IncidentElapsedTrigger):
        return
    for i, action in enumerate(policy.actions):
        if isinstance(action, OpenIncidentAction):
            r.issues.append(
                ValidationIssue(
                    code="E_SELF_TRIGGER_LOOP",
                    path=f"actions[{i}]",
                    message=(
                        "open_incident 产生的 incident_opened 会再次唤醒 "
                        "incident_elapsed 触发的本策略, 构成自触发环"
                    ),
                    hint="事故由另一条策略开 (如 sensor_state_changed 触发), 本策略只做升级/通知",
                )
            )
