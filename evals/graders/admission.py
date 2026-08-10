"""判别性准入 (SPEC-007 第三节): 每条会跑 evaluate() 的用例, 其全部机械变异体
必须在声明的场景集合上被判"不等价" —— 这是数据集的准入条件, 不是加分项。

行为等价判分真正的风险不是误判为错, 是**误判为对**: 冷却 300 与 600 在只触发
一次的场景上 Effect 序列一模一样。有一个变异体被判等价, 说明场景没有判别力。

两个例外口子, 各有边界:
- **also_accept 自动排除** (SPEC-007 补入 20): 生成器机械产出的变异体可能恰好
  就是某个 also_accept (repairable-003 的 scope.type:zone->global 正是那个正确
  答案), 要求判它"不等价"是自相矛盾的。与任一 also_accept 行为等价的变异体
  排除出准入, 不占 known_equivalent 的额度;
- **known_equivalent 手工例外**: 每条必须有理由; 且引用的变异 id 必须真实存在
  于生成集合 (id 由内容推导, 规则改了引用会失效 —— 存在性断言让失效当场红)。

性能: 场景事件由调用方装载一次传入 (100 条 × ~20 变异 × 场景数 ≈ 数千次
evaluate(), 单次 14-16ms 可接受; 但每个变异体重新装载富化一遍 1258 事件的
history_csv 就不行了)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .behavior_grader import CANDIDATE_POLICY_ID, Trace, trace_on
from .mutants import generate_mutants
from .reference_runner import JUDGED_POLICY_ID


@dataclass(frozen=True)
class AdmissionResult:
    case_id: str
    total_mutants: int
    discriminated: int                      # 被判"不等价"的 (好)
    excluded_by_also_accept: tuple[str, ...] = ()
    covered_by_known_equivalent: tuple[str, ...] = ()
    unexplained_equivalent: tuple[str, ...] = ()  # 非空 = 准入失败
    missing_known_equivalent_refs: tuple[str, ...] = ()  # 引用了不存在的变异 id
    stale_known_equivalent: tuple[str, ...] = ()  # 声明了例外但其实分得开 (该删)

    @property
    def ok(self) -> bool:
        return not self.unexplained_equivalent and not self.missing_known_equivalent_refs


@dataclass
class _TraceCache:
    """(body 的 json 键, 场景名) -> 轨迹。reference 与 also_accept 在多个变异体
    的比较里反复出现, 不缓存就是 O(变异数 × 场景数) 次重复回放。"""

    events_by_scenario: dict[str, list[dict[str, Any]]]
    companions: list[dict[str, Any]] | None
    _store: dict[tuple[int, str, int], Trace] = field(default_factory=dict)

    def trace(self, body: dict[str, Any], scenario: str, policy_id: int) -> Trace:
        key = (id(body), scenario, policy_id)
        if key not in self._store:
            self._store[key] = trace_on(
                body, self.events_by_scenario[scenario],
                companions=self.companions,
                judged_policy_id=policy_id, source=scenario,
            )
        return self._store[key]


def check_case(
    case: dict[str, Any],
    events_by_scenario: dict[str, list[dict[str, Any]]],
    *,
    zone_ids: frozenset[int],
    sensor_ids: frozenset[int],
) -> AdmissionResult:
    expected = case["expected"]
    reference = expected["reference"]
    also_accept = [alt["policy"] for alt in expected.get("also_accept", [])]
    ke_ids = {k["mutant"] for k in case.get("known_equivalent", [])}
    cache = _TraceCache(events_by_scenario, case.get("companions"))
    scenarios = list(events_by_scenario)

    mutants = generate_mutants(reference, zone_ids=zone_ids, sensor_ids=sensor_ids)
    missing_refs = tuple(sorted(ke_ids - set(mutants)))

    def equivalent(body_a: dict[str, Any], mutant: dict[str, Any]) -> bool:
        return all(
            cache.trace(body_a, name, JUDGED_POLICY_ID)
            == cache.trace(mutant, name, CANDIDATE_POLICY_ID)
            for name in scenarios
        )

    discriminated = 0
    excluded: list[str] = []
    covered: list[str] = []
    unexplained: list[str] = []
    for mutant_id, mutant_body in sorted(mutants.items()):
        if not equivalent(reference, mutant_body):
            discriminated += 1
            continue
        if any(equivalent(alt, mutant_body) for alt in also_accept):
            excluded.append(mutant_id)
        elif mutant_id in ke_ids:
            covered.append(mutant_id)
        else:
            unexplained.append(mutant_id)

    # 声明了例外、但变异体其实分得开: 例外过期了, 该删 (报告用, 不算失败 ——
    # 但 lint 会把它列出来, "定形时若换场景则删除本条"那句话要真的兑现)
    stale = tuple(sorted(ke_ids - set(covered) - set(missing_refs)))

    return AdmissionResult(
        case_id=case["id"],
        total_mutants=len(mutants),
        discriminated=discriminated,
        excluded_by_also_accept=tuple(excluded),
        covered_by_known_equivalent=tuple(covered),
        unexplained_equivalent=tuple(unexplained),
        missing_known_equivalent_refs=missing_refs,
        stale_known_equivalent=stale,
    )
