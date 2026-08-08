"""引擎行为验收 (SPEC-001 验收 4/5/6/7)。

场景文件一律经 packages/scenario 的 loader 读入 (那个包允许读文件),
事件列表再交给 replay / evaluate —— policy_engine 零 IO 边界不破。
"""
from _helpers import SCENARIOS_DIR, loaded, make_policy, opener_policy
from scenario import events_from_yaml

from policy_engine import (
    EngineState,
    Event,
    evaluate,
    replay,
    wet_sensor_count_now,
)

MULTI = events_from_yaml(SCENARIOS_DIR / "multi_sensor_escalation.yaml")
AUTO_CLOSE = events_from_yaml(SCENARIOS_DIR / "auto_close.yaml")


def _sensor_events_until(source, cutoff_s):
    return [
        Event(
            ts_ms=round(e["at_s"] * 1000),
            kind="sensor_state",
            device_id=e.get("device_id"),
            sensor_id=e.get("sensor_id"),
            zone_id=e.get("zone_id"),
            state=e.get("state"),
        )
        for e in source.events
        if e["kind"] == "sensor_state" and e["at_s"] <= cutoff_s
    ]


# --------------------------------------------------------------------------- #
# 验收 4: 计数语义定死为读法甲
# --------------------------------------------------------------------------- #


def test_wet_count_is_two__multi_sensor_at_t200():
    """1 号 t=5s 变湿、2 号 t=40s 变湿, 两个在 t=200s 都还湿着。
    读法甲 (此刻正湿着) 计 2; 读法乙 (窗口内曾变湿) 会把 t=5 挤出
    [20, 200] 的窗口只计 1。这条测试的存在本身就是防实现漂移成读法乙。"""
    state = EngineState()
    list(evaluate([], _sensor_events_until(MULTI, 200), state))
    count = wet_sensor_count_now(
        state, count_within="same_zone", zone_id=1, window_s=180
    )
    assert count == 2


def test_wet_count_drops_to_zero__after_both_sensors_dry():
    state = EngineState()
    list(evaluate([], _sensor_events_until(MULTI, 290), state))  # 260/290 转干
    assert (
        wet_sensor_count_now(state, count_within="same_zone", zone_id=1, window_s=180)
        == 0
    )


# --------------------------------------------------------------------------- #
# 验收 5: 升级链路 —— 事故 1 在第一个不早于 125s 的 tick (t=130s) 产出
# --------------------------------------------------------------------------- #


def _escalation_replay(escalation_scope):
    policies = [
        loaded(opener_policy(), policy_id=1),
        loaded(make_policy(scope=escalation_scope), policy_id=2),
    ]
    report = replay(policies, MULTI.events, source=MULTI.name)
    return [e for e in report.effects if e.policy_id == 2]


def test_escalation_fires_at_t130__first_tick_after_elapsed():
    effects = _escalation_replay({"type": "zone", "ids": [1]})
    # t=125s 那一刻一个事件都没有, 事件驱动的引擎不会被唤醒 —— 必须等 t=130s 的 tick
    assert [e.ts_ms for e in effects] == [130_000, 130_000]
    assert [e.action_type for e in effects] == ["notify", "set_led"]
    for e in effects:
        assert e.subject.incident_id == 1
        assert e.subject.zone_id == 1
        assert e.subject.device_id == "Arduino1"  # 由 incident_opened 事件带进来
    assert effects[0].detail == {"channel": "email", "target_role": "manager"}


def test_opener_creates_two_incidents__wet_edges_at_t5_and_t40():
    policies = [loaded(opener_policy(), policy_id=1)]
    report = replay(policies, MULTI.events, source=MULTI.name)
    opens = [e for e in report.effects if e.action_type == "open_incident"]
    assert [(e.ts_ms, e.subject.sensor_id) for e in opens] == [
        (5_000, 1),
        (40_000, 2),
    ]


# --------------------------------------------------------------------------- #
# 验收 6: 对照测试 —— 边沿与冷却是两个不同的键
# --------------------------------------------------------------------------- #


def test_second_incident_suppressed__cooldown_bucketed_by_zone():
    """事故 2 在 t=160s 也满足 elapsed, 是另一个触发主体、有自己的边沿;
    拦住它的是 cooldown: scope 按 zone 分桶时两个事故落在同一个桶 (zone 1),
    t=130 刚触发过, 600 秒冷却内不再产出。"""
    effects = _escalation_replay({"type": "zone", "ids": [1]})
    assert {e.ts_ms for e in effects} == {130_000}


def test_both_incidents_fire__cooldown_bucketed_by_sensor():
    """同一份数据、同一条规则, 仅冷却分桶依据不同而结果不同。
    这条与上一条守的是"冷却确实按 scope 作用对象分桶"; 两个事故的触发时刻
    只差 35 秒、小于冷却下限, "边沿与冷却不是同一个键"在这个场景里分不开,
    由下面的判别性测试 (验收 6b) 单独守。"""
    effects = _escalation_replay({"type": "sensor", "ids": [1, 2]})
    fired = sorted({(e.ts_ms, e.subject.incident_id) for e in effects})
    assert fired == [(130_000, 1), (160_000, 2)]


def test_edge_keyed_by_subject_not_cooldown_bucket__firings_far_apart():
    """验收 6b 判别性测试: 两个事故的 elapsed 时刻相隔 200 秒, 远大于冷却 60 秒 ——
    第二个事故触发时冷却早已过期, 唯一还能吞掉它的就只有边沿。
    正确实现 (边沿按触发主体 incident_id) 两条都产出; 把边沿键合进冷却桶
    (zone 1) 则事故 1 持续 open 会把该桶的边沿一直压在"满足", 事故 2 永远
    等不到新的上升沿, 只产出第一条。"""
    policies = [
        loaded(opener_policy(), policy_id=1),
        loaded(
            {
                "scope": {"type": "zone", "ids": [1]},
                "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 120},
                "conditions": [],
                "actions": [{"type": "escalate_incident", "to_severity": "high"}],
                "cooldown_s": 60,
            },
            policy_id=2,
        ),
    ]
    events = [
        {"at_s": 5, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
        {"at_s": 205, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 2, "zone_id": 1, "state": "WET"},
    ]
    report = replay(policies, events, source="inline")
    escalations = [e for e in report.effects if e.policy_id == 2]
    assert [(e.ts_ms, e.subject.incident_id) for e in escalations] == [
        (130_000, 1),  # 事故 1: t=5 开, elapsed 120s -> 第一个不早于 125s 的 tick
        (330_000, 2),  # 事故 2: t=205 开, elapsed 时刻 325s -> t=330s 的 tick
    ]


# --------------------------------------------------------------------------- #
# 验收 7: 关单链路, 场景末尾靠 tail_s 的 tick 才走得到
# --------------------------------------------------------------------------- #


def test_close_incident_fires_at_t400__via_tail_ticks():
    policies = [
        loaded(opener_policy(scope={"type": "sensor", "ids": [1]}), policy_id=1),
        loaded(
            {
                "scope": {"type": "zone", "ids": [1]},
                "trigger": {"type": "sensor_dry_for", "dry_for_s": 300},
                "conditions": [],
                "actions": [{"type": "close_incident"}],
                "cooldown_s": 60,
            },
            policy_id=2,
        ),
    ]
    report = replay(policies, AUTO_CLOSE.events, source=AUTO_CLOSE.name)
    closes = [e for e in report.effects if e.action_type == "close_incident"]
    assert [(e.ts_ms, e.subject.incident_id) for e in closes] == [(400_000, 1)]
    # 场景最后一个事件在 t=100s: 没有 tail 的 tick, 这条关单永远验证不到
    last_event_ms = round(max(e["at_s"] for e in AUTO_CLOSE.events) * 1000)
    assert closes[0].ts_ms > last_event_ms


# --------------------------------------------------------------------------- #
# tick 触发的边沿语义与冷却语义
# --------------------------------------------------------------------------- #


def test_device_offline_fires_once_per_episode__edge_not_cooldown():
    """离线持续满足时不逐 tick 重复触发 (哪怕冷却早已过期) —— 这是边沿在拦,
    不是冷却; 设备回线再离线形成新的上升沿, 才触发第二次。"""
    policy = loaded(
        {
            "scope": {"type": "global", "ids": []},
            "trigger": {"type": "device_offline", "offline_for_s": 60},
            "conditions": [],
            "actions": [{"type": "notify", "channel": "email", "target_role": "admin"}],
            "cooldown_s": 60,  # 冷却只有 60s: 若靠冷却拦, 会每 60s 重复一次
        }
    )
    events = [
        {"at_s": 0, "kind": "heartbeat", "device_id": "Arduino1"},
        {"at_s": 300, "kind": "heartbeat", "device_id": "Arduino1"},  # 回线
    ]
    report = replay([policy], events, source="inline", tail_s=300)
    assert [e.ts_ms for e in report.effects] == [60_000, 360_000]


def test_cooldown_updates_state_not_skips_evaluation__wet_since_tracked():
    """冷却抑制的是产出 Effect, 不是跳过判断: 被冷却吞掉的那次触发,
    滑动窗口状态照常更新。"""
    policy = loaded(opener_policy(scope={"type": "zone", "ids": [1]}, cooldown_s=600))
    state = EngineState()
    events = [
        Event(ts_ms=5_000, kind="sensor_state", device_id="Arduino1",
              sensor_id=1, zone_id=1, state="WET"),
        Event(ts_ms=40_000, kind="sensor_state", device_id="Arduino1",
              sensor_id=2, zone_id=1, state="WET"),
    ]
    effects = list(evaluate([policy], events, state))
    assert [e.subject.sensor_id for e in effects] == [1]  # 第二次被 zone 桶冷却吞掉
    assert state.wet_since == {1: 5_000, 2: 40_000}  # 但状态照常更新


def test_level_state_does_not_retrigger__edge_on_sensor_state():
    policy = loaded(opener_policy())
    state = EngineState()
    events = [
        Event(ts_ms=5_000, kind="sensor_state", device_id="Arduino1",
              sensor_id=1, zone_id=1, state="WET"),
        Event(ts_ms=200_000, kind="sensor_state", device_id="Arduino1",
              sensor_id=1, zone_id=1, state="WET"),  # 持续报湿, 冷却已过
    ]
    effects = list(evaluate([policy], events, state))
    assert len(effects) == 1  # 没有新的边沿, 不重复触发


# --------------------------------------------------------------------------- #
# 条件性上下文运行时缺失: 不产出 + 记 skipped, 不静默丢弃
# --------------------------------------------------------------------------- #


def test_missing_conditional_context__skipped_recorded_not_silently_dropped():
    policy = loaded(
        {
            "scope": {"type": "sensor", "ids": [1]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [],
            "actions": [
                {"type": "open_incident", "severity": "normal"},
                {"type": "escalate_incident", "to_severity": "high"},
            ],
            "cooldown_s": 60,
        }
    )
    state = EngineState()
    events = [
        Event(ts_ms=1_000, kind="sensor_state", device_id="Arduino1",
              sensor_id=1, zone_id=1, state="WET"),
    ]
    effects = list(evaluate([policy], events, state))
    # 该传感器还没有事故: escalate_incident 缺 incident_id, 不产出但要留痕
    assert [e.action_type for e in effects] == ["open_incident"]
    assert len(state.skipped) == 1
    assert state.skipped[0].action_type == "escalate_incident"
    assert state.skipped[0].missing == ("incident_id",)
    assert state.skipped[0].reason == "missing_context"


# --------------------------------------------------------------------------- #
# 事故投影器: rfid 接单与开事故去重
# --------------------------------------------------------------------------- #


def test_rfid_acknowledges_earliest_open_incident__projector():
    """t=200s 的刷卡应把该区最早的未解决事故 (事故 1) 转为 acknowledged。
    for_s=30 从进入 acknowledged 状态起算 (t=200s), 不从开单起算 (t=5s) ——
    满足时刻是 t=230s, 恰是 tick。若误从开单起算, 会在 t=200s 的 tick 就触发,
    这条断言同时锁住 status_since 语义与"接单最早未解决事故"的投影规则。"""
    policies = [
        loaded(opener_policy(), policy_id=1),
        loaded(
            make_policy(
                trigger={
                    "type": "incident_elapsed",
                    "in_status": "acknowledged",
                    "for_s": 30,
                },
                conditions=[],
                scope={"type": "zone", "ids": [1]},
            ),
            policy_id=2,
        ),
    ]
    report = replay(policies, MULTI.events, source=MULTI.name)
    acked = [e for e in report.effects if e.policy_id == 2]
    assert {(e.ts_ms, e.subject.incident_id) for e in acked} == {(230_000, 1)}


def test_elapsed_counts_from_status_entry__not_from_open():
    """SPEC-001 第二节末: in_status=acknowledged, for_s=60 的含义是"已接单 60 秒",
    不是"开单 60 秒以上且当前已接单"。事故 t=0 开、t=100s 接单:
    t=120s 的 tick 上按旧口径 (从开单起算) 已满足, 正确口径要等 t=160s。"""
    policy = loaded(
        {
            "scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "incident_elapsed", "in_status": "acknowledged", "for_s": 60},
            "conditions": [],
            "actions": [{"type": "escalate_incident", "to_severity": "high"}],
            "cooldown_s": 60,
        }
    )
    state = EngineState()
    events = [
        Event(ts_ms=0, kind="incident_opened", incident_id=1,
              sensor_id=1, zone_id=1, device_id="Arduino1"),
        Event(ts_ms=100_000, kind="incident_acknowledged", incident_id=1),
        Event(ts_ms=120_000, kind="tick"),  # 从开单起算的错误口径会在这里触发
        Event(ts_ms=160_000, kind="tick"),  # 接单满 60s, 正确口径在这里触发
    ]
    effects = list(evaluate([policy], events, state))
    assert [e.ts_ms for e in effects] == [160_000]


def test_reopen_while_unresolved_is_noop__projector_dedup():
    """同一传感器已有未解决事故时 open_incident 是空操作 (投影器显式实现,
    否则模拟会比线上多开事故): 干湿反复只产生一个事故。"""
    policies = [
        loaded(opener_policy(scope={"type": "sensor", "ids": [1]}), policy_id=1),
        loaded(
            {
                "scope": {"type": "zone", "ids": [1]},
                "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 60},
                "conditions": [],
                "actions": [{"type": "escalate_incident", "to_severity": "high"}],
                "cooldown_s": 60,
            },
            policy_id=2,
        ),
    ]
    events = [
        {"at_s": 5, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},
        {"at_s": 50, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "DRY"},
        {"at_s": 100, "kind": "sensor_state", "device_id": "Arduino1",
         "sensor_id": 1, "zone_id": 1, "state": "WET"},  # 冷却已过, 引擎会再产出 open
    ]
    report = replay(policies, events, source="inline", tail_s=300)
    opens = [e for e in report.effects if e.action_type == "open_incident"]
    assert len(opens) == 2  # 引擎产出两次 open (第二次是新的湿边沿)
    prober = {e.subject.incident_id for e in report.effects if e.policy_id == 2}
    assert prober == {1}  # 但投影器只开出事故 1: 第二次 open 是空操作
