"""数据集 lint (SPEC-007 第二节 + 验收 6/7/8/9): 让走散当场变成 CI 红灯。

断言用例里出现的东西全部来自**代码里的真实枚举**:
- 错误码/警告码: 从冻结的 policy_engine 源码 AST 抽取 (那个包没有导出枚举对象,
  评测不许往里塞东西 —— 读源码字符串常量是不动它的前提下唯一的单一事实源);
- 动作/触发器 type 白名单: dsl 的 whitelist 函数;
- missing_slots: apps/api 的 agent_slots (零依赖模块, 单元测试任务里也 import 得动);
- 场景名: scenarios/ 与 evals/scenarios/ 的实际文件 (resolve_scenario);
- 角色/zone/sensor id: 答案键全部过静态验证器, 库存取 evals/fixtures/inventory.json。

"把约定变成 CI 能拦的规则"在本项目的第七处。变异 M4 的靶子是错误码那条断言。
本文件不跑 evaluate() —— 行为类检查在 test_dataset_6b (空对空) 与
test_mutant_admission (判别性) 里。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from evals.graders.reference_runner import resolve_scenario
from policy_engine import Inventory, Policy, validate

REPO = Path(__file__).resolve().parents[2]
DATASET = REPO / "evals/datasets/policies_v1.jsonl"
DATASET_README = REPO / "evals/datasets/README.md"

sys.path.insert(0, str(REPO / "apps/api"))
from app.services.agent_slots import MISSING_SLOTS  # noqa: E402

# ===== 事实源 =====

SOURCES = {"store_layout", "readings_csv", "scenario_pack",
           "w4_model_outputs", "legacy_lambda"}
KINDS_BY_CATEGORY = {
    "simple": {"behavior_equiv"},
    "combo": {"behavior_equiv"},
    "ambiguous": {"clarify"},
    "illegal": {"reject"},
    "repairable": {"repairable"},
    "capability_gap": {"capability_gap"},
    "tool_fault": {"behavior_equiv", "dead_letter"},
    "prompt_injection": {"injection_resisted"},
}
CATEGORY_QUOTA = {"simple": 22, "combo": 22, "ambiguous": 16, "illegal": 10,
                  "repairable": 4, "capability_gap": 8, "tool_fault": 8,
                  "prompt_injection": 10}
CORE_QUOTA = {"simple": 7, "combo": 6, "ambiguous": 5, "illegal": 4,
              "repairable": 2, "capability_gap": 3, "tool_fault": 3,
              "prompt_injection": 10}
REPAIRABLE_CODES = {"E_COOLDOWN_TOO_SHORT", "E_ALWAYS_TRUE_CONDITION",
                    "E_SCOPE_IDS_MISMATCH"}
# illegal 必须覆盖的意图类错误码。E_ROLE_NOT_STAFFED 构造不出: 当前种子下四个
# 角色都有账号, 而 target_role 是这四个值的 Literal —— 该码在评测集里不可达,
# 已在完成报告里单独上报 (它由 SPEC-001 验收 2 的单元测试覆盖)。
ILLEGAL_REQUIRED_CODES = {"E_UNKNOWN_ZONE", "E_UNKNOWN_SENSOR",
                          "E_CONTEXT_UNAVAILABLE", "E_SELF_TRIGGER_LOOP"}


def _string_constants(path: Path, pattern: str) -> frozenset[str]:
    """从冻结包的源码 AST 里抽取匹配的字符串常量 (只读, 不动那个包)。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and re.fullmatch(pattern, node.value)):
            found.add(node.value)
    return frozenset(found)


ERROR_CODES = _string_constants(
    REPO / "packages/policy_engine/policy_engine/validator.py", r"E_[A-Z_]+"
)
WARNING_CODES = _string_constants(
    REPO / "packages/policy_engine/policy_engine/replay.py", r"W_[A-Z_]+"
)


def _cases() -> list[dict]:
    return [json.loads(x) for x in DATASET.read_text().splitlines() if x.strip()]


def _inventory() -> Inventory:
    data = json.loads((REPO / "evals/fixtures/inventory.json").read_text())
    return Inventory(
        zone_ids=frozenset(z["id"] for z in data["zones"]),
        sensor_ids=frozenset(s["id"] for s in data["sensors"]),
        sensor_zone={s["id"]: s["zone_id"] for s in data["sensors"]},
        roles_present=frozenset(data["roles_present"]),
    )


# ===== 结构与配比 (验收 6) =====


def test_dataset__ids_unique_and_quotas_exact():
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "id 重复"
    assert Counter(c["category"] for c in cases) == Counter(CATEGORY_QUOTA)
    core = Counter(c["category"] for c in cases if c["core"])
    assert core == Counter(CORE_QUOTA), f"core 配比不对: {dict(core)}"
    assert sum(core.values()) == 40


def test_dataset__kind_matches_category_and_fault_split():
    cases = _cases()
    for c in cases:
        assert c["expected"]["kind"] in KINDS_BY_CATEGORY[c["category"]], c["id"]
    faults = [c for c in cases if c["category"] == "tool_fault"]
    kinds = Counter(c["expected"]["kind"] for c in faults)
    assert kinds == Counter({"behavior_equiv": 5, "dead_letter": 3})
    for c in faults:
        assert c["inject"]["fault"] in ("timeout_once", "unretryable"), c["id"]
        expected_fault = ("timeout_once" if c["expected"]["kind"] == "behavior_equiv"
                         else "unretryable")
        assert c["inject"]["fault"] == expected_fault, c["id"]


def test_injection__at_least_four_with_legitimate_and_valid_must_not():
    injections = [c for c in _cases() if c["category"] == "prompt_injection"]
    with_legit = [c for c in injections if "legitimate" in c["expected"]]
    assert len(with_legit) >= 4, "拒绝一切的系统不该能拿 100% (SPEC-007 第二节)"
    for c in injections:
        must_not = c["expected"]["must_not"]
        assert must_not, c["id"]
        for entry in must_not:
            assert entry["kind"] in ("tool", "action", "scope"), c["id"]
            if entry["kind"] == "tool":
                assert entry.get("name"), c["id"]
            else:
                assert entry.get("match"), c["id"]


# ===== why (验收 7 + 每类来源 ≥10) =====


def test_why__source_enum_note_nonempty_and_spread():
    cases = _cases()
    counts: Counter[str] = Counter()
    for c in cases:
        assert c["why"]["source"] in SOURCES, c["id"]
        assert c["why"]["note"].strip(), c["id"]
        counts[c["why"]["source"]] += 1
    for source in SOURCES:
        assert counts[source] >= 10, (
            f"来源 {source} 只有 {counts[source]} 条 (<10) —— 60 条挤在同一来源, "
            f"这个字段就成了走过场"
        )


# ===== 枚举走散 (验收 8; M4 的靶子是错误码那条) =====


def test_error_codes__all_from_validator_source():
    assert len(ERROR_CODES) == 9, f"validator 源码里抽到 {sorted(ERROR_CODES)}"
    for c in _cases():
        for code in c["expected"].get("error_codes", []):
            assert code in ERROR_CODES, f"{c['id']}: 未知错误码 {code}"
        for code in c["expected"].get("expect_codes", []):
            assert code in ERROR_CODES, f"{c['id']}: 未知错误码 {code}"


def test_error_codes__category_coverage():
    cases = _cases()
    illegal_codes = {code for c in cases if c["category"] == "illegal"
                     for code in c["expected"]["error_codes"]}
    assert illegal_codes >= ILLEGAL_REQUIRED_CODES
    repairable_codes = {code for c in cases if c["category"] == "repairable"
                        for code in c["expected"]["expect_codes"]}
    assert repairable_codes == REPAIRABLE_CODES  # 三个手滑码各至少一条且不越界


def test_warning_codes__enum_exists():
    # 数据集 v1 的字段里没有引用警告码的地方; 这条断言钉住事实源本身,
    # 未来有字段引用警告码时把取值校验挂到这里
    assert {"W_HIGH_TRIGGER_RATE", "W_NEVER_TRIGGERED",
                             "W_SINGLE_SUBJECT", "W_ACTIONS_SKIPPED"} == WARNING_CODES


def test_clarify_answer__present_on_every_policy_producing_case():
    """会产出策略的用例一条不落地有非空 `clarify_answer` (数据集 v1.2)。

    runner 是"有这个字段才自动回答追问" (evals/runner/client.drive_case)。缺了它,
    模型一追问就没人应答, 挂在 clarifying 直到被判 no_draft_submitted —— v1.1 下
    L2 有 18 条 (18%) 这么死的, repairable 4/4 全灭。**模型在信息不全的正例上追问
    是正确行为**, 判它失败的是评测设施。这条断言让同一个洞不会再开一次。
    """
    missing = []
    for c in _cases():
        expected = c["expected"]
        produces_policy = expected["kind"] in ("behavior_equiv", "repairable") or (
            expected["kind"] == "injection_resisted" and "legitimate" in expected
        )
        if not produces_policy and expected["kind"] != "clarify":
            continue
        answer = expected.get("clarify_answer")
        if not isinstance(answer, dict) or not any(
            str(v).strip() for v in answer.values()
        ):
            missing.append(c["id"])
    assert not missing, (
        f"这些用例会产出策略却没有冻结回答, 模型一追问就会被判'没产出草案': {missing}"
    )


def test_clarify_answer__is_a_slot_dict_with_known_slots():
    """`clarify_answer` 必须是**按槽位索引的字典**, 键在 MISSING_SLOTS 枚举里 (v1.3)。

    v1.2 它是一段死文本, runner 每一轮把同一段话再念一遍 —— 而模型每一轮问的槽位
    不一样, 从第二轮起它问的东西根本没被回答。后果是**一进第二轮就必然耗尽三轮
    然后死**: L2 19 条 / C1 17 条, 无一存活, 且集中打击 repairable 与 ambiguous
    这两类 —— 追问正是 A1→A2 最大的能力增量, 而它从没被公平测过。

    这条断言钉住形状。**退回字符串不会让任何用例判错**, 只会让分数悄悄变差,
    所以必须有东西专门看着它 (与 v1.2 那条同一个理由)。
    """
    bad_shape, bad_slots, empty = [], [], []
    for c in _cases():
        answer = c["expected"].get("clarify_answer")
        if answer is None:
            continue
        if not isinstance(answer, dict):
            bad_shape.append(f"{c['id']}({type(answer).__name__})")
            continue
        for slot, text in answer.items():
            if slot not in MISSING_SLOTS:
                bad_slots.append(f"{c['id']}.{slot}")
            if not str(text).strip():
                empty.append(f"{c['id']}.{slot}")
    assert not bad_shape, f"clarify_answer 不是槽位字典 (v1.2 的死文本形态): {bad_shape}"
    assert not bad_slots, f"槽位不在 MISSING_SLOTS 枚举里: {bad_slots}"
    assert not empty, f"槽位答案是空的: {empty}"


def test_missing_slots__from_agent_slots_enum():
    for c in _cases():
        for slot in c["expected"].get("must_include_slots", []):
            assert slot in MISSING_SLOTS, f"{c['id']}: 未知槽位 {slot}"


def test_scenarios__resolvable_and_type_whitelists():
    for c in _cases():
        for name in c["scenarios"]:
            resolve_scenario(name)  # 未知场景名当场炸
        if c["expected"]["kind"] == "capability_gap":
            assert c["expected"]["capability"].strip(), c["id"]


# ===== 答案键 (验收 9) =====


def test_answer_keys__pass_schema_and_static_validator():
    inv = _inventory()
    checked = 0
    for c in _cases():
        expected = c["expected"]
        bodies = []
        if "reference" in expected:
            bodies.append(("reference", expected["reference"]))
        if "legitimate" in expected:
            bodies.append(("legitimate", expected["legitimate"]))
        bodies += [(f"also_accept[{i}]", alt["policy"])
                   for i, alt in enumerate(expected.get("also_accept", []))]
        bodies += [(f"companions[{i}]", b)
                   for i, b in enumerate(c.get("companions", []))]
        for label, body in bodies:
            result = validate(Policy.model_validate(body), inv)
            assert result.ok, f"{c['id']} {label}: {[i.code for i in result.issues]}"
            checked += 1
    assert checked >= 97


def test_answer_keys__rationale_covers_scope_and_cooldown():
    for c in _cases():
        expected = c["expected"]
        if "reference" in expected or "legitimate" in expected:
            rationale = expected["rationale"]
            assert rationale["scope"].strip(), c["id"]
            assert rationale["cooldown_s"].strip(), c["id"]
        for alt in expected.get("also_accept", []):
            assert alt["reason"].strip(), c["id"]
        for ke in c.get("known_equivalent", []):
            assert ke["reason"].strip(), c["id"]  # 没理由的例外 lint 红 (验收 11)


# ===== 冻结: 版本与内容哈希 =====


def dataset_content_hash() -> str:
    return hashlib.sha256(DATASET.read_bytes()).hexdigest()[:16]


def test_dataset__readme_records_current_hash():
    """README 里记录的 dataset_version 与内容哈希必须与文件现状一致 ——
    改了用例不换哈希 (或不写 CHANGELOG) 当场红, "看过跑分再改"的规矩才有牙。
    v1.1 起版本号出现了第二个消费方 (runner 的 manifest), 一并钉住:
    README 的版本串与 evals.runner.archive.DATASET_VERSION 必须相等,
    否则归档快照里的 dataset_version 会与数据集文档静悄悄走散。"""
    from evals.runner.archive import DATASET_VERSION

    text = DATASET_README.read_text()
    match = re.search(r"`(v1(?:\.\d+)?)` \(sha256:([0-9a-f]{16})\)", text)
    assert match, "README 未记录 dataset_version 哈希"
    assert match.group(1) == DATASET_VERSION, (
        f"README 的版本 {match.group(1)} 与 runner 归档的 DATASET_VERSION"
        f" {DATASET_VERSION} 不一致"
    )
    assert match.group(2) == dataset_content_hash(), (
        "README 记录的哈希与 policies_v1.jsonl 现状不一致 —— 改用例要同步"
        "改哈希并在 CHANGELOG.md 写明改了哪条、为什么"
    )
