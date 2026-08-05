"""W1 验收: /ingest 落库、幂等、乱序保护。对应 docs/specs/SPEC-000-w1-ingest.md。"""
from __future__ import annotations

TS = 1_773_600_000_000


def sensor_event(**over):
    base = {
        "kind": "sensor_state", "device_id": "Arduino1", "ts": TS,
        "sensor_id": 1, "zone_id": 1, "state": "WET", "value": 845,
    }
    return {**base, **over}


def test_health__returns_ok(client):
    assert client.get("/health").json()["ok"] is True


def test_ingest_sensor_state__writes_reading_and_state(client):
    r = client.post("/ingest", json=sensor_event())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "kind": "sensor_state", "stored": True, "state_updated": True}

    sensors = client.get("/status/sensors").json()["sensors"]
    one = next(s for s in sensors if s["sensor_id"] == 1)
    assert one["state"] == "WET" and one["wet"] is True and one["last_value"] == 845
    assert one["zone_name"] == "Zone 1 - Entrance"  # 来自种子数据的关联


def test_ingest_same_event_twice__is_idempotent(client):
    first = client.post("/ingest", json=sensor_event()).json()
    second = client.post("/ingest", json=sensor_event()).json()
    assert first["stored"] is True
    assert second["stored"] is False  # 幂等键命中, 没有产生第二行
    assert client.get("/status/summary").json()["readings"] == 1


def test_ingest_out_of_order__older_event_does_not_overwrite_state(client):
    client.post("/ingest", json=sensor_event(ts=TS + 10_000, state="DRY", value=90))
    client.post("/ingest", json=sensor_event(ts=TS, state="WET", value=845))  # 迟到的旧事件

    one = next(s for s in client.get("/status/sensors").json()["sensors"] if s["sensor_id"] == 1)
    assert one["state"] == "DRY"  # 当前状态仍是较新的那条
    assert client.get("/status/summary").json()["readings"] == 2  # 但两条读数都留档


def test_ingest_without_state__derives_wet_from_threshold(client):
    client.post("/ingest", json=sensor_event(state=None, value=700))
    one = next(s for s in client.get("/status/sensors").json()["sensors"] if s["sensor_id"] == 1)
    assert one["wet"] is True and one["state"] == "WET"

    client.post("/ingest", json=sensor_event(ts=TS + 1, state=None, value=100))
    one = next(s for s in client.get("/status/sensors").json()["sensors"] if s["sensor_id"] == 1)
    assert one["wet"] is False


def test_ingest_sensor_state_without_sensor_id__rejected(client):
    r = client.post("/ingest", json=sensor_event(sensor_id=None))
    assert r.status_code == 422


def test_ingest_rfid__stored_and_idempotent(client):
    ev = {"kind": "rfid_scan", "device_id": "Arduino1", "ts": TS,
          "rfid_uid": "04A1B2C3", "rfid_id": "EMP-007"}
    assert client.post("/ingest", json=ev).json()["stored"] is True
    assert client.post("/ingest", json=ev).json()["stored"] is False
    assert client.get("/status/summary").json()["rfid_scans"] == 1


def test_ingest_rfid_without_uid__rejected(client):
    r = client.post("/ingest", json={"kind": "rfid_scan", "device_id": "A", "ts": TS})
    assert r.status_code == 422


def test_ingest_batch__accepts_list_in_one_transaction(client):
    payload = [sensor_event(ts=TS + i * 1000, value=800 + i) for i in range(5)]
    body = client.post("/ingest/batch", json=payload).json()
    assert body == {"ok": True, "accepted": 5, "stored": 5}
    assert client.get("/status/summary").json()["readings"] == 5
