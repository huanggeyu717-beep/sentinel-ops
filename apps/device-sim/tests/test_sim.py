"""模拟器单测: 不需要数据库, CI 里最先跑。"""
from __future__ import annotations

import sys
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SIM_DIR))

import sim  # noqa: E402

CSV = SIM_DIR / "seed" / "waterlevel_readings.csv"
SCENARIO = SIM_DIR / "scenarios" / "multi_sensor_escalation.yaml"


def test_load_csv__produces_sensor_events_and_synthetic_heartbeats():
    src = sim.events_from_csv(CSV, device_id=None)
    kinds = {e["kind"] for e in src.events}
    assert kinds == {"sensor_state", "heartbeat"}
    assert src.origin_epoch_ms is not None
    # 事件按相对时间单调不减, live 模式才不会出现负等待
    ats = [e["at_s"] for e in src.events]
    assert ats == sorted(ats)


def test_load_csv__preserves_real_state_from_raw_json():
    src = sim.events_from_csv(CSV, device_id="Arduino1")
    sensor_events = [e for e in src.events if e["kind"] == "sensor_state"]
    assert {e["state"] for e in sensor_events} <= {"WET", "DRY"}
    assert all(e["device_id"] == "Arduino1" for e in src.events)


def test_load_yaml__keeps_scenario_events_verbatim():
    src = sim.events_from_yaml(SCENARIO)
    assert src.name == "multi_sensor_escalation"
    assert src.origin_epoch_ms is None
    assert src.events[0]["kind"] == "heartbeat"


def test_to_payload__drops_at_s_and_computes_absolute_ts():
    ev = {"at_s": 40.0, "kind": "sensor_state", "device_id": "A", "sensor_id": 2, "value": None}
    payload = sim.to_payload(ev, epoch_ms=1_000_000, time_scale=1.0)
    assert payload == {"kind": "sensor_state", "device_id": "A", "sensor_id": 2, "ts": 1_040_000}


def test_to_payload__time_scale_compresses_timeline():
    ev = {"at_s": 100.0, "kind": "heartbeat", "device_id": "A"}
    assert sim.to_payload(ev, 0, time_scale=1 / 5)["ts"] == 20_000


def test_resolve_epoch__csv_keeps_history_unless_shifted():
    src = sim.events_from_csv(CSV, device_id=None)
    assert sim.resolve_epoch(src, shift_to_now=False) == src.origin_epoch_ms
    assert sim.resolve_epoch(src, shift_to_now=True) > src.origin_epoch_ms
