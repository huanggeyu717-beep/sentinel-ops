"""grader 金样 (SPEC-007 第三节 "grader 自己怎么被测" + 验收 12)。

一个有 bug 的 grader 会安安静静给出一串很自信的数字, 没有任何东西会红 ——
所以判分器必须有自己的正负样本:

- **正样** (写法不同但行为一致): 动作数组顺序调换 / 条件数组顺序调换 /
  scope.ids 顺序调换 / 逐字相同的策略 —— 变异 M1 (比较改成整字典字符串相等)
  与 M2 (去掉剥 policy_id 的归一化) 的靶子;
- **负样** (看着像但行为不同): 变异表每个维度各至少一条;
- **M3 靶子**: 一对 Effect 序列相同、skipped 不同的策略 —— 把 skipped 从
  比较里去掉时它必须红;
- **M10 靶子**: 候选命中 also_accept 的**第二个**答案 —— 比较改成"只比第一个"
  时它必须红。
"""
from __future__ import annotations

import pytest

from evals.graders.behavior_grader import equivalent_on_all, grade
from evals.graders.reference_runner import load_events, load_inventory, sensor_zone_map

ZONE_MAP = sensor_zone_map(load_inventory())
LADDER = {"eval_wet_ladder": load_events("eval_wet_ladder", ZONE_MAP)}
SPILL = {"basic_spill": load_events("basic_spill", ZONE_MAP)}
MSE = {"multi_sensor_escalation": load_events("multi_sensor_escalation", ZONE_MAP)}


def pol(**kw):
    base = {"scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [], "actions": [{"type": "open_incident", "severity": "normal"}],
            "cooldown_s": 60}
    base.update(kw)
    return base


TWO_ACTIONS = [{"type": "notify", "channel": "email", "target_role": "manager"},
               {"type": "set_led", "target": "incident_device", "state": "ON"}]
TWO_CONDS = [{"type": "wet_sensor_count", "count_within": "same_zone",
              "op": ">=", "value": 2, "window_s": 180},
             {"type": "wet_sensor_count", "count_within": "same_zone",
              "op": ">=", "value": 1, "window_s": 300}]


# ===== 正样: 写法不同, 行为一致 =====


def test_golden_positive__identical_bodies_equivalent():
    """逐字相同也要判等价 —— 参考与候选用不同 policy_id 跑, 不剥 id 的话
    连这条都过不了 (变异 M2 的靶子)。"""
    assert equivalent_on_all(pol(), pol(), LADDER)


def test_golden_positive__action_order_swapped():
    a = pol(actions=list(TWO_ACTIONS), cooldown_s=600)
    b = pol(actions=list(reversed(TWO_ACTIONS)), cooldown_s=600)
    assert equivalent_on_all(a, b, LADDER)  # M1 的靶子


def test_golden_positive__condition_order_swapped():
    a = pol(conditions=list(TWO_CONDS))
    b = pol(conditions=list(reversed(TWO_CONDS)))
    assert equivalent_on_all(a, b, LADDER)


def test_golden_positive__scope_ids_order_swapped():
    a = pol(scope={"type": "sensor", "ids": [1, 2]})
    b = pol(scope={"type": "sensor", "ids": [2, 1]})
    assert equivalent_on_all(a, b, LADDER)


# ===== 负样: 看着像, 行为不同 (变异表每维度一条) =====


@pytest.mark.parametrize("label,mutation", [
    ("scope.type", {"scope": {"type": "sensor", "ids": [1]}}),
    ("scope.ids", {"scope": {"type": "zone", "ids": [1, 2]}}),
    ("cooldown_s", {"cooldown_s": 120}),
    ("枚举 to", {"trigger": {"type": "sensor_state_changed", "to": "DRY"}}),
    ("枚举 severity", {"actions": [{"type": "open_incident", "severity": "high"}]}),
    ("结构 dup", {"actions": [{"type": "open_incident", "severity": "normal"},
                              {"type": "open_incident", "severity": "normal"}]}),
])
def test_golden_negative__mutated_not_equivalent(label, mutation):
    assert not equivalent_on_all(pol(), pol(**mutation), LADDER), label


def test_golden_negative__numeric_value_and_window():
    base = pol(conditions=[{"type": "wet_sensor_count", "count_within": "same_zone",
                            "op": ">=", "value": 2, "window_s": 180}], cooldown_s=600)
    worse = pol(conditions=[{"type": "wet_sensor_count", "count_within": "same_zone",
                             "op": ">=", "value": 3, "window_s": 180}], cooldown_s=600)
    assert not equivalent_on_all(base, worse, LADDER)
    narrower = pol(conditions=[{"type": "wet_sensor_count", "count_within": "same_zone",
                                "op": ">=", "value": 2, "window_s": 179}], cooldown_s=600)
    assert not equivalent_on_all(base, narrower, LADDER)


def test_golden_negative__structure_dropped_action():
    a = pol(actions=list(TWO_ACTIONS), cooldown_s=600)
    b = pol(actions=[TWO_ACTIONS[0]], cooldown_s=600)
    assert not equivalent_on_all(a, b, LADDER)


def test_golden_negative__same_type_different_params_not_merged_by_sort():
    """钉住同 ts 排序没排过头 (SPEC-007 补入 25 的配套负样): 两份策略都是
    [notify manager, notify X], 只差第二个收件角色 —— 排序归一化后必须仍判
    不等价。排序若把 detail 参与键丢了 (或按 action_type 去重), 这条会假绿。"""
    a = pol(actions=[{"type": "notify", "channel": "email", "target_role": "manager"},
                     {"type": "notify", "channel": "email", "target_role": "admin"}],
            cooldown_s=600)
    b = pol(actions=[{"type": "notify", "channel": "email", "target_role": "manager"},
                     {"type": "notify", "channel": "email", "target_role": "operator"}],
            cooldown_s=600)
    assert not equivalent_on_all(a, b, LADDER)


# ===== M3 靶子: Effect 相同、skipped 不同 =====


def test_golden__same_effects_different_skipped_not_equivalent():
    """basic_spill 上 4 号首湿: open 正常产出; close 因该传感器此刻没有未解决
    事故而落 skipped —— 两份策略 Effect 序列逐字相同, 只差 skipped。
    把 skipped 从比较里去掉, "动作全被跳过"与"条件没满足"就判成同一种对。"""
    just_open = pol(scope={"type": "sensor", "ids": [4]})
    open_plus_skipped_close = pol(
        scope={"type": "sensor", "ids": [4]},
        actions=[{"type": "open_incident", "severity": "normal"},
                 {"type": "close_incident"}],
    )
    assert not equivalent_on_all(just_open, open_plus_skipped_close, SPILL)


# ===== M10 靶子: 多答案都要被比, 不只第一个 =====


def test_golden__candidate_matching_second_also_accept_passes():
    reference = pol(scope={"type": "sensor", "ids": [4]})
    alt1 = pol(scope={"type": "sensor", "ids": [4]},
               actions=[{"type": "open_incident", "severity": "high"}])
    alt2 = pol(scope={"type": "sensor", "ids": [4]},
               actions=[{"type": "notify", "channel": "email",
                         "target_role": "manager"}], cooldown_s=300)
    expected = {"reference": reference,
                "also_accept": [{"reason": "金样", "policy": alt1},
                                {"reason": "金样", "policy": alt2}]}
    verdict = grade(alt2, expected, SPILL)
    assert verdict == {"passed": True, "matched": "also_accept[1]"}
    # 反向: 三个答案都不像的候选不通过
    stranger = pol(scope={"type": "sensor", "ids": [4]},
                   actions=[{"type": "set_led", "target": "incident_device",
                             "state": "ON"}])
    assert grade(stranger, expected, SPILL)["passed"] is False


# ===== 多场景: 有一个场景分得开就不等价 =====


def test_golden__equivalence_requires_all_scenarios():
    """zone[1] 与 sensor[1,2] 在 multi_sensor_escalation 上产出相同 (两次变湿
    相隔 35 秒, zone 桶吞掉的恰是 sensor 桶也各自首发的), 在梯子上分得开 ——
    多场景等价必须是全场景等价。"""
    a = pol()
    b = pol(scope={"type": "sensor", "ids": [1, 2]})
    both = {**MSE, **LADDER}
    assert not equivalent_on_all(a, b, both)
