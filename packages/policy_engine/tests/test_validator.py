"""语义层验收 (SPEC-001 验收 2/3): 每个错误码至少一条测试;
错误码带 path 与 hint, 仅凭错误码即可完成修复循环。
"""
from _helpers import make_policy

from policy_engine import Inventory, Policy, validate

# zone 1: 传感器 1,2,3; zone 2: 传感器 4,5; zone 3: 传感器 6
INV = Inventory(
    zone_ids=frozenset({1, 2, 3}),
    sensor_ids=frozenset({1, 2, 3, 4, 5, 6}),
    sensor_zone={1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 3},
    roles_present=frozenset({"operator", "manager", "admin"}),  # 事实源: user_roles
)


def _validate(**overrides):
    return validate(Policy.model_validate(make_policy(**overrides)), INV)


def _codes(result):
    return [i.code for i in result.issues]


def _assert_repairable(result):
    """验收 2: 每条 issue 都要有 path 与 hint, 修复循环才闭得上。"""
    for issue in result.issues:
        assert issue.path, issue
        assert issue.hint, issue


def test_ok__spec_example_policy():
    assert _validate().ok


def test_unknown_zone__scope_points_to_missing_zone():
    r = _validate(scope={"type": "zone", "ids": [99]})
    assert _codes(r) == ["E_UNKNOWN_ZONE"]
    assert r.issues[0].path == "scope.ids"
    assert "1, 2, 3" in r.issues[0].hint
    _assert_repairable(r)


def test_unknown_sensor__scope_points_to_missing_sensor():
    r = _validate(scope={"type": "sensor", "ids": [1, 42]})
    assert _codes(r) == ["E_UNKNOWN_SENSOR"]
    assert "42" in r.issues[0].message
    _assert_repairable(r)


def test_scope_ids_mismatch__global_with_ids():
    r = _validate(scope={"type": "global", "ids": [1]})
    assert _codes(r) == ["E_SCOPE_IDS_MISMATCH"]
    _assert_repairable(r)


def test_scope_ids_mismatch__zone_without_ids():
    r = _validate(scope={"type": "zone", "ids": []})
    assert _codes(r) == ["E_SCOPE_IDS_MISMATCH"]
    _assert_repairable(r)


def test_role_not_staffed__viewer_has_no_account():
    inv = Inventory(
        zone_ids=INV.zone_ids,
        sensor_ids=INV.sensor_ids,
        sensor_zone=INV.sensor_zone,
        roles_present=frozenset({"manager"}),
    )
    p = Policy.model_validate(
        make_policy(
            actions=[{"type": "notify", "channel": "email", "target_role": "viewer"}]
        )
    )
    r = validate(p, inv)
    assert _codes(r) == ["E_ROLE_NOT_STAFFED"]
    assert r.issues[0].path == "actions[0].target_role"
    assert "manager" in r.issues[0].hint
    _assert_repairable(r)


def test_cooldown_too_short__with_notify():
    r = _validate(cooldown_s=60)
    assert _codes(r) == ["E_COOLDOWN_TOO_SHORT"]
    _assert_repairable(r)


def test_cooldown_ok__short_but_without_notify():
    r = _validate(
        cooldown_s=60,
        actions=[{"type": "escalate_incident", "to_severity": "high"}],
    )
    assert r.ok


def test_duplicate_action__identical_pair():
    r = _validate(
        actions=[
            {"type": "set_led", "target": "incident_device", "state": "ON"},
            {"type": "set_led", "target": "incident_device", "state": "ON"},
        ]
    )
    assert _codes(r) == ["E_DUPLICATE_ACTION"]
    assert r.issues[0].path == "actions[1]"
    _assert_repairable(r)


def test_always_true__le_value_covers_zone_sensor_count():
    # zone 1 只有 3 个传感器: same_zone 计数上限是 3, 不是全部 6 个。
    # 修正前的实现拿 len(sensor_ids)=6 比, value=4 时不报 —— 这条测试守住修正。
    r = _validate(
        conditions=[
            {
                "type": "wet_sensor_count",
                "count_within": "same_zone",
                "op": "<=",
                "value": 4,
                "window_s": 180,
            }
        ]
    )
    assert _codes(r) == ["E_ALWAYS_TRUE_CONDITION"]
    _assert_repairable(r)


def test_not_always_true__any_zone_counts_all_sensors():
    # 同样 value=4, any_zone 的上限是 6, 不恒真 —— 与上一条构成对照
    r = _validate(
        conditions=[
            {
                "type": "wet_sensor_count",
                "count_within": "any_zone",
                "op": "<=",
                "value": 4,
                "window_s": 180,
            }
        ]
    )
    assert r.ok


def test_context_unavailable__device_offline_with_close_incident():
    # 验收 3: device_offline 提供不了 incident_id, 提交草稿那一刻就拦住,
    # hint 指出该 trigger 能提供哪些上下文
    r = _validate(
        scope={"type": "global", "ids": []},
        trigger={"type": "device_offline", "offline_for_s": 60},
        conditions=[],
        actions=[{"type": "close_incident"}],
    )
    assert _codes(r) == ["E_CONTEXT_UNAVAILABLE"]
    issue = r.issues[0]
    assert "incident_id" in issue.message
    assert "device_id" in issue.hint  # 必定提供的
    assert "zone_id" in issue.hint  # 条件性提供的
    _assert_repairable(r)


def test_context_ok__conditional_context_passes_statically():
    # incident_id 对 sensor_state_changed 是"条件性提供": 静态阶段无从判断
    # 运行时有没有事故, 放行; 运行时为空由引擎记 skipped (见 test_engine)
    r = _validate(
        scope={"type": "sensor", "ids": [1]},
        trigger={"type": "sensor_state_changed", "to": "WET"},
        conditions=[],
        actions=[{"type": "escalate_incident", "to_severity": "high"}],
        cooldown_s=60,
    )
    assert r.ok


def test_self_trigger_loop__incident_elapsed_opens_incident():
    r = _validate(
        conditions=[],
        actions=[{"type": "open_incident", "severity": "high"}],
    )
    assert _codes(r) == ["E_SELF_TRIGGER_LOOP"]
    _assert_repairable(r)
