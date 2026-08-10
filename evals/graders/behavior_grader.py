"""行为等价 Grader (SPEC-007 第三节)。

判分方式: 把候选 Policy 与答案键 (reference / 每一个 also_accept) 分别连同
companions 在同一场景集合上跑 policy_engine.replay(), 比较归一化后的 Effect
序列**与 skipped 列表**。写法不同但行为一致的策略同样得分, 避免 AST 精确匹配
的假阴性。不使用 LLM judge: 策略正确性有可执行定义, 判分必须零方差、零成本。

归一化规则 (SPEC-007 第三节, 外加一条本模块的实现决定):
1. 先按 policy_id 筛出被判分那条的 Effect (companions 不参与比较), 再剥
   policy_id / policy_version —— 参考与候选刻意用不同 id 跑, 剥离才有约束力;
2. subject 里的 None 保留 (None 与 0 不是一回事); detail 按 key 排序;
   ts_ms 严格相等;
3. **同一 ts_ms 内的多个 Effect 按内容排序后比较** (SPEC 未写死, 本实现的决定):
   金样正样要求"动作数组顺序调换 = 行为一致", 而引擎按动作声明序产出 —— 不做
   这一步, [notify, set_led] 与 [set_led, notify] 会被判不同。排序键以 ts_ms
   开头, 跨时刻的先后仍然严格保序; 复制动作的变异体照样可分 (两条相同条目 ≠
   一条)。已在完成报告第一节报备。

过程指标 (修复轮次、澄清轮次, 读 agent_steps) 不归本模块, 归第二段的消融 runner。
"""
from __future__ import annotations

from typing import Any

from policy_engine import ReplayReport

from .reference_runner import (
    JUDGED_POLICY_ID,
    judged_effects,
    judged_skipped,
    run_reference,
)

# 候选策略跑判分时用的 id, 刻意不等于 JUDGED_POLICY_ID (见 run_reference docstring)
CANDIDATE_POLICY_ID = 2

# 归一化后的行为轨迹: (effects 元组序列, skipped 元组序列)
Trace = tuple[tuple[Any, ...], tuple[Any, ...]]


def normalized_trace(report: ReplayReport, judged_policy_id: int) -> Trace:
    effects = tuple(
        sorted(
            (
                e.ts_ms,
                e.action_type,
                (
                    e.subject.sensor_id,
                    e.subject.zone_id,
                    e.subject.device_id,
                    e.subject.incident_id,
                ),
                tuple(sorted(e.detail.items())),
            )
            for e in judged_effects(report, judged_policy_id)
        )
    )
    # skipped 必须进比较: 不比它, "动作全被跳过"与"条件根本没满足"会被判成同一种对
    skipped = tuple(
        sorted(
            (s.ts_ms, s.action_type, tuple(s.missing), s.reason)
            for s in judged_skipped(report, judged_policy_id)
        )
    )
    return (effects, skipped)


def trace_on(
    body: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    companions: list[dict[str, Any]] | None = None,
    judged_policy_id: int = JUDGED_POLICY_ID,
    source: str = "eval",
) -> Trace:
    """一份策略在一个场景上的归一化行为轨迹。"""
    report = run_reference(
        body, events, companions=companions, source=source,
        judged_policy_id=judged_policy_id,
    )
    return normalized_trace(report, judged_policy_id)


def equivalent_on_all(
    body_a: dict[str, Any],
    body_b: dict[str, Any],
    events_by_scenario: dict[str, list[dict[str, Any]]],
    *,
    companions: list[dict[str, Any]] | None = None,
) -> bool:
    """两份策略在**每个**声明场景上轨迹都相同才算等价 —— 有一个场景分得开就不等价。
    a 与 b 刻意用不同 policy_id 跑 (归一化剥离因此有约束力)。"""
    for name, events in events_by_scenario.items():
        trace_a = trace_on(
            body_a, events, companions=companions,
            judged_policy_id=JUDGED_POLICY_ID, source=name,
        )
        trace_b = trace_on(
            body_b, events, companions=companions,
            judged_policy_id=CANDIDATE_POLICY_ID, source=name,
        )
        if trace_a != trace_b:
            return False
    return True


def grade(
    generated_policy: dict[str, Any],
    expected: dict[str, Any],
    events_by_scenario: dict[str, list[dict[str, Any]]],
    *,
    companions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """行为等价类 (behavior_equiv / repairable / clarify 的产物半边) 的判分。

    候选与 reference 及**每一个** also_accept 依次比较, 命中任一即通过
    (多个正确答案, SPEC-007 第二节; "只比第一个"是变异 M10 要打红的错法)。
    返回 {passed, matched}: matched 标明命中的是哪个答案, 进 results.jsonl。
    """
    if equivalent_on_all(
        expected["reference"], generated_policy, events_by_scenario,
        companions=companions,
    ):
        return {"passed": True, "matched": "reference"}
    for i, alt in enumerate(expected.get("also_accept", [])):
        if equivalent_on_all(
            alt["policy"], generated_policy, events_by_scenario,
            companions=companions,
        ):
            return {"passed": True, "matched": f"also_accept[{i}]"}
    return {"passed": False, "matched": None}
