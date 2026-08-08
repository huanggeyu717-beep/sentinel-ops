"""EngineState 的上界 (SPEC-001 第四节末: 内存里的东西必须有上界)。

规则: 未解决的事故一条都不淘汰 (数量由数据库 partial unique index 封顶);
已解决的保留最近 incident_history_limit 条, 超出丢最旧, 名下的 tick_edge
边沿记录一并清理。清掉不丢历史 —— 事实源在数据库, 这里只是触发判断的工作台账。
"""
from __future__ import annotations

from _helpers import loaded, make_policy

from policy_engine import EngineState, Event, evaluate

LIMIT = 50
# 升级策略去掉湿度条件: 这里只关心事故投影的簿记, 不铺遥测
ELAPSED_POLICY = loaded(make_policy(conditions=[], cooldown_s=60))


def _lifecycle(state: EngineState, iid: int, t0: int) -> None:
    """一个完整生命周期: 开 -> 中途一个 tick (产生边沿记录) -> 解决。"""
    list(evaluate(
        [ELAPSED_POLICY],
        [
            Event(ts_ms=t0, kind="incident_opened", incident_id=iid,
                  sensor_id=1, zone_id=1, device_id="Arduino1"),
            # 130s 后的 tick: elapsed(120s) 满足, 留下 (policy, incident) 的边沿记录
            Event(ts_ms=t0 + 130_000, kind="tick"),
            Event(ts_ms=t0 + 140_000, kind="incident_resolved", incident_id=iid),
        ],
        state,
    ))


def test_resolved_incidents__evicted_beyond_limit_with_their_edges():
    state = EngineState(incident_history_limit=LIMIT)
    total = 500
    for i in range(1, total + 1):
        _lifecycle(state, iid=i, t0=i * 200_000)

    # 已解决的只留最近 LIMIT 条, 且留的是最新那批
    assert len(state.incidents) == LIMIT
    assert set(state.incidents) == set(range(total - LIMIT + 1, total + 1))
    assert len(state.resolved_order) == LIMIT
    # 被淘汰事故名下的边沿记录一并清掉, 不是换个容器继续涨
    incident_edges = [k for k in state.tick_edge if str(k[1]).startswith("incident:")]
    assert len(incident_edges) == LIMIT
    # 冷却桶按 scope 作用对象 (这里是 zone 1) 分桶, 与历史开单数无关
    assert len(state.last_fired) == 1


def test_unresolved_incident__never_evicted_even_if_oldest():
    state = EngineState(incident_history_limit=LIMIT)
    # 最老的一条一直不解决 (挂在 sensor 2 上, 不与生命周期循环的 sensor 1 抢桶)
    list(evaluate(
        [ELAPSED_POLICY],
        [Event(ts_ms=0, kind="incident_opened", incident_id=9999,
               sensor_id=2, zone_id=1, device_id="Arduino2")],
        state,
    ))
    for i in range(1, 301):
        _lifecycle(state, iid=i, t0=1_000_000 + i * 200_000)

    assert 9999 in state.incidents            # 未解决的一条没丢
    assert state.incidents[9999].status == "open"
    assert state.sensor_incident[2] == 9999
    assert len(state.incidents) == LIMIT + 1  # 最近 LIMIT 条已解决 + 1 条未解决


def test_duplicate_resolved_events__do_not_consume_history_slots():
    """重复的 resolved 事件不占历史名额, 也不撑爆队列。"""
    state = EngineState(incident_history_limit=LIMIT)
    for i in range(1, 4):
        _lifecycle(state, iid=i, t0=i * 200_000)
        # 同一事故的 resolved 事件迟到重放
        list(evaluate(
            [ELAPSED_POLICY],
            [Event(ts_ms=i * 200_000 + 150_000, kind="incident_resolved",
                   incident_id=i)],
            state,
        ))
    assert len(state.resolved_order) == 3
    assert list(state.resolved_order) == [1, 2, 3]
