"""report_service.load_incident_facts 的真库测试 (事实包读库的那一半, SPEC-008 第二节)。

造数用裸 SQL (绕过 service 直插 incidents / incident_events), 读取走 service;
区域与员工用 dev seed 的固定行 (zone 1 生鲜区; 员工 1 Alex zone 1 / 2 Bo zone 2)。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_agent_helpers import db

from app.services import report_service
from app.services.incident_service import IncidentNotFound
from app.services.report_render import MISSING_TEXT, build_fact_pack, check_draft

T0 = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)


async def _insert_incident(conn, **cols) -> int:
    fields = {
        "zone_id": 1, "sensor_id": 1, "severity": "normal", "status": "resolved",
        "opened_at": T0, "resolved_at": T0 + timedelta(hours=1),
        "resolved_by": "user:2",
        **cols,
    }
    names = ", ".join(fields)
    placeholders = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    incident_id: int = await conn.fetchval(
        f"INSERT INTO incidents ({names}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return incident_id


async def _insert_event(conn, incident_id: int, kind: str, at: datetime) -> None:
    await conn.execute(
        "INSERT INTO incident_events (incident_id, kind, actor, at) "
        "VALUES ($1, $2, 'system', $3)",
        incident_id, kind, at,
    )


def _load(svc, incident_id: int):
    async def go(factory):
        async with factory() as session:
            return await report_service.load_incident_facts(session, incident_id)

    return svc(go)


def test_load_incident_facts__joins_names_zone_and_orders_events(client, svc):
    async def setup(conn):
        incident_id = await _insert_incident(
            conn,
            assigned_employee_id=1, assigned_at=T0 + timedelta(minutes=3),
            acknowledged_by_employee_id=2,
            acknowledged_at=T0 + timedelta(minutes=12),
        )
        # 乱序插入, 断言读出来按 at 升序
        await _insert_event(conn, incident_id, "resolved", T0 + timedelta(hours=1))
        await _insert_event(conn, incident_id, "opened", T0)
        await _insert_event(conn, incident_id, "acknowledged", T0 + timedelta(minutes=12))
        return incident_id

    incident_id = db(setup)
    raw = _load(svc, incident_id)

    inc = raw.incident
    assert inc["zone_name"] == "Zone 1 - 生鲜区"
    assert inc["assigned_employee_name"] == "Alex Chen"
    assert inc["assigned_employee_zone_id"] == 1
    assert inc["acknowledged_by_employee_name"] == "Bo Wang"
    assert inc["resolved_by"] == "user:2"
    assert [e["kind"] for e in raw.events] == ["opened", "acknowledged", "resolved"]


def test_load_incident_facts__unknown_incident_raises(client, svc):
    with pytest.raises(IncidentNotFound):
        _load(svc, 999_999)


def test_sensor_30d_count__window_anchored_on_opened_at(client, svc):
    async def setup(conn):
        # 同一传感器: 40 天前的不算, 10 天前的算, 本单自己也算 -> 2
        await _insert_incident(
            conn, opened_at=T0 - timedelta(days=40),
            resolved_at=T0 - timedelta(days=40, hours=-1),
        )
        await _insert_incident(
            conn, opened_at=T0 - timedelta(days=10),
            resolved_at=T0 - timedelta(days=10, hours=-1),
        )
        return await _insert_incident(conn)

    incident_id = db(setup)
    raw = _load(svc, incident_id)
    assert raw.sensor_30d_count == 2


def test_zone_concurrent__counts_overlapping_same_zone_only(client, svc):
    async def setup(conn):
        me = await _insert_incident(conn)  # zone 1, [T0, T0+1h]
        # 同区且存续重叠 -> 算
        await _insert_incident(
            conn, sensor_id=2, opened_at=T0 + timedelta(minutes=30),
            resolved_at=T0 + timedelta(hours=3),
        )
        # 同区但在本单开单前已解决 -> 不算
        await _insert_incident(
            conn, sensor_id=2, opened_at=T0 - timedelta(hours=5),
            resolved_at=T0 - timedelta(hours=4),
        )
        # 存续重叠但不同区 -> 不算
        await _insert_incident(
            conn, zone_id=2, sensor_id=3, opened_at=T0 + timedelta(minutes=10),
            resolved_at=T0 + timedelta(hours=2),
        )
        return me

    incident_id = db(setup)
    raw = _load(svc, incident_id)
    assert raw.zone_concurrent == 1


def test_facts_end_to_end__unacked_incident_renders_missing(client, svc):
    """验收串起来走一遍: 未派单未接单的真实行 -> 事实包仍产全 -> 渲染出"无此记录"。"""

    async def setup(conn):
        incident_id = await _insert_incident(conn)  # 无派单、无接单
        await _insert_event(conn, incident_id, "opened", T0)
        await _insert_event(conn, incident_id, "resolved", T0 + timedelta(hours=1))
        return incident_id

    incident_id = db(setup)
    raw = _load(svc, incident_id)
    facts = build_fact_pack(raw, tz="Asia/Shanghai")

    ack = next(f for f in facts if f.id == "ack_by")
    assert ack.value is None
    assert ack.text == MISSING_TEXT

    body = {
        "summary": "事故 {{incident_id}} 已解决。",
        "handling": "实际到场刷卡的是 {{ack_by}}, 全程 {{handle_duration}}。",
        "impact": "", "notable": "", "suggestion": "",
    }
    result = check_draft(body, facts)
    assert result.ok, result.violations
    assert result.rendered is not None
    assert "无此记录" in result.rendered["handling"]
    assert "1 小时" in result.rendered["handling"]
