"""device-sim —— 数字孪生模拟器 (W1 核心交付)。

硬件已归还, 本模拟器是整个系统唯一的数据源: live 演示 / Policy 模拟引擎输入 /
eval fixtures / E2E 冒烟测试 共用同一份场景语义。

两种数据源, 按扩展名自动识别:
  1) YAML 场景包  —— 手写剧本, 相对时间轴, 用于确定性演示与回归测试
  2) CSV 历史回放 —— 回放原系统真实采集的读数 (队友仓库导出的 waterlevel_readings.csv)

用法:
    # 剧本模式, 10 倍速
    python sim.py scenarios/basic_spill.yaml --speed 10

    # 真实数据回放, 一次性灌库并保留其真实历史时间戳 (15 小时数据几秒钟灌完)
    python sim.py seed/waterlevel_readings.csv --batch

    # 真实数据按 600 倍速"直播", 时间轴平移到当前时刻
    python sim.py seed/waterlevel_readings.csv --speed 600 --shift-to-now
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
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


# --------------------------------------------------------------------------- 事件源


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


# --------------------------------------------------------------------------- 发送


def _post(url: str, body: Any, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_for_api(base_url: str, attempts: int = 30) -> None:
    """compose 里 api 可能比 sim 晚就绪, 先探活再开跑。"""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=3):
                return
        except (urllib.error.URLError, OSError):
            if i == 0:
                print(f"等待 API 就绪 {base_url} ...")
            time.sleep(2)
    raise SystemExit(f"API 一直没起来: {base_url}/health")


def to_payload(ev: dict[str, Any], epoch_ms: int, time_scale: float = 1.0) -> dict[str, Any]:
    """事件 -> /ingest 报文。

    time_scale 决定事件时间戳是否随播放加速一起压缩:
    - 1.0(live 默认按 1/speed 传入): 时间戳贴合墙上时钟, /status 的 age/online 才有意义;
    - 1.0 且 --scenario-ts / batch 模式: 保留场景原始间隔, W3 的策略时间窗(如"3 分钟内")
      才能被真实触发。两者不可兼得, 因此做成显式开关而不是猜。
    """
    payload = {k: v for k, v in ev.items() if k != "at_s" and v is not None}
    payload["ts"] = epoch_ms + int(ev["at_s"] * 1000 * time_scale)
    return payload


def chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def run_batch(base_url: str, src: Source, epoch_ms: int, size: int) -> None:
    stored = 0
    for batch in chunks(src.events, size):
        res = _post(f"{base_url}/ingest/batch", [to_payload(e, epoch_ms) for e in batch])
        stored += res.get("stored", 0)
        print(f"  batch {len(batch):>4} 条 -> 新增 {res.get('stored')}")
    print(f"[{src.name}] 批量灌入完成: 提交 {len(src.events)} 条, 新增 {stored} 条")


def run_live(base_url: str, src: Source, epoch_ms: int, speed: float, time_scale: float) -> None:
    t0 = time.time()
    for ev in src.events:
        wait = t0 + ev["at_s"] / speed - time.time()
        if wait > 0:
            time.sleep(wait)
        payload = to_payload(ev, epoch_ms, time_scale)
        res = _post(f"{base_url}/ingest", payload)
        tag = payload.get("sensor_id", payload["device_id"])
        flag = "" if res.get("stored") else "  (幂等命中, 未重复写入)"
        print(f"  t+{ev['at_s']:>8.1f}s {payload['kind']:<13} {tag}{flag}")
    print(f"[{src.name}] 回放结束")


def resolve_epoch(src: Source, shift_to_now: bool) -> int:
    """决定事件绝对时间戳的起点。

    - YAML 剧本没有真实时间, 永远以"现在"为起点;
    - CSV 默认保留其真实历史时间戳(便于分析真实数据);
    - --shift-to-now 把整条时间轴平移到当前时刻, 演示时看起来像刚发生。
    """
    if shift_to_now or src.origin_epoch_ms is None:
        return int(time.time() * 1000)
    return src.origin_epoch_ms


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sentinel 设备模拟器")
    p.add_argument("source", help="YAML 场景包 或 CSV 历史读数")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--speed", type=float, default=1.0, help="时间加速倍数 (live 模式)")
    p.add_argument("--batch", action="store_true", help="不按时间轴等待, 整批灌库")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--loop", action="store_true", help="循环回放 (live 模式演示用)")
    p.add_argument("--shift-to-now", action="store_true",
                   help="把时间轴平移到当前时刻, 让历史数据看起来是刚刚发生的")
    p.add_argument("--device-id", default=None, help="覆盖 CSV 里的 device_id")
    p.add_argument("--scenario-ts", action="store_true",
                   help="live 模式下保留场景原始时间间隔(不随 --speed 压缩); "
                        "W3 验证策略时间窗时需要, 代价是事件时间戳会领先墙上时钟")
    a = p.parse_args(argv)

    src = load_source(a.source, a.device_id)
    span = src.events[-1]["at_s"] if src.events else 0
    mode = "batch" if a.batch else f"live x{a.speed}"
    print(
        f"数据源: {src.name} | {len(src.events)} 条事件 | "
        f"覆盖 {span / 3600:.2f} 小时 | 模式: {mode}"
    )

    wait_for_api(a.base_url)

    while True:
        epoch_ms = resolve_epoch(src, a.shift_to_now)
        if a.batch:
            run_batch(a.base_url, src, epoch_ms, a.batch_size)
        else:
            time_scale = 1.0 if a.scenario_ts else 1.0 / a.speed
            run_live(a.base_url, src, epoch_ms, a.speed, time_scale)
        if not a.loop:
            return 0
        print("--- loop ---")


if __name__ == "__main__":
    sys.exit(main())
