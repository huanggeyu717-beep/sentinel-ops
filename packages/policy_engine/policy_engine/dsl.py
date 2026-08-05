"""Policy DSL v1 — Pydantic models.

设计不变量（见 docs/specs/SPEC-001-policy-dsl.md）：
1. 所有 type 字段为 Literal 白名单，模型无法生成白名单外的能力。
2. requires_approval 不属于 DSL —— 由服务端根据 action 分级推导，永不信任模型输入。
3. DSL 的能力边界 == 模拟器可验证的边界。新增任何 trigger/condition/action
   必须同时提交：schema、语义校验、引擎实现、至少一个场景包用例。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


class SensorStateChangedTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["sensor_state_changed"]
    to: Literal["WET", "DRY"]


class DeviceOfflineTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["device_offline"]
    offline_for_s: int = Field(ge=30, le=3600)


Trigger = Annotated[
    SensorStateChangedTrigger | DeviceOfflineTrigger,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #


class WetSensorCountCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wet_sensor_count"]
    scope: Literal["same_zone", "any_zone"]
    op: Literal[">=", "==", "<="]
    value: int = Field(ge=1, le=32)
    window_s: int = Field(ge=10, le=3600)


class IncidentUnacknowledgedCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["incident_unacknowledged"]
    duration_s: int = Field(ge=30, le=7200)


class TimeWindowCondition(BaseModel):
    """仅在每日 [start, end) 本地时间窗内生效。"""

    model_config = ConfigDict(extra="forbid")
    type: Literal["time_window"]
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class ZoneInCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["zone_in"]
    zone_ids: list[int] = Field(min_length=1, max_length=16)


Condition = Annotated[
    WetSensorCountCondition
    | IncidentUnacknowledgedCondition
    | TimeWindowCondition
    | ZoneInCondition,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


class NotifyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["notify"]
    channel: Literal["email"]
    # 只允许角色，不允许自由邮箱字符串 —— 防注入外发。
    target_role: Literal["zone_manager", "operator_on_duty", "admin"]


class SetLedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["set_led"]
    target: Literal["incident_device"]
    state: Literal["ON", "OFF"]


class EscalateIncidentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["escalate_incident"]
    to_severity: Literal["high", "critical"]


Action = Annotated[
    NotifyAction | SetLedAction | EscalateIncidentAction,
    Field(discriminator="type"),
]

# 服务端推导审批需求的依据（不进 DSL）
ACTION_APPROVAL_CLASS: dict[str, str] = {
    "notify": "external_side_effect",
    "set_led": "external_side_effect",
    "escalate_incident": "internal_write",
}

# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=120)
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=8)
    actions: list[Action] = Field(min_length=1, max_length=4)
    cooldown_s: int = Field(ge=60, le=86400)


def policy_json_schema() -> dict:
    """给 Agent prompt 和 contracts 用的 JSON Schema。"""
    return Policy.model_json_schema()
