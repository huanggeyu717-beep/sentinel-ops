"""agent_runtime 状态机: 打桩脚本驱动的端到端 (service 层, 不走 HTTP)。

覆盖 SPEC-002 验收 1(后半)/4/5/6/7/8/11/17 里 service 层能验的部分。
打桩响应是**一串**: 修复循环要"第一次吐错的 zone、第二次吐对", 澄清要
"连错两次、第三次改口问人" —— 只能吐单条的打桩写不出这些测试。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from test_agent_helpers import clean_agent_tables  # noqa: F401

from app.config import settings
from app.services import agent_runtime, agent_service, policy_service
from app.services.agent_tools import REGISTRY, ToolSpec
from app.services.auth_service import AuthUser
from app.services.llm_client import LLMResponse, LLMToolCall, ScriptedLLMClient

OWNER = 3  # alex (operator)
CHRIS = AuthUser(id=2, email="chris@example.com", display_name="Chris Li",
                 employee_id=3, roles=["manager"])
DANA_LIKE = AuthUser(id=1, email="admin@example.com", display_name="Admin",
                     employee_id=None, roles=["admin"])

VALID_BODY = {
    "scope": {"type": "zone", "ids": [1]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}
# 引用不存在的 zone 99 -> 静态验证器 E_UNKNOWN_ZONE (验收 4 靠它触发修复循环)
WRONG_ZONE_BODY = {**VALID_BODY, "scope": {"type": "zone", "ids": [99]}}


def tool(_tool_name: str, **arguments) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def say(content: str) -> LLMResponse:
    return LLMResponse(text=content, input_tokens=10, output_tokens=5)


async def _open_and_run(factory, script, *, input_text, task_id=None):
    """开任务 (或续跑已有任务) 并跑一轮, 返回 (task_id, outcome, llm)。"""
    if task_id is None:
        async with factory() as session, session.begin():
            created = await agent_service.create_task(
                session, user_id=OWNER, input_text=input_text
            )
        task_id = created["task_id"]
    llm = ScriptedLLMClient(script=list(script))
    outcome = await agent_runtime.run_task(task_id, llm, factory)
    return task_id, outcome, llm


async def _task(factory, task_id):
    async with factory() as session, session.begin():
        return await agent_service.get_task(session, task_id)


async def _scalar(factory, sql, **params):
    async with factory() as session, session.begin():
        return (await session.execute(text(sql), params)).scalar_one()


async def _rows(factory, sql, **params):
    async with factory() as session, session.begin():
        return [
            dict(r)
            for r in (await session.execute(text(sql), params)).mappings().all()
        ]


# ===== 正常路径: 一句人话 -> 草案 -> 校验 -> 回放 -> 等审批 =====


def test_happy_path__one_sentence_to_awaiting_approval(svc):
    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [say("在 1 区变湿时开事故"),
             tool("create_policy", name="生鲜区漏水开单", body=VALID_BODY)],
            input_text="生鲜区湿了就开单",
        )
        assert outcome == "awaiting_approval"
        task = await _task(factory, task_id)
        versions = await _rows(factory, """
            SELECT pv.id, pv.status, pv.source, pv.created_by
            FROM policy_versions pv JOIN policies p ON p.id = pv.policy_id
            WHERE p.name = '生鲜区漏水开单'
        """)
        approval = await _rows(
            factory,
            "SELECT task_id, decision FROM approvals WHERE task_id = :t", t=task_id,
        )
        llm_calls = await _scalar(
            factory, "SELECT count(*) FROM ai_usage WHERE task_id = :t", t=task_id
        )
        async with factory() as session, session.begin():
            timeline = await agent_service.get_timeline(session, task_id)
        return task, versions, approval, llm_calls, timeline

    task, versions, approval, llm_calls, timeline = svc(go)
    assert task["status"] == "awaiting_approval"
    # 草案归属: created_by 是发起人, source 是 agent (验收 3 的 service 层半边)
    assert len(versions) == 1
    assert versions[0]["status"] == "awaiting_approval"
    assert versions[0]["source"] == "agent" and versions[0]["created_by"] == OWNER
    # 审批挂着 task_id, 等人裁决
    assert approval == [{"task_id": task["id"], "decision": None}]
    assert llm_calls == 2  # parsing + compiling, 打桩也落 ai_usage
    seqs = [t["seq"] for t in timeline]
    assert seqs == list(range(1, len(seqs) + 1))


def test_decide_approval__both_decisions_complete_task(svc):
    """批准和否决都把任务推进到 completed —— 批没批通过与 Agent 无关 (第四节)。"""
    async def go(factory):
        results = []
        for i, decision in enumerate(("approved", "rejected")):
            task_id, outcome, _ = await _open_and_run(
                factory,
                [say("ok"), tool("create_policy", name=f"决定-{decision}", body=VALID_BODY)],
                input_text=f"输入 {i}",
            )
            assert outcome == "awaiting_approval"
            approval_id = await _scalar(
                factory, "SELECT id FROM approvals WHERE task_id = :t", t=task_id
            )
            async with factory() as session, session.begin():
                await policy_service.decide_approval(
                    session, approval_id, CHRIS, decision, audit_factory=factory
                )
            task = await _task(factory, task_id)
            version_status = await _scalar(factory, """
                SELECT pv.status FROM policy_versions pv
                JOIN approvals a ON a.policy_version_id = pv.id WHERE a.id = :a
            """, a=approval_id)
            results.append((decision, task["status"], task["completed_at"] is not None,
                            version_status))
        return results

    for decision, task_status, completed, version_status in svc(go):
        assert task_status == "completed", decision
        assert completed, decision
        # 版本自己的命运与任务无关: 批准 -> published, 否决 -> rejected
        assert version_status == ("published" if decision == "approved" else "rejected")


# ===== 修复循环 (验收 4/5): 仅凭错误码修对, 草稿只有一行, 中间态进 Trace =====


def test_repair_loop__wrong_zone_fixed_in_place(svc):
    async def go(factory):
        task_id, outcome, llm = await _open_and_run(
            factory,
            [say("开单"),
             tool("create_policy", name="修复循环", body=WRONG_ZONE_BODY),
             tool("update_policy_draft", body=VALID_BODY)],
            input_text="生鲜区湿了就开单 (修复)",
        )
        assert outcome == "awaiting_approval"
        versions = await _rows(factory, """
            SELECT pv.id FROM policy_versions pv
            JOIN policies p ON p.id = pv.policy_id WHERE p.name = '修复循环'
        """)
        validate_steps = await _rows(factory, """
            SELECT result_summary FROM agent_steps
            WHERE task_id = :t AND tool_name = 'validate_policy' ORDER BY seq
        """, t=task_id)
        update_steps = await _rows(factory, """
            SELECT result_summary FROM agent_steps
            WHERE task_id = :t AND tool_name = 'update_policy_draft' ORDER BY seq
        """, t=task_id)
        repair_request = llm.requests[-1]
        return versions, validate_steps, update_steps, repair_request

    versions, validate_steps, update_steps, repair_request = svc(go)
    # 修复过程中 policy_versions 始终只有一条草稿行 (验收 5)
    assert len(versions) == 1
    # 第一次校验红 (E_UNKNOWN_ZONE), 修完重新校验绿 —— 修完不验等于没修
    assert len(validate_steps) == 2
    first, second = (s["result_summary"] for s in validate_steps)
    assert first["ok"] is False
    assert any(i["code"] == "E_UNKNOWN_ZONE" for i in first["issues"])
    assert second["ok"] is True
    # 修复前的完整 body 原样进了 agent_steps (第六节: 中间态一条不丢)
    assert update_steps[0]["result_summary"]["previous_body"] == WRONG_ZONE_BODY
    # 修复靠的是错误码: repairing 那次请求里带着校验错误, 工具清单只有就地改
    assert "E_UNKNOWN_ZONE" in repair_request.messages[-1]["content"]
    assert {t["name"] for t in repair_request.tools} == {
        "update_policy_draft", "ask_clarification"
    }


# ===== 多轮澄清 (验收 6/7/8) =====


def test_vague_input__asks_instead_of_guessing(svc):
    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [tool("ask_clarification", question="通知谁? 几个探头算都湿了?",
                  missing_slots=["role", "threshold"])],
            input_text="漏水了通知一下",
        )
        task = await _task(factory, task_id)
        question = await _scalar(
            factory,
            "SELECT question FROM agent_clarifications WHERE task_id = :t", t=task_id,
        )
        return outcome, task, question

    outcome, task, question = svc(go)
    assert outcome == "clarifying" and task["status"] == "clarifying"
    assert "通知谁" in question
    assert task["runner_id"] is None  # 没有进程在跑它, 不参与失联清扫


def test_clarify_without_missing_slots__protocol_error_not_silent_default(svc):
    """模型问人却不报 missing_slots (SPEC-007 对 SPEC-002 的修订 1 定为必填):
    归 model_protocol_error 落 failed, 不替模型编一个默认槽位 —— 槽位是判分
    依据, 兜底默认值等于替模型撒谎。"""
    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [tool("ask_clarification", question="通知谁?")],  # 漏了 missing_slots
            input_text="漏水了通知一下下",
        )
        task = await _task(factory, task_id)
        rows = await _scalar(
            factory,
            "SELECT count(*) FROM agent_clarifications WHERE task_id = :t", t=task_id,
        )
        return outcome, task, rows

    outcome, task, rows = svc(go)
    assert outcome == "failed" and task["error_code"] == "model_protocol_error"
    assert rows == 0  # 半截澄清行没有落库 (事务随异常回滚)


def test_repair_exhausted__clarifying_then_same_task_resumes(svc):
    """连错两次、第三次改口问人 (验收 7); 人回答后**同一条任务**从 discovering
    继续, task_id 不变, Trace 接着往下长 (验收 8)。"""
    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [say("开单"),
             tool("create_policy", name="澄清后修好", body=WRONG_ZONE_BODY),
             tool("update_policy_draft", body=WRONG_ZONE_BODY),   # 修 1: 还是错
             tool("update_policy_draft", body=WRONG_ZONE_BODY),   # 修 2: 还是错
             tool("ask_clarification", question="你说的区到底是哪个?",
                  missing_slots=["scope"])],  # 改口问人
            input_text="那个区湿了就开单",
        )
        assert outcome == "clarifying"  # 修满 2 次仍不过 -> 问人, 不是 failed
        trace_before = len(await _rows(
            factory, "SELECT seq FROM agent_steps WHERE task_id = :t", t=task_id
        ))
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(
                session, task_id, OWNER, "生鲜区, 也就是 1 区"
            )
        # 同一条任务续跑: 从 discovering 重捞库存, 有草稿所以只给就地改
        task_id2, outcome2, llm2 = await _open_and_run(
            factory, [tool("update_policy_draft", body=VALID_BODY)],
            input_text="", task_id=task_id,
        )
        versions = await _rows(factory, """
            SELECT pv.id FROM policy_versions pv
            JOIN policies p ON p.id = pv.policy_id WHERE p.name = '澄清后修好'
        """)
        trace_after = len(await _rows(
            factory, "SELECT seq FROM agent_steps WHERE task_id = :t", t=task_id
        ))
        async with factory() as session, session.begin():
            timeline = await agent_service.get_timeline(session, task_id)
        compile_request = llm2.requests[-1]
        return (task_id, task_id2, outcome2, versions, trace_before, trace_after,
                timeline, compile_request)

    (task_id, task_id2, outcome2, versions, before, after, timeline,
     compile_request) = svc(go)
    assert task_id2 == task_id and outcome2 == "awaiting_approval"
    assert len(versions) == 1          # 澄清回来仍是同一版草稿, 没开第二版
    assert after > before              # Trace 接着往下长
    seqs = [t["seq"] for t in timeline]
    assert seqs == list(range(1, len(seqs) + 1))  # 两张表一条编号, 断线续传有序
    # 有草稿的再编译只给就地改 (第六节: 每个任务只新建一版草稿)
    assert {t["name"] for t in compile_request.tools} == {
        "update_policy_draft", "ask_clarification"
    }


def test_clarify_rounds_exhausted__failed_and_draft_discarded(svc, monkeypatch):
    """澄清轮次用尽 -> failed (验收 11 前半); 失败任务的草稿标 discarded 不删。"""
    monkeypatch.setattr(settings(), "agent_max_clarify_rounds", 1)

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [say("开单"),
             tool("create_policy", name="终将失败", body=WRONG_ZONE_BODY),
             tool("update_policy_draft", body=WRONG_ZONE_BODY),
             tool("update_policy_draft", body=WRONG_ZONE_BODY),
             tool("ask_clarification", question="哪个区?", missing_slots=["scope"])],
            input_text="含糊其辞的输入",
        )
        assert outcome == "clarifying"
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(session, task_id, OWNER, "还是含糊")
        _, outcome2, _ = await _open_and_run(
            factory, [tool("ask_clarification", question="还是不懂", missing_slots=["scope"])],
            input_text="", task_id=task_id,
        )
        task = await _task(factory, task_id)
        version_status = await _scalar(factory, """
            SELECT pv.status FROM policy_versions pv
            JOIN policies p ON p.id = pv.policy_id WHERE p.name = '终将失败'
        """)
        return outcome2, task, version_status

    outcome2, task, version_status = svc(go)
    assert outcome2 == "failed" and task["status"] == "failed"
    assert task["error_code"] == "clarify_rounds_exhausted"
    assert task["error_detail"]
    assert version_status == "discarded"  # 不删: W5 要评"它当时写成了什么样"


def test_llm_budget_exhausted__failed(svc, monkeypatch):
    """单任务 LLM 调用总数是跨轮累加的硬上限, 用尽 -> failed。"""
    monkeypatch.setattr(settings(), "agent_max_llm_calls", 1)

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory, [say("这一次调用就是全部预算")], input_text="预算测试",
        )
        return outcome, await _task(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "failed" and task["status"] == "failed"
    assert task["error_code"] == "llm_calls_exhausted"


# ===== 可靠性 (验收 17): 超时退避后成功; 不可重试错误干净失败 =====


def test_tool_timeout__retries_with_backoff_then_succeeds(svc, monkeypatch):
    calls = {"n": 0}
    real_fn = REGISTRY["list_zones"].fn

    async def flaky(ctx, args):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(1)  # 超过 0.05 秒上限, 触发一次超时
        return await real_fn(ctx, args)

    monkeypatch.setitem(REGISTRY, "list_zones",
                        ToolSpec("list_zones", "read", flaky, "flaky"))
    monkeypatch.setattr(settings(), "agent_tool_timeout_seconds", 0.05)
    monkeypatch.setattr(agent_runtime, "_RETRY_BACKOFF_BASE_S", 0.01)

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [say("ok"), tool("create_policy", name="超时重试", body=VALID_BODY)],
            input_text="超时注入",
        )
        retry_count = await _scalar(factory, """
            SELECT retry_count FROM agent_steps
            WHERE task_id = :t AND tool_name = 'list_zones'
        """, t=task_id)
        return outcome, retry_count

    outcome, retry_count = svc(go)
    assert outcome == "awaiting_approval"  # 退避一次后成功, 任务照常走完
    assert retry_count == 1                # 成功那步记着重试过一次


def test_parsing_offside_tool_call__failed_and_nothing_created(svc):
    """parsing 阶段模型越界调工具, 与 compiling/repairing 同一口径报协议错,
    不静默吞掉 —— 且那次越界调用没有被执行, 一条策略都没建出来。"""
    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory,
            [tool("create_policy", name="越界草稿", body=VALID_BODY)],
            input_text="parsing 越界注入",
        )
        policies = await _scalar(
            factory, "SELECT count(*) FROM policies WHERE name = '越界草稿'"
        )
        return outcome, await _task(factory, task_id), policies

    outcome, task, policies = svc(go)
    assert outcome == "failed" and task["status"] == "failed"
    assert task["error_code"] == "model_protocol_error"
    assert policies == 0  # 工具没被执行, 只是被拒


def test_tool_fatal_error__dead_letter_with_detail(svc, monkeypatch):
    async def broken(ctx, args):
        raise RuntimeError("数据库连不上了")

    monkeypatch.setitem(REGISTRY, "list_zones",
                        ToolSpec("list_zones", "read", broken, "broken"))

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory, [say("ok")], input_text="致命错误注入",
        )
        return outcome, await _task(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "dead_letter" and task["status"] == "dead_letter"
    assert task["error_code"] == "tool_error"
    assert "list_zones" in task["error_detail"]  # 人话里说得出是哪个工具坏了
