"""报告任务的状态机: 打桩脚本驱动的端到端 (SPEC-008 第四节 + 第十节验收里
service 层能验的部分 + 第十一节变异 3 的守卫)。

collecting -> drafting -> validating -(有错)-> repairing -> validating,
通过即 awaiting_review; 修满 2 次不过 -> failed + 报告 discarded。
打桩不花钱, 全流程零真实模型调用。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from test_agent_helpers import clean_agent_tables, db  # noqa: F401
from test_report_task_service import GOOD_BODY, insert_incident

from app.services import agent_runtime, agent_service, report_task_service
from app.services.agent_runtime import AblationProfile
from app.services.llm_client import LLMResponse, LLMToolCall, ScriptedLLMClient

T0 = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
ALEX = 3

# 裸写数字的坏草稿: "3" 命中阿拉伯数字 0 容忍, 恰好一个违规项
BAD_BODY = {**GOOD_BODY, "impact": "全程耗时 3 分钟。"}


def tool(_tool_name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def make_incident(**cols) -> int:
    async def go(conn):
        incident_id = await insert_incident(
            conn,
            assigned_employee_id=1, assigned_at=T0 + timedelta(minutes=3),
            acknowledged_by_employee_id=2,
            acknowledged_at=T0 + timedelta(minutes=12),
            **cols,
        )
        for kind, at in (("opened", T0), ("resolved", T0 + timedelta(hours=1))):
            await conn.execute(
                "INSERT INTO incident_events (incident_id, kind, actor, at) "
                "VALUES ($1, $2, 'system', $3)", incident_id, kind, at,
            )
        return incident_id

    return db(go)


async def _open_and_run(factory, incident_id: int, script, profile=None):
    async with factory() as session, session.begin():
        created = await report_task_service.create_report_task(
            session, user_id=ALEX, incident_id=incident_id
        )
    task_id = created["task_id"]
    llm = ScriptedLLMClient(script=list(script))
    outcome = await agent_runtime.run_task(task_id, llm, factory, profile=profile)
    return task_id, outcome, llm


async def _row(factory, sql: str, **params):
    async with factory() as session, session.begin():
        row = (await session.execute(text(sql), params)).mappings().one_or_none()
        return dict(row) if row is not None else None


async def _report_of_task(factory, task_id: int):
    return await _row(
        factory,
        "SELECT id, status, body, bare_fact_attempts, dangling_ref_attempts, "
        "fact_pack, created_at, updated_at "
        "FROM incident_reports WHERE task_id = :t", t=task_id,
    )


# ===== 正常路径 =====


def test_report_happy_path__facts_draft_validate_to_awaiting_review(client, svc):
    incident_id = make_incident()

    async def go(factory):
        task_id, outcome, llm = await _open_and_run(
            factory, incident_id, [tool("create_report_draft", body=GOOD_BODY)]
        )
        task = await _row(
            factory,
            "SELECT status, stage, completed_at FROM agent_tasks WHERE id = :t",
            t=task_id,
        )
        report = await _report_of_task(factory, task_id)
        async with factory() as session, session.begin():
            timeline = await agent_service.get_timeline(session, task_id)
        return outcome, task, report, timeline, llm

    outcome, task, report, timeline, llm = svc(go)
    assert outcome == "awaiting_review"
    assert task["status"] == "awaiting_review"
    # 等人过目不是终态 (雷区 2): completed_at 不许被盖
    assert task["completed_at"] is None
    assert report["status"] == "draft"
    assert report["body"] == GOOD_BODY
    assert report["bare_fact_attempts"] == 0
    assert report["dangling_ref_attempts"] == 0
    assert len(report["fact_pack"]) >= 17  # 静态清单产全 + 时间线
    # Studio 时间线依次看得见 collecting 的取事实、阶段推进与校验 (验收 1)
    labels = [item["label"] for item in timeline]
    for expected in ("get_incident_facts", "create_report_draft", "validate_report"):
        assert expected in labels, labels
    transitions = [
        item["arguments"]["to"] for item in timeline
        if item["label"] == "stage_transition"
    ]
    assert transitions == ["drafting", "validating", "awaiting_review"]
    # 只发了一次模型调用, 工具清单只有 create_report_draft (没有 ask_clarification)
    assert len(llm.requests) == 1
    assert [t["name"] for t in llm.requests[0].tools] == ["create_report_draft"]
    # 事实包进了 user 消息 (id/label/text 三列)
    assert "ack_by" in llm.requests[0].messages[1]["content"]


def test_report_repair_loop__violation_fed_back_then_fixed(client, svc):
    """修复回路: 校验打回 -> repairing 的 prompt 里必须有错误码 + 违规片段 +
    上一版草稿 (不给草稿等于让模型盲改) -> 修好 -> awaiting_review。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, outcome, llm = await _open_and_run(
            factory, incident_id,
            [tool("create_report_draft", body=BAD_BODY),
             tool("update_report_draft", body=GOOD_BODY)],
        )
        report = await _report_of_task(factory, task_id)
        return task_id, outcome, llm, report

    _task_id, outcome, llm, report = svc(go)
    assert outcome == "awaiting_review"
    assert report["status"] == "draft"
    assert report["body"] == GOOD_BODY
    assert report["bare_fact_attempts"] == 1  # 那一处 "3", 按违规项计
    # 修复调用的输入: 错误码、违规的那个片段、当前草稿, 一样不能少
    repair_user_msg = llm.requests[1].messages[1]["content"]
    assert "E_BARE_FACT" in repair_user_msg
    assert '"3"' in repair_user_msg
    assert "当前草稿" in repair_user_msg
    assert "3 分钟" in repair_user_msg  # 上一版草稿原文在场
    assert [t["name"] for t in llm.requests[1].tools] == ["update_report_draft"]
    # 就地改草稿盖了 updated_at (雷区 9)
    assert report["updated_at"] > report["created_at"]


def test_report_repairs_exhausted__failed_and_report_discarded(client, svc):
    """修满 2 次仍不过 -> failed (不是 clarifying, 现场没人可问), 报告 discarded
    —— failed 专指模型没写对 (SPEC-008 第四节)。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory, incident_id,
            [tool("create_report_draft", body=BAD_BODY),
             tool("update_report_draft", body=BAD_BODY),
             tool("update_report_draft", body=BAD_BODY)],
        )
        task = await _row(
            factory,
            "SELECT status, error_code FROM agent_tasks WHERE id = :t", t=task_id,
        )
        report = await _report_of_task(factory, task_id)
        return outcome, task, report

    outcome, task, report = svc(go)
    assert outcome == "failed"
    assert task["status"] == "failed"
    assert task["error_code"] == "report_validation_failed"
    assert report["status"] == "discarded"
    assert report["bare_fact_attempts"] == 3  # 三轮校验各拦一次


def test_report_wrong_tool__model_protocol_error(client, svc):
    """drafting 阶段只接受 create_report_draft: 模型调策略工具就是协议错,
    落 failed —— 与策略任务同一套失败出口分类 (雷区 7)。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory, incident_id,
            [tool("create_policy", name="错栈了", body={})],
        )
        task = await _row(
            factory, "SELECT status, error_code FROM agent_tasks WHERE id = :t",
            t=task_id,
        )
        return outcome, task

    outcome, task = svc(go)
    assert outcome == "failed"
    assert task["error_code"] == "model_protocol_error"


def test_report_tools_immune_to_ablation__a0_profile_same_flow(client, svc):
    """雷区 8: 消融能力档是策略编译的自变量, 报告任务在 A0 档下照常跑完,
    工具清单一字不差。"""
    incident_id = make_incident()

    async def go(factory):
        _task_id, outcome, llm = await _open_and_run(
            factory, incident_id,
            [tool("create_report_draft", body=GOOD_BODY)],
            profile=AblationProfile.from_level("A0"),
        )
        return outcome, llm

    outcome, llm = svc(go)
    assert outcome == "awaiting_review"
    assert [t["name"] for t in llm.requests[0].tools] == ["create_report_draft"]


# ===== 快照生效 (SPEC-008 第十一节变异 3, 本段唯一归属的那条) =====


def test_report_snapshot__rendered_body_unchanged_after_new_timeline_event(client, svc):
    """报告定稿口径: fact_pack 存快照不重算。生成之后往时间线**前面**补一条
    事件 (tl_1 若重算会变成新事件), 渲染后的正文必须一个字不变。"""
    incident_id = make_incident()
    body = {**GOOD_BODY, "notable": "首条记录为 {{tl_1}}。"}

    async def go(factory):
        task_id, outcome, _ = await _open_and_run(
            factory, incident_id, [tool("create_report_draft", body=body)]
        )
        assert outcome == "awaiting_review"
        report_row = await _report_of_task(factory, task_id)
        async with factory() as session:
            before = await report_task_service.get_report(session, report_row["id"])
        # 往时间线最前面补一条: 重算的话 tl_1 就不再是 opened
        await _insert_early_event(factory, incident_id)
        async with factory() as session:
            after = await report_task_service.get_report(session, report_row["id"])
        return before, after

    before, after = svc(go)
    assert before["rendered"] is not None
    assert "开单" in before["rendered"]["notable"]  # tl_1 = opened 事件
    assert after["rendered"] == before["rendered"]
    assert after["fact_pack"] == before["fact_pack"]


async def _insert_early_event(factory, incident_id: int) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO incident_events (incident_id, kind, actor, at) "
                 "VALUES (:id, 'escalated', 'system', :at)"),
            {"id": incident_id, "at": T0 - timedelta(minutes=30)},
        )


# ===== 清扫路径: 判死的报告任务草稿标弃 =====


def test_report_sweep__stale_task_dead_letter_discards_draft(client, svc):
    """失联判死走清扫 (不经任何轮次收尾): 报告草稿必须一并标 discarded ——
    这条路径与 _fail 是两条, 只守一条另一条会漏。"""
    incident_id = make_incident()

    async def go(factory):
        async with factory() as session, session.begin():
            created = await report_task_service.create_report_task(
                session, user_id=ALEX, incident_id=incident_id
            )
            task_id = created["task_id"]
            fact_pack = await report_task_service.load_fact_pack(session, incident_id)
            await report_task_service.create_report_draft(
                session, task_id=task_id, incident_id=incident_id,
                body=GOOD_BODY, fact_pack=fact_pack, created_by=ALEX,
            )
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE agent_tasks SET heartbeat_at = now() - interval '1 hour' "
                     "WHERE id = :t"), {"t": task_id},
            )
        async with factory() as session, session.begin():
            reaped = await agent_runtime.sweep_once(session)
        assert any(r["task_id"] == task_id for r in reaped)
        return await _report_of_task(factory, task_id)

    report = svc(go)
    assert report["status"] == "discarded"
