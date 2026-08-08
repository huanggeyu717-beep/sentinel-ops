"""把策略引擎接进请求链路 (SPEC-006 第四节)。

职责四件:
1. 加载当前生效的策略集 (policy_publications 里 revoked_at IS NULL 的版本),
   带进程内缓存, 发布/撤销的事务提交后失效;
2. 把 /ingest 的遥测事件与事故域事件规范化成引擎的 Event ——
   zone_id 的唯一事实源是数据库, 遥测报文与 CSV 里没有它, 在这里从 sensors 表补上
   (SPEC-001 验收 8 的已知限制在 service 层收口, 补完之后 CSV 才对全部动作类型可用);
3. 调 evaluate() 拿到 Effect 序列 (执行器与模拟器是同一份 evaluate, 不变量 2);
4. Effect 应用器: 唯一产生副作用的地方, 每类动作都有幂等保证, 全部落 policy_runs。

时序与防递归 (与回放模块必须一致, 否则模拟对线上没有预测力):
- 遥测事件在到达的请求事务里立即进引擎, Effect 就地应用;
- 事故域事件由 incident_service 在写库的同一事务里投递 (先攒在 session.info,
  提交成功才进待消费队列, 回滚则不存在), 在**下一个 tick** 才被引擎消费;
- 应用 Effect 的过程中绝不递归调用 evaluate() —— 本轮 Effect 全部收集并应用完,
  新产生的域事件才排进下一轮。

已知边界 (SPEC-006, 按要求写进代码而不只写在文档):
- 多实例部署会重复 tick, 且生效策略集与引擎状态在各实例内存里各存一份,
  发布后失效时机不同步。W3 是单实例; W6 扩多实例需要选主或数据库咨询锁。
- 进程重启后引擎从空状态重新学习: 重启前已开事故的 incident_elapsed 类触发
  在重启后不再命中, 直到有新的域事件喂进来。
- notify / set_led 在 W3 不产生真实外部动作 (原系统的 SES 与 IoT Core 已随
  小组账号注销), 只落 policy_runs 与事故时间线; 真实出口 W6 上线时补。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session as SyncSession

from policy_engine import (
    Effect,
    EngineState,
    Event,
    LoadedPolicy,
    Policy,
    SkippedAction,
    evaluate,
)

from ..config import settings
from . import incident_service

log = logging.getLogger(__name__)

# policy_service 在发布/撤销的事务里做标记, 提交成功后才让缓存失效 (见 _on_commit)
INVALIDATE_INFO_KEY = "sentinel_invalidate_policy_cache"


@dataclass(frozen=True)
class AppliedEffect:
    """一个 Effect 的应用结果, 也是 policy_runs.effects 的一项。"""

    effect: Effect
    outcome: str  # applied / noop / recorded (notify、set_led 只留记录)
    incident_id: int | None


@dataclass(frozen=True)
class _ActivePolicies:
    loaded: list[LoadedPolicy]
    version_ids: dict[int, int]  # policy_id -> policy_versions.id (写 policy_runs 用)


_ACTIVE = text("""
    SELECT pp.policy_id, pv.id AS version_id, pv.version, pv.body
    FROM policy_publications pp
    JOIN policy_versions pv ON pv.id = pp.policy_version_id
    WHERE pp.revoked_at IS NULL
    ORDER BY pp.policy_id
""")

_SENSOR_ZONE = text("SELECT zone_id FROM sensors WHERE id = :sensor_id")

_INSERT_RUN = text("""
    INSERT INTO policy_runs (policy_version_id, policy_id, fired_at, effects)
    VALUES (:version_id, :policy_id,
            to_timestamp(CAST(:fired_at_ms AS bigint) / 1000.0), CAST(:effects AS jsonb))
""")


def _parse_body(body: Any) -> Policy:
    return Policy.model_validate(body if isinstance(body, dict) else json.loads(body))


class PolicyRuntime:
    """进程内单例: 引擎状态 + 生效策略集缓存 + 待消费域事件队列。

    _lock 串行化所有 evaluate 调用 —— EngineState 不是并发安全的,
    并发的 /ingest 请求与 tick 任务必须排队 (W3 单实例, 锁的代价可忽略)。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # 已解决事故只留最近 N 条 (SPEC-001 第四节末): tick 每轮的状态快照与
        # incident_elapsed 的遍历开销都必须随"当前未解决数"而不是"历史开单数"变化
        self._state = EngineState(
            incident_history_limit=settings().engine_incident_history
        )
        self._pending: deque[Event] = deque()
        self._cache: _ActivePolicies | None = None

    # ----- 缓存与队列 -----

    def invalidate_cache(self) -> None:
        self._cache = None

    def enqueue_domain_events(self, events: list[dict[str, Any]]) -> None:
        """提交成功的事务里攒下的域事件进入待消费队列, 下一个 tick 消费。"""
        for d in events:
            self._pending.append(Event(**d))

    async def _active(self, session: AsyncSession) -> _ActivePolicies:
        if self._cache is None:
            rows = (await session.execute(_ACTIVE)).mappings().all()
            self._cache = _ActivePolicies(
                loaded=[
                    LoadedPolicy(
                        policy_id=r["policy_id"],
                        version=r["version"],
                        body=_parse_body(r["body"]),
                    )
                    for r in rows
                ],
                version_ids={r["policy_id"]: r["version_id"] for r in rows},
            )
        return self._cache

    # ----- 事件入口 -----

    async def on_telemetry(
        self,
        session: AsyncSession,
        *,
        kind: str,
        ts: int,
        device_id: str | None = None,
        sensor_id: int | None = None,
        state: str | None = None,
        rfid_uid: str | None = None,
    ) -> list[AppliedEffect]:
        """/ingest 的遥测事件立即进引擎, Effect 在同一请求事务里应用。

        只在状态真正被推进时调用 (乱序被拒的旧事件不进引擎, 与时间线同口径) ——
        引擎按时间序消费事件, 喂乱序会弄脏 wet_since 等投影。
        """
        zone_id: int | None = None
        if kind == "sensor_state" and sensor_id is not None:
            zone_id = (
                await session.execute(_SENSOR_ZONE, {"sensor_id": sensor_id})
            ).scalar_one_or_none()
        ev = Event(
            ts_ms=ts, kind=kind,  # type: ignore[arg-type]
            device_id=device_id, sensor_id=sensor_id, zone_id=zone_id,
            state=state, rfid_uid=rfid_uid,
        )
        async with self._lock:
            # 状态推进与副作用同生共死 (SPEC-006 第四节), 与 tick() 同一套保护:
            # 应用 Effect 失败会让请求事务整体回滚, 引擎状态必须跟着回退 ——
            # sensor_state_changed 是边沿触发, 状态推进了而工单没开成, 持续报湿
            # 不构成新边沿, 那张单永远补不回来; 回退后设备重试同一事件即可补开。
            # (域事件在 session.info 里, 随请求事务的回滚由 _on_rollback 丢弃。)
            state_backup = copy.deepcopy(self._state)
            try:
                return await self._run(session, [ev])
            except BaseException:
                self._state = state_backup
                raise

    async def tick(
        self,
        factory: async_sessionmaker[AsyncSession],
        now_ms: int | None = None,
    ) -> list[AppliedEffect]:
        """一个 tick: 先消费上一轮攒下的域事件, 再投 tick 事件, 自己开事务。

        与回放模块的批次结构一致 (先 pending 后 tick, SPEC-006 第四节)。
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with self._lock:
            drained: list[Event] = []
            while self._pending:
                drained.append(self._pending.popleft())
            batch = [*drained, Event(ts_ms=now, kind="tick")]
            state_backup = copy.deepcopy(self._state)
            try:
                async with factory() as session, session.begin():
                    return await self._run(session, batch)
            except BaseException:
                # 事务失败: 域事件放回队首、引擎状态回退到本轮之前,
                # 下一个 tick 原样重试 —— 否则边沿触发的"已触发过"标记会把
                # 这次没写进库的 Effect 永远吞掉。
                self._state = state_backup
                for ev in reversed(drained):
                    self._pending.appendleft(ev)
                raise

    # ----- 引擎调用与 Effect 应用 -----

    async def _run(self, session: AsyncSession, events: list[Event]) -> list[AppliedEffect]:
        active = await self._active(session)
        # 防递归 (SPEC-006 第四节): 先收集完本轮全部 Effect, 再逐个应用。
        # 应用过程中 incident_service 产生的域事件只进 session 缓冲, 事务提交后
        # 才排进 _pending、下一个 tick 才被消费 —— 这里绝不再次调用 evaluate()。
        effects = list(evaluate(active.loaded, events, self._state))
        skipped = list(self._state.skipped)
        self._state.skipped.clear()
        applied = [await self._apply(session, eff) for eff in effects]
        if applied or skipped:
            await self._record_runs(session, active, applied, skipped)
        return applied

    async def _apply(self, session: AsyncSession, eff: Effect) -> AppliedEffect:
        """Effect -> 副作用。幂等保证 (SPEC-006 第四节的表):
        open 撞 partial unique index、close 已 resolved、escalate 已达标 => 空操作。
        """
        actor = f"policy:{eff.policy_id}@v{eff.policy_version}"
        subject = eff.subject
        if eff.action_type == "open_incident":
            if subject.sensor_id is None:  # 引擎已按 ACTION_REQUIRED_CONTEXT 拦住, 兜底
                return AppliedEffect(eff, "noop", None)
            opened = await incident_service.open_incident(
                session, subject.sensor_id, eff.detail["severity"], eff.ts_ms, actor
            )
            return AppliedEffect(eff, "applied" if opened is not None else "noop", opened)
        if eff.action_type == "close_incident":
            if subject.incident_id is None:
                return AppliedEffect(eff, "noop", None)
            closed = await incident_service.close_incident(
                session, subject.incident_id, eff.ts_ms, actor
            )
            return AppliedEffect(
                eff, "applied" if closed is not None else "noop", subject.incident_id
            )
        if eff.action_type == "escalate_incident":
            if subject.incident_id is None:
                return AppliedEffect(eff, "noop", None)
            escalated = await incident_service.escalate_incident(
                session, subject.incident_id, eff.detail["to_severity"], eff.ts_ms, actor
            )
            return AppliedEffect(
                eff, "applied" if escalated is not None else "noop", subject.incident_id
            )
        # notify / set_led: W3 不接真实出口, 只留记录 (见模块 docstring 已知边界)。
        # 有事故可挂时额外记一条时间线; 没有时只落 policy_runs ——
        # incident_events.incident_id 是外键, "设备离线通知管理员"这类 Effect 硬塞会撞它。
        if subject.incident_id is not None:
            await incident_service.record_policy_decision(
                session, subject.incident_id, eff.action_type, eff.ts_ms, actor, eff.detail
            )
        return AppliedEffect(eff, "recorded", subject.incident_id)

    async def _record_runs(
        self,
        session: AsyncSession,
        active: _ActivePolicies,
        applied: list[AppliedEffect],
        skipped: list[SkippedAction],
    ) -> None:
        """本轮触发按策略归组落 policy_runs (线上触发历史 / Trace)。

        缺上下文被跳过的动作也落进来 —— 引擎那层规定"不产出并记一条, 不静默丢弃",
        这里不带出来它就还是被静默丢弃了 (SPEC-001 第三节)。
        """
        runs: dict[int, dict[str, Any]] = {}
        for ae in applied:
            eff = ae.effect
            entry = runs.setdefault(
                eff.policy_id, {"version": eff.policy_version, "fired_at_ms": eff.ts_ms,
                                "effects": []}
            )
            entry["fired_at_ms"] = max(entry["fired_at_ms"], eff.ts_ms)
            entry["effects"].append({
                "ts_ms": eff.ts_ms,
                "action_type": eff.action_type,
                "subject": asdict(eff.subject),
                "detail": eff.detail,
                "outcome": ae.outcome,
                "incident_id": ae.incident_id,
            })
        for sk in skipped:
            entry = runs.setdefault(
                sk.policy_id, {"version": sk.policy_version, "fired_at_ms": sk.ts_ms,
                               "effects": []}
            )
            entry["fired_at_ms"] = max(entry["fired_at_ms"], sk.ts_ms)
            entry["effects"].append({
                "ts_ms": sk.ts_ms,
                "action_type": sk.action_type,
                "outcome": "skipped",
                "reason": sk.reason,
                "missing": list(sk.missing),
            })
        for policy_id, entry in runs.items():
            await session.execute(_INSERT_RUN, {
                "version_id": active.version_ids[policy_id],
                "policy_id": policy_id,
                "fired_at_ms": entry["fired_at_ms"],
                "effects": json.dumps(entry["effects"], ensure_ascii=False),
            })


# --------------------------------------------------------------------------- #
# 进程内单例与事务提交挂钩
# --------------------------------------------------------------------------- #

_runtime = PolicyRuntime()


def runtime() -> PolicyRuntime:
    return _runtime


def reset_runtime() -> PolicyRuntime:
    """测试夹具用: 换一个全新实例 (引擎状态、队列、缓存、锁全部归零)。

    asyncio.Lock 在第一次 acquire 时绑定事件循环, 跨用例复用会把上一个用例的
    循环带进来 —— 每个用例都要重置。
    """
    global _runtime
    _runtime = PolicyRuntime()
    return _runtime


def invalidate_on_commit(session: AsyncSession) -> None:
    """发布/撤销走这里: 事务提交成功后再让缓存失效 (提交前失效会让并发请求
    把未提交的旧集合重新缓存住)。"""
    session.sync_session.info[INVALIDATE_INFO_KEY] = True


@sa_event.listens_for(SyncSession, "after_commit")
def _on_commit(sync_session: SyncSession) -> None:
    """事务提交成功 => 攒下的域事件进队列、缓存失效标记生效。

    incident_service 只往 session.info 写, 不 import 本模块 (避免循环依赖);
    回滚的事务在 _on_rollback 里整体丢弃 —— 引擎看到的与库里的永远一致。
    """
    events = sync_session.info.pop(incident_service.DOMAIN_EVENTS_INFO_KEY, None)
    if events:
        runtime().enqueue_domain_events(events)
    if sync_session.info.pop(INVALIDATE_INFO_KEY, None):
        runtime().invalidate_cache()


@sa_event.listens_for(SyncSession, "after_rollback")
def _on_rollback(sync_session: SyncSession) -> None:
    sync_session.info.pop(incident_service.DOMAIN_EVENTS_INFO_KEY, None)
    sync_session.info.pop(INVALIDATE_INFO_KEY, None)


# --------------------------------------------------------------------------- #
# tick 后台任务
# --------------------------------------------------------------------------- #


async def tick_loop() -> None:
    """apps/api 启动时拉起, 每 SENTINEL_ENGINE_TICK_SECONDS 秒投一个 tick 事件。

    - tick 间隔必须与回放模块的 DEFAULT_TICK_SECONDS 一致 (SPEC-001 第一节),
      否则模拟结果对线上没有预测力;
    - **已知边界: 多实例部署会重复 tick** —— 每个进程各有时钟与引擎状态。
      W3 是单实例; W6 若扩多实例需要选主或改用数据库咨询锁 (SPEC-006 第四节);
    - 不进 agent_tasks 表: 那张表有审计/重试/死信语义, tick 是纯时钟;
    - 单轮失败只记日志不退出, 域事件已由 tick() 放回队首, 下一轮重试;
    - 关停由 lifespan cancel, 先 sleep 后 tick 保证取消点永远可达, 测试不挂住。
    """
    from ..db import session_factory

    while True:
        await asyncio.sleep(settings().engine_tick_seconds)
        try:
            await runtime().tick(session_factory())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("engine tick 执行失败, 下一轮重试")
