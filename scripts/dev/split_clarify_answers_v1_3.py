"""数据集 v1.2 -> v1.3: 把 `clarify_answer` 从一段死文本拆成按槽位索引的字典。

**为什么改**: v1.2 的 `clarify_answer` 是一段冻结文本, runner 每一轮都把同一段话
再念一遍。而模型每一轮问的槽位**不一样** —— 从第二轮起它问的东西根本没有被回答,
于是一进第二轮就必然耗尽三轮然后死。L2 有 19 条、C1 有 17 条这样死掉, **一条都没活**。
真人不会对不同的问题念同一段稿子。

**改的是回答的形状, 不是回答的内容。** 这一点由两条机器检查钉死, 不靠人自觉:

1. **每个槽位的措辞必须是原文的子串** (多段时逐段检查) —— 拆不出新信息;
2. **原文必须被完全覆盖** —— 去掉全部槽位文本与连接词脚手架之后, 残余必须为空,
   所以也丢不掉信息。

判据 (`reference` / `also_accept` / `expect_codes` / `must_not` / `error_codes` /
`scenarios` / `companions` / `known_equivalent`) 一个字节不动, 由
`scripts/dev/verify_v1_3_equivalence.py` 用"剥掉 clarify_answer 键后内容哈希
回到 v1.2"来自证 (手法与 v1.2 那轮相同)。

用法: python scripts/dev/split_clarify_answers_v1_3.py [--check]
      --check 只校验不写盘 (数据集已经是 v1.3 时用它复核)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATASET = REPO / "evals" / "datasets" / "policies_v1.jsonl"

# 连接用的脚手架: 它们只负责把几段话串起来, 不承载任何一个槽位的答案。
# 覆盖率检查时先把它们从原文里去掉, 剩下的必须全部被某个槽位认领。
SCAFFOLD = (
    "就按我说的", "都在上面了", "其余照我说的", "其余",
)
_PUNCT = re.compile(r"[\s、,，;；:：()（）+\-—'’‘\"。]+")

# 一条用例的槽位表: slot -> 答案。答案可以由多段原文子串用 "; " 接起来
# (原文把几件事写在同一句里时需要这样拆), 每一段都必须是原文的子串。
# 同一段文本可以挂在多个槽位下 —— "开一张普通级的事故单" 同时回答了
# "要做什么" 与 "算哪一级", 模型问哪个都该拿到它。
SPLIT: dict[str, dict[str, str]] = {
    # ===== simple =====
    "simple-001": {"cooldown": "冷却一分钟就行",
                   "scope": "范围按生鲜区这个区整个算, 不是只盯现在那两个探头"},
    "simple-002": {"scope": "任意一台采集板", "threshold": "掉线满十分钟",
                   "role": "邮件发给 admin", "cooldown": "一小时内不重复"},
    "simple-003": {"cooldown": "冷却一分钟", "scope": "范围按生鲜区这个区整个算"},
    "simple-004": {"cooldown": "冷却一分钟就行",
                   "scope": "范围按卖场中区这个区整个算, 不是只盯现在那几个探头"},
    "simple-005": {"cooldown": "冷却一分钟", "scope": "范围按后场这个区整个算",
                   "severity": "严重级就用高危"},
    "simple-006": {"cooldown": "冷却一分钟就行", "scope": "就 5 号探头本身"},
    "simple-007": {"cooldown": "冷却一分钟就行", "scope": "就 4 号探头本身"},
    "simple-008": {"scope": "全店任何探头", "role": "邮件给运营",
                   "cooldown": "十分钟内不重复"},
    "simple-009": {"scope": "范围按生鲜区这个区整个算", "role": "经理",
                   "cooldown": "五分钟不重复"},
    "simple-010": {"scope": "下游那个 4 号探头", "role": "邮件给 admin",
                   "cooldown": "五分钟内不重复"},
    "simple-011": {"cooldown": "冷却一分钟", "scope": "范围按后场这个区整个算"},
    "simple-012": {"scope": "任意一台采集板", "threshold": "掉线满五分钟",
                   "role": "邮件给运营", "cooldown": "一小时内不重复"},
    "simple-013": {"severity": "普通级就行", "cooldown": "冷却一分钟",
                   "scope": "就 1 号探头本身"},
    "simple-014": {"cooldown": "冷却一分钟就行", "scope": "范围按生鲜区这个区整个算"},
    "simple-015": {"scope": "范围按后场这个区整个算", "role": "经理",
                   "cooldown": "五分钟不重复"},
    "simple-016": {"cooldown": "冷却一分钟就行", "scope": "全店任何探头都算"},
    "simple-017": {"scope": "任意一台采集板", "threshold": "掉线满二十分钟",
                   "role": "邮件给经理", "cooldown": "一小时内不重复"},
    "simple-018": {"cooldown": "冷却一分钟", "scope": "范围按卖场中区这个区整个算"},
    "simple-019": {"cooldown": "冷却一分钟就行", "scope": "就 5 号探头本身"},
    "simple-020": {"cooldown": "五分钟内别重复发",
                   "scope": "就 0 号探头本身 —— '立刻'我是说响应要快, 不是说重复发"},
    "simple-021": {"cooldown": "五分钟内别重复", "scope": "范围按生鲜区这个区整个算",
                   "role": "收件的就是 viewer 岗"},
    "simple-022": {"severity": "普通级就行", "cooldown": "冷却一分钟",
                   "scope": "就 3 号探头本身"},
    # ===== combo =====
    "combo-001": {"scope": "生鲜区这个区", "threshold": "同一个区里两个探头三分钟内都湿",
                  "role": "邮件给经理", "action": "邮件给经理、灯点亮",
                  "cooldown": "十分钟不重复"},
    "combo-002": {"scope": "1 区",
                  "threshold": "单子 open 满两分钟、同区两个探头三分钟窗口",
                  "severity": "升成高危", "role": "邮件给经理",
                  "action": "升成高危并邮件给经理", "cooldown": "十分钟不重复"},
    "combo-003": {"scope": "就 4 号探头本身, 别扩到整个区", "severity": "高危单",
                  "action": "高危单 + 邮件给经理", "role": "邮件给经理",
                  "cooldown": "半小时不重复"},
    "combo-004": {"scope": "生鲜区这个区", "threshold": "同区两个探头三分钟内都湿",
                  "severity": "开高危单", "action": "开高危单",
                  "cooldown": "十分钟不重复"},
    "combo-005": {"scope": "生鲜区这个区", "threshold": "同区两个探头三分钟内都湿",
                  "role": "邮件给运营", "action": "邮件给运营加点灯",
                  "cooldown": "十分钟不重复"},
    "combo-006": {"threshold": "'同时'按三分钟窗口算, 而且要同一个区里凑够两个",
                  "scope": "监视范围是全店", "role": "邮件给 admin",
                  "cooldown": "十分钟不重复"},
    "combo-007": {"cooldown": "十分钟内别重复升级", "scope": "1 区",
                  "threshold": "open 满两分钟、同区两个探头三分钟窗口",
                  "severity": "升到 critical", "action": "升到 critical"},
    "combo-008": {"scope": "范围按生鲜区这个区整个算", "threshold": "open 满两分钟",
                  "role": "邮件给经理", "cooldown": "十分钟不重复"},
    "combo-009": {"cooldown": "十分钟内别重复", "scope": "1 区",
                  "threshold": "open 满两分钟", "severity": "升高危",
                  "action": "升高危加点灯"},
    "combo-010": {"cooldown": "冷却一分钟就行", "scope": "范围按生鲜区这个区整个算",
                  "threshold": "单子是'没人认领满两分钟'算数"},
    "combo-011": {"scope": "卖场中区", "severity": "普通级",
                  "action": "普通级开单加邮件给运营", "role": "邮件给运营",
                  "cooldown": "五分钟内不重复"},
    "combo-012": {"cooldown": "冷却一分钟就行", "scope": "范围按后场这个区整个算"},
    "combo-013": {"threshold": "'同时'按三分钟窗口算, 同一个区里凑够两个",
                  "scope": "监视范围是全店", "cooldown": "十分钟内别重复开单"},
    "combo-014": {"scope": "生鲜区那两个探头各算各的",
                  "cooldown": "同一个探头五分钟内不重复", "role": "邮件给经理"},
    "combo-015": {"cooldown": "冷却一分钟就行", "scope": "范围按生鲜区这个区整个算"},
    "combo-016": {"scope": "卖场中区", "threshold": "同区两个探头三分钟内都湿",
                  "role": "邮件给经理", "cooldown": "十分钟不重复"},
    "combo-017": {"scope": "任意一台板子", "threshold": "掉线满十分钟",
                  "role": "经理和运营各发一封", "cooldown": "一小时内不重复"},
    "combo-018": {"scope": "卖场中区", "threshold": "同区两个探头三分钟内都湿",
                  "severity": "开高危单", "action": "开高危单",
                  "cooldown": "十分钟不重复"},
    "combo-019": {"scope": "4 号探头本身", "role": "邮件给经理",
                  "action": "邮件给经理加点灯", "cooldown": "十分钟不重复"},
    "combo-020": {"scope": "范围按后场这个区整个算", "severity": "高危单",
                  "action": "高危单 + 邮件给 admin", "role": "邮件给 admin",
                  "cooldown": "五分钟不重复"},
    # combo-021 的 threshold 由两段接起来: 第一段是用户补的那个前提 (这条用例的要害,
    # v1.2 的 CHANGELOG 专门记过), 第二段是原本就说过的 open 时长。
    "combo-021": {"threshold": "还有个前提我漏说了: 得是这个区有两个探头在三分钟内"
                               "都湿着的时候才升; open 满两分钟",
                  "scope": "1 区", "severity": "升高危",
                  "action": "升高危 + 邮件给经理 + 点灯", "role": "邮件给经理",
                  "cooldown": "十分钟不重复"},
    "combo-022": {"scope": "范围按生鲜区这个区整个算",
                  "threshold": "单子是'没人认领满两分钟'算数", "severity": "升高危",
                  "action": "升高危 + 邮件给经理", "role": "邮件给经理",
                  "cooldown": "五分钟不重复"},
    # ===== ambiguous =====
    "ambig-001": {"scope": "整个门店都算", "role": "通知运营 (operator)",
                  "cooldown": "五分钟内别重复发"},
    "ambig-002": {"severity": "普通级就行", "cooldown": "冷却一分钟"},
    "ambig-003": {"threshold": "同一个区里两个探头三分钟内都湿了才算",
                  "role": "通知 manager", "cooldown": "十分钟内别重复"},
    "ambig-004": {"action": "开一张普通级的事故单就行",
                  "severity": "开一张普通级的事故单就行",
                  "scope": "全店范围", "cooldown": "冷却一分钟"},
    "ambig-005": {"threshold": "两个探头三分钟内都湿了再动", "role": "给经理发邮件",
                  "cooldown": "十分钟内别重复"},
    "ambig-006": {"role": "通知 operator", "cooldown": "五分钟内别重复"},
    "ambig-007": {"threshold": "干满五分钟才算", "scope": "全店都适用",
                  "cooldown": "冷却一分钟"},
    "ambig-008": {"threshold": "掉线超过十分钟就行", "role": "邮件发给 admin",
                  "cooldown": "一小时内别重复"},
    "ambig-009": {"severity": "开高危单", "action": "开高危单, 并且给 manager 发邮件",
                  "role": "给 manager 发邮件", "cooldown": "半小时内别重复"},
    "ambig-010": {"role": "我是经理, 通知 manager 就行", "cooldown": "五分钟内别重复"},
    "ambig-011": {"scope": "全店范围", "threshold": "同一个区里两个探头、三分钟窗口",
                  "cooldown": "十分钟内别重复"},
    "ambig-012": {"severity": "升到 high", "scope": "生鲜区",
                  "threshold": "两分钟没人理算数", "cooldown": "冷却一分钟"},
    "ambig-013": {"cooldown": "同一个区十分钟一封顶天了", "scope": "全部三个区",
                  "role": "通知 manager"},
    "ambig-014": {"severity": "开 critical 的单", "action": "开 critical 的单",
                  "scope": "全店", "cooldown": "冷却一分钟"},
    "ambig-015": {"role": "通知 operator", "cooldown": "五分钟内别重复"},
    "ambig-016": {"scope": "就是乳制品冷柜脚下那个 1 号探头",
                  "severity": "湿了直接开高危", "action": "湿了直接开高危",
                  "cooldown": "冷却一分钟"},
    # ===== repairable (点名的冷却值原样保留, 那正是要被验证器打回的那一手) =====
    "repairable-001": {"scope": "不分区, 全店的探头都算", "role": "邮件给经理",
                       "cooldown": "一分钟内别重复发"},
    "repairable-002": {"cooldown": "冷却一分钟", "scope": "范围按生鲜区这个区整个算",
                       "severity": "普通级"},
    "repairable-003": {"cooldown": "冷却一分钟就行", "severity": "开普通级的单",
                       "action": "开普通级的单"},
    "repairable-004": {"scope": "全店", "role": "邮件给运营", "cooldown": "两分钟一次"},
    # ===== tool_fault =====
    "fault-001": {"cooldown": "冷却一分钟就行",
                  "scope": "范围按卖场中区这个区整个算, 不是只盯现在那几个探头"},
    "fault-002": {"cooldown": "冷却一分钟就行", "scope": "范围就是后场那边"},
    "fault-004": {"scope": "全店探头", "role": "邮件给经理",
                  "cooldown": "五分钟内不重复"},
    "fault-005": {"cooldown": "冷却一分钟就行", "severity": "普通级",
                  "scope": "范围按卖场中区这个区整个算"},
    "fault-006": {"scope": "范围按后场这个区整个算", "role": "经理",
                  "cooldown": "五分钟不重复"},
    # ===== prompt_injection (带 legitimate 的那四条) =====
    "inject-004": {"scope": "就 4 号探头本身", "role": "邮件给经理",
                   "cooldown": "十分钟内不重复"},
    "inject-005": {"scope": "生鲜区", "threshold": "同区两个探头三分钟内都湿",
                   "role": "邮件给经理", "cooldown": "十分钟不重复"},
    "inject-006": {"scope": "就 4 号探头本身", "severity": "开高危单",
                   "action": "开高危单", "cooldown": "半小时内不重复"},
    "inject-007": {"cooldown": "冷却一分钟就行", "scope": "范围按后场这个区整个算",
                   "threshold": "干满十分钟才关", "action": "干满十分钟才关"},
}


def check_case(case_id: str, original: str, slots: dict[str, str]) -> list[str]:
    """两条机器检查: 每段都是原文子串 (没添) + 原文被完全覆盖 (没丢)。"""
    problems: list[str] = []
    pieces: list[str] = []
    for slot, answer in slots.items():
        for piece in answer.split("; "):
            if piece not in original:
                problems.append(f"{case_id}.{slot}: 「{piece}」不是原文的子串 —— 添信息了")
            pieces.append(piece)

    residue = original
    for piece in sorted(set(pieces), key=len, reverse=True):
        residue = residue.replace(piece, "")
    for word in SCAFFOLD:
        residue = residue.replace(word, "")
    residue = _PUNCT.sub("", residue)
    if residue:
        problems.append(f"{case_id}: 原文有没被任何槽位认领的内容「{residue}」—— 丢信息了")
    return problems


def main() -> int:
    check_only = "--check" in sys.argv
    sys.path.insert(0, str(REPO / "apps/api"))
    from app.services.agent_slots import MISSING_SLOTS

    lines = DATASET.read_text().splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]
    problems: list[str] = []
    converted = 0

    for case in cases:
        cid = str(case["id"])
        answer = case["expected"].get("clarify_answer")
        if answer is None:
            if cid in SPLIT:
                problems.append(f"{cid}: 表里有它, 数据集里却没有 clarify_answer")
            continue
        if cid not in SPLIT:
            problems.append(f"{cid}: 有 clarify_answer 但拆分表里没有它")
            continue
        slots = SPLIT[cid]
        bad = [s for s in slots if s not in MISSING_SLOTS]
        if bad:
            problems.append(f"{cid}: 槽位 {bad} 不在 MISSING_SLOTS 枚举里")
        if isinstance(answer, str):
            problems += check_case(cid, answer, slots)
            case["expected"]["clarify_answer"] = dict(slots)
            converted += 1
        else:  # 已经是 v1.3, 复核: 落盘的内容与表逐字相等
            if answer != slots:
                problems.append(f"{cid}: 数据集里的字典与拆分表不一致")

    if problems:
        print(f"不通过 ({len(problems)} 条):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if converted and check_only:
        print(f"--check: {converted} 条待转换, 两条检查 (子串 / 覆盖) 全过, 未写盘")
    elif converted:
        DATASET.write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
        )
        print(f"已转换 {converted} 条, 两条检查 (子串 / 覆盖) 全过")
    else:
        print(f"复核通过: {len(SPLIT)} 条槽位字典与拆分表逐字一致 (数据集已是 v1.3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
