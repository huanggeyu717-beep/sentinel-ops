"""判分侧的组装: 场景事件缓存 + 调 evals.graders (逻辑全在 graders, 这里零判据)。"""
from __future__ import annotations

from typing import Any

from evals.graders.case_grader import CaseGrade, CaseOutcome, grade_case
from evals.graders.mutants import generate_mutants
from evals.graders.reference_runner import (
    load_events,
    load_inventory,
    sensor_zone_map,
)


class ScenarioEvents:
    """场景名 -> 富化后事件流, 每场景装载一次。zone 富化用库存快照
    (evals/fixtures/inventory.json), 与数据集 lint 同一份 —— 不连库。"""

    def __init__(self) -> None:
        self._inventory = load_inventory()
        self._zone_map = sensor_zone_map(self._inventory)
        self._cache: dict[str, list[dict[str, Any]]] = {}

    @property
    def inventory(self) -> dict[str, Any]:
        return self._inventory

    def events_for_case(self, case: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name in case.get("scenarios", []):
            if name not in self._cache:
                self._cache[name] = load_events(name, self._zone_map)
            out[name] = self._cache[name]
        return out


def grade_row(
    case: dict[str, Any], outcome: CaseOutcome, scenarios: ScenarioEvents
) -> CaseGrade:
    return grade_case(case, outcome, scenarios.events_for_case(case))


def mutant_sets(
    cases: list[dict[str, Any]], scenarios: ScenarioEvents
) -> dict[str, list[str]]:
    """每条 behavior_equiv 类用例当时生成的变异 id 集合 —— 进 run 归档不进数据集
    (SPEC-007 第二节: 归档本来就是快照, 半年后规则改了还能算清当时的分)。"""
    inventory = scenarios.inventory
    zone_ids = frozenset(int(z["id"]) for z in inventory["zones"])
    sensor_ids = frozenset(int(s["id"]) for s in inventory["sensors"])
    out: dict[str, list[str]] = {}
    for case in cases:
        reference = case.get("expected", {}).get("reference")
        if isinstance(reference, dict):
            out[str(case["id"])] = sorted(
                generate_mutants(reference, zone_ids=zone_ids, sensor_ids=sensor_ids)
            )
    return out
