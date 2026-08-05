import pytest
from pydantic import ValidationError

from policy_engine import Inventory, Policy, validate

INV = Inventory(
    zone_ids=frozenset({1, 2, 3}),
    sensor_ids=frozenset({1, 2, 3, 4, 5, 6}),
    roles_present=frozenset({"zone_manager", "operator_on_duty"}),
)

VALID = {
    "name": "同区多传感器无人响应升级",
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [
        {"type": "wet_sensor_count", "scope": "same_zone", "op": ">=", "value": 2, "window_s": 180},
        {"type": "incident_unacknowledged", "duration_s": 120},
    ],
    "actions": [
        {"type": "notify", "channel": "email", "target_role": "zone_manager"},
        {"type": "set_led", "target": "incident_device", "state": "ON"},
    ],
    "cooldown_s": 600,
}


def test_valid_policy_parses_and_validates():
    p = Policy.model_validate(VALID)
    assert validate(p, INV).ok


def test_unknown_action_type_rejected_by_schema():
    bad = {**VALID, "actions": [{"type": "run_shell", "cmd": "rm -rf /"}]}
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_free_text_email_target_rejected():
    bad = {
        **VALID,
        "actions": [{"type": "notify", "channel": "email", "target_role": "attacker@evil.com"}],
    }
    with pytest.raises(ValidationError):
        Policy.model_validate(bad)


def test_unknown_zone_gets_error_code_and_hint():
    p = Policy.model_validate(
        {**VALID, "conditions": VALID["conditions"] + [{"type": "zone_in", "zone_ids": [99]}]}
    )
    r = validate(p, INV)
    assert [i.code for i in r.issues] == ["E_UNKNOWN_ZONE"]
    assert "1, 2, 3" in r.issues[0].hint


def test_short_cooldown_with_notify_rejected():
    p = Policy.model_validate({**VALID, "cooldown_s": 60})
    r = validate(p, INV)
    assert "E_COOLDOWN_TOO_SHORT" in [i.code for i in r.issues]
