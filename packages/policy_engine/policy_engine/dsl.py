"""Policy DSL v1 — Pydantic 模型 (SPEC-001 第二节)。

设计不变量:
1. 所有 type 字段为 Literal 白名单, 模型在语法层面就说不出白名单以外的话。
2. `name` 不在 DSL 里 (归 policies 表那一列); `requires_approval` 不在 DSL 里
   (由服务端从动作分级推导, 永不信任模型输入, CLAUDE.md 不变量 5)。
   所有模型 extra="forbid", 模型自作主张塞字段在 Schema 层直接失败。
3. DSL 的能力边界 == 模拟器可验证边界。新增 trigger/condition/action 必须同时提交
   schema、语义校验、引擎实现、场景包用例 (CLAUDE.md 不变量 3)。

v1 明确不做 (SPEC-001 非目标): time_window (无时区、跨午夜未定义、破坏可复现性)、
operator_on_duty (系统内无排班数据)、跨策略编排与优先级。
"""
from __future__ import annotations

import typing
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Scope: 这条策略管哪里
# --------------------------------------------------------------------------- #


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["global", "zone", "sensor"]
    # type=global 时必须为空; 其余必须非空 —— 属语义检查 (E_SCOPE_IDS_MISMATCH),
    # Schema 层只限制条数上限。
    ids: list[int] = Field(default_factory=list, max_length=16)


# --------------------------------------------------------------------------- #
# Triggers (四类)
# --------------------------------------------------------------------------- #


class SensorStateChangedTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["sensor_state_changed"]
    to: Literal["WET", "DRY"]


class DeviceOfflineTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["device_offline"]
    offline_for_s: int = Field(ge=30, le=3600)


class IncidentElapsedTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["incident_elapsed"]
    in_status: Literal["open", "assigned", "acknowledged"]
    for_s: int = Field(ge=30, le=7200)


class SensorDryForTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["sensor_dry_for"]
    dry_for_s: int = Field(ge=60, le=7200)


Trigger = Annotated[
    SensorStateChangedTrigger
    | DeviceOfflineTrigger
    | IncidentElapsedTrigger
    | SensorDryForTrigger,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
# Conditions (两类, 刻意少而完整)
# --------------------------------------------------------------------------- #


class WetSensorCountCondition(BaseModel):
    """语义定死为读法甲 (SPEC-001 第二节): 统计此刻正湿着的传感器数量,
    且这些传感器的变湿时刻必须落在同一个 window_s 窗口内。
    count_within=same_zone 时只数与触发对象同区的传感器。
    (顶层"管哪些地方"归 Policy.scope, 与这里的 count_within 是两个层次。)
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["wet_sensor_count"]
    count_within: Literal["same_zone", "any_zone"]
    op: Literal[">=", "==", "<="]
    value: int = Field(ge=1, le=32)
    window_s: int = Field(ge=10, le=3600)


class IncidentUnacknowledgedCondition(BaseModel):
    """关联事故已处于 open 或 assigned (即无人 acknowledge) 超过 duration_s。"""

    model_config = ConfigDict(extra="forbid")
    type: Literal["incident_unacknowledged"]
    duration_s: int = Field(ge=30, le=7200)


Condition = Annotated[
    WetSensorCountCondition | IncidentUnacknowledgedCondition,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
# Actions (五类)
# --------------------------------------------------------------------------- #


class OpenIncidentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["open_incident"]
    severity: Literal["normal", "high", "critical"]


class CloseIncidentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["close_incident"]


class EscalateIncidentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["escalate_incident"]
    to_severity: Literal["high", "critical"]


class NotifyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["notify"]
    channel: Literal["email"]
    # 只允许 roles 表那四个值, 不接受自由字符串 —— 防注入外发;
    # "通知本区的 manager"由 scope 表达 (scope 是 1 区 + 目标是 manager)。
    target_role: Literal["viewer", "operator", "manager", "admin"]


class SetLedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["set_led"]
    target: Literal["incident_device"]
    state: Literal["ON", "OFF"]


Action = Annotated[
    OpenIncidentAction
    | CloseIncidentAction
    | EscalateIncidentAction
    | NotifyAction
    | SetLedAction,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
# 动作分级与上下文表 (验证器与服务端的依据, 不进 DSL)
# --------------------------------------------------------------------------- #

# 审批分级: v1 一律 manager 审批, 门槛不因分级而变; 分级只用于审批界面标红提示
# "这条策略会往外发邮件"。键集合必须与动作白名单完全相等 (有一致性断言测试守着)。
ACTION_APPROVAL_CLASS: dict[str, str] = {
    "open_incident": "internal_write",
    "close_incident": "internal_write",
    "escalate_incident": "internal_write",
    "notify": "external_side_effect",
    "set_led": "external_side_effect",
}

# trigger -> (必定提供, 条件性提供) 的上下文字段 (SPEC-001 第二节 trigger 表)。
# 这两列是有约束力的: 静态验证器的 E_CONTEXT_UNAVAILABLE 按它判;
# 引擎运行时若"条件性提供"的字段为空, 该 Effect 不产出并记 skipped: missing_context。
TRIGGER_CONTEXT: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "sensor_state_changed": (
        frozenset({"sensor_id", "zone_id", "device_id"}),
        frozenset({"incident_id"}),
    ),
    "device_offline": (frozenset({"device_id"}), frozenset({"zone_id"})),
    "incident_elapsed": (
        frozenset({"incident_id", "sensor_id", "zone_id", "device_id"}),
        frozenset(),
    ),
    "sensor_dry_for": (
        frozenset({"sensor_id", "zone_id", "device_id"}),
        frozenset({"incident_id"}),
    ),
}

# action -> 必需上下文 (SPEC-001 第二节动作表)。notify 刻意为空:
# 它的投递目标是角色, "设备离线就通知管理员"这类策略才写得出来。
ACTION_REQUIRED_CONTEXT: dict[str, frozenset[str]] = {
    "open_incident": frozenset({"sensor_id", "zone_id"}),
    "close_incident": frozenset({"incident_id"}),
    "escalate_incident": frozenset({"incident_id"}),
    "notify": frozenset(),
    "set_led": frozenset({"device_id"}),
}


def _literal_types(union: Any) -> frozenset[str]:
    """从 Annotated[Union[...], Field] 里取各成员 type 字段的 Literal 值。"""
    members = typing.get_args(typing.get_args(union)[0])
    values: set[str] = set()
    for member in members:
        values.update(typing.get_args(member.model_fields["type"].annotation))
    return frozenset(values)


def action_type_whitelist() -> frozenset[str]:
    """动作类型白名单, 直接从 Action 联合的 Literal 取 —— 供一致性断言用,
    让"加了新动作却忘了补分级/上下文表"当场变红灯 (SPEC-001 第二节)。"""
    return _literal_types(Action)


def trigger_type_whitelist() -> frozenset[str]:
    """触发器类型白名单, 同上, 供 TRIGGER_CONTEXT 的一致性断言用。"""
    return _literal_types(Trigger)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=8)
    actions: list[Action] = Field(min_length=1, max_length=4)
    cooldown_s: int = Field(ge=60, le=86400)


def policy_json_schema() -> dict[str, Any]:
    """给 Agent prompt 和 contracts 用的 JSON Schema。"""
    return Policy.model_json_schema()
