"""W2 验收: 事故生命周期状态机 + RFID 接单 + 自动解决。对应 docs/specs/SPEC-003。

传感器与区域来自种子数据: sensor 1 在 Zone 1 (Arduino1), sensor 4 在 Zone 2 (Arduino2)。
Alex(employee 1) 的卡 04A1B2C3。时间戳全部显式给出, 自动解决的稳定窗口默认 300s。
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import asyncpg

TS = 1_773_600_000_000
WINDOW_MS = 300 * 1000


def sensor_event(sensor_id=1, ts=TS, state="WET", value=845, device_id="Arduino1", **over):
    base = {
        "kind": "sensor_state", "device_id": device_id, "ts": ts,
        "sensor_id": sensor_id, "state": state, "value": value,
    }
    return {**base, **over}


def rfid_event(rfid_uid="04A1B2C3", device_id="Arduino1", ts=TS + 30_000, **over):
    base = {"kind": "rfid_scan", "device_id": device_id, "ts": ts, "rfid_uid": rfid_uid}
    return {**base, **over}


def open_incident(client, sensor_id=1, ts=TS, device_id="Arduino1") -> int:
    """触发一条湿事件并返回开出的事故 id。"""
    body = client.post(
        "/ingest", json=sensor_event(sensor_id=sensor_id, ts=ts, device_id=device_id)
    ).json()
    assert body["incident_id"] is not None
    return body["incident_id"]


def get_incident(client, incident_id):
    return client.get(f"/incidents/{incident_id}").json()


def timeline_kinds(client, incident_id) -> list[str]:
    return [e["kind"] for e in get_incident(client, incident_id)["events"]]


def fetch_audit(incident_id) -> list[dict]:
    """直查 audit_log (没有对外接口, 刻意的)。conftest 已把测试库地址写进环境变量。"""
    async def go() -> list[dict]:
        dsn = os.environ["SENTINEL_DATABASE_URL"].replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT action, detail FROM audit_log "
                "WHERE entity = 'incident' AND entity_id = $1 ORDER BY id",
                str(incident_id),
            )
            return [{"action": r["action"], "detail": json.loads(r["detail"])} for r in rows]
        finally:
            await conn.close()

    return asyncio.run(go())


# ===== 开单与去重 =====

def test_wet_sensor__opens_incident(client):
    incident_id = open_incident(client)

    incidents = client.get("/incidents").json()["incidents"]
    assert len(incidents) == 1
    one = incidents[0]
    assert one["id"] == incident_id
    assert one["status"] == "open"
    assert one["sensor_id"] == 1
    assert one["zone_id"] == 1  # 来自 sensors 表的关联, 不信任事件里的 zone_id
    assert timeline_kinds(client, incident_id) == ["opened"]


def test_wet_again__appends_still_wet_without_new_incident(client):
    incident_id = open_incident(client)
    second = client.post("/ingest", json=sensor_event(ts=TS + 5_000)).json()

    assert second["incident_id"] == incident_id
    assert len(client.get("/incidents").json()["incidents"]) == 1
    assert timeline_kinds(client, incident_id) == ["opened", "sensor_still_wet"]


def test_wet_after_resolved__opens_new_incident(client):
    first = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + WINDOW_MS + 1_000, state="DRY", value=90))
    assert get_incident(client, first)["incident"]["status"] == "resolved"

    second = open_incident(client, ts=TS + WINDOW_MS + 60_000)
    assert second != first
    assert len(client.get("/incidents").json()["incidents"]) == 2


# ===== 自动解决 =====

def test_dry_within_window__keeps_incident_open(client):
    incident_id = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + 60_000, state="DRY", value=90))

    assert get_incident(client, incident_id)["incident"]["status"] == "open"
    assert timeline_kinds(client, incident_id) == ["opened", "sensor_dry"]


def test_auto_resolve__fires_when_dry_window_met(client):
    incident_id = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + WINDOW_MS + 1_000, state="DRY", value=90))

    one = get_incident(client, incident_id)["incident"]
    assert one["status"] == "resolved"
    assert one["resolved_by"] == "auto_sensor_dry"
    assert one["resolved_at"] is not None
    assert timeline_kinds(client, incident_id) == ["opened", "sensor_dry", "resolved"]


def test_auto_resolve__skipped_when_dry_window_not_met(client):
    """转干未满窗口又转湿: 不关单, 也不重复开单。"""
    incident_id = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + 100_000, state="DRY", value=90))
    client.post("/ingest", json=sensor_event(ts=TS + 150_000))  # 又湿了
    # 距上一次湿只有 50s < 300s, 不触发自动解决
    client.post("/ingest", json=sensor_event(ts=TS + 200_000, state="DRY", value=90))

    assert get_incident(client, incident_id)["incident"]["status"] == "open"
    assert len(client.get("/incidents").json()["incidents"]) == 1


# ===== 手工流转 =====

def test_assign__moves_open_to_assigned(client):
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "assigned"
    assert one["assigned_employee_id"] == 1
    assert one["assigned_employee_name"] == "Alex Chen"
    assert one["assigned_at"] is not None


def test_assign__reassign_records_previous_assignee(client):
    """修订 3: 已 assigned 可再次 assign = 改派, 时间线带前后两人。"""
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 3})  # Chris, 同区

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "assigned"
    assert one["assigned_employee_id"] == 3
    events = get_incident(client, incident_id)["events"]
    assert [e["kind"] for e in events] == ["opened", "assigned", "reassigned"]
    assert events[-1]["detail"] == {"from_employee_id": 1, "to_employee_id": 3}


def test_assign__rejects_when_acknowledged(client):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/acknowledge")
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})
    assert r.status_code == 409


def test_assign__rejects_cross_zone_by_default(client):
    """决策 7: Bo 在 Zone 2, 事故在 Zone 1, 不带 allow_cross_zone 直接 422。"""
    incident_id = open_incident(client)  # sensor 1 → Zone 1
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 2})

    assert r.status_code == 422
    assert get_incident(client, incident_id)["incident"]["status"] == "open"


def test_assign__cross_zone_with_flag_succeeds_and_audited(client):
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2, "allow_cross_zone": True},  # Bo, Zone 2
    )

    assert r.status_code == 200
    assert r.json()["incident"]["assigned_employee_id"] == 2
    assign_audit = [a for a in fetch_audit(incident_id) if a["action"] == "incident.assign"][-1]
    assert assign_audit["detail"]["cross_zone"] is True
    assert assign_audit["detail"]["employee_zone_id"] == 2
    assert assign_audit["detail"]["incident_zone_id"] == 1


def test_assign__rejects_unknown_employee(client):
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 999})
    assert r.status_code == 422
    assert get_incident(client, incident_id)["incident"]["status"] == "open"


def test_acknowledge__allowed_from_open_skipping_assign(client):
    """未预先分配也允许接单; assigned_employee_id 保持为空, 不回填 (修订 1)。"""
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/acknowledge", headers={"X-Actor": "employee:2"})

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "acknowledged"
    assert one["acknowledged_at"] is not None
    assert one["assigned_employee_id"] is None
    assert one["acknowledged_by_employee_id"] == 2  # 手工接单人从 X-Actor 解析并回表验证


def test_acknowledge__allowed_from_assigned(client):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})
    r = client.post(f"/incidents/{incident_id}/acknowledge")
    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] == 1          # 派单记录不动
    assert one["acknowledged_by_employee_id"] is None  # actor=system, 解析不出员工


def test_acknowledge__unverifiable_actor_leaves_acknowledger_empty(client):
    """X-Actor 是不受信占位: 报了不存在的员工号, 接单照常, 接单人记空。"""
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/acknowledge", headers={"X-Actor": "employee:999"})
    assert r.status_code == 200
    assert r.json()["incident"]["acknowledged_by_employee_id"] is None


def test_resolve__records_manual_actor(client):
    incident_id = open_incident(client)
    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={"note": "拖干并检查了接头"},
        headers={"X-Actor": "employee:3"},
    )

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "resolved"
    assert one["resolved_by"] == "employee:3"
    assert one["resolved_at"] is not None
    resolved_event = get_incident(client, incident_id)["events"][-1]
    assert resolved_event["kind"] == "resolved"
    assert resolved_event["actor"] == "employee:3"
    assert resolved_event["detail"]["note"] == "拖干并检查了接头"


def test_resolve__rejects_when_already_resolved(client):
    incident_id = open_incident(client)
    assert client.post(f"/incidents/{incident_id}/resolve").status_code == 200
    assert client.post(f"/incidents/{incident_id}/resolve").status_code == 409


def test_resolve__concurrent_calls_only_one_succeeds(client):
    """并发下条件更新只放行一个: UPDATE ... WHERE status <> 'resolved'。"""
    incident_id = open_incident(client)

    def hit():
        return client.post(f"/incidents/{incident_id}/resolve").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _: hit(), range(2)))
    assert codes == [200, 409]


def test_transition__404_when_incident_missing(client):
    assert client.post("/incidents/9999/resolve").status_code == 404
    assert client.get("/incidents/9999").status_code == 404


# ===== RFID 刷卡接单 (复用 /ingest) =====

def test_rfid_scan__acknowledges_incident_in_device_zone(client):
    incident_id = open_incident(client)  # sensor 1, Zone 1
    body = client.post("/ingest", json=rfid_event()).json()  # Alex 的卡, Arduino1 也在 Zone 1

    assert body["stored"] is True
    assert body["matched"] is True
    assert body["incident_id"] == incident_id

    detail = get_incident(client, incident_id)
    one = detail["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] is None       # 未派单直接刷卡, 不回填 (修订 1)
    assert one["acknowledged_by_employee_id"] == 1   # 实际接单人 = 刷卡人
    ack = detail["events"][-1]
    assert ack["kind"] == "acknowledged"
    assert ack["actor"] == "employee:1"
    assert ack["detail"]["via"] == "rfid_scan"
    assert ack["detail"]["rfid_uid"] == "04A1B2C3"


def test_rfid_scan__after_assign_keeps_assignee_and_records_acknowledger(client):
    """派给 Alex, 实际是 Bo 刷卡: 两个字段各记各的 (修订 1)。

    Bo 属于 Zone 2 但刷卡不校验刷卡人区域 (决策 7), 只看设备所在区域。
    """
    incident_id = open_incident(client)  # Zone 1
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})  # 派给 Alex
    # Bo 在 Arduino1 (Zone 1) 刷卡
    body = client.post("/ingest", json=rfid_event(rfid_uid="04D9E8F7")).json()

    assert body["matched"] is True
    detail = get_incident(client, incident_id)
    one = detail["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] == 1          # 派给谁: 仍是 Alex
    assert one["acknowledged_by_employee_id"] == 2   # 谁接的单: Bo
    assert one["acknowledged_by_employee_name"] == "Bo Wang"
    # 时间线按事件时间排序: 刷卡带的是设备时间戳, 手工 assign 是数据库时钟, 按 kind 取
    ack = next(e for e in detail["events"] if e["kind"] == "acknowledged")
    assert ack["actor"] == "employee:2"


def test_rfid_scan__unknown_card_stored_but_matches_nothing(client):
    """决策 6: 刷卡事实照常落库, 但不推进任何事故。"""
    incident_id = open_incident(client)
    body = client.post("/ingest", json=rfid_event(rfid_uid="DEADBEEF")).json()

    assert body["stored"] is True
    assert body["matched"] is False
    assert body["reason"] == "unknown_card"
    assert client.get("/status/summary").json()["rfid_scans"] == 1
    assert get_incident(client, incident_id)["incident"]["status"] == "open"


def test_rfid_scan__no_open_incident_in_zone(client):
    body = client.post("/ingest", json=rfid_event()).json()
    assert body["stored"] is True
    assert body["matched"] is False
    assert body["reason"] == "no_open_incident_in_zone"


def test_rfid_scan__replay_does_not_reacknowledge(client):
    """同一次刷卡重放: 幂等落库, 不再推进事故。"""
    open_incident(client)
    first = client.post("/ingest", json=rfid_event()).json()
    replay = client.post("/ingest", json=rfid_event()).json()

    assert first["matched"] is True
    assert replay["stored"] is False
    assert replay["matched"] is False
    assert replay["reason"] == "duplicate_scan"


# ===== 列表过滤与审计 =====

def test_incident_list__filters_by_status_and_zone(client):
    first = open_incident(client, sensor_id=1, device_id="Arduino1")           # Zone 1
    open_incident(client, sensor_id=4, ts=TS + 1_000, device_id="Arduino2")    # Zone 2
    client.post(f"/incidents/{first}/resolve")

    assert len(client.get("/incidents").json()["incidents"]) == 2
    opened = client.get("/incidents", params={"status": "open"}).json()["incidents"]
    assert [i["zone_id"] for i in opened] == [2]
    zone1 = client.get("/incidents", params={"zone_id": 1}).json()["incidents"]
    assert [i["status"] for i in zone1] == ["resolved"]


def test_transitions__write_audit_log(client):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1})
    client.post(f"/incidents/{incident_id}/resolve", headers={"X-Actor": "employee:1"})

    assert [a["action"] for a in fetch_audit(incident_id)] == [
        "incident.open", "incident.assign", "incident.resolve"
    ]
