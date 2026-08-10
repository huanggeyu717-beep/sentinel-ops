"""判别性变异生成器 (SPEC-007 第三节的机械规则, 不靠人想)。

三条配套约束 (SPEC-007 第二节 "mutants 不落盘"):
1. 变异 id 由**变异内容**推导 (`scope.type:zone->sensor` / `cooldown_s:x2`),
   不用序号 —— 序号会在规则表插行时整体平移, known_equivalent 里的引用会
   静静地指向另一个变异体, 两个方向同时错且没有任何东西会红;
2. known_equivalent 引用的 id 必须存在于生成集合 (存在性断言在准入测试里);
3. 生成集合进 run 归档, 不进数据集。

**合法性门槛是 Schema (Pydantic), 不是静态验证器**: 变异体模拟的是"写错了的
草案", cooldown 150 配 notify 这种语义非法但语法合法的策略, 恰恰是 grader 必须
分得开的候选 —— 拿验证器过滤会把最有价值的负例筛掉。Schema 都过不了的 (数值
出区间、动作数超上限) 引擎跑不了, 丢弃。

已知的先天空转维度, 照实写: "复制一个 action" 产出的变异体带两个完全相同的
动作, 引擎会产出双份 Effect —— 天然可分, 但它同时是 E_DUPLICATE_ACTION 的
必中项, 真实候选永远到不了这一步。保留它是按 SPEC 的表办事, 判别贡献接近零。
"""
from __future__ import annotations

import json
from typing import Any

from policy_engine import Policy

# 枚举参数的候选值顺序 (固定, "各换一个合法值"取环上的下一个)
_ENUM_VALUES: dict[str, list[Any]] = {
    "to": ["WET", "DRY"],
    "in_status": ["open", "assigned", "acknowledged"],
    "severity": ["normal", "high", "critical"],
    "to_severity": ["high", "critical"],
    "target_role": ["viewer", "operator", "manager", "admin"],
    "count_within": ["same_zone", "any_zone"],
    "state": ["ON", "OFF"],
}
_NUMERIC_FIELDS = frozenset(
    {"value", "window_s", "for_s", "dry_for_s", "offline_for_s", "duration_s"}
)
_SCOPE_TYPES = ["global", "zone", "sensor"]


def _clone(body: dict[str, Any]) -> dict[str, Any]:
    cloned: dict[str, Any] = json.loads(json.dumps(body))
    return cloned


def _schema_valid(body: dict[str, Any]) -> bool:
    try:
        Policy.model_validate(body)
    except Exception:
        return False
    return True


def _next_enum(field: str, current: Any) -> Any:
    values = _ENUM_VALUES[field]
    return values[(values.index(current) + 1) % len(values)]


def generate_mutants(
    body: dict[str, Any], *, zone_ids: frozenset[int], sensor_ids: frozenset[int]
) -> dict[str, dict[str, Any]]:
    """reference -> {变异 id: 变异体 body}。确定性: 同一份输入永远同一个集合。"""
    out: dict[str, dict[str, Any]] = {}

    def add(mutant_id: str, mutant: dict[str, Any]) -> None:
        if _schema_valid(mutant) and mutant != body:
            out[mutant_id] = mutant

    # --- scope.type: 换一档 (换成 global 时 ids 清空; 反向保留原 ids —— 空 ids
    # 的 zone/sensor 是语义非法但语法合法的"写错了", 正是要分辨的对象) ---
    current_type = body["scope"]["type"]
    for other in _SCOPE_TYPES:
        if other == current_type:
            continue
        m = _clone(body)
        m["scope"]["type"] = other
        if other == "global":
            m["scope"]["ids"] = []
        add(f"scope.type:{current_type}->{other}", m)

    # --- scope.ids: 增一个 (最小的不在列的合法 id)、删每一个 ---
    ids = body["scope"]["ids"]
    if current_type in ("zone", "sensor"):
        pool = zone_ids if current_type == "zone" else sensor_ids
        absent = sorted(pool - set(ids))
        if absent:
            m = _clone(body)
            m["scope"]["ids"] = sorted([*ids, absent[0]])
            add(f"scope.ids:+{absent[0]}", m)
        for victim in ids:
            m = _clone(body)
            m["scope"]["ids"] = [i for i in ids if i != victim]
            add(f"scope.ids:-{victim}", m)

    # --- cooldown_s: x2 与 /2 ---
    cooldown = body["cooldown_s"]
    for suffix, mutated in (("x2", cooldown * 2), ("/2", cooldown // 2)):
        m = _clone(body)
        m["cooldown_s"] = mutated
        add(f"cooldown_s:{suffix}", m)

    # --- trigger 与 conditions 里的数值/枚举参数 ---
    def mutate_params(obj: dict[str, Any], path: str) -> None:
        for key, value in obj.items():
            if key in _NUMERIC_FIELDS and isinstance(value, int):
                for suffix, mutated in (
                    ("+1", value + 1), ("-1", value - 1), ("x2", value * 2),
                ):
                    m = _clone(body)
                    _set_by_path(m, path, key, mutated)
                    add(f"{path}.{key}:{suffix}", m)
            elif key in _ENUM_VALUES:
                mutated = _next_enum(key, value)
                m = _clone(body)
                _set_by_path(m, path, key, mutated)
                add(f"{path}.{key}:{value}->{mutated}", m)

    mutate_params(body["trigger"], "trigger")
    for i, cond in enumerate(body.get("conditions", [])):
        mutate_params(cond, f"conditions[{i}]")
    for i, action in enumerate(body.get("actions", [])):
        mutate_params(action, f"actions[{i}]")

    # --- 结构: 删一个 condition / 删一个 action / 复制一个 action ---
    for i in range(len(body.get("conditions", []))):
        m = _clone(body)
        del m["conditions"][i]
        add(f"conditions:-{i}", m)
    for i in range(len(body.get("actions", []))):
        m = _clone(body)
        del m["actions"][i]
        add(f"actions:-{i}", m)  # actions 只剩 0 个时 Schema 拒 (min_length 1), 自动丢弃
    for i in range(len(body.get("actions", []))):
        m = _clone(body)
        m["actions"].append(_clone(body["actions"][i]))
        add(f"actions:dup{i}", m)

    return out


def _set_by_path(body: dict[str, Any], path: str, key: str, value: Any) -> None:
    """path ∈ {trigger, conditions[i], actions[i]} —— 生成器内部的窄用途寻址。"""
    if path == "trigger":
        body["trigger"][key] = value
    elif path.startswith("conditions["):
        body["conditions"][int(path[len("conditions["):-1])][key] = value
    elif path.startswith("actions["):
        body["actions"][int(path[len("actions["):-1])][key] = value
    else:  # pragma: no cover
        raise ValueError(f"未知路径 {path!r}")
