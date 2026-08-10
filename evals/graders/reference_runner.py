"""参考回放器: 把 (policy + companions) 在用例声明的场景上跑出 ReplayReport。

三件事各归各位 (SPEC-007 第七节):
- **场景装载**走 `packages/scenario` 的 loader (那个包允许读文件), 本模块只解析
  场景名到路径: 产品场景在仓库根 `scenarios/`, 评测专用场景在 `evals/scenarios/`,
  `history_csv` 是那 344 条真实读数。评测场景不进产品目录 —— 演练面板与
  `simulate_policy` 的枚举都不该看见它;
- **zone 富化住在这里**, 不进 policy_engine (零 IO + W3 冻结) 也不进 scenario
  (装载是装载, 富化是判分侧的事)。CSV 事件不带 zone_id (那一列不在遥测报文里,
  线上由 policy_runtime 查库补), 离线判分只能从库存快照补 —— 快照是
  `evals/fixtures/inventory.json` **一份**, 与数据集 lint 共用, 不许再写第二份
  sensor→zone 映射;
- **companions 与被判分策略一起进同一次回放** (SPEC-007 第二节): incident_elapsed
  / close_incident 类策略单跑永不触发, 判分是空对空。被判分策略固定用
  JUDGED_POLICY_ID, companions 从 COMPANION_BASE_ID 起编 —— 比较前按 policy_id
  筛出被判分那条的 Effect (归一化规则, SPEC-007 第三节)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scenario.loader import load_source

from policy_engine import Effect, LoadedPolicy, Policy, ReplayReport, SkippedAction, replay

REPO = Path(__file__).resolve().parents[2]
PRODUCT_SCENARIOS = REPO / "scenarios"
EVAL_SCENARIOS = REPO / "evals" / "scenarios"
HISTORY_CSV = REPO / "apps" / "device-sim" / "seed" / "waterlevel_readings.csv"
INVENTORY_FIXTURE = REPO / "evals" / "fixtures" / "inventory.json"

# 被判分策略与陪跑策略的 id 段。归一化第一步"按 policy_id 筛"靠它们分辨谁是谁。
JUDGED_POLICY_ID = 1
COMPANION_BASE_ID = 1001

# 判分用的固定回放配置 (SPEC-007 第三节 + 第二停顿点补入 19)。**显式传值,
# 不靠引擎默认值**: 有人调了 policy_engine 的 DEFAULT_TICK_SECONDS, 全部历史
# 分数会静悄悄变成另一套而没有任何东西会红。两个值随 run 快照一起归档;
# 用例覆盖时必须在用例里显式写出。
REPLAY_TICK_SECONDS = 10
REPLAY_TAIL_S = 600


def load_inventory() -> dict[str, Any]:
    """库存快照 (evals/fixtures/inventory.json)。与 dev seed 的一致性由
    apps/api/tests/test_eval_fixtures.py 连库断言, 本模块信任它。"""
    data: dict[str, Any] = json.loads(INVENTORY_FIXTURE.read_text())
    return data


def sensor_zone_map(inventory: dict[str, Any]) -> dict[int, int]:
    return {s["id"]: s["zone_id"] for s in inventory["sensors"]}


def resolve_scenario(name: str) -> Path:
    """场景名 -> 文件路径。产品场景优先 (评测场景不许与产品场景重名)。"""
    if name == "history_csv":
        return HISTORY_CSV
    product = PRODUCT_SCENARIOS / f"{name}.yaml"
    evaluation = EVAL_SCENARIOS / f"{name}.yaml"
    if product.exists() and evaluation.exists():
        raise ValueError(f"场景 {name!r} 在产品目录与评测目录都存在, 不许重名")
    if product.exists():
        return product
    if evaluation.exists():
        return evaluation
    raise ValueError(f"未知场景 {name!r}: {product} 与 {evaluation} 都不存在")


def load_events(name: str, sensor_zone: dict[int, int]) -> list[dict[str, Any]]:
    """装载 + zone 富化。富化只补空缺, 不覆盖场景里显式写的 zone_id
    (YAML 剧本手写的 zone 只在纯离线模拟时生效, SPEC-001 第一节 —— 判分正是
    纯离线模拟, 且三个产品场景的手写 zone 与 seed 一致)。"""
    source = load_source(str(resolve_scenario(name)), None)
    events: list[dict[str, Any]] = []
    for raw in source.events:
        event = dict(raw)
        sensor_id = event.get("sensor_id")
        if event.get("zone_id") is None and isinstance(sensor_id, int):
            zone = sensor_zone.get(sensor_id)
            if zone is not None:
                event["zone_id"] = zone
        events.append(event)
    return events


def run_reference(
    body: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    companions: list[dict[str, Any]] | None = None,
    source: str = "eval",
    judged_policy_id: int = JUDGED_POLICY_ID,
) -> ReplayReport:
    """跑一次 (被判分策略 + companions) 的回放, 返回完整报告 (未筛)。

    judged_policy_id 可覆盖: 判分时参考策略与候选策略**刻意用不同的 id 跑**
    (与线上事实一致 —— 两者的 id 天然不同), 归一化剥掉 policy_id 才因此是
    有约束力的一步, 变异 M2 (去掉剥离) 才打得红。"""
    policies = [
        LoadedPolicy(
            policy_id=judged_policy_id, version=1, body=Policy.model_validate(body)
        )
    ]
    for i, companion in enumerate(companions or []):
        policies.append(
            LoadedPolicy(
                policy_id=COMPANION_BASE_ID + i,
                version=1,
                body=Policy.model_validate(companion),
            )
        )
    return replay(
        policies, events, source=source,
        tick_seconds=REPLAY_TICK_SECONDS, tail_s=REPLAY_TAIL_S,
    )


def judged_effects(
    report: ReplayReport, judged_policy_id: int = JUDGED_POLICY_ID
) -> list[Effect]:
    """归一化第一步: 按 policy_id 筛出被判分那条的 Effect (companions 的不参与
    比较, 它们的意义在于副作用经投影器变成事故事件喂回事件流)。"""
    return [e for e in report.effects if e.policy_id == judged_policy_id]


def judged_skipped(
    report: ReplayReport, judged_policy_id: int = JUDGED_POLICY_ID
) -> list[SkippedAction]:
    return [s for s in report.skipped if s.policy_id == judged_policy_id]
