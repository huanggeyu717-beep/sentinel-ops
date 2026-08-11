"""按槽位应答 (数据集 v1.3): 模型问什么, runner 答什么。

住 `evals/runner/tests/` 是因为 `client.py` import httpx (归 api 档, 见本包 __init__)。

背景: v1.2 的 `clarify_answer` 是一段死文本, 每一轮原样再念一遍。而模型每一轮问的
槽位**不一样** —— 从第二轮起它问的东西根本没被回答, 于是一进第二轮就必然耗尽三轮
然后死 (L2 19 条 / C1 17 条, 无一存活)。
"""
from __future__ import annotations

from evals.runner.client import compose_answer

ANSWER = {
    "scope": "整个门店都算",
    "role": "通知运营 (operator)",
    "cooldown": "五分钟内别重复发",
}


def test_compose__answers_only_the_slots_the_model_asked():
    assert compose_answer(ANSWER, ["role"]) == "通知运营 (operator)"


def test_compose__follows_the_order_the_model_asked_not_the_dict_order():
    """答案的排列跟着问题走才像人在答 —— 字典顺序是用例作者的顺序, 不是对话的顺序。"""
    assert compose_answer(ANSWER, ["cooldown", "scope"]) == "五分钟内别重复发; 整个门店都算"


def test_compose__unknown_slot_falls_back_to_a_default_line():
    assert compose_answer(ANSWER, ["severity"]) == "这条你按合理默认来"


def test_compose__capability_gap_has_its_own_fallback():
    """能力缺口不能用"你按合理默认来"打发 —— 那会诱导模型去编一个做不到的东西。"""
    assert compose_answer(ANSWER, ["capability_gap"]) == "这个系统做不到"


def test_compose__deduplicates_text_shared_by_two_slots():
    """同一段话挂在 action 与 severity 下时 (如"开一张普通级的事故单"), 只说一遍。"""
    shared = {"action": "开一张普通级的事故单就行", "severity": "开一张普通级的事故单就行"}
    assert compose_answer(shared, ["action", "severity"]) == "开一张普通级的事故单就行"


def test_compose__mixes_known_and_unknown_slots_in_one_reply():
    got = compose_answer(ANSWER, ["scope", "threshold"])
    assert got == "整个门店都算; 这条你按合理默认来"


def test_compose__without_slots_says_everything_it_knows():
    """降级路径 (槽位还没落库的一瞬)。它退化成 v1.2 的行为, 所以调用方要计数留痕。"""
    assert compose_answer(ANSWER, []) == "整个门店都算; 通知运营 (operator); 五分钟内别重复发"
