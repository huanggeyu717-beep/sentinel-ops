"""W3 验收: 引擎接管开关事故的线上链路 (policy_runtime)。对应 docs/specs/SPEC-006 第四节。

后台 tick 用墙上时钟, 用例的场景时间戳是假的, 所以 tick 由用例显式注入
(conftest 已把后台 tick 间隔调到 1 小时)。场景事件走 ingest_service ——
与 /ingest 路由同一份代码, 只少 HTTP 一跳; tick 结构与回放模块一致:
同一时刻遥测在前、tick 在后, 域事件在下一个 tick 才被消费。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import insert_published_policy
from scenario import load_source
from sqlalchemy import text

from app.services import incident_service, ingest_service, policy_runtime, policy_service
from app.services.auth_service import AuthUser

T0 = 1_773_600_000_000
REPO_ROOT = Path(__file__).resolve().parents[3]

CHRIS = AuthUser(id=2, email="chris@example.com", display_name="Chris Li",
                 employee_id=3, roles=["manager"])

WET_OPEN_SENSORS = {
    "scope": {"type": "sensor", "ids": [1, 2]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}


async def run_scenario(factory, name: str, epoch_ms: int, tick_seconds: int = 10,
                       tail_s: int = 600) -> None:
    """按场景时间轴投递事件并注入 tick, 时序与 replay() 相同 (SPEC-006 第四节):
    遥测在到达时刻立即进引擎, tick 消费上一轮攒下的域事件。"""
    src = load_source(str(REPO_ROOT / "scenarios" / f"{name}.yaml"), None)
    events = sorted(src.events, key=lambda e: e["at_s"])
    rt = policy_runtime.runtime()
    end_s = events[-1]["at_s"] + tail_s
    ei, t = 0, 0
    while t <= end_s:
        while ei < len(events) and events[ei]["at_s"] <= t:
            ev = events[ei]
            ei += 1
            payload = {k: v for k, v in ev.items() if k != "at_s" and v is not None}
            payload["ts"] = epoch_ms + int(ev["at_s"] * 1000)
            # 每条事件独立事务, 与 drill/真机逐条上报一致
            async with factory() as session, session.begin():
                await ingest_service.ingest_event(session, payload)
        await rt.tick(factory, now_ms=epoch_ms + t * 1000)
        t += tick_seconds


# ===== 验收 9: 场景验证策略确实接管了开事故 =====

def test_scenario_multi_sensor__policy_opens_and_escalation_leaves_trace(svc):
    async def go(factory):
        wet_open = await insert_published_policy(
            "wet-open-s12", json.dumps(WET_OPEN_SENSORS)
        )
        escalate = await insert_published_policy("escalate-unacked", json.dumps({
            "scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 120},
            "conditions": [{"type": "wet_sensor_count", "count_within": "same_zone",
                            "op": ">=", "value": 2, "window_s": 180}],
            "actions": [{"type": "notify", "channel": "email", "target_role": "manager"},
                        {"type": "set_led", "target": "incident_device", "state": "ON"}],
            "cooldown_s": 300,
        }))
        await run_scenario(factory, "multi_sensor_escalation", T0)

        async with factory() as s:
            incidents = (await s.execute(text(
                "SELECT id, sensor_id, status FROM incidents ORDER BY id"
            ))).mappings().all()
        # 两个传感器各开一条; t=200s 的刷卡把最早那条接走
        assert [i["sensor_id"] for i in incidents] == [1, 2]
        assert incidents[0]["status"] == "acknowledged"

        # policy_runs 能查到是哪条策略哪一版开的 (验收 9)
        async with factory() as s:
            runs = (await s.execute(text(
                "SELECT policy_id, effects FROM policy_runs ORDER BY id"
            ))).mappings().all()
        opens = [
            e for r in runs if r["policy_id"] == wet_open["policy_id"]
            for e in (r["effects"] if isinstance(r["effects"], list)
                      else json.loads(r["effects"]))
        ]
        assert {e["incident_id"] for e in opens if e["action_type"] == "open_incident"} \
            == {incidents[0]["id"], incidents[1]["id"]}
        assert any(r["policy_id"] == escalate["policy_id"] for r in runs)

        # 升级策略触发: notify/set_led 不接真实出口, 但事故时间线看得见这次决定
        async with factory() as s:
            kinds = (await s.execute(text(
                "SELECT kind FROM incident_events WHERE incident_id = :id ORDER BY at, id"
            ), {"id": incidents[0]["id"]})).scalars().all()
        assert "notify" in kinds and "set_led" in kinds

    svc(go)


# ===== 自动关单改由 sensor_dry_for 策略实现 (原 W2 硬编码行为的等价物) =====

def test_scenario_auto_close__policy_closes_after_dry_window(svc):
    async def go(factory):
        await insert_published_policy("wet-open-s1", json.dumps({
            **WET_OPEN_SENSORS, "scope": {"type": "sensor", "ids": [1]},
        }))
        closer = await insert_published_policy("dry-close", json.dumps({
            "scope": {"type": "sensor", "ids": [1]},
            "trigger": {"type": "sensor_dry_for", "dry_for_s": 300},
            "conditions": [],
            "actions": [{"type": "close_incident"}],
            "cooldown_s": 60,
        }))
        await run_scenario(factory, "auto_close", T0)

        async with factory() as s:
            one = (await s.execute(text(
                "SELECT id, status, resolved_by, resolved_at FROM incidents"
            ))).mappings().one()
        assert one["status"] == "resolved"
        # 自动解决口径改为 policy:{id}@v{version} (SPEC-006 对 SPEC-003 的修订 1)
        assert one["resolved_by"] == f"policy:{closer['policy_id']}@v1"
        async with factory() as s:
            kinds = (await s.execute(text(
                "SELECT kind FROM incident_events WHERE incident_id = :id ORDER BY at, id"
            ), {"id": one["id"]})).scalars().all()
        assert kinds == ["opened", "sensor_dry", "resolved"]

    svc(go)


def test_dry_interrupted_by_wet__policy_does_not_close(svc):
    """转干未满稳定窗口又转湿: dry_since 被重置, 不关单也不重复开单
    (原 W2 行为 test_auto_resolve__skipped_when_dry_window_not_met 的引擎等价)。"""
    async def go(factory):
        await insert_published_policy("wet-open-s1", json.dumps({
            **WET_OPEN_SENSORS, "scope": {"type": "sensor", "ids": [1]},
        }))
        await insert_published_policy("dry-close", json.dumps({
            "scope": {"type": "sensor", "ids": [1]},
            "trigger": {"type": "sensor_dry_for", "dry_for_s": 300},
            "conditions": [],
            "actions": [{"type": "close_incident"}],
            "cooldown_s": 60,
        }))
        rt = policy_runtime.runtime()

        async def ingest(at_s: int, state: str) -> None:
            async with factory() as s, s.begin():
                await ingest_service.ingest_event(s, {
                    "kind": "sensor_state", "device_id": "Arduino1",
                    "ts": T0 + at_s * 1000, "sensor_id": 1, "state": state,
                    "value": 845 if state == "WET" else 90,
                })

        await ingest(0, "WET")
        await rt.tick(factory, now_ms=T0)
        await ingest(100, "DRY")
        await ingest(250, "WET")   # 干了 150s < 300s 又湿
        for at_s in range(10, 700, 10):
            await rt.tick(factory, now_ms=T0 + at_s * 1000)

        async with factory() as s:
            rows = (await s.execute(text("SELECT status FROM incidents"))).scalars().all()
        assert rows == ["open"]

    svc(go)


# ===== 验收 21: 撤销发布后缓存立即失效 =====

def test_revoke__next_event_no_longer_matches_policy(svc):
    async def go(factory):
        seeded = await insert_published_policy("wet-open-zones", json.dumps({
            "scope": {"type": "zone", "ids": [1, 2, 3]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [],
            "actions": [{"type": "open_incident", "severity": "normal"}],
            "cooldown_s": 60,
        }))

        async def wet(sensor_id: int, device: str, at_s: int):
            async with factory() as s, s.begin():
                return await ingest_service.ingest_event(s, {
                    "kind": "sensor_state", "device_id": device, "ts": T0 + at_s * 1000,
                    "sensor_id": sensor_id, "state": "WET", "value": 845,
                })

        first = await wet(1, "Arduino1", 0)
        assert first.incident_id is not None  # 策略在线, 湿了开单

        async with factory() as s, s.begin():
            await policy_service.revoke_publication(s, seeded["policy_id"], CHRIS)

        second = await wet(4, "Arduino2", 5)  # 另一传感器, 若策略还在必然开单
        assert second.incident_id is None
        async with factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM incidents"))).scalar_one()
        assert count == 1

    svc(go)


# ===== 域事件与数据库的一致性: 投递挂在事务提交上 =====

def test_domain_events__rollback_means_engine_never_sees_them(svc):
    async def go(factory):
        rt = policy_runtime.runtime()
        with pytest.raises(RuntimeError):
            async with factory() as s, s.begin():
                await incident_service.open_incident(s, 1, "normal", T0, "policy:9@v1")
                raise RuntimeError("boom")
        assert len(rt._pending) == 0  # 事务回滚 -> 事件不存在
        async with factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM incidents"))).scalar_one()
        assert count == 0

        # 对照: 提交成功才进队列
        async with factory() as s, s.begin():
            opened = await incident_service.open_incident(s, 1, "normal", T0, "policy:9@v1")
        assert opened is not None
        assert [e.kind for e in rt._pending] == ["incident_opened"]

    svc(go)


def test_open_incident__idempotent_against_partial_unique_index(svc):
    """幂等表第一行: 已有未解决事故时 open 撞唯一索引 = 空操作 (SPEC-006 第四节)。"""
    async def go(factory):
        async with factory() as s, s.begin():
            first = await incident_service.open_incident(s, 1, "normal", T0, "policy:9@v1")
        async with factory() as s, s.begin():
            dup = await incident_service.open_incident(s, 1, "high", T0 + 1000, "policy:9@v1")
        assert first is not None and dup is None

    svc(go)


def test_escalate__noop_when_target_severity_reached(svc):
    async def go(factory):
        async with factory() as s, s.begin():
            iid = await incident_service.open_incident(s, 1, "normal", T0, "policy:9@v1")
        async with factory() as s, s.begin():
            up = await incident_service.escalate_incident(s, iid, "high", T0, "policy:9@v1")
        async with factory() as s, s.begin():
            again = await incident_service.escalate_incident(s, iid, "high", T0, "policy:9@v1")
        async with factory() as s, s.begin():
            down = await incident_service.escalate_incident(s, iid, "normal", T0, "policy:9@v1")
        assert up == iid and again is None and down is None  # 只升不降, 已达标即空操作

    svc(go)


# ===== 状态推进与副作用同生共死 (SPEC-006 第四节): 两条路径的失败回退 =====

def test_on_telemetry_failure__state_rolls_back_and_retry_reopens(svc, monkeypatch):
    """应用 Effect 时数据库出错 -> 请求事务回滚, 引擎状态必须跟着回退。

    不回退的话: wet_since / 边沿已推进, 同一传感器持续报湿不构成新边沿,
    这张单永远补不回来。回退后设备重试同一事件即可补开。
    """
    async def go(factory):
        await insert_published_policy("wet-open-s1", json.dumps({
            **WET_OPEN_SENSORS, "scope": {"type": "sensor", "ids": [1]},
        }))
        real = incident_service.open_incident
        boom = {"armed": True}

        async def flaky(session, sensor_id, severity, ts, actor):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("模拟数据库出错")
            return await real(session, sensor_id, severity, ts, actor)

        monkeypatch.setattr(incident_service, "open_incident", flaky)
        rt = policy_runtime.runtime()
        wet = {"kind": "sensor_state", "device_id": "Arduino1", "ts": T0,
               "sensor_id": 1, "state": "WET", "value": 845}

        with pytest.raises(RuntimeError):
            async with factory() as s, s.begin():
                await ingest_service.ingest_event(s, wet)
        # 失败后: 引擎状态与域事件队列都回到本轮之前, 库里也没有事故
        assert rt._state.wet_since == {} and rt._state.last_fired == {}
        assert len(rt._pending) == 0
        async with factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM incidents"))).scalar_one()
        assert count == 0

        # 设备重试同一条事件: 边沿重新构成, 开单成功
        async with factory() as s, s.begin():
            r = await ingest_service.ingest_event(s, wet)
        assert r.incident_id is not None
        assert [e.kind for e in rt._pending] == ["incident_opened"]

    svc(go)


def test_tick_failure__state_and_domain_events_roll_back_then_retry(svc, monkeypatch):
    """tick 的事务失败 -> 本轮取走的域事件放回队首, 引擎状态回退, 下一轮原样重试。

    不回退的话: incident_elapsed 的边沿标记已置位, 重试的 tick 判定"已触发过",
    这次没写进库的升级永远丢失。
    """
    async def go(factory):
        await insert_published_policy("elapse-escalate", json.dumps({
            "scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 120},
            "conditions": [],
            "actions": [{"type": "escalate_incident", "to_severity": "high"}],
            "cooldown_s": 60,
        }))
        rt = policy_runtime.runtime()
        async with factory() as s, s.begin():
            iid = await incident_service.open_incident(s, 1, "normal", T0, "user:2")
        assert [e.kind for e in rt._pending] == ["incident_opened"]

        real = incident_service.escalate_incident
        boom = {"armed": True}

        async def flaky(session, incident_id, to_severity, ts, actor):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("模拟数据库出错")
            return await real(session, incident_id, to_severity, ts, actor)

        monkeypatch.setattr(incident_service, "escalate_incident", flaky)

        # 这个 tick 消费 incident_opened 并触发升级, 应用 Effect 时事务失败
        with pytest.raises(RuntimeError):
            await rt.tick(factory, now_ms=T0 + 130_000)
        # 域事件放回队首, 引擎状态回到本轮之前 (事故投影不存在)
        assert [e.kind for e in rt._pending] == ["incident_opened"]
        assert rt._state.incidents == {} and rt._state.tick_edge == {}
        async with factory() as s:
            sev = (await s.execute(
                text("SELECT severity FROM incidents WHERE id = :id"), {"id": iid}
            )).scalar_one()
        assert sev == "normal"

        # 下一轮原样重试: 边沿重新构成, 升级成功落库
        await rt.tick(factory, now_ms=T0 + 140_000)
        assert len(rt._pending) == 0
        async with factory() as s:
            sev = (await s.execute(
                text("SELECT severity FROM incidents WHERE id = :id"), {"id": iid}
            )).scalar_one()
        assert sev == "high"

    svc(go)


def test_engine_state__history_limit_wired_from_settings():
    """上界从 SENTINEL_ENGINE_INCIDENT_HISTORY 进 EngineState (SPEC-001 第四节末)。"""
    from app.config import settings

    rt = policy_runtime.reset_runtime()
    assert rt._state.incident_history_limit == settings().engine_incident_history


# ===== 验收 23: tick 后台任务干净取消 =====

def test_tick_loop__cancels_cleanly_without_hanging():
    async def go():
        task = asyncio.create_task(policy_runtime.tick_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done and task.cancelled()

    asyncio.run(go())
