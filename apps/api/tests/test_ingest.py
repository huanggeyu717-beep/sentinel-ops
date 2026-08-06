"""W1 验收: /ingest 落库、幂等、乱序保护。对应 docs/specs/SPEC-000-w1-ingest.md。

/ingest 本身不需要登录 (SPEC-004 决策 7: 它模拟设备上报, 真机走 MQTT + 证书,
加 JWT 会让模拟器与真机行为分叉); 校验落库结果的 /status 读接口需要登录。
"""
from __future__ import annotations

import pytest

TS = 1_773_600_000_000


@pytest.fixture(scope="module")
def hdr(auth_headers):
    return auth_headers("alex@example.com")


def sensor_event(**over):
    base = {
        "kind": "sensor_state", "device_id": "Arduino1", "ts": TS,
        "sensor_id": 1, "zone_id": 1, "state": "WET", "value": 845,
    }
    return {**base, **over}


def test_health__returns_ok(client):
    assert client.get("/health").json()["ok"] is True


def test_ingest_sensor_state__writes_reading_and_state(client, hdr):
    r = client.post("/ingest", json=sensor_event())
    assert r.status_code == 200
    body = r.json()
    # W2 起, 转湿会联动开事故, 响应多出 incident_id (SPEC-003), 详细断言在 test_incidents.py
    assert body["ok"] is True and body["kind"] == "sensor_state"
    assert body["stored"] is True and body["state_updated"] is True
    assert body["incident_id"] is not None

    sensors = client.get("/status/sensors", headers=hdr).json()["sensors"]
    one = next(s for s in sensors if s["sensor_id"] == 1)
    assert one["state"] == "WET" and one["wet"] is True and one["last_value"] == 845
    assert one["zone_name"] == "Zone 1 - Entrance"  # 来自种子数据的关联


def test_ingest_same_event_twice__is_idempotent(client, hdr):
    first = client.post("/ingest", json=sensor_event()).json()
    second = client.post("/ingest", json=sensor_event()).json()
    assert first["stored"] is True
    assert second["stored"] is False  # 幂等键命中, 没有产生第二行
    assert client.get("/status/summary", headers=hdr).json()["readings"] == 1


def test_ingest_out_of_order__older_event_does_not_overwrite_state(client, hdr):
    client.post("/ingest", json=sensor_event(ts=TS + 10_000, state="DRY", value=90))
    client.post("/ingest", json=sensor_event(ts=TS, state="WET", value=845))  # 迟到的旧事件

    sensors = client.get("/status/sensors", headers=hdr).json()["sensors"]
    one = next(s for s in sensors if s["sensor_id"] == 1)
    assert one["state"] == "DRY"  # 当前状态仍是较新的那条
    assert client.get("/status/summary", headers=hdr).json()["readings"] == 2  # 两条读数都留档


def test_ingest_without_state__derives_wet_from_threshold(client, hdr):
    client.post("/ingest", json=sensor_event(state=None, value=700))
    sensors = client.get("/status/sensors", headers=hdr).json()["sensors"]
    one = next(s for s in sensors if s["sensor_id"] == 1)
    assert one["wet"] is True and one["state"] == "WET"

    client.post("/ingest", json=sensor_event(ts=TS + 1, state=None, value=100))
    sensors = client.get("/status/sensors", headers=hdr).json()["sensors"]
    one = next(s for s in sensors if s["sensor_id"] == 1)
    assert one["wet"] is False


def test_ingest_sensor_state_without_sensor_id__rejected(client):
    r = client.post("/ingest", json=sensor_event(sensor_id=None))
    assert r.status_code == 422


def test_ingest_rfid__stored_and_idempotent(client, hdr):
    ev = {"kind": "rfid_scan", "device_id": "Arduino1", "ts": TS,
          "rfid_uid": "04A1B2C3", "rfid_id": "EMP-007"}
    assert client.post("/ingest", json=ev).json()["stored"] is True
    assert client.post("/ingest", json=ev).json()["stored"] is False
    assert client.get("/status/summary", headers=hdr).json()["rfid_scans"] == 1


def test_ingest_rfid_without_uid__rejected(client):
    r = client.post("/ingest", json={"kind": "rfid_scan", "device_id": "A", "ts": TS})
    assert r.status_code == 422


def test_ingest_batch__accepts_list_in_one_transaction(client, hdr):
    payload = [sensor_event(ts=TS + i * 1000, value=800 + i) for i in range(5)]
    body = client.post("/ingest/batch", json=payload).json()
    assert body == {"ok": True, "accepted": 5, "stored": 5}
    assert client.get("/status/summary", headers=hdr).json()["readings"] == 5
