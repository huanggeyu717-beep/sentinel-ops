"""第二层: 动态验证 —— 历史数据回放 (SPEC-001 第六节)。

静态验证回答"写得对不对", 动态验证回答"它在真实数据上会干什么"。

IO 边界: 本模块住在 packages/policy_engine 里, 所以**不读文件** ——
装载场景交给 packages/scenario (那个包允许读场景文件), 这里只接收已经装载好的
事件列表 (与 scenario.Source.events 同形状的 dict 列表, 每条含相对时间 at_s)。
零 IO 这条边界不因为"回放看起来像个工具"就放宽。

硬性规定: 回放**只出警告, 不出拒绝**。历史数据是样本不是全集, 做成硬性拦截
等于用一个有限样本替人做了决定; 数字摆在审批人面前, 判断权在人手里。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .engine import (
    Effect,
    EffectSubject,
    EngineState,
    Event,
    EventKind,
    LoadedPolicy,
    SkippedAction,
    evaluate,
)

# tick 间隔与线上 SENTINEL_ENGINE_TICK_SECONDS 的默认值一致 —— 两边间隔必须一致,
# 否则模拟结果对线上没有预测力。间隔值写进报告元数据, 让回放结果可复现。
DEFAULT_TICK_SECONDS = 10
# 模拟时间轴是 [0, 最后一个事件时刻 + tail_s]: 没有这条尾巴, "持续干燥 5 分钟后
# 关单"这类规则在场景末尾永远验证不到 —— 最后一个事件之后就没有 tick 了。
DEFAULT_TAIL_S = 600
DEFAULT_HIGH_RATE_PER_HOUR = 6.0

_TELEMETRY_KINDS = frozenset({"sensor_state", "heartbeat", "rfid_scan"})


@dataclass(frozen=True)
class ReplayWarning:
    code: str  # W_HIGH_TRIGGER_RATE / W_NEVER_TRIGGERED / W_SINGLE_SUBJECT
    message: str


@dataclass(frozen=True)
class ReplayReport:
    """回放报告 (SPEC-001 第六节)。tick_seconds 与 tail_s 是复现所需的元数据;
    data_note 显式带上数据规模的说明, 不藏 —— 结论是提示性的, 不构成误报担保。

    skipped 不可省略: 引擎那层规定"缺上下文 → 不产出并记一条, 不静默丢弃",
    报告不带出来, 对审批人而言它就还是被静默丢弃了。
    """

    source: str
    events_count: int
    span_s: float  # 喂入事件的时间跨度 (不含 tail)
    tick_seconds: int
    tail_s: int
    effects: list[Effect]
    skipped: list[SkippedAction]
    by_action_type: dict[str, int]
    by_zone: dict[int, int]
    by_sensor: dict[int, int]
    warnings: list[ReplayWarning]
    data_note: str


def replay(
    policies: Sequence[LoadedPolicy],
    events: Sequence[Mapping[str, Any]],
    *,
    source: str,
    tick_seconds: int = DEFAULT_TICK_SECONDS,
    tail_s: int = DEFAULT_TAIL_S,
    high_rate_per_hour: float = DEFAULT_HIGH_RATE_PER_HOUR,
) -> ReplayReport:
    """把策略在一段历史/剧本数据上从头跑一遍, 产出完整 Effect 序列与警告。

    events 只允许三类遥测事件 —— 场景描述的是"设备发生了什么", 不该也无法描述
    "系统开了一个事故"; 事故事件由内置的事故投影器从 Effect 反向投影产生,
    并在下一个 tick 才被消费 (与线上执行器的时序一致, SPEC-006 第四节)。
    """
    telemetry = sorted(
        (_normalize(raw, i) for i, raw in enumerate(events)),
        key=lambda e: e.ts_ms,
    )
    first_ts = telemetry[0].ts_ms if telemetry else 0
    last_ts = telemetry[-1].ts_ms if telemetry else 0
    end_ms = last_ts + tail_s * 1000
    tick_ms = tick_seconds * 1000

    state = EngineState()
    projector = _IncidentProjector()
    pending: list[Event] = []  # 投影出的事故事件, 下一个 tick 回灌
    effects: list[Effect] = []
    ei = 0

    def _run(batch: Sequence[Event]) -> None:
        for eff in evaluate(policies, batch, state):
            effects.append(eff)
            pending.extend(projector.apply_effect(eff))

    t = 0
    while t <= end_ms:
        # 同一时刻遥测在前、tick 在后, tick 看到的是已推进的状态
        while ei < len(telemetry) and telemetry[ei].ts_ms <= t:
            ev = telemetry[ei]
            ei += 1
            pending.extend(projector.observe_telemetry(ev))
            _run([ev])
        batch = [*pending, Event(ts_ms=t, kind="tick")]
        pending.clear()
        _run(batch)
        t += tick_ms
    while ei < len(telemetry):  # tail_s < tick_seconds 时兜底: 剩余遥测也要进引擎
        ev = telemetry[ei]
        ei += 1
        pending.extend(projector.observe_telemetry(ev))
        _run([ev])

    span_s = (last_ts - first_ts) / 1000
    warnings = _build_warnings(
        policies, effects, state.skipped, span_s + tail_s, high_rate_per_hour
    )
    sensors_involved = {e.sensor_id for e in telemetry if e.sensor_id is not None}
    return ReplayReport(
        source=source,
        events_count=len(telemetry),
        span_s=span_s,
        tick_seconds=tick_seconds,
        tail_s=tail_s,
        effects=effects,
        skipped=list(state.skipped),
        by_action_type=dict(Counter(e.action_type for e in effects)),
        by_zone=dict(
            Counter(
                e.subject.zone_id for e in effects if e.subject.zone_id is not None
            )
        ),
        by_sensor=dict(
            Counter(
                e.subject.sensor_id
                for e in effects
                if e.subject.sensor_id is not None
            )
        ),
        warnings=warnings,
        data_note=(
            f"历史数据是样本, 不是全集: {len(telemetry)} 条事件, "
            f"跨度 {span_s:.0f} 秒, 覆盖 {len(sensors_involved)} 个传感器。"
            f"历史上没误报不等于上线后不误报, 结论仅供审批人参考。"
        ),
    )


# --------------------------------------------------------------------------- #
# 事件规范化
# --------------------------------------------------------------------------- #


def _normalize(raw: Mapping[str, Any], index: int) -> Event:
    kind = raw.get("kind")
    if kind not in _TELEMETRY_KINDS:
        raise ValueError(
            f"events[{index}]: 场景数据只允许遥测事件 {sorted(_TELEMETRY_KINDS)}, "
            f"不允许 {kind!r} —— 事故事件由事故投影器产生 (SPEC-001 第六节)"
        )
    at_s = raw.get("at_s")
    if not isinstance(at_s, (int, float)):
        raise ValueError(f"events[{index}]: 缺少数值型 at_s: {raw!r}")
    return Event(
        ts_ms=round(at_s * 1000),
        kind=cast(EventKind, kind),
        device_id=_opt(raw, "device_id", str),
        sensor_id=_opt(raw, "sensor_id", int),
        zone_id=_opt(raw, "zone_id", int),
        state=_opt(raw, "state", str),
        rfid_uid=_opt(raw, "rfid_uid", str),
    )


def _opt(raw: Mapping[str, Any], key: str, kind: type) -> Any:
    value = raw.get(key)
    return value if isinstance(value, kind) else None


# --------------------------------------------------------------------------- #
# 事故投影器
# --------------------------------------------------------------------------- #


@dataclass
class _ProjectedIncident:
    incident_id: int
    sensor_id: int | None
    zone_id: int | None
    opened_ts_ms: int
    resolved: bool = False


@dataclass
class _IncidentProjector:
    """事故投影器: 模拟侧的假执行器 (SPEC-001 第六节)。纯函数、零 IO。

    场景文件里只有遥测事件, 而 incident_elapsed / incident_unacknowledged /
    close_incident 全都依赖事故事件; 线上这些事件由 incident_service 投递,
    模拟侧没有它。这里把引擎产出的 Effect 与遥测事件反过来投影成事故事件回灌:
    open_incident -> incident_opened (id 用递增序号); close_incident ->
    incident_resolved; rfid_scan -> incident_acknowledged (取该设备所在区最早的
    未解决事故, 与 SPEC-003 决策 7 一致; 找不到则不投影)。

    已知的近似: 只模拟事故生命周期里与策略相关的那几步 —— 不模拟派单, 也不模拟
    数据库的 partial unique index。因此"同一传感器已有未解决事故时 open_incident
    是空操作"在这里**显式实现** (apply_effect 里那个 no-op 分支);
    不实现的话, 模拟会比线上多开事故。
    device -> zone 的映射从已经过去的 sensor_state 事件里学。
    """

    _next_id: int = 1
    _incidents: dict[int, _ProjectedIncident] = field(default_factory=dict)
    _open_by_sensor: dict[int, int] = field(default_factory=dict)
    _device_zone: dict[str, int] = field(default_factory=dict)

    def observe_telemetry(self, ev: Event) -> list[Event]:
        if ev.kind == "sensor_state":
            if ev.device_id is not None and ev.zone_id is not None:
                self._device_zone[ev.device_id] = ev.zone_id
            return []
        if ev.kind != "rfid_scan" or ev.device_id is None:
            return []
        zone = self._device_zone.get(ev.device_id)
        if zone is None:
            return []
        candidates = [
            p for p in self._incidents.values() if not p.resolved and p.zone_id == zone
        ]
        if not candidates:
            return []
        earliest = min(candidates, key=lambda p: (p.opened_ts_ms, p.incident_id))
        return [
            Event(
                ts_ms=ev.ts_ms,
                kind="incident_acknowledged",
                incident_id=earliest.incident_id,
            )
        ]

    def apply_effect(self, eff: Effect) -> list[Event]:
        if eff.action_type == "open_incident":
            sid = eff.subject.sensor_id
            if sid is not None and sid in self._open_by_sensor:
                return []  # 同一传感器已有未解决事故: 空操作, 对齐线上唯一索引
            iid = self._next_id
            self._next_id += 1
            self._incidents[iid] = _ProjectedIncident(
                incident_id=iid,
                sensor_id=sid,
                zone_id=eff.subject.zone_id,
                opened_ts_ms=eff.ts_ms,
            )
            if sid is not None:
                self._open_by_sensor[sid] = iid
            return [
                Event(
                    ts_ms=eff.ts_ms,
                    kind="incident_opened",
                    incident_id=iid,
                    sensor_id=sid,
                    zone_id=eff.subject.zone_id,
                    # incident_opened 必须带 device_id (SPEC-001 第一节),
                    # 否则由事故唤醒的策略产不出 set_led
                    device_id=eff.subject.device_id,
                )
            ]
        if eff.action_type == "close_incident":
            target = eff.subject.incident_id
            rec = self._incidents.get(target) if target is not None else None
            if rec is None or rec.resolved:
                return []  # 已 resolved: 空操作
            rec.resolved = True
            if (
                rec.sensor_id is not None
                and self._open_by_sensor.get(rec.sensor_id) == rec.incident_id
            ):
                del self._open_by_sensor[rec.sensor_id]
            return [
                Event(
                    ts_ms=eff.ts_ms,
                    kind="incident_resolved",
                    incident_id=rec.incident_id,
                )
            ]
        return []


# --------------------------------------------------------------------------- #
# 警告
# --------------------------------------------------------------------------- #


def _build_warnings(
    policies: Sequence[LoadedPolicy],
    effects: Sequence[Effect],
    skipped: Sequence[SkippedAction],
    simulated_s: float,
    high_rate_per_hour: float,
) -> list[ReplayWarning]:
    """只出警告, 不出拒绝 (SPEC-001 第六节的硬性规定, 不是实现者可以自行加强的)。

    simulated_s 是实际仿真时长 (span_s + tail_s): tail 期间的触发照样进分子,
    分子分母必须是同一段时间, 否则"短事件 + 长尾巴"的场景会算出荒唐的频率。
    W_NEVER_TRIGGERED 与 W_ACTIONS_SKIPPED 必须能同时出现 —— 二者一起才说得清
    "一次都没产出"的真实原因是条件太严还是数据缺字段。
    """
    firings: dict[int, list[tuple[int, EffectSubject]]] = {}
    for eff in effects:
        per_policy = firings.setdefault(eff.policy_id, [])
        key = (eff.ts_ms, eff.subject)
        if key not in per_policy:  # 一次命中可产出多个 Effect, 只算一次触发
            per_policy.append(key)

    warnings: list[ReplayWarning] = []
    for lp in sorted(policies, key=lambda p: p.policy_id):
        fired = firings.get(lp.policy_id, [])
        if not fired:
            warnings.append(
                ReplayWarning(
                    code="W_NEVER_TRIGGERED",
                    message=(
                        f"策略 {lp.policy_id} 在这段数据上一次都没产出 —— "
                        f"可能条件写太严, 或数据里就没有这种情况; "
                        f"若同时有 W_ACTIONS_SKIPPED, 原因是数据缺字段而非条件"
                    ),
                )
            )
        if fired and simulated_s > 0:
            rate = len(fired) / (simulated_s / 3600)
            if rate > high_rate_per_hour:
                warnings.append(
                    ReplayWarning(
                        code="W_HIGH_TRIGGER_RATE",
                        message=(
                            f"策略 {lp.policy_id} 折算触发频率 {rate:.1f} 次/小时, "
                            f"超过阈值 {high_rate_per_hour:g} 次/小时 "
                            f"(共 {len(fired)} 次 / 仿真 {simulated_s:.0f} 秒)"
                        ),
                    )
                )
        if len(fired) >= 2:
            sensors = {s.sensor_id for _, s in fired}
            zones = {s.zone_id for _, s in fired}
            if sensors != {None} and len(sensors) == 1:
                warnings.append(
                    ReplayWarning(
                        code="W_SINGLE_SUBJECT",
                        message=(
                            f"策略 {lp.policy_id} 的全部 {len(fired)} 次触发都集中在 "
                            f"传感器 {next(iter(sensors))} 上"
                        ),
                    )
                )
            elif zones != {None} and len(zones) == 1:
                warnings.append(
                    ReplayWarning(
                        code="W_SINGLE_SUBJECT",
                        message=(
                            f"策略 {lp.policy_id} 的全部 {len(fired)} 次触发都集中在 "
                            f"区域 {next(iter(zones))} 上"
                        ),
                    )
                )
        per_skip = Counter(
            (s.action_type, s.missing)
            for s in skipped
            if s.policy_id == lp.policy_id
        )
        if per_skip:
            parts = "; ".join(
                f"{action} 缺 {', '.join(missing)} ×{count}"
                for (action, missing), count in sorted(per_skip.items())
            )
            warnings.append(
                ReplayWarning(
                    code="W_ACTIONS_SKIPPED",
                    message=(
                        f"策略 {lp.policy_id} 有 {sum(per_skip.values())} 次动作"
                        f"因缺上下文未产出: {parts}"
                    ),
                )
            )
    return warnings
