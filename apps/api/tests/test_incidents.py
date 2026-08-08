"""W2 验收: 事故生命周期状态机 + RFID 接单。对应 docs/specs/SPEC-003。

传感器与区域来自种子数据: sensor 1 在 Zone 1 (Arduino1), sensor 4 在 Zone 2 (Arduino2)。
Alex(employee 1) 的卡 04A1B2C3。时间戳全部显式给出。

SPEC-004 后所有 /incidents 接口都要登录: 用例默认以 manager Chris (user 2, 绑员工 3)
操作; 权限矩阵 (403/422 的边界) 的专门用例在 test_auth.py。

W3 (SPEC-006) 后开事故由策略引擎接管: 本文件依赖 published_baseline 夹具注入的
wet->open 基线策略。按稳定窗口自动关单的行为改由 sensor_dry_for 策略实现,
对应用例挪到 test_policy_runtime.py (需要显式注入 tick); 这里保留的是 SPEC-003
的**事实记录**验收 —— sensor_still_wet / sensor_dry 时间线与删掉的判断逻辑无关。
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import pytest

TS = 1_773_600_000_000


@pytest.fixture(autouse=True)
def _engine_baseline(published_baseline):
    """开事故由引擎接管后, 本文件全部用例都需要已发布的 wet->open 基线策略。"""


@pytest.fixture(scope="module")
def mgr(auth_headers):
    """manager Chris: user 2, 绑员工 3 (Zone 1)。流转与跨区放行权限齐全。"""
    return auth_headers("chris@example.com")


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
    """触发一条湿事件并返回开出的事故 id (/ingest 不需要登录, SPEC-004 决策 7)。"""
    body = client.post(
        "/ingest", json=sensor_event(sensor_id=sensor_id, ts=ts, device_id=device_id)
    ).json()
    assert body["incident_id"] is not None
    return body["incident_id"]


def get_incident(client, incident_id, headers):
    return client.get(f"/incidents/{incident_id}", headers=headers).json()


def timeline_kinds(client, incident_id, headers) -> list[str]:
    return [e["kind"] for e in get_incident(client, incident_id, headers)["events"]]


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

def test_wet_sensor__opens_incident(client, mgr):
    incident_id = open_incident(client)

    incidents = client.get("/incidents", headers=mgr).json()["incidents"]
    assert len(incidents) == 1
    one = incidents[0]
    assert one["id"] == incident_id
    assert one["status"] == "open"
    assert one["sensor_id"] == 1
    assert one["zone_id"] == 1  # 来自 sensors 表的关联, 不信任事件里的 zone_id
    assert timeline_kinds(client, incident_id, mgr) == ["opened"]


def test_wet_again__appends_still_wet_without_new_incident(client, mgr):
    incident_id = open_incident(client)
    second = client.post("/ingest", json=sensor_event(ts=TS + 5_000)).json()

    assert second["incident_id"] == incident_id
    assert len(client.get("/incidents", headers=mgr).json()["incidents"]) == 1
    assert timeline_kinds(client, incident_id, mgr) == ["opened", "sensor_still_wet"]


def test_wet_after_resolved__opens_new_incident(client, mgr):
    """resolved 是终态, 重开 = 开新事故。引擎是边沿触发, 先转干再转湿才有新的边沿。"""
    first = open_incident(client)
    client.post(f"/incidents/{first}/resolve", headers=mgr)
    client.post("/ingest", json=sensor_event(ts=TS + 60_000, state="DRY", value=90))

    second = open_incident(client, ts=TS + 180_000)
    assert second != first
    assert len(client.get("/incidents", headers=mgr).json()["incidents"]) == 2


# ===== 遥测事实的时间线记录 (SPEC-006 保留项: 删的是判断逻辑, 不是事实记录) =====

def test_dry__appends_sensor_dry_and_does_not_close(client, mgr):
    """每次转干仍记一条 sensor_dry (决策 4); 没有关单策略在线, 引擎不会关单。"""
    incident_id = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + 60_000, state="DRY", value=90))

    assert get_incident(client, incident_id, mgr)["incident"]["status"] == "open"
    assert timeline_kinds(client, incident_id, mgr) == ["opened", "sensor_dry"]


def test_dry_then_wet_again__no_duplicate_incident(client, mgr):
    """转干又转湿: 引擎再次产出 open_incident, 撞 partial unique index 即空操作,
    时间线只累加事实, 不重复开单。"""
    incident_id = open_incident(client)
    client.post("/ingest", json=sensor_event(ts=TS + 100_000, state="DRY", value=90))
    client.post("/ingest", json=sensor_event(ts=TS + 170_000))  # 又湿了 (170s > 冷却 60s)

    assert get_incident(client, incident_id, mgr)["incident"]["status"] == "open"
    assert len(client.get("/incidents", headers=mgr).json()["incidents"]) == 1
    assert timeline_kinds(client, incident_id, mgr) == [
        "opened", "sensor_dry", "sensor_still_wet"
    ]


# ===== 手工流转 =====

def test_assign__moves_open_to_assigned(client, mgr):
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "assigned"
    assert one["assigned_employee_id"] == 1
    assert one["assigned_employee_name"] == "Alex Chen"
    assert one["assigned_at"] is not None


def test_assign__reassign_records_previous_assignee(client, mgr):
    """修订 3: 已 assigned 可再次 assign = 改派, 时间线带前后两人。"""
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)
    # 改派给 Chris (employee 3), 同区
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 3}, headers=mgr)

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "assigned"
    assert one["assigned_employee_id"] == 3
    events = get_incident(client, incident_id, mgr)["events"]
    assert [e["kind"] for e in events] == ["opened", "assigned", "reassigned"]
    assert events[-1]["detail"] == {"from_employee_id": 1, "to_employee_id": 3}


def test_assign__rejects_when_acknowledged(client, mgr):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/acknowledge", headers=mgr)
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)
    assert r.status_code == 409


def test_assign__rejects_cross_zone_by_default(client, mgr):
    """决策 7: Bo 在 Zone 2, 事故在 Zone 1。manager 有跨区权限, 但没带 flag 仍是业务 422。"""
    incident_id = open_incident(client)  # sensor 1 → Zone 1
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 2}, headers=mgr)

    assert r.status_code == 422
    assert get_incident(client, incident_id, mgr)["incident"]["status"] == "open"


def test_assign__cross_zone_with_flag_succeeds_and_audited(client, mgr):
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2, "allow_cross_zone": True},  # Bo, Zone 2
        headers=mgr,
    )

    assert r.status_code == 200
    assert r.json()["incident"]["assigned_employee_id"] == 2
    assign_audit = [a for a in fetch_audit(incident_id) if a["action"] == "incident.assign"][-1]
    assert assign_audit["detail"]["cross_zone"] is True
    assert assign_audit["detail"]["employee_zone_id"] == 2
    assert assign_audit["detail"]["incident_zone_id"] == 1


def test_assign__rejects_unknown_employee(client, mgr):
    incident_id = open_incident(client)
    r = client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 999}, headers=mgr)
    assert r.status_code == 422
    assert get_incident(client, incident_id, mgr)["incident"]["status"] == "open"


def test_acknowledge__allowed_from_open_skipping_assign(client, auth_headers, mgr):
    """未预先分配也允许接单; assigned_employee_id 保持为空, 不回填 (修订 1)。

    以 operator Alex (user 3, 绑员工 1) 登录: 接单人取自账号绑定的 employee_id。
    """
    incident_id = open_incident(client)
    r = client.post(
        f"/incidents/{incident_id}/acknowledge", headers=auth_headers("alex@example.com")
    )

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "acknowledged"
    assert one["acknowledged_at"] is not None
    assert one["assigned_employee_id"] is None
    assert one["acknowledged_by_employee_id"] == 1  # Alex 账号绑定的员工


def test_acknowledge__allowed_from_assigned(client, mgr):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)
    r = client.post(f"/incidents/{incident_id}/acknowledge", headers=mgr)
    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] == 1           # 派单记录不动
    assert one["acknowledged_by_employee_id"] == 3    # 实际接单人 = Chris 绑定的员工


def test_acknowledge__unlinked_account_leaves_acknowledger_empty(client, auth_headers):
    """admin 有账号但不绑现场员工 (SPEC-004 方案 A): 接单照常, 接单人记空。"""
    incident_id = open_incident(client)
    r = client.post(
        f"/incidents/{incident_id}/acknowledge", headers=auth_headers("admin@example.com")
    )
    assert r.status_code == 200
    assert r.json()["incident"]["acknowledged_by_employee_id"] is None


def test_resolve__records_manual_actor(client, mgr):
    incident_id = open_incident(client)
    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={"note": "拖干并检查了接头"},
        headers=mgr,
    )

    assert r.status_code == 200
    one = r.json()["incident"]
    assert one["status"] == "resolved"
    assert one["resolved_by"] == "user:2"  # SPEC-004 口径: 登录用户记 user:{id}
    assert one["resolved_at"] is not None
    resolved_event = get_incident(client, incident_id, mgr)["events"][-1]
    assert resolved_event["kind"] == "resolved"
    assert resolved_event["actor"] == "user:2"
    assert resolved_event["detail"]["note"] == "拖干并检查了接头"


def test_resolve__rejects_when_already_resolved(client, mgr):
    incident_id = open_incident(client)
    assert client.post(f"/incidents/{incident_id}/resolve", headers=mgr).status_code == 200
    assert client.post(f"/incidents/{incident_id}/resolve", headers=mgr).status_code == 409


def test_resolve__concurrent_calls_only_one_succeeds(client, mgr):
    """并发下条件更新只放行一个: UPDATE ... WHERE status <> 'resolved'。"""
    incident_id = open_incident(client)

    def hit():
        return client.post(f"/incidents/{incident_id}/resolve", headers=mgr).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _: hit(), range(2)))
    assert codes == [200, 409]


def test_transition__404_when_incident_missing(client, mgr):
    assert client.post("/incidents/9999/resolve", headers=mgr).status_code == 404
    assert client.get("/incidents/9999", headers=mgr).status_code == 404


# ===== RFID 刷卡接单 (复用 /ingest) =====

def test_rfid_scan__acknowledges_incident_in_device_zone(client, mgr):
    incident_id = open_incident(client)  # sensor 1, Zone 1
    body = client.post("/ingest", json=rfid_event()).json()  # Alex 的卡, Arduino1 也在 Zone 1

    assert body["stored"] is True
    assert body["matched"] is True
    assert body["incident_id"] == incident_id

    detail = get_incident(client, incident_id, mgr)
    one = detail["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] is None       # 未派单直接刷卡, 不回填 (修订 1)
    assert one["acknowledged_by_employee_id"] == 1   # 实际接单人 = 刷卡人
    ack = detail["events"][-1]
    assert ack["kind"] == "acknowledged"
    assert ack["actor"] == "employee:1"
    assert ack["detail"]["via"] == "rfid_scan"
    assert ack["detail"]["rfid_uid"] == "04A1B2C3"


def test_rfid_scan__after_assign_keeps_assignee_and_records_acknowledger(client, mgr):
    """派给 Alex, 实际是 Bo 刷卡: 两个字段各记各的 (修订 1)。

    Bo 属于 Zone 2 但刷卡不校验刷卡人区域 (决策 7), 只看设备所在区域。
    """
    incident_id = open_incident(client)  # Zone 1
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)
    # Bo 在 Arduino1 (Zone 1) 刷卡
    body = client.post("/ingest", json=rfid_event(rfid_uid="04D9E8F7")).json()

    assert body["matched"] is True
    detail = get_incident(client, incident_id, mgr)
    one = detail["incident"]
    assert one["status"] == "acknowledged"
    assert one["assigned_employee_id"] == 1          # 派给谁: 仍是 Alex
    assert one["acknowledged_by_employee_id"] == 2   # 谁接的单: Bo
    assert one["acknowledged_by_employee_name"] == "Bo Wang"
    # 时间线按事件时间排序: 刷卡带的是设备时间戳, 手工 assign 是数据库时钟, 按 kind 取
    ack = next(e for e in detail["events"] if e["kind"] == "acknowledged")
    assert ack["actor"] == "employee:2"


def test_rfid_scan__unknown_card_stored_but_matches_nothing(client, mgr):
    """决策 6: 刷卡事实照常落库, 但不推进任何事故。"""
    incident_id = open_incident(client)
    body = client.post("/ingest", json=rfid_event(rfid_uid="DEADBEEF")).json()

    assert body["stored"] is True
    assert body["matched"] is False
    assert body["reason"] == "unknown_card"
    assert client.get("/status/summary", headers=mgr).json()["rfid_scans"] == 1
    assert get_incident(client, incident_id, mgr)["incident"]["status"] == "open"


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

def test_incident_list__filters_by_status_and_zone(client, mgr):
    first = open_incident(client, sensor_id=1, device_id="Arduino1")           # Zone 1
    open_incident(client, sensor_id=4, ts=TS + 1_000, device_id="Arduino2")    # Zone 2
    client.post(f"/incidents/{first}/resolve", headers=mgr)

    assert len(client.get("/incidents", headers=mgr).json()["incidents"]) == 2
    opened = client.get(
        "/incidents", params={"status": "open"}, headers=mgr
    ).json()["incidents"]
    assert [i["zone_id"] for i in opened] == [2]
    zone1 = client.get("/incidents", params={"zone_id": 1}, headers=mgr).json()["incidents"]
    assert [i["status"] for i in zone1] == ["resolved"]


def test_transitions__write_audit_log(client, mgr):
    incident_id = open_incident(client)
    client.post(f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=mgr)
    client.post(f"/incidents/{incident_id}/resolve", headers=mgr)

    assert [a["action"] for a in fetch_audit(incident_id)] == [
        "incident.open", "incident.assign", "incident.resolve"
    ]
