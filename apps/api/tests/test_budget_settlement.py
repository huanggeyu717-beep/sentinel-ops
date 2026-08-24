"""第 0 件: 回补挂在"任务进终态"上, 且是幂等的结算 (SPEC-009 第二节末尾)。

三个方向缺一不可 (缺任何一条, 另一条就守不住钱):

1. **清扫判死也要结算**: 停在 clarifying 被清扫判死的任务, 预扣回到台账。
   没有它, 预扣 0.60 × 单账号配额 3 只需一个访客"点开、看到反问、关掉"几下,
   当天的演示额度就锁死到 UTC 零点;
2. **同一笔钱不会被减两遍**: 轮次收尾与清扫都对同一条任务结算时台账只减一次。
   没有这一条, 一个"每次都回补"的实现也能让上一条绿 —— 而它把台账越算越少,
   直到见底、护栏形同虚设。
3. **该留在账上的不许退**: 一轮以 clarifying 收场时预扣留着不动 (方向三,
   2026-08-24 补)。这一条原本**没有任何测试守着** —— 往
   `_REFUNDABLE_OUTCOMES` 里加一个 `clarifying`, 上面两条照样全绿。
   它是方向一的反面: 只测"该结算的结算了", 一个"什么都结算"的实现全绿。

清扫侧刻意调 agent_runtime.sweep_once —— maintenance_loop 每轮跑的就是这个
函数, 测它等于测循环体; 只测 budget_service.refund_task_hold 的话, "清扫忘了
调结算"这种变异永远打不红。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

from app.config import settings
from app.services import agent_runtime, budget_service


def _seed_ledger(monkeypatch, *, spent: float, hold: float = 0.6) -> None:
    monkeypatch.setattr(settings(), "agent_task_hold_cny", hold)

    async def go(conn):
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, $1, 10.0)", spent,
        )

    db(go)


def _spent() -> float:
    async def go(conn):
        return float(await conn.fetchval("SELECT spent_cny FROM llm_spend_daily"))

    return db(go)


def _task_state(task_id: int) -> dict:
    async def go(conn):
        return dict(
            await conn.fetchrow(
                "SELECT status, error_code, hold_refunded_at "
                "FROM agent_tasks WHERE id = $1", task_id,
            )
        )

    return db(go)


def _sweep(svc) -> list[dict]:
    async def go(factory):
        async with factory() as session, session.begin():
            return await agent_runtime.sweep_once(session)

    result: list[dict] = svc(go)
    return result


# ===== 方向一: 清扫判死 = 进终态 = 结算 =====


def test_sweep__clarify_timeout_settles_hold(client, svc, monkeypatch):
    """停在 clarifying 超过生存期被判死后, 预扣回到台账 (第 0 件的新行为)。"""
    _seed_ledger(monkeypatch, spent=0.6)

    async def seed(conn):
        task_id = await insert_task(
            conn, input_hash="sweep-clarify", status="clarifying",
        )
        await conn.execute(
            "INSERT INTO agent_clarifications (task_id, asked_seq, question, asked_at) "
            "VALUES ($1, 1, '哪个区?', now() - interval '48 hours')", task_id,
        )
        return task_id

    task_id = db(seed)
    reaped = _sweep(svc)

    assert {"task_id": task_id, "reason": "clarify_timeout"} in reaped
    state = _task_state(task_id)
    assert state["status"] == "dead_letter"
    assert state["hold_refunded_at"] is not None
    assert _spent() == pytest.approx(0.0)  # 无 ai_usage, 整笔预扣回台账


def test_sweep__lease_timeout_settles_hold_minus_actual_usage(client, svc, monkeypatch):
    """失联判死同样结算, 且回补按 ai_usage 真实合计算差额 (不是无脑退整笔)。"""
    _seed_ledger(monkeypatch, spent=0.6)

    async def seed(conn):
        task_id = await insert_task(
            conn, input_hash="sweep-lease", status="running",
            heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
        )
        await conn.execute(
            "INSERT INTO ai_usage (task_id, model, estimated_cost_cny) "
            "VALUES ($1, 'stub', 0.10)", task_id,
        )
        return task_id

    task_id = db(seed)
    reaped = _sweep(svc)

    assert {"task_id": task_id, "reason": "lease_timeout"} in reaped
    assert _task_state(task_id)["status"] == "dead_letter"
    assert _spent() == pytest.approx(0.10)  # 退的是 0.6 - 0.1, 真花掉的留在账上


# ===== 方向二: 同一笔钱只减一次 =====


def test_settle_twice__ledger_decreases_only_once(client, svc, monkeypatch):
    """轮次收尾与清扫走的是同一个结算函数; 对同一条任务调两遍只减一次。

    台账刻意放两笔预扣 (1.2): 正确实现第二遍结算是 0 行更新, 留 0.6;
    "每次都回补"的实现会减到 0.0 —— 只放一笔的话两种实现都停在 0, 分不出来。
    """
    _seed_ledger(monkeypatch, spent=1.2)
    task_id = db(lambda conn: insert_task(
        conn, input_hash="settle-twice", status="completed",
    ))

    async def settle(factory):
        async with factory() as session, session.begin():
            await budget_service.refund_task_hold(session, task_id)

    svc(settle)
    assert _spent() == pytest.approx(0.6)
    svc(settle)
    assert _spent() == pytest.approx(0.6)  # 第二遍抢不到钥匙, 一个字不写


def test_round_end_then_sweep__settles_once(client, svc, monkeypatch):
    """两条真实路径先后到场: 轮次收尾式结算之后, 清扫再跑一轮不重复减账。

    clarifying 判死先由清扫结算; 再跑一轮清扫 (幂等条件更新, 任务已是
    dead_letter, reap 不再命中) 台账不动 —— 这是 maintenance_loop 每 5 秒
    一轮的真实节奏, 同一条死信任务会被后续每一轮清扫看见。
    """
    _seed_ledger(monkeypatch, spent=1.2)

    async def seed(conn):
        task_id = await insert_task(
            conn, input_hash="sweep-again", status="clarifying",
        )
        await conn.execute(
            "INSERT INTO agent_clarifications (task_id, asked_seq, question, asked_at) "
            "VALUES ($1, 1, '哪个区?', now() - interval '48 hours')", task_id,
        )
        return task_id

    task_id = db(seed)
    first = _sweep(svc)
    assert any(r["task_id"] == task_id for r in first)
    assert _spent() == pytest.approx(0.6)

    second = _sweep(svc)  # 下一轮清扫: 任务已判死, 不再命中, 更不再减账
    assert not any(r["task_id"] == task_id for r in second)
    assert _spent() == pytest.approx(0.6)


# ===== 方向三: 还会再花钱的中间态, 不结算 =====


def test_round_ends_clarifying__hold_stays_on_ledger(client, svc, monkeypatch):
    """一轮以 clarifying 收场时预扣**留在账上**, 不回补。

    人回答之后恢复的那一轮花的还是同一笔预扣。这时候把钱退回去, 一个访客就能
    "问一句反问 -> 拿回额度 -> 答一句接着跑", 同一笔预扣支撑无限多轮模型调用,
    SPEC-009 的花钱护栏当场作废。一直没人答的那些由清扫判死时结算 (方向一)。

    这一条是评审方跑变异查出来的空白 (2026-08-24): 把 clarifying 加进
    `_REFUNDABLE_OUTCOMES` 之后, 报告档与结算档**全部仍然是绿的** ——
    与 test_report_render 那条"计数器恒返回 1"同一课, 只测一个方向的判据,
    从外面看和真的守着一模一样。
    """
    import asyncio

    _seed_ledger(monkeypatch, spent=0.6)

    async def seed(conn):
        return await insert_task(
            conn, input_hash="round-clarify", status="clarifying",
        )

    task_id = db(seed)

    async def go(factory):
        # 模拟"这一轮以 clarifying 收场"的后台协程, 挂上真实的回补钩子
        async def fake_round() -> str:
            return "clarifying"

        round_task = asyncio.create_task(fake_round())
        budget_service.refund_when_done(round_task, task_id, factory)
        await round_task
        for _ in range(200):  # 真回补的话是独立协程, 给它跑完的机会
            if not budget_service._REFUND_TASKS:
                break
            await asyncio.sleep(0.01)

    svc(go)

    assert _spent() == pytest.approx(0.6)  # 预扣原封不动留在台账上
    assert _task_state(task_id)["hold_refunded_at"] is None
