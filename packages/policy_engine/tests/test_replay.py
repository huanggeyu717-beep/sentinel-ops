"""动态验证验收 (SPEC-001 验收 8): 真实 CSV 回放出完整 ReplayReport,
触发过密给 W_HIGH_TRIGGER_RATE 且不阻断。CSV/YAML 的装载由 packages/scenario
的 loader 完成, 回放模块本身不碰文件系统。
"""
import pytest
from _helpers import SEED_CSV, loaded
from scenario import events_from_csv

from policy_engine import ReplayReport, replay

# 验收 8 的示例策略必须用不需要 zone 上下文的动作 (SPEC-001 验收 8 的已知限制):
# events_from_csv 产出的事件没有 zone_id (那一列不在遥测报文里, zone 的事实源是
# 数据库), 任何需要 zone 的动作 (open_incident) 在 CSV 回放上产出恒为 0、全落
# skipped —— 见下面的共现测试。给 CSV 补 zone 映射是第二段 service 层的事,
# 在那之前验收 8 不覆盖开事故链路。
WET_NOTIFY = {
    "scope": {"type": "global", "ids": []},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "notify", "channel": "email", "target_role": "manager"}],
    "cooldown_s": 300,
}


def _csv_report() -> ReplayReport:
    src = events_from_csv(SEED_CSV, "Arduino1")
    return replay([loaded(WET_NOTIFY, policy_id=7, version=2)], src.events,
                  source=src.name)


def test_csv_replay_full_report__344_real_readings():
    report = _csv_report()
    assert report.source == "replay:waterlevel_readings.csv"
    assert report.events_count >= 344  # 344 条读数 + loader 补的心跳
    assert report.span_s > 0
    assert report.tick_seconds == 10  # 复现所需的元数据
    assert report.tail_s == 600
    assert report.by_action_type.get("notify", 0) > 0
    assert all(e.action_type == "notify" for e in report.effects)
    assert all(e.policy_id == 7 and e.policy_version == 2 for e in report.effects)
    assert "样本" in report.data_note  # 数据规模的说明显式带上, 不藏


def test_high_trigger_rate_warns_but_never_blocks__dense_csv():
    """触发过密 -> W_HIGH_TRIGGER_RATE; 但回放只出警告不出拒绝 (硬性规定):
    报告照常返回, 判断权在审批人手里。

    实测说明: 这份 CSV 的漏水是簇状的 (一次漏水事件里干湿快速反复), 在 DSL
    冷却下限 60s 的约束下, 单策略折算频率最高只有 ~4.5 次/小时, 够不到默认
    阈值 6 —— 所以这里用显式阈值参数验证"过密 → 警告 → 不阻断"这条链路本身,
    默认阈值由 test_csv_replay_full_report 隐含覆盖 (不出警告也不阻断)。"""
    src = events_from_csv(SEED_CSV, "Arduino1")
    per_sensor = {**WET_NOTIFY, "scope": {"type": "sensor", "ids": [0, 1, 2, 4, 5]}}
    report = replay([loaded(per_sensor)], src.events, source=src.name,
                    high_rate_per_hour=1.0)
    codes = [w.code for w in report.warnings]
    assert "W_HIGH_TRIGGER_RATE" in codes
    assert len(report.effects) > 0  # 没有任何东西被"拦截"


def test_skipped_surfaces_with_never_triggered__csv_lacks_zone_context():
    """open_incident 需要 zone_id 而 CSV 没有: 产出为 0, 但报告必须靠 skipped +
    W_ACTIONS_SKIPPED 把真实原因说出来, 且与 W_NEVER_TRIGGERED 同时出现 ——
    只给后者会把人引向改条件, 而条件根本没错。"""
    src = events_from_csv(SEED_CSV, "Arduino1")
    open_policy = {
        **WET_NOTIFY,
        "actions": [{"type": "open_incident", "severity": "normal"}],
        "cooldown_s": 60,
    }
    report = replay([loaded(open_policy)], src.events, source=src.name)
    assert report.effects == []
    assert len(report.skipped) > 0
    assert all(s.action_type == "open_incident" for s in report.skipped)
    assert all(s.missing == ("zone_id",) for s in report.skipped)
    codes = [w.code for w in report.warnings]
    assert "W_NEVER_TRIGGERED" in codes
    assert "W_ACTIONS_SKIPPED" in codes
    skip_warning = next(w for w in report.warnings if w.code == "W_ACTIONS_SKIPPED")
    assert "zone_id" in skip_warning.message  # 缺的是哪个字段
    assert str(len(report.skipped)) in skip_warning.message  # 多少次


def test_no_high_rate_on_short_span_long_tail__denominator_is_simulated_time():
    """验收 8b 回归: 分母是实际仿真时长 span_s + tail_s。只按遥测跨度 (1 秒) 算,
    1 次触发会折成 3600 次/小时的荒唐频率并误报。"""
    events = [
        {"at_s": 0, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
        {"at_s": 1, "kind": "heartbeat", "device_id": "Arduino1"},
    ]
    report = replay([loaded(WET_NOTIFY)], events, source="inline", tail_s=3600)
    assert len(report.effects) == 1
    assert "W_HIGH_TRIGGER_RATE" not in [w.code for w in report.warnings]


def test_never_triggered_warning__condition_impossible_on_data():
    src = events_from_csv(SEED_CSV, "Arduino1")
    strict = {
        **WET_NOTIFY,
        "conditions": [
            {
                "type": "wet_sensor_count",
                "count_within": "any_zone",
                "op": ">=",
                "value": 32,
                "window_s": 60,
            }
        ],
    }
    report = replay([loaded(strict)], src.events, source=src.name)
    assert [w.code for w in report.warnings] == ["W_NEVER_TRIGGERED"]
    assert report.effects == []


def test_single_subject_warning__all_firings_on_one_sensor():
    events = [
        {"at_s": 5, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
        {"at_s": 100, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "DRY"},
        {"at_s": 500, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
    ]
    report = replay([loaded(WET_NOTIFY)], events, source="inline")
    codes = [w.code for w in report.warnings]
    assert "W_SINGLE_SUBJECT" in codes


def test_report_metadata_echoes_overrides__reproducibility():
    events = [
        {"at_s": 0, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
    ]
    report = replay([loaded(WET_NOTIFY)], events, source="inline",
                    tick_seconds=5, tail_s=60)
    assert report.tick_seconds == 5
    assert report.tail_s == 60


def test_incident_events_rejected_in_input__projector_owns_them():
    """场景数据只允许三类遥测事件: 事故事件由投影器产生, 手写进场景就等于
    绕开了"场景描述设备发生了什么"这条边界。"""
    bad = [{"at_s": 0, "kind": "incident_opened", "incident_id": 1}]
    with pytest.raises(ValueError, match="遥测"):
        replay([loaded(WET_NOTIFY)], bad, source="inline")
