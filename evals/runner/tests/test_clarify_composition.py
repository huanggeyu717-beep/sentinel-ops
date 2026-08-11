"""按槽位应答 (数据集 v1.3): 模型问什么, runner 答什么。

住 `evals/runner/tests/` 是因为 `client.py` import httpx (归 api 档, 见本包 __init__)。

背景: v1.2 的 `clarify_answer` 是一段死文本, 每一轮原样再念一遍。而模型每一轮问的
槽位**不一样** —— 从第二轮起它问的东西根本没被回答, 于是一进第二轮就必然耗尽三轮
然后死 (L2 19 条 / C1 17 条, 无一存活)。

文件后半段测 `drive_case` 的**降级路径计数器** `blind_answers`: 组装答案是纯函数,
但"读不到槽位"这件事只在 drive_case 里发生, 所以那一条必须驱动整条循环才测得到。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from evals.runner.client import EvalApiClient, compose_answer, drive_case

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


# ===== 降级路径计数器 blind_answers (drive_case) =====
#
# 这个计数器进了 results.jsonl、也进了横向表第 1b 节的第 3 条证据 ("blind_answers
# 全程为 0, 所以改善不是降级路径凑出来的"), 但在此之前**没有任何断言守着它**。
# 而它坏掉的表现恰恰是**永远为 0** —— 与"从来没降级过"长得一模一样。本项目在这个
# 形状上栽过五次 (defect-log), 所以这里要有一条**会因为它坏掉而翻红**的测试。


def _clarify_once_transport(answers: list[str]) -> httpx.MockTransport:
    """一条会进澄清的用例: 建任务 -> clarifying -> 收到回答 -> awaiting_approval。"""
    state = {"replied": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent-tasks" and request.method == "POST":
            return httpx.Response(200, json={"created": True, "task_id": 7})
        if request.url.path == "/agent-tasks/7/reply":
            answers.append(str(json.loads(request.content)["answer"]))
            state["replied"] = True
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/agent-tasks/7" and request.method == "GET":
            status = "awaiting_approval" if state["replied"] else "clarifying"
            return httpx.Response(200, json={"task": {"status": status}})
        raise AssertionError(f"意料之外的请求: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _drive(read_missing_slots) -> tuple[dict, list[str]]:
    sent: list[str] = []
    client = EvalApiClient("http://eval.test", transport=_clarify_once_transport(sent))
    case = {"id": "ambig-x", "input": "湿了就通知", "expected": {"clarify_answer": ANSWER}}

    async def go():
        try:
            return await drive_case(
                client, case, poll_s=0, max_wall_s=30,
                read_missing_slots=read_missing_slots,
            )
        finally:
            await client.aclose()

    return asyncio.run(go()), sent


def test_drive_case__counts_a_blind_answer_when_slots_are_unreadable():
    """读不到槽位 -> 退回"把知道的都说一遍", 且这次降级**必须留痕**。

    打桩一个恒返回空槽位的 slots_reader (槽位那一列还没落库、或读库这条路断了),
    跑一条会进澄清的用例: 回答照发 (比不答强), 但 blind_answers 要涨到 1。
    变异测试: 删掉 client.py 里 `blind += 1` 那一行, 本条必红。
    """
    async def reads_nothing(task_id: int) -> list[str]:
        return []

    info, sent = _drive(reads_nothing)

    assert info["blind_answers"] == 1
    assert info["answered_rounds"] == 1
    assert info["final_status"] == "awaiting_approval"
    # 发出去的确实是降级那一句 (把知道的都说一遍), 不是别的路径顺手把计数带上来的
    assert "整个门店都算; 通知运营 (operator); 五分钟内别重复发" in sent[0]


def test_drive_case__normal_slot_read_is_not_counted_as_blind():
    """对照组: 槽位读得到时计数器必须保持 0 —— 否则"全程为 0"这个证据一文不值。"""
    async def reads_a_slot(task_id: int) -> list[str]:
        return ["role"]

    info, sent = _drive(reads_a_slot)

    assert info["blind_answers"] == 0
    assert info["answered_rounds"] == 1
    assert "通知运营 (operator)" in sent[0]
    assert "整个门店都算" not in sent[0]  # 只答了问的那一个槽位
