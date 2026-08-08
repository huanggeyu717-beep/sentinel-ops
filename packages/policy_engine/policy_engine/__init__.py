"""policy_engine —— Policy DSL v1 + 双层验证器 + 确定性引擎 (SPEC-001)。

零 IO 包 (CLAUDE.md 不变量 2): 纯函数吃事件流, 执行器与模拟器是同一份 evaluate()。
装载场景文件归 packages/scenario; 数据库归 services/ 层。
"""
from .dsl import (
    ACTION_APPROVAL_CLASS,
    ACTION_REQUIRED_CONTEXT,
    TRIGGER_CONTEXT,
    Policy,
    action_type_whitelist,
    policy_json_schema,
    trigger_type_whitelist,
)
from .engine import (
    Effect,
    EffectSubject,
    EngineState,
    Event,
    LoadedPolicy,
    SkippedAction,
    evaluate,
    wet_sensor_count_now,
)
from .replay import (
    DEFAULT_TAIL_S,
    DEFAULT_TICK_SECONDS,
    ReplayReport,
    ReplayWarning,
    replay,
)
from .validator import Inventory, ValidationIssue, ValidationResult, validate

__all__ = [
    "ACTION_APPROVAL_CLASS",
    "ACTION_REQUIRED_CONTEXT",
    "DEFAULT_TAIL_S",
    "DEFAULT_TICK_SECONDS",
    "TRIGGER_CONTEXT",
    "Effect",
    "EffectSubject",
    "EngineState",
    "Event",
    "Inventory",
    "LoadedPolicy",
    "Policy",
    "ReplayReport",
    "ReplayWarning",
    "SkippedAction",
    "ValidationIssue",
    "ValidationResult",
    "action_type_whitelist",
    "evaluate",
    "policy_json_schema",
    "replay",
    "trigger_type_whitelist",
    "validate",
    "wet_sensor_count_now",
]
