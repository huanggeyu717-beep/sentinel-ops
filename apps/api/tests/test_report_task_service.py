"""report_task_service: 建任务口径 (stage/去重/hash)、草稿写路径、定稿与弃稿
(SPEC-008 第四、五、七、八节 + 第二段雷区 1/5/6/9 与变异 B2/B3/B4 的守卫)。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_agent_helpers import clean_agent_tables, db  # noqa: F401

from app.services import agent_service, budget_service, report_task_service
from app.services.incident_service import IncidentNotFound
from app.services.report_task_service import (
    IncidentNotResolved,
    ReportNotFound,
    ReportStateConflict,
)

T0 = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
ALEX = 3   # operator, 发起人
CHRIS = 2  # manager, 第二个用户 (跨用户去重用)

GOOD_BODY = {
    "summary": "事故 {{incident_id}} 于 {{opened_at}} 开单, 已解决。",
    "handling": "派给了 {{assigned_to}}, 实际到场的是 {{ack_by}}。",
    "impact": "全程耗时 {{handle_duration}}。",
    "notable": "",
    "suggestion": "",
}


async def insert_incident(conn, **cols) -> int:
    fields = {
        "zone_id": 1, "sensor_id": 1, "severity": "normal", "status": "resolved",
        "opened_at": T0, "resolved_at": T0 + timedelta(hours=1),
        "resolved_by": "user:2",
        **cols,
    }
    names = ", ".join(fields)
    placeholders = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    incident_id: int = await conn.fetchval(
        f"INSERT INTO incidents ({names}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return incident_id


def make_incident(**cols) -> int:
    return db(lambda conn: insert_incident(conn, **cols))


async def _create_task(factory, incident_id: int, user_id: int = ALEX):
    async with factory() as session, session.begin():
        return await report_task_service.create_report_task(
            session, user_id=user_id, incident_id=incident_id
        )


async def _fetch_one(factory, sql: str, **params):
    from sqlalchemy import text

    async with factory() as session, session.begin():
        row = (await session.execute(text(sql), params)).mappings().one_or_none()
        return dict(row) if row is not None else None


async def _make_draft(factory, incident_id: int, body=None):
    """建任务 + 直接经 service 落一版草稿 (不跑状态机, 状态机在 runtime 档测)。"""
    created = await _create_task(factory, incident_id)
    task_id = created["task_id"]
    async with factory() as session, session.begin():
        fact_pack = await report_task_service.load_fact_pack(session, incident_id)
        report = await report_task_service.create_report_draft(
            session, task_id=task_id, incident_id=incident_id,
            body=body or GOOD_BODY, fact_pack=fact_pack, created_by=ALEX,
        )
    return task_id, report["report_id"]


async def _set_task_status(factory, task_id: int, status: str) -> None:
    from sqlalchemy import text

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE agent_tasks SET status = :s, stage = :s WHERE id = :id"),
            {"s": status, "id": task_id},
        )


# ===== input_hash 的稳定口径 (雷区 5) =====


def test_report_input_text__pinned_format():
    """这个格式决定 agent_tasks_one_open 认不认得出重复 —— 改它等于让同一条
    事故的新旧任务互相认不出, 所以钉死。"""
    assert report_task_service.report_input_text(42) == "incident_report:42"
    _, h1 = agent_service.normalize_input(
        report_task_service.report_input_text(42), None
    )
    _, h2 = agent_service.normalize_input(
        report_task_service.report_input_text(42), None
    )
    assert h1 == h2


# ===== 建任务 =====


def test_create_report_task__stage_explicitly_collecting(client, svc):
    """变异 B2 的守卫 (雷区 1): agent_tasks.stage 的默认值是 'parsing',
    不显式写 collecting, 报告任务会一头栽进策略状态机。"""
    incident_id = make_incident()

    async def go(factory):
        created = await _create_task(factory, incident_id)
        assert created["created"] is True
        return await _fetch_one(
            factory,
            "SELECT stage, status, task_type FROM agent_tasks WHERE id = :id",
            id=created["task_id"],
        )

    task = svc(go)
    assert task["stage"] == "collecting"
    assert task["status"] == "running"
    assert task["task_type"] == "incident_report"


def test_create_report_task__unresolved_422_and_unknown_404(client, svc):
    open_incident = make_incident(status="open", resolved_at=None, resolved_by=None)

    async def go(factory):
        with pytest.raises(IncidentNotResolved) as e:
            await _create_task(factory, open_incident)
        assert e.value.status == "open"
        with pytest.raises(IncidentNotFound):
            await _create_task(factory, 999_999)

    svc(go)


def test_create_report_task__dedupe_across_users(client, svc):
    """变异 B3 的守卫 (雷区 6): agent_tasks_one_open 只挡同一个用户 ——
    去重必须是 service 自己那一次查询, 用**两个不同的用户**才测得出来。"""
    incident_id = make_incident()

    async def go(factory):
        first = await _create_task(factory, incident_id, user_id=ALEX)
        second = await _create_task(factory, incident_id, user_id=CHRIS)
        return first, second

    first, second = svc(go)
    assert first["created"] is True
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]


def test_create_report_task__dedupe_covers_awaiting_review(client, svc):
    """去重判据是"未走完的报告任务", awaiting_review 也算 —— 等人过目时
    另一个人再点, 带回同一条, 不新开。"""
    incident_id = make_incident()

    async def go(factory):
        first = await _create_task(factory, incident_id, user_id=ALEX)
        await _set_task_status(factory, first["task_id"], "awaiting_review")
        second = await _create_task(factory, incident_id, user_id=CHRIS)
        return first, second

    first, second = svc(go)
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]


def test_create_report_task__completed_task_allows_new_one(client, svc):
    """走完的任务不占坑: 上一份已定稿/退回后 (任务 completed 且报告已弃),
    重新点生成开的是新任务。"""
    incident_id = make_incident()

    async def go(factory):
        first = await _create_task(factory, incident_id, user_id=ALEX)
        await _set_task_status(factory, first["task_id"], "completed")
        second = await _create_task(factory, incident_id, user_id=ALEX)
        return first, second

    first, second = svc(go)
    assert second["created"] is True
    assert second["task_id"] != first["task_id"]


# ===== 草稿写路径 =====


def test_update_report_draft__bumps_updated_at_and_returns_previous(client, svc):
    """雷区 9 的守卫: updated_at 没有触发器, service 必须自己盖戳;
    previous_body 是修复前的中间态, 随返回值进时间线。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id)
        before = await _fetch_one(
            factory, "SELECT updated_at FROM incident_reports WHERE id = :id",
            id=report_id,
        )
        new_body = {**GOOD_BODY, "notable": "处理人第一时间赶到现场。"}
        async with factory() as session, session.begin():
            result = await report_task_service.update_report_draft(
                session, task_id=task_id, body=new_body
            )
        after = await _fetch_one(
            factory, "SELECT body, updated_at FROM incident_reports WHERE id = :id",
            id=report_id,
        )
        return before, after, result

    before, after, result = svc(go)
    assert result["previous_body"] == GOOD_BODY
    assert after["body"]["notable"] == "处理人第一时间赶到现场。"
    assert after["updated_at"] > before["updated_at"]


def test_update_report_draft__no_draft_raises(client, svc):
    incident_id = make_incident()

    async def go(factory):
        created = await _create_task(factory, incident_id)
        async with factory() as session, session.begin():
            with pytest.raises(ReportNotFound):
                await report_task_service.update_report_draft(
                    session, task_id=created["task_id"], body=GOOD_BODY
                )

    svc(go)


def test_validate_task_report__counts_accumulate_per_item(client, svc):
    """两个计数按违规项累加进列 (SPEC-008 第三节): 一轮两处裸写加 2,
    再验一轮再加 —— 计的是"想写的次数", 不是当前草稿的状态。"""
    incident_id = make_incident()
    bad = {**GOOD_BODY,
           "impact": "全程耗时 3 分钟, 到场 2 人。",       # 两段阿拉伯数字
           "notable": "另见 {{nonexistent}}。"}            # 一处悬空引用

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id, body=bad)
        async with factory() as session, session.begin():
            first = await report_task_service.validate_task_report(
                session, task_id=task_id
            )
        async with factory() as session, session.begin():
            await report_task_service.validate_task_report(session, task_id=task_id)
        row = await _fetch_one(
            factory,
            "SELECT bare_fact_attempts, dangling_ref_attempts "
            "FROM incident_reports WHERE id = :id", id=report_id,
        )
        return first, row

    first, row = svc(go)
    assert first["ok"] is False
    assert first["bare_fact_attempts"] == 2
    assert first["dangling_ref_attempts"] == 1
    codes = {v["code"] for v in first["violations"]}
    assert codes == {"E_BARE_FACT", "E_DANGLING_REF"}
    assert row["bare_fact_attempts"] == 4        # 两轮各 2
    assert row["dangling_ref_attempts"] == 2


# ===== 定稿 / 弃稿 =====


def test_finalize_report__draft_to_final_task_completed(client, svc):
    incident_id = make_incident()

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id)
        await _set_task_status(factory, task_id, "awaiting_review")
        async with factory() as session, session.begin():
            await report_task_service.finalize_report(
                session, report_id=report_id, user_id=CHRIS
            )
        report = await _fetch_one(
            factory,
            "SELECT status, finalized_by, finalized_at "
            "FROM incident_reports WHERE id = :id", id=report_id,
        )
        task = await _fetch_one(
            factory,
            "SELECT status, completed_at FROM agent_tasks WHERE id = :id", id=task_id,
        )
        return report, task

    report, task = svc(go)
    assert report["status"] == "final"
    assert report["finalized_by"] == CHRIS
    assert report["finalized_at"] is not None
    assert task["status"] == "completed"
    assert task["completed_at"] is not None


def test_finalize_report__conflicts(client, svc):
    """重复定稿 409; 任务不在等待过目 (还在跑) 也 409 —— 定稿一个模型还会
    改写的东西等于把草稿钉成终稿。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id)
        # 任务还是 running: 不许定稿
        async with factory() as session, session.begin():
            with pytest.raises(ReportStateConflict):
                await report_task_service.finalize_report(
                    session, report_id=report_id, user_id=CHRIS
                )
        await _set_task_status(factory, task_id, "awaiting_review")
        async with factory() as session, session.begin():
            await report_task_service.finalize_report(
                session, report_id=report_id, user_id=CHRIS
            )
        async with factory() as session, session.begin():
            with pytest.raises(ReportStateConflict):
                await report_task_service.finalize_report(
                    session, report_id=report_id, user_id=CHRIS
                )

    svc(go)


def test_discard_draft__task_completed_not_failed(client, svc):
    """变异 B4 的守卫 (SPEC-008 第四节): 人退回 -> 任务 completed + 报告
    discarded。failed 专指"模型没写对", 人不要这一份不是模型的错。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id)
        await _set_task_status(factory, task_id, "awaiting_review")
        async with factory() as session, session.begin():
            await report_task_service.discard_report(
                session, report_id=report_id, user_id=CHRIS
            )
        report = await _fetch_one(
            factory, "SELECT status FROM incident_reports WHERE id = :id",
            id=report_id,
        )
        task = await _fetch_one(
            factory, "SELECT status FROM agent_tasks WHERE id = :id", id=task_id,
        )
        audit = await _fetch_one(
            factory,
            "SELECT user_id, action, detail FROM audit_log "
            "WHERE entity = 'incident_report' AND entity_id = :id",
            id=str(report_id),
        )
        return report, task, audit

    report, task, audit = svc(go)
    assert report["status"] == "discarded"
    assert task["status"] == "completed"
    assert task["status"] != "failed"
    assert audit is not None and audit["action"] == "report_discard"
    assert audit["user_id"] == CHRIS


def test_discard_final__allowed_and_running_draft_blocked(client, svc):
    """final 也能弃 (SPEC-008 第八节: final 占部分唯一索引的位, 不许弃等于
    定稿后永远开不出第二份); 任务还在跑时弃草稿 409。"""
    incident_id = make_incident()

    async def go(factory):
        task_id, report_id = await _make_draft(factory, incident_id)
        # running 时弃草稿: 409
        async with factory() as session, session.begin():
            with pytest.raises(ReportStateConflict):
                await report_task_service.discard_report(
                    session, report_id=report_id, user_id=CHRIS
                )
        await _set_task_status(factory, task_id, "awaiting_review")
        async with factory() as session, session.begin():
            await report_task_service.finalize_report(
                session, report_id=report_id, user_id=CHRIS
            )
        async with factory() as session, session.begin():
            await report_task_service.discard_report(
                session, report_id=report_id, user_id=CHRIS
            )
        # 已弃再弃: 409
        async with factory() as session, session.begin():
            with pytest.raises(ReportStateConflict):
                await report_task_service.discard_report(
                    session, report_id=report_id, user_id=CHRIS
                )
        return await _fetch_one(
            factory, "SELECT status FROM incident_reports WHERE id = :id",
            id=report_id,
        )

    report = svc(go)
    assert report["status"] == "discarded"


# ===== 预扣回补 (变异 B1 的守卫, 雷区 3) =====


def test_hold_refunded__when_round_ends_awaiting_review(client, svc):
    """报告任务停在等人过目 = 不再发任何 LLM 调用, 预扣必须回补 ——
    _REFUNDABLE_OUTCOMES 里把 awaiting_review 拿掉, 这条会红
    (每生成一份报告就永久占住一笔预扣, 配额一份一份漏光且不报错)。"""
    import asyncio

    from sqlalchemy import text

    incident_id = make_incident()

    async def go(factory):
        async def spent() -> float:
            async with factory() as session, session.begin():
                value = (await session.execute(
                    text("SELECT coalesce(sum(spent_cny), 0) FROM llm_spend_daily")
                )).scalar_one()
                return float(value)

        base = await spent()
        async with factory() as session, session.begin():
            created = await report_task_service.create_report_task(
                session, user_id=ALEX, incident_id=incident_id
            )
            await budget_service.reserve_task_budget(session, user_id=ALEX)
        task_id = created["task_id"]
        assert await spent() > base  # 预扣已入账

        # 模拟"这一轮以 awaiting_review 收场"的后台协程, 挂上真实的回补钩子
        async def fake_round() -> str:
            await _set_task_status(factory, task_id, "awaiting_review")
            return "awaiting_review"

        round_task = asyncio.create_task(fake_round())
        budget_service.refund_when_done(round_task, task_id, factory)
        await round_task
        for _ in range(200):  # 回补是独立协程, 等它跑完
            if not budget_service._REFUND_TASKS:
                break
            await asyncio.sleep(0.01)
        return base, await spent()

    base, after = svc(go)
    # 打桩零花费, 预扣全额回补, 台账回到起点
    assert after == pytest.approx(base)
