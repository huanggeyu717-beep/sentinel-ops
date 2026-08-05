"""W1 验收: /status 心跳在线判定 —— 保持 legacy status-api Lambda 的 60s 语义。"""
from __future__ import annotations

import time


def now_ms() -> int:
    return int(time.time() * 1000)


def test_device_status__recent_heartbeat_is_online(client):
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino1",
                                 "ts": now_ms(), "uptime_ms": 30_000})
    dev = next(d for d in client.get("/status/devices").json()["devices"]
               if d["device_id"] == "Arduino1")
    assert dev["online"] is True
    assert dev["age_seconds"] <= 5


def test_device_status__stale_heartbeat_is_offline(client):
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino2",
                                 "ts": now_ms() - 120_000, "uptime_ms": 30_000})
    dev = next(d for d in client.get("/status/devices").json()["devices"]
               if d["device_id"] == "Arduino2")
    assert dev["online"] is False
    assert dev["age_seconds"] >= 60


def test_heartbeat_out_of_order__does_not_move_last_seen_backwards(client):
    latest = now_ms()
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino3", "ts": latest})
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino3",
                                 "ts": latest - 300_000})
    dev = next(d for d in client.get("/status/devices").json()["devices"]
               if d["device_id"] == "Arduino3")
    assert dev["online"] is True


def test_readings_endpoint__filters_by_sensor_and_orders_desc(client):
    ts = now_ms()
    client.post("/ingest/batch", json=[
        {"kind": "sensor_state", "device_id": "A", "ts": ts, "sensor_id": 1, "value": 100},
        {"kind": "sensor_state", "device_id": "A", "ts": ts + 1000, "sensor_id": 1, "value": 900},
        {"kind": "sensor_state", "device_id": "A", "ts": ts + 2000, "sensor_id": 2, "value": 500},
    ])
    rows = client.get("/status/readings", params={"sensor_id": 1}).json()["readings"]
    assert [r["value"] for r in rows] == [900, 100]
    assert len(client.get("/status/readings").json()["readings"]) == 3
