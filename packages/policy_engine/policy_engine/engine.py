"""确定性策略引擎。

关键不变量：执行器与模拟器是同一份代码 —— evaluate() 只消费事件流与当前态，
不做任何 IO。线上执行喂 live 事件，模拟/评测喂场景包事件。
这保证 simulation 对生产行为有真实预测力，也让评测能用"行为等价"判分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal

from .dsl import Policy


@dataclass(frozen=True)
class Event:
    """规范化事件（ingest 层将原始 MQTT 消息格式转换为此结构）。"""

    ts_ms: int
    kind: Literal["sensor_state", "heartbeat", "rfid_scan"]
    device_id: str
    sensor_id: int | None = None
    zone_id: int | None = None
    state: str | None = None  # WET / DRY
    rfid_uid: str | None = None


@dataclass(frozen=True)
class Effect:
    """引擎输出的触发效果 —— 评测中的"行为轨迹"即 Effect 序列。"""

    ts_ms: int
    policy_name: str
    action_type: str
    detail: dict


@dataclass
class EngineState:
    """滑动窗口所需的最小状态。"""

    wet_since: dict[int, int] = field(default_factory=dict)  # sensor_id -> ts_ms
    last_fired: dict[str, int] = field(default_factory=dict)  # policy -> ts_ms
    unacked_incident_since: dict[int, int] = field(default_factory=dict)  # zone -> ts


def evaluate(
    policies: list[Policy],
    events: Iterator[Event],
    state: EngineState | None = None,
) -> Iterator[Effect]:
    """按时间序消费事件，产出 Effect 序列。

    W3 实现要点：
    1. trigger 匹配（边沿触发：DRY->WET，沿用原 automatic-alert Lambda 的语义）
    2. condition 求值（wet_sensor_count 用 state.wet_since 滑窗；
       incident_unacknowledged 用 unacked_incident_since）
    3. cooldown 用 state.last_fired 抑制
    4. 命中则 yield 每个 action 对应的 Effect
    """
    state = state or EngineState()
    raise NotImplementedError("W3: 见 docs/specs/SPEC-001-policy-dsl.md 验收用例")
