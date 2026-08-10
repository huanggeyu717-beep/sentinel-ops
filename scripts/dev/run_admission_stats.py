#!/usr/bin/env python3
"""判别性准入的完整统计 (完成报告第 11 节用)。

与 evals/tests/test_mutant_admission.py 共用同一份 admission.check_case;
测试只断言"零未解释等价", 这里把每条用例的明细摆出来:
一次通过 / 打回 (未解释等价的变异体) / also_accept 自动排除 / known_equivalent
使用与过期情况。用法: python scripts/dev/run_admission_stats.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "packages/policy_engine"), str(REPO / "packages/scenario"),
                str(REPO)]

from evals.graders.admission import check_case  # noqa: E402
from evals.graders.reference_runner import (  # noqa: E402
    load_events,
    load_inventory,
    sensor_zone_map,
)

DATASET = REPO / "evals/datasets/policies_v1.jsonl"
_SIMULATED_KINDS = frozenset({"behavior_equiv", "repairable", "clarify"})


def main() -> int:
    inventory = load_inventory()
    zone_ids = frozenset(z["id"] for z in inventory["zones"])
    sensor_ids = frozenset(s["id"] for s in inventory["sensors"])
    zone_map = sensor_zone_map(inventory)
    events_cache: dict[str, list[dict]] = {}

    def events(name: str) -> list[dict]:
        if name not in events_cache:
            events_cache[name] = load_events(name, zone_map)
        return events_cache[name]

    passed_first = 0
    bounced: list[str] = []
    excluded_total = 0
    ke_used: list[tuple[str, str]] = []
    ke_stale: list[tuple[str, str]] = []
    mutant_total = 0

    for line in DATASET.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if case["expected"]["kind"] not in _SIMULATED_KINDS:
            continue
        result = check_case(
            case, {n: events(n) for n in case["scenarios"]},
            zone_ids=zone_ids, sensor_ids=sensor_ids,
        )
        mutant_total += result.total_mutants
        status = "ok" if result.ok else "BOUNCED"
        detail = ""
        if result.unexplained_equivalent:
            detail = f" 未解释等价: {list(result.unexplained_equivalent)}"
        if result.excluded_by_also_accept:
            excluded_total += len(result.excluded_by_also_accept)
            detail += f" also_accept排除: {list(result.excluded_by_also_accept)}"
        for m in result.covered_by_known_equivalent:
            ke_used.append((case["id"], m))
        for m in result.stale_known_equivalent:
            ke_stale.append((case["id"], m))
        if result.ok:
            passed_first += 1
        else:
            bounced.append(case["id"])
        print(f"{case['id']:<16} {status:<8} 变异 {result.total_mutants:>2}, "
              f"可分 {result.discriminated:>2}{detail}")

    print(f"\n共 {passed_first + len(bounced)} 条判分类用例, 变异体合计 {mutant_total}")
    print(f"一次通过 {passed_first}, 打回 {len(bounced)}: {bounced}")
    print(f"also_accept 自动排除共 {excluded_total} 次")
    print(f"known_equivalent 生效 {len(ke_used)} 处: {ke_used}")
    if ke_stale:
        print(f"过期例外 (变异其实分得开, 该删): {ke_stale}")
    return 0 if not bounced else 1


if __name__ == "__main__":
    raise SystemExit(main())
