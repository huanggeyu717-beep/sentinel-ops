"""device-sim —— 数字孪生模拟器 (W1 核心交付)。

硬件已归还, 本模拟器是整个系统唯一的数据源: live 演示 / Policy 模拟引擎输入 /
eval fixtures / E2E 冒烟测试 共用同一份场景语义。

两种数据源, 按扩展名自动识别:
  1) YAML 场景包  —— 手写剧本, 相对时间轴, 用于确定性演示与回归测试
  2) CSV 历史回放 —— 回放原系统真实采集的读数 (队友仓库导出的 waterlevel_readings.csv)

场景装载与时间轴换算在 packages/scenario (SPEC-005 方案 B 抽包, API 的演练接口
复用同一份); 本文件只剩 命令行参数 / 按 --speed 推进时间 / HTTP 投递 / 进度打印。

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
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# 让 `python sim.py ...` 不经 pip install 也能找到仓库内的 scenario 包
# (与根目录 pytest.ini 的 pythonpath 同理, 避免"换了解释器就 ModuleNotFoundError")。
# Docker 里该包是 pip install 进镜像的, 这个目录不存在, 走正常导入。
_SCENARIO_PKG = Path(__file__).resolve().parent.parent.parent / "packages" / "scenario"
if _SCENARIO_PKG.is_dir():
    sys.path.insert(0, str(_SCENARIO_PKG))

from scenario import Source, load_source, resolve_epoch, to_payload  # noqa: E402

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
