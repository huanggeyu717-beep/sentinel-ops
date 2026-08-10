"""判别性变异准入 (SPEC-007 验收 11): 全部变异体判"不等价", 否则不许进数据集。

变异 M6 的靶子 (关掉准入检查 + 把某条用例的场景换成没判别力的 → 这里必须红)。
known_equivalent 的存在性断言也在这里 (SPEC-007 第二节 mutants 三条约束之 2)。

统计口径的完整输出 (一次通过/打回/例外使用情况) 由
scripts/dev/run_admission_stats.py 打印, 与本测试共用 admission.check_case。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.graders.admission import check_case
from evals.graders.reference_runner import load_events, load_inventory, sensor_zone_map

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "policies_v1.jsonl"
_SIMULATED_KINDS = frozenset({"behavior_equiv", "repairable", "clarify"})


def _simulated_cases() -> list[dict]:
    return [
        c
        for c in (
            json.loads(line)
            for line in DATASET.read_text().splitlines()
            if line.strip()
        )
        if c["expected"]["kind"] in _SIMULATED_KINDS
    ]


_INVENTORY = load_inventory()
_ZONE_IDS = frozenset(z["id"] for z in _INVENTORY["zones"])
_SENSOR_IDS = frozenset(s["id"] for s in _INVENTORY["sensors"])
_EVENTS_CACHE: dict[str, list[dict]] = {}


def _events(name: str) -> list[dict]:
    # 场景装载一次、复用 —— 每个变异体重新装载富化 history_csv (1258 事件)
    # 会把一分钟量级的准入拖成十分钟量级
    if name not in _EVENTS_CACHE:
        _EVENTS_CACHE[name] = load_events(name, sensor_zone_map(_INVENTORY))
    return _EVENTS_CACHE[name]


@pytest.mark.parametrize("case", _simulated_cases(), ids=lambda c: c["id"])
def test_all_mutants_discriminated__on_declared_scenarios(case):
    result = check_case(
        case,
        {name: _events(name) for name in case["scenarios"]},
        zone_ids=_ZONE_IDS,
        sensor_ids=_SENSOR_IDS,
    )
    assert not result.missing_known_equivalent_refs, (
        f"{case['id']}: known_equivalent 引用了生成集合里不存在的变异 id "
        f"{result.missing_known_equivalent_refs} —— id 由内容推导, 引用失效说明"
        f"生成规则或 reference 变了, 例外要跟着重审"
    )
    assert not result.unexplained_equivalent, (
        f"{case['id']}: {len(result.unexplained_equivalent)}/{result.total_mutants} "
        f"个变异体被判等价且无例外覆盖: {result.unexplained_equivalent}。"
        f"第一反应是换场景, 不是加 known_equivalent —— 先问: 是真等价, "
        f"还是场景太弱?"
    )
