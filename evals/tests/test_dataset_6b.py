"""验收 6b: 会跑 evaluate() 的用例, reference 在声明的场景上不许空对空。

这条 lint 是 companions 缺失、场景选错这两类问题的统一探测器 (SPEC-007 验收 6b),
**必须排在判别性变异准入之前跑** —— 准入检查遇到空产出会报一大片"全部等价",
而真正的病因是 reference 自己什么都没发生。

变异 M9 的靶子: 删掉某条 incident_elapsed 用例的 companions, 这里必须红
(**不是**该用例的判分测试 —— 判分在那个变异下照样绿: reference 与全部变异体
一起变成空序列, 彼此"等价")。

口径说明: "至少一个 Effect 或一条 skipped" 按**声明场景的并集**判, 不按单场景 ——
用例允许声明刻意不触发的反例场景 (simple-001 的 basic_spill 守的是"2 区的水
不该惊动 1 区的策略", 它上面空产出是设计, 不是病)。单场景全空才是判别力为零。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.graders.reference_runner import (
    judged_effects,
    judged_skipped,
    load_events,
    load_inventory,
    run_reference,
    sensor_zone_map,
)

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "policies_v1.jsonl"

# 会跑 evaluate() 的 kind (SPEC-007 第二节: 其余 kind 不跑场景)
_SIMULATED_KINDS = frozenset({"behavior_equiv", "repairable", "clarify"})


def _cases() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET.read_text().splitlines()
        if line.strip()
    ]


def _simulated_cases() -> list[dict]:
    return [c for c in _cases() if c["expected"]["kind"] in _SIMULATED_KINDS]


@pytest.fixture(scope="module")
def zone_map() -> dict[int, int]:
    return sensor_zone_map(load_inventory())


@pytest.mark.parametrize(
    "case", _simulated_cases(), ids=lambda c: c["id"]
)
def test_reference_produces_effects__on_declared_scenarios(case, zone_map):
    """并集口径: 全部声明场景加起来, 被判分策略至少产出一个 **Effect**。

    收紧到 Effect 而不是 "Effect 或 skipped" (SPEC-007 补入 16, 第二停顿点裁决):
    close_incident 类缺陪跑时会退化成纯 skipped —— 不是全空, 但"从来没真关过单"
    的答案键判别力同样是零。失败信息带 (effects, skipped) 二元组, skipped 多而
    effects 为零通常指向缺 companions 而不是场景选错。"""
    effects_total = 0
    skipped_total = 0
    for name in case["scenarios"]:
        events = load_events(name, zone_map)
        report = run_reference(
            case["expected"]["reference"], events,
            companions=case.get("companions"), source=name,
        )
        effects_total += len(judged_effects(report))
        skipped_total += len(judged_skipped(report))
    assert effects_total > 0, (
        f"{case['id']}: reference 在 {case['scenarios']} 上 (effects={effects_total}, "
        f"skipped={skipped_total}) —— 判别力必然是零, 而它看起来是绿的。"
        f"skipped>0 而 effects=0 通常是缺 companions (incident_elapsed / "
        f"close_incident 类策略单跑永不触发); 双零是场景选错"
    )


def test_dataset__scenarios_declared_for_simulated_kinds():
    """会跑 evaluate() 的 kind 必须声明至少一个场景 (其余 kind 允许为空)。"""
    for case in _simulated_cases():
        assert case["scenarios"], f"{case['id']}: {case['expected']['kind']} 类必须声明场景"


def _legitimate_injection_cases() -> list[dict]:
    return [
        c for c in _cases()
        if c["expected"]["kind"] == "injection_resisted"
        and "legitimate" in c["expected"]
    ]


@pytest.mark.parametrize(
    "case", _legitimate_injection_cases(), ids=lambda c: c["id"]
)
def test_legitimate_produces_something__on_declared_scenarios(case, zone_map):
    """6b 对 injection 带 legitimate 那半边的延伸 (SPEC 验收 6b 未点名, 同一条
    道理): "产物与 legitimate 行为等价"的判据里, legitimate 自己在判分场景上
    空产出的话, 等价比较会退化成空对空 —— 一份什么都不做的错误草案也能通过。
    第一版 inject-004 就踩过 (legitimate 选了 CSV 里不存在的 3 号探头)。"""
    effects_total = 0
    for name in case["scenarios"]:
        events = load_events(name, zone_map)
        # companions 同样要带上 —— 关单类的 legitimate 没有陪跑开单必然空产出
        # (第一版这里没传, inject-007 当场红, 修的是测试不是用例)
        report = run_reference(case["expected"]["legitimate"], events,
                               companions=case.get("companions"), source=name)
        effects_total += len(judged_effects(report))
    assert effects_total > 0, (
        f"{case['id']}: legitimate 在 {case['scenarios']} 上零产出, "
        f"行为等价那半边的判据是空的"
    )
