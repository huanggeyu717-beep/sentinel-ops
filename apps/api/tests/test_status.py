"""W1 验收: /status 心跳在线判定 —— 保持 legacy status-api Lambda 的 60s 语义。

SPEC-004 后 /status/* 需要登录 (任意角色可读), 用例以 operator Alex 的身份查询。
"""
from __future__ import annotations

import time

import pytest


def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture(scope="module")
def hdr(auth_headers):
    return auth_headers("alex@example.com")


def test_device_status__recent_heartbeat_is_online(client, hdr):
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino1",
                                 "ts": now_ms(), "uptime_ms": 30_000})
    dev = next(d for d in client.get("/status/devices", headers=hdr).json()["devices"]
               if d["device_id"] == "Arduino1")
    assert dev["online"] is True
    assert dev["age_seconds"] <= 5


def test_device_status__stale_heartbeat_is_offline(client, hdr):
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino2",
                                 "ts": now_ms() - 120_000, "uptime_ms": 30_000})
    dev = next(d for d in client.get("/status/devices", headers=hdr).json()["devices"]
               if d["device_id"] == "Arduino2")
    assert dev["online"] is False
    assert dev["age_seconds"] >= 60


def test_heartbeat_out_of_order__does_not_move_last_seen_backwards(client, hdr):
    latest = now_ms()
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino3", "ts": latest})
    client.post("/ingest", json={"kind": "heartbeat", "device_id": "Arduino3",
                                 "ts": latest - 300_000})
    dev = next(d for d in client.get("/status/devices", headers=hdr).json()["devices"]
               if d["device_id"] == "Arduino3")
    assert dev["online"] is True


# ===== SPEC-005 前置 A: 平面图坐标 (百分比 0-100, 种子按 zone 1 左 / 2 中 / 3 右排布) =====

def test_sensor_status__seed_positions_follow_zone_layout(client, hdr):
    ts = now_ms()
    client.post("/ingest/batch", json=[
        {"kind": "sensor_state", "device_id": "Arduino1", "ts": ts, "sensor_id": 1, "value": 100},
        {"kind": "sensor_state", "device_id": "Arduino2", "ts": ts, "sensor_id": 3, "value": 100},
        {"kind": "sensor_state", "device_id": "X", "ts": ts, "sensor_id": 5, "value": 100},
    ])
    rows = {s["sensor_id"]: s for s in client.get("/status/sensors", headers=hdr).json()["sensors"]}
    for sensor_id in (1, 3, 5):
        assert rows[sensor_id]["pos_x"] is not None, f"种子该给传感器 {sensor_id} 坐标"
        assert 0 <= rows[sensor_id]["pos_x"] <= 100 and 0 <= rows[sensor_id]["pos_y"] <= 100
    # zone 1 在左、zone 2 居中、zone 3 在右
    assert rows[1]["pos_x"] < rows[3]["pos_x"] < rows[5]["pos_x"]


def test_sensor_status__placeholder_sensor_has_no_position(client, hdr):
    """传感器 0 (挂在 UNKNOWN_DEVICE 上的占位) 刻意不给坐标, 走前端"未定位"分支。"""
    client.post("/ingest", json={"kind": "sensor_state", "device_id": "X",
                                 "ts": now_ms(), "sensor_id": 0, "value": 100})
    row = next(s for s in client.get("/status/sensors", headers=hdr).json()["sensors"]
               if s["sensor_id"] == 0)
    assert row["pos_x"] is None and row["pos_y"] is None


def test_device_status__seed_leaves_one_device_unpositioned(client, hdr):
    ts = now_ms()
    for name in ("Arduino1", "Arduino2", "UNKNOWN_DEVICE"):
        client.post("/ingest", json={"kind": "heartbeat", "device_id": name, "ts": ts})
    rows = {d["device_id"]: d for d in client.get("/status/devices", headers=hdr).json()["devices"]}
    assert rows["Arduino1"]["pos_x"] is not None
    assert rows["Arduino2"]["pos_x"] is not None
    # 刻意不填的那台: 前端"未定位"区靠它在演示数据里就能走到
    assert rows["UNKNOWN_DEVICE"]["pos_x"] is None and rows["UNKNOWN_DEVICE"]["pos_y"] is None


def test_readings_endpoint__filters_by_sensor_and_orders_desc(client, hdr):
    ts = now_ms()
    client.post("/ingest/batch", json=[
        {"kind": "sensor_state", "device_id": "A", "ts": ts, "sensor_id": 1, "value": 100},
        {"kind": "sensor_state", "device_id": "A", "ts": ts + 1000, "sensor_id": 1, "value": 900},
        {"kind": "sensor_state", "device_id": "A", "ts": ts + 2000, "sensor_id": 2, "value": 500},
    ])
    rows = client.get(
        "/status/readings", params={"sensor_id": 1}, headers=hdr
    ).json()["readings"]
    assert [r["value"] for r in rows] == [900, 100]
    assert len(client.get("/status/readings", headers=hdr).json()["readings"]) == 3
