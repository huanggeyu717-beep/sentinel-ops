"""场景装载: YAML 剧本 / CSV 历史回放 -> 规范化事件流 (每条含相对时间 at_s)。

IO 边界: 只允许读场景文件; 禁止发网络请求、禁止碰数据库 (见包 __init__ 的 docstring)。
代码原样搬自 apps/device-sim/sim.py (SPEC-005 方案 B 抽包), 行为逐字不变。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HEARTBEAT_EVERY_S = 60  # 回放时按仿真时间每 60s 补一次心跳, 让 /status/devices 有数据


@dataclass(slots=True)
class Source:
    name: str
    events: list[dict[str, Any]]      # 每条含相对时间 at_s
    origin_epoch_ms: int | None       # 数据自带的绝对起点; YAML 剧本没有, 为 None


def events_from_yaml(path: Path) -> Source:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Source(doc["name"], list(doc["events"]), None)


def events_from_csv(path: Path, device_id: str | None) -> Source:
    """真实读数 CSV -> 事件列表。

    列: received_ts(ms), device_id, sensor_id, value, wet, raw(JSON, 内含 state)。
    相对时间轴 = (本行 ts - 首行 ts) / 1000, 保持真实采样节奏。
    """
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"{path} 里没有数据行")
    base_ts = int(rows[0]["received_ts"])
    events: list[dict[str, Any]] = []
    next_hb = 0.0
    for r in rows:
        at_s = (int(r["received_ts"]) - base_ts) / 1000.0
        dev = device_id or r.get("device_id") or "UNKNOWN_DEVICE"
        try:
            state = json.loads(r["raw"]).get("state")
        except (json.JSONDecodeError, KeyError, TypeError):
            state = "WET" if str(r.get("wet", "")).lower() == "true" else "DRY"
        while at_s >= next_hb:  # 真实数据没有心跳报文, 按仿真时间补
            events.append({"at_s": next_hb, "kind": "heartbeat", "device_id": dev,
                           "uptime_ms": int(next_hb * 1000)})
            next_hb += HEARTBEAT_EVERY_S
        events.append({
            "at_s": at_s,
            "kind": "sensor_state",
            "device_id": dev,
            "sensor_id": int(r["sensor_id"]),
            "state": state,
            "value": int(r["value"]) if r.get("value") else None,
        })
    events.sort(key=lambda e: e["at_s"])
    return Source(f"replay:{path.name}", events, base_ts)


def load_source(source: str, device_id: str | None) -> Source:
    path = Path(source)
    if not path.exists():
        raise SystemExit(f"找不到文件: {path}")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return events_from_yaml(path)
    if suffix == ".csv":
        return events_from_csv(path, device_id)
    raise SystemExit(f"不支持的数据源类型: {suffix} (只支持 .yaml/.yml/.csv)")
