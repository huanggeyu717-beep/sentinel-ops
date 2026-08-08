"""Schema 层验收 (SPEC-001 验收 1): 白名单外的 type 直接失败,
DSL 外字段被 extra="forbid" 拒绝, 分级表/上下文表与白名单完全相等。
"""
import pytest
from _helpers import make_policy
from pydantic import ValidationError

from policy_engine import (
    ACTION_APPROVAL_CLASS,
    ACTION_REQUIRED_CONTEXT,
    TRIGGER_CONTEXT,
    Policy,
    action_type_whitelist,
    policy_json_schema,
    trigger_type_whitelist,
)


def test_parses__spec_example_policy():
    p = Policy.model_validate(make_policy())
    assert p.trigger.type == "incident_elapsed"
    assert p.scope.type == "zone"


def test_rejected__unknown_action_type():
    bad = make_policy(actions=[{"type": "run_shell", "cmd": "rm -rf /"}])
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__unknown_trigger_type():
    bad = make_policy(trigger={"type": "cron", "expr": "* * * * *"})
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__name_not_in_dsl():
    # 名字归 policies 表那一列, 两处各存一份必然走散 (SPEC-001 第二节)
    bad = make_policy(name="生鲜区告警")
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__requires_approval_not_in_dsl():
    # requires_approval 由服务端从动作分级推导, 永不信任模型输入 (不变量 5)
    bad = make_policy(requires_approval=False)
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__free_text_email_target():
    bad = make_policy(
        actions=[
            {"type": "notify", "channel": "email", "target_role": "attacker@evil.com"}
        ]
    )
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__zone_manager_role_not_in_roles_table():
    # 通知目标对齐 roles 表四个值; 原三套角色名里的 zone_manager 已废弃
    bad = make_policy(
        actions=[{"type": "notify", "channel": "email", "target_role": "zone_manager"}]
    )
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__operator_on_duty_cut_to_v2():
    bad = make_policy(
        actions=[
            {"type": "notify", "channel": "email", "target_role": "operator_on_duty"}
        ]
    )
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__zone_in_condition_replaced_by_scope():
    bad = make_policy(conditions=[{"type": "zone_in", "zone_ids": [1]}])
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__time_window_condition_cut_to_v2():
    bad = make_policy(
        conditions=[{"type": "time_window", "start": "22:00", "end": "06:00"}]
    )
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__empty_actions():
    bad = make_policy(actions=[])
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_rejected__scope_ids_over_limit():
    bad = make_policy(scope={"type": "sensor", "ids": list(range(17))})
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_approval_class_keys__equal_action_whitelist():
    # 加了新动作却忘了补分级不会报任何错, 查表返回空, 审批界面什么都不提示 ——
    # 让不一致当场变红灯 (SPEC-001 第二节, 本项目同类手法第四处)
    assert frozenset(ACTION_APPROVAL_CLASS) == action_type_whitelist()


def test_required_context_keys__equal_action_whitelist():
    assert frozenset(ACTION_REQUIRED_CONTEXT) == action_type_whitelist()


def test_trigger_context_keys__equal_trigger_whitelist():
    assert frozenset(TRIGGER_CONTEXT) == trigger_type_whitelist()


def test_json_schema__exported_for_agent_prompt():
    schema = policy_json_schema()
    assert "properties" in schema
    assert set(schema["properties"]) == {
        "scope",
        "trigger",
        "conditions",
        "actions",
        "cooldown_s",
    }
