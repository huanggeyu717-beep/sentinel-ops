#!/usr/bin/env python3
"""答案键静态自检 (W5 第一段, 样例批用)。

对 evals/datasets/policies_v1.jsonl 里每份答案键策略 (reference / also_accept /
legitimate / companions):
1. 过 Pydantic Schema (白名单外的 type / extra 字段直接失败);
2. 过静态验证器 (与 Agent 用的同一个 validate())。

库存**只读 evals/fixtures/inventory.json 一份快照** (SPEC-007 第七节: 不许第二份
sensor→zone 映射); 快照与 dev seed 的一致性由 apps/api/tests/test_eval_fixtures.py
连库断言。

"一个自己都编译不过的答案键是评测集最经典的 bug" (SPEC-007 第二节答案键规则 2)。
这份检查后续会被 evals/ 的数据集 lint 吸收成 CI 测试 (第二停顿点之后);
在那之前它是手动跑的探针: python scripts/dev/check_dataset_refs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages/policy_engine"))

from policy_engine import Inventory, Policy, validate  # noqa: E402

DATASET = REPO / "evals/datasets/policies_v1.jsonl"
INVENTORY_FIXTURE = REPO / "evals/fixtures/inventory.json"


def load_inventory() -> Inventory:
    data = json.loads(INVENTORY_FIXTURE.read_text())
    return Inventory(
        zone_ids=frozenset(z["id"] for z in data["zones"]),
        sensor_ids=frozenset(s["id"] for s in data["sensors"]),
        sensor_zone={s["id"]: s["zone_id"] for s in data["sensors"]},
        roles_present=frozenset(data["roles_present"]),
    )


def check_policy(inv: Inventory, case_id: str, label: str, body: dict) -> list[str]:
    problems: list[str] = []
    try:
        policy = Policy.model_validate(body)
    except Exception as e:  # 报告用, 收敛所有校验错
        return [f"{case_id} {label}: Schema 不过: {e}"]
    result = validate(policy, inv)
    for issue in result.issues:
        problems.append(f"{case_id} {label}: {issue.code} @ {issue.path}: {issue.message}")
    return problems


def main() -> int:
    inv = load_inventory()
    problems: list[str] = []
    cases = 0
    checked = 0
    for line_no, line in enumerate(DATASET.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"第 {line_no} 行不是合法 JSON: {e}")
            continue
        cases += 1
        expected = case.get("expected", {})
        keyed: list[tuple[str, dict]] = []
        if "reference" in expected:
            keyed.append(("reference", expected["reference"]))
        if "legitimate" in expected:
            keyed.append(("legitimate", expected["legitimate"]))
        keyed += [
            (f"also_accept[{i}]", alt["policy"])
            for i, alt in enumerate(expected.get("also_accept", []))
        ]
        keyed += [
            (f"companions[{i}]", body)
            for i, body in enumerate(case.get("companions", []))
        ]
        for label, body in keyed:
            problems += check_policy(inv, case["id"], label, body)
            checked += 1
    print(f"{cases} 条用例, {checked} 份答案键策略过检")
    if problems:
        print("\n".join(problems))
        return 1
    print("全部通过 Pydantic + 静态验证器")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
