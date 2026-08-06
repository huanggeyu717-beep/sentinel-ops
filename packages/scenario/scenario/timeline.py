"""时间轴换算: 相对时间 at_s -> /ingest 报文的绝对时间戳。

代码原样搬自 apps/device-sim/sim.py (SPEC-005 方案 B 抽包), 行为逐字不变。
"""
from __future__ import annotations

import time
from typing import Any

from .loader import Source


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


def resolve_epoch(src: Source, shift_to_now: bool) -> int:
    """决定事件绝对时间戳的起点。

    - YAML 剧本没有真实时间, 永远以"现在"为起点;
    - CSV 默认保留其真实历史时间戳(便于分析真实数据);
    - --shift-to-now 把整条时间轴平移到当前时刻, 演示时看起来像刚发生。
    """
    if shift_to_now or src.origin_epoch_ms is None:
        return int(time.time() * 1000)
    return src.origin_epoch_ms
