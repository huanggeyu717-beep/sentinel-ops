"""确定性策略引擎 (SPEC-001 第一/三/四节)。

关键不变量:
- 执行器与模拟器是同一份代码 —— evaluate() 只消费事件流与自身状态, 不做任何 IO
  (CLAUDE.md 不变量 2)。线上执行喂 live 事件, 模拟/评测喂场景包事件。
- **事件流是引擎唯一的输入**: 事故存不存在、有没有人接单这些事实, 也由
  incident_* 域事件进入引擎 (线上由 incident_service 在写库的同一事务里投递,
  模拟侧由回放模块的事故投影器产生), 引擎不查库。
- 纯事件驱动表达不了"过了多久还没发生事", 所以输入里必须有固定间隔的 tick 事件;
  线上与模拟的 tick 间隔必须一致, 否则模拟结果对线上没有预测力。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .dsl import (
    ACTION_REQUIRED_CONTEXT,
    Condition,
    DeviceOfflineTrigger,
    IncidentElapsedTrigger,
    IncidentUnacknowledgedCondition,
    Policy,
    SensorDryForTrigger,
    SensorStateChangedTrigger,
    WetSensorCountCondition,
)

EventKind = Literal[
    "sensor_state",
    "heartbeat",
    "rfid_scan",
    "tick",
    "incident_opened",
    "incident_assigned",
    "incident_acknowledged",
    "incident_resolved",
]


@dataclass(frozen=True)
class Event:
    """规范化事件 (八类, SPEC-001 第一节)。

    遥测三类由 /ingest (线上) 或场景装载 (模拟) 规范化而来; tick 由引擎时钟产生;
    incident_* 四类由 incident_service (线上) 或事故投影器 (模拟) 产生。
    incident_opened 必须带 device_id, 否则由事故唤醒的策略产不出 set_led。
    """

    ts_ms: int
    kind: EventKind
    device_id: str | None = None
    sensor_id: int | None = None
    zone_id: int | None = None
    state: str | None = None  # sensor_state: WET / DRY
    rfid_uid: str | None = None
    incident_id: int | None = None


@dataclass(frozen=True)
class EffectSubject:
    """Effect 的作用对象 (SPEC-001 第三节, 强制)。

    引擎内部也用同一形状承载触发上下文: trigger 表里"必定提供/条件性提供"
    的字段就是这四个。
    """

    sensor_id: int | None = None
    zone_id: int | None = None
    device_id: str | None = None
    incident_id: int | None = None


@dataclass(frozen=True)
class Effect:
    """引擎输出 —— 评测中的"行为轨迹"即 Effect 序列。

    subject 强制带作用对象: 不带对象的模拟结果只能告诉你"会点三次灯",
    不能告诉你"点的是哪三台", 执行器也只能去猜 —— 那会破坏输出确定性。
    """

    ts_ms: int
    policy_id: int
    policy_version: int
    action_type: str
    subject: EffectSubject
    detail: dict[str, Any]  # 动作参数原样回显 (不含 type, 它在 action_type 里)


@dataclass(frozen=True)
class SkippedAction:
    """条件性提供的上下文运行时为空 → 该 Effect 不产出, 记录而非静默丢弃
    (SPEC-001 第二节)。调用方 (policy_runtime) 把它落进 policy_runs。"""

    ts_ms: int
    policy_id: int
    policy_version: int
    action_type: str
    missing: tuple[str, ...]
    reason: str = "missing_context"


@dataclass(frozen=True)
class LoadedPolicy:
    """引擎的策略输入。DSL 里没有 name/id (归 policies 表), 而 Effect 必须带
    policy_id 与版本 (身份要用 id, 名字是可随时修改的展示字段),
    所以由调用方把 id/版本与 body 配对喂进来。"""

    policy_id: int
    version: int
    body: Policy


@dataclass
class IncidentRecord:
    """由 incident_* 事件喂养的事故快照 (引擎零 IO, 不查库)。

    opened_ts_ms 与 status_since_ms 各答各的问题: 前者供 incident_unacknowledged
    ("开单后无人 acknowledge 超过 duration_s"), 后者供 incident_elapsed
    ("处于该状态超过 for_s", 从进入该状态起算, SPEC-001 第二节末)。
    """

    incident_id: int
    opened_ts_ms: int
    status: str  # open / assigned / acknowledged / resolved
    status_since_ms: int
    sensor_id: int | None = None
    zone_id: int | None = None
    device_id: str | None = None


@dataclass
class EngineState:
    """evaluate() 的全部可变状态。冷却期内这些状态照常更新 ——
    冷却抑制的是产出 Effect, 不是跳过判断 (SPEC-001 第四节)。

    **有上界, 清掉不丢历史** (SPEC-001 第四节末): 事实源在数据库
    (incidents / incident_events / policy_runs), 这里只是为了判断"该不该触发"
    而攒的工作台账, 淘汰任何条目都不丢失任何历史。
    - 未解决的事故一条都不淘汰 —— 数量由数据库的 partial unique index 封顶
      (同一传感器最多一条未解决), 上界就是传感器数;
    - 已解决的保留最近 incident_history_limit 条, 超出丢最旧, 连同它名下的
      tick_edge 边沿记录一并清理 (已解决的事故没有任何触发器还会用到它,
      留一小批只为迟到事件与排查方便);
    - 其余容器 (遥测投影、last_fired 冷却桶) 以库存规模为上界, 不随历史增长。
    """

    # 遥测投影
    last_state: dict[int, str] = field(default_factory=dict)  # sensor -> WET/DRY
    wet_since: dict[int, int] = field(default_factory=dict)  # sensor -> 变湿时刻
    dry_since: dict[int, int] = field(default_factory=dict)  # sensor -> 变干时刻
    last_seen: dict[str, int] = field(default_factory=dict)  # device -> 最后一条消息
    # 从 sensor_state 事件里学到的映射 (SPEC-001 第一节)
    sensor_device: dict[int, str] = field(default_factory=dict)
    sensor_zone: dict[int, int] = field(default_factory=dict)
    device_zone: dict[str, int] = field(default_factory=dict)
    # 事故投影 (由 incident_* 事件喂养)
    incidents: dict[int, IncidentRecord] = field(default_factory=dict)
    sensor_incident: dict[int, int] = field(default_factory=dict)  # sensor -> 未解决事故
    # 已解决事故按解决顺序排队, 超出 incident_history_limit 淘汰最旧 (见类 docstring)
    incident_history_limit: int = 200
    resolved_order: deque[int] = field(default_factory=deque)
    # 触发簿记 —— 注意是两个不同的键, 不要合并 (SPEC-001 第二节末 + 第四节, 验收 6):
    # 边沿按 (policy_id, 触发主体) 分桶, 防"同一主体每个 tick 都触发";
    # 冷却按 (policy_id, scope 作用对象) 分桶, 防"同一作用范围短时间反复产出"。
    # 事故主体的边沿键带 "incident:" 前缀 —— 裸 int 会与恰好同号的 sensor 撞键,
    # 淘汰事故清边沿时就分不清删的是谁。
    tick_edge: dict[tuple[int, int | str], bool] = field(default_factory=dict)
    last_fired: dict[tuple[int, int | None], int] = field(default_factory=dict)
    # 运行时缺上下文而未产出的动作, 调用方消费后应清空 (evaluate 只追加)
    skipped: list[SkippedAction] = field(default_factory=list)


def evaluate(
    policies: Sequence[LoadedPolicy],
    events: Iterable[Event],
    state: EngineState | None = None,
) -> Iterator[Effect]:
    """按时间序消费事件, 产出 Effect 序列。

    - 同一个事件命中多条策略时, Effect 按 (事件时序, policy_id) 稳定排序输出;
      同一策略在一个 tick 上命中多个主体时按主体 id 排序。同样输入必得同样输出。
    - 冲突消解不在这里: 交给执行器靠幂等 (SPEC-001 第四节), 引擎不引入优先级。
    - 生成器是惰性的: 调用方必须消费完毕, state 才是完整推进后的状态。
    """
    engine_state = state if state is not None else EngineState()
    ordered = sorted(policies, key=lambda lp: lp.policy_id)
    for ev in events:
        if ev.kind == "sensor_state":
            yield from _on_sensor_state(ordered, ev, engine_state)
        elif ev.kind in ("heartbeat", "rfid_scan"):
            if ev.device_id is not None:
                engine_state.last_seen[ev.device_id] = ev.ts_ms
        elif ev.kind == "tick":
            yield from _on_tick(ordered, ev, engine_state)
        else:
            _on_incident_event(ev, engine_state)


def wet_sensor_count_now(
    state: EngineState,
    *,
    count_within: str,
    zone_id: int | None,
    window_s: int,
) -> int:
    """wet_sensor_count 的计数, 语义定死为读法甲 (SPEC-001 第二节):
    统计**此刻正湿着**的传感器数量, 且这些传感器的**变湿时刻**必须落在同一个
    window_s 窗口内 —— 即取能被一个 window_s 长的窗口盖住的最大数量。
    不是"过去 window_s 里曾经变湿过的有几个" (读法乙): 持续湿着的传感器
    不因变湿事件掉出窗口而不被计数。

    count_within=same_zone 时只数与触发对象同区的传感器; zone 上下文缺失时计 0。
    """
    if count_within == "same_zone":
        if zone_id is None:
            return 0
        onsets = sorted(
            ts
            for sid, ts in state.wet_since.items()
            if state.sensor_zone.get(sid) == zone_id
        )
    else:
        onsets = sorted(state.wet_since.values())
    if not onsets:
        return 0
    best = 1
    left = 0
    for right in range(len(onsets)):
        while onsets[right] - onsets[left] > window_s * 1000:
            left += 1
        best = max(best, right - left + 1)
    return best


# --------------------------------------------------------------------------- #
# 内部实现
# --------------------------------------------------------------------------- #


def _on_sensor_state(
    policies: Sequence[LoadedPolicy], ev: Event, state: EngineState
) -> Iterator[Effect]:
    if ev.device_id is not None:
        state.last_seen[ev.device_id] = ev.ts_ms
    sid = ev.sensor_id
    if sid is None or ev.state is None:
        return
    # 学习 sensor -> device 与 device -> zone 两张映射 (SPEC-001 第一节)。
    # zone_id 的唯一事实源是数据库, /ingest 规范化时已从库里补上;
    # 纯离线模拟时用场景 YAML 手写的值。
    if ev.device_id is not None:
        state.sensor_device[sid] = ev.device_id
        if ev.zone_id is not None:
            state.device_zone[ev.device_id] = ev.zone_id
    if ev.zone_id is not None:
        state.sensor_zone[sid] = ev.zone_id
    prev = state.last_state.get(sid)
    state.last_state[sid] = ev.state
    if ev.state == "WET":
        if prev != "WET":
            state.wet_since[sid] = ev.ts_ms
        state.dry_since.pop(sid, None)
    elif ev.state == "DRY":
        if prev != "DRY":
            state.dry_since[sid] = ev.ts_ms
        state.wet_since.pop(sid, None)
    if prev == ev.state:
        return  # 边沿触发: 持续报同一状态不重复触发 (沿用原 automatic-alert 语义)
    subject = EffectSubject(
        sensor_id=sid,
        zone_id=ev.zone_id if ev.zone_id is not None else state.sensor_zone.get(sid),
        device_id=ev.device_id
        if ev.device_id is not None
        else state.sensor_device.get(sid),
        incident_id=state.sensor_incident.get(sid),  # 条件性提供
    )
    for lp in policies:
        trig = lp.body.trigger
        if isinstance(trig, SensorStateChangedTrigger) and trig.to == ev.state:
            yield from _fire(lp, subject, ev.ts_ms, state)


def _on_tick(
    policies: Sequence[LoadedPolicy], ev: Event, state: EngineState
) -> Iterator[Effect]:
    """tick 驱动的三类 trigger。全部是边沿触发: 只在条件从"不满足"变为"满足"的
    那个 tick 触发一次, 之后持续满足不重复触发 (SPEC-001 第二节)。
    边沿状态的更新不受 scope/conditions/cooldown 影响 —— 它只跟踪 trigger 本身。
    """
    now = ev.ts_ms
    for lp in policies:
        trig = lp.body.trigger
        if isinstance(trig, DeviceOfflineTrigger):
            # 离线的定义就是"没有消息", 只能由 tick 主动检查
            for dev in sorted(state.last_seen):
                satisfied = now - state.last_seen[dev] >= trig.offline_for_s * 1000
                if _edge_fire(state, lp.policy_id, dev, satisfied):
                    subject = EffectSubject(
                        device_id=dev,
                        zone_id=state.device_zone.get(dev),  # 条件性提供
                    )
                    yield from _fire(lp, subject, now, state)
        elif isinstance(trig, IncidentElapsedTrigger):
            for iid in sorted(state.incidents):
                rec = state.incidents[iid]
                # 时长从事故进入该状态的时刻起算 (in_status=acknowledged 才是
                # "已接单 for_s 秒"而非"开单 for_s 秒且已接单"), 不从引擎观察到
                # 它的那个 tick 起算 (SPEC-001 第二节末)。
                satisfied = (
                    rec.status == trig.in_status
                    and now - rec.status_since_ms >= trig.for_s * 1000
                )
                if _edge_fire(state, lp.policy_id, _incident_edge_key(iid), satisfied):
                    subject = EffectSubject(
                        sensor_id=rec.sensor_id,
                        zone_id=rec.zone_id,
                        device_id=rec.device_id,
                        incident_id=iid,
                    )
                    yield from _fire(lp, subject, now, state)
        elif isinstance(trig, SensorDryForTrigger):
            for sid in sorted(state.dry_since):
                satisfied = now - state.dry_since[sid] >= trig.dry_for_s * 1000
                if _edge_fire(state, lp.policy_id, sid, satisfied):
                    subject = EffectSubject(
                        sensor_id=sid,
                        zone_id=state.sensor_zone.get(sid),
                        device_id=state.sensor_device.get(sid),
                        incident_id=state.sensor_incident.get(sid),  # 条件性提供
                    )
                    yield from _fire(lp, subject, now, state)


def _incident_edge_key(iid: int) -> str:
    """事故主体的边沿键。带前缀是为了淘汰时能安全清理: 裸 int 会与同号 sensor 撞键。"""
    return f"incident:{iid}"


def _evict_resolved(state: EngineState) -> None:
    """已解决事故只留最近 incident_history_limit 条 (SPEC-001 第四节末)。

    未解决的从不进 resolved_order, 所以永远不会被淘汰; 被淘汰事故名下的
    tick_edge 边沿记录一并清掉 (它的 last_fired 冷却桶是 scope 作用对象
    sensor/zone/None, 本来就不按事故分桶, 以库存规模为上界, 无需清理)。
    """
    while len(state.resolved_order) > state.incident_history_limit:
        victim = state.resolved_order.popleft()
        state.incidents.pop(victim, None)
        edge_key = _incident_edge_key(victim)
        for key in [k for k in state.tick_edge if k[1] == edge_key]:
            del state.tick_edge[key]


def _on_incident_event(ev: Event, state: EngineState) -> None:
    iid = ev.incident_id
    if iid is None:
        return
    if ev.kind == "incident_opened":
        state.incidents[iid] = IncidentRecord(
            incident_id=iid,
            opened_ts_ms=ev.ts_ms,
            status="open",
            status_since_ms=ev.ts_ms,
            sensor_id=ev.sensor_id,
            zone_id=ev.zone_id,
            device_id=ev.device_id,
        )
        if ev.sensor_id is not None:
            state.sensor_incident[ev.sensor_id] = iid
        return
    rec = state.incidents.get(iid)
    if rec is None:
        return
    if ev.kind == "incident_assigned" and rec.status != "resolved":
        rec.status = "assigned"
        rec.status_since_ms = ev.ts_ms
    elif ev.kind == "incident_acknowledged" and rec.status != "resolved":
        rec.status = "acknowledged"
        rec.status_since_ms = ev.ts_ms
    elif ev.kind == "incident_resolved" and rec.status != "resolved":
        # 重复的 resolved 事件不再入队, 否则同一事故占多个历史名额
        rec.status = "resolved"
        rec.status_since_ms = ev.ts_ms
        if (
            rec.sensor_id is not None
            and state.sensor_incident.get(rec.sensor_id) == iid
        ):
            del state.sensor_incident[rec.sensor_id]
        state.resolved_order.append(iid)
        _evict_resolved(state)


def _edge_fire(
    state: EngineState, policy_id: int, subject_key: int | str, satisfied: bool
) -> bool:
    """tick 驱动 trigger 的边沿判定, 键是 (policy_id, 触发主体)。
    与 cooldown 的键 (policy_id, scope 作用对象) 是两个独立机制 —— 事故 1 和
    事故 2 是两个主体、各有各的边沿, 但可能落在同一个冷却桶里 (验收 6)。"""
    key = (policy_id, subject_key)
    prev = state.tick_edge.get(key, False)
    state.tick_edge[key] = satisfied
    return satisfied and not prev


def _subject_field(subject: EffectSubject, name: str) -> int | str | None:
    value: int | str | None = getattr(subject, name)
    return value


def _fire(
    lp: LoadedPolicy, subject: EffectSubject, ts_ms: int, state: EngineState
) -> Iterator[Effect]:
    body = lp.body
    scope = body.scope
    # scope 筛选: 事件进来的第一步就筛掉不相关的策略
    if scope.type == "zone" and (
        subject.zone_id is None or subject.zone_id not in scope.ids
    ):
        return
    if scope.type == "sensor" and (
        subject.sensor_id is None or subject.sensor_id not in scope.ids
    ):
        return
    for cond in body.conditions:
        if not _condition_holds(cond, subject, ts_ms, state):
            return
    # 冷却分桶按 scope 的作用对象, 不按策略名一刀切 —— 1 区在冷却期
    # 不能吞掉后场的漏水 (SPEC-001 第四节)。
    bucket: int | None
    if scope.type == "sensor":
        bucket = subject.sensor_id
    elif scope.type == "zone":
        bucket = subject.zone_id
    else:
        bucket = None
    key = (lp.policy_id, bucket)
    last = state.last_fired.get(key)
    if last is not None and ts_ms - last < body.cooldown_s * 1000:
        # 冷却抑制的只是产出: trigger 匹配、条件求值、状态更新都已照常发生,
        # 否则滑动窗口会断, 冷却一结束的第一次判断就会算错。
        return
    # 命中即计入冷却, 哪怕下面全部动作都因缺上下文被跳过 (SPEC-001 第四节):
    # 冷却防的是命中频率, 不是产出数量。
    state.last_fired[key] = ts_ms
    for action in body.actions:
        missing = sorted(
            name
            for name in ACTION_REQUIRED_CONTEXT[action.type]
            if _subject_field(subject, name) is None
        )
        if missing:
            state.skipped.append(
                SkippedAction(
                    ts_ms=ts_ms,
                    policy_id=lp.policy_id,
                    policy_version=lp.version,
                    action_type=action.type,
                    missing=tuple(missing),
                )
            )
            continue
        yield Effect(
            ts_ms=ts_ms,
            policy_id=lp.policy_id,
            policy_version=lp.version,
            action_type=action.type,
            subject=subject,
            detail=action.model_dump(exclude={"type"}),
        )


def _condition_holds(
    cond: Condition, subject: EffectSubject, now_ms: int, state: EngineState
) -> bool:
    if isinstance(cond, WetSensorCountCondition):
        count = wet_sensor_count_now(
            state,
            count_within=cond.count_within,
            zone_id=subject.zone_id,
            window_s=cond.window_s,
        )
        if cond.op == ">=":
            return count >= cond.value
        if cond.op == "==":
            return count == cond.value
        return count <= cond.value
    if isinstance(cond, IncidentUnacknowledgedCondition):
        if subject.incident_id is None:
            return False
        rec = state.incidents.get(subject.incident_id)
        if rec is None:
            return False
        return (
            rec.status in ("open", "assigned")
            and now_ms - rec.opened_ts_ms >= cond.duration_s * 1000
        )
    return False  # 不可达: Condition 是封闭联合
