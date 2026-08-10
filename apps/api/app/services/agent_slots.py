"""ask_clarification 的 missing_slots 封闭枚举 (SPEC-007 第三节, 七项)。

为什么单独一个零依赖模块: 这份枚举同时被三处引用 —— 工具 JSON Schema
(agent_prompts, 给模型看)、service 层写库前的取值校验 (agent_service, 不信任
模型输入, CLAUDE.md 不变量 5 的同一条道理)、评测数据集 lint (evals/, 断言用例
里的 missing_slots 全部来自这里)。最后一处跑在不装 apps/api 依赖的单元测试
任务里, 所以它不能住在 import sqlalchemy 的 agent_service 里。

追问判分用**必含**口径: grader 断言模型报的槽位 ⊇ 用例的 must_include_slots。
多问的槽位不算错 (单独报"多问率"作观察值) —— 判据苛刻会把措辞差异当成错误,
指标全是噪音, 这正是原自然语言 must_ask_about 被废掉的原因 (SPEC-007 第三节)。
"""
from __future__ import annotations

# 槽位 -> 含义 (进工具 Schema 的 description, 给模型看)
SLOT_MEANING: dict[str, str] = {
    "scope": "管哪个区 / 哪些探头",
    "role": "通知给哪个角色",
    "cooldown": "多久之内不重复",
    "threshold": "几个探头 / 多久 / 多少",
    "severity": "事故算哪一级",
    "action": "到底要做什么",
    "capability_gap": "这件事本系统表达不了",
}

MISSING_SLOTS: tuple[str, ...] = tuple(SLOT_MEANING)
