"""共享夹具: 路径与策略构造器。

测试读 YAML/CSV 都调 packages/scenario 的 loader (那个包允许读文件),
读出来的事件列表再交给 replay —— policy_engine 是零 IO 包, 这条边界不破。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from policy_engine import LoadedPolicy, Policy

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO_ROOT / "scenarios"
SEED_CSV = REPO_ROOT / "apps" / "device-sim" / "seed" / "waterlevel_readings.csv"


def make_policy(**overrides: Any) -> dict[str, Any]:
    """SPEC-001 验收 5 的示例升级策略, 逐字段可覆盖。"""
    base: dict[str, Any] = {
        "scope": {"type": "zone", "ids": [1]},
        "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 120},
        "conditions": [
            {
                "type": "wet_sensor_count",
                "count_within": "same_zone",
                "op": ">=",
                "value": 2,
                "window_s": 180,
            }
        ],
        "actions": [
            {"type": "notify", "channel": "email", "target_role": "manager"},
            {"type": "set_led", "target": "incident_device", "state": "ON"},
        ],
        "cooldown_s": 600,
    }
    base.update(overrides)
    return base


def opener_policy(**overrides: Any) -> dict[str, Any]:
    """前置策略: 传感器变湿就开事故 (验收 5 的前置)。

    scope 用 sensor 分桶: 场景里 1 号 t=5s、2 号 t=40s 相继变湿, 间隔 35s
    小于 cooldown_s 的下限 60s —— zone/global 分桶会把第二次开事故吞掉,
    事故 2 就不存在了, 验收 6 的对照无从谈起。
    """
    base: dict[str, Any] = {
        "scope": {"type": "sensor", "ids": [1, 2]},
        "trigger": {"type": "sensor_state_changed", "to": "WET"},
        "conditions": [],
        "actions": [{"type": "open_incident", "severity": "normal"}],
        "cooldown_s": 60,
    }
    base.update(overrides)
    return base


def loaded(body: dict[str, Any], policy_id: int = 1, version: int = 1) -> LoadedPolicy:
    return LoadedPolicy(
        policy_id=policy_id, version=version, body=Policy.model_validate(body)
    )
