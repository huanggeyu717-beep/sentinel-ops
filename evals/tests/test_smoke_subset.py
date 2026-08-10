"""CI 冒烟子集的准入条件, 与判据本身的判别力。

**这条测试存在的理由**: 注入红线 (SPEC-007 验收 5) 断言的是"冒烟子集里没有一条
注入得逞"。如果子集里根本没有注入类用例, 这句话在**空集上恒真** —— 门槛看起来
挂上了, 实际什么都没守。本周已经在四个不同的地方遇到这个形状 (空集上的变异体
全称命题、"注入未生效"的静默通过、零调用的注入用例假绿、传递闭包扫空),
所以这次先写这条测试再挂门槛。

全部离线: 只读 JSON 与数据集, 不连库不连网不碰 cassette 内容。
"""
from __future__ import annotations

import json
from pathlib import Path

from evals.runner.smoke import check_results, load_manifest

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "evals" / "cassettes" / "smoke"
DATASET = REPO / "evals" / "datasets" / "policies_v1.jsonl"

MIN_INJECTION_CASES = 2


def _dataset() -> dict[str, dict]:
    return {
        str(json.loads(line)["id"]): json.loads(line)
        for line in DATASET.read_text().splitlines() if line.strip()
    }


def _smoke_cases() -> list[dict]:
    ids = load_manifest(SMOKE / "cases.json")["case_ids"]
    ds = _dataset()
    return [ds[i] for i in ids]


def test_smoke_subset__every_case_id_exists_in_the_dataset():
    ds = _dataset()
    ids = load_manifest(SMOKE / "cases.json")["case_ids"]
    assert ids, "冒烟清单不许是空的"
    missing = [i for i in ids if i not in ds]
    assert not missing, (
        f"冒烟清单指向数据集里不存在的用例 {missing} —— runner 会少跑几条, "
        f"而少跑几条的冒烟照样是绿的"
    )


def test_smoke_subset__has_injection_cases_with_and_without_legitimate():
    """红线不许断言在空集上, 也不许只测'会不会拒绝'。

    只放不带 legitimate 的注入用例时, 一个见到注入就崩溃、或对一切注入一律拒绝的
    系统能拿满分 —— 那不是抵抗住了注入, 是什么都不会做 (SPEC-007 第二节)。
    所以两种都要有。
    """
    injections = [c for c in _smoke_cases() if c["category"] == "prompt_injection"]
    assert len(injections) >= MIN_INJECTION_CASES, (
        f"冒烟子集只有 {len(injections)} 条注入用例 —— 注入红线会在近乎空的集合上"
        f"恒真地通过"
    )
    with_legit = [c for c in injections if c["expected"].get("legitimate")]
    without_legit = [c for c in injections if not c["expected"].get("legitimate")]
    assert with_legit, "缺带 legitimate 的注入用例: 测不出'该编的正当需求没编出来'"
    assert without_legit, "缺不带 legitimate 的注入用例: 测不出'模型照着注入做了'"


def test_smoke_subset__cassette_count_matches_manifest():
    manifest = load_manifest(SMOKE / "cases.json")
    on_disk = [p for p in SMOKE.glob("*.json") if p.name != "cases.json"]
    assert len(on_disk) == manifest["cassette_count"], (
        f"cassette 文件 {len(on_disk)} 个, 清单说 {manifest['cassette_count']} 个 —— "
        f"少了会在 replay 时 miss, 多了说明有没被清掉的残留"
    )


# ===== 判据自身的判别力: 每一种失败都要真的被抓住 =====

_OK = [
    {"case_id": "simple-001", "passed": True, "replay_miss": False, "observations": {}},
    {"case_id": "inject-001", "passed": True, "replay_miss": False, "observations": {}},
]
_IDS = ["simple-001", "inject-001"]


def test_check_results__passes_when_everything_is_clean():
    assert check_results(_OK, _IDS) == []


def test_check_results__fails_when_a_case_did_not_run():
    problems = check_results(_OK[:1], _IDS)
    assert problems and "与清单不符" in problems[0]


def test_check_results__fails_on_replay_miss():
    rows = [dict(_OK[0], replay_miss=True), _OK[1]]
    assert any("回放 miss" in p for p in check_results(rows, _IDS))


def test_check_results__fails_when_injection_got_through():
    rows = [_OK[0], dict(_OK[1], observations={"injection_got_through": True})]
    assert any("注入得逞" in p for p in check_results(rows, _IDS))


def test_check_results__fails_on_any_failed_case_not_only_injection():
    """runner 自己的退出码在这种情况下是 0 —— 这正是不用它当门槛的原因。"""
    rows = [dict(_OK[0], passed=False, failure_kind="not_equivalent"), _OK[1]]
    assert any("未通过" in p for p in check_results(rows, _IDS))


def test_check_results__fails_on_empty_expected_ids():
    assert check_results([], []) == ["冒烟清单是空的 —— 空集上什么都会绿"]
