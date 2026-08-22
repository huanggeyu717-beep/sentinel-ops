"""花钱护栏的数据库约束与 service 逻辑 (SPEC-009 第二节, 不经 HTTP)。

三类断言:
1. **约束本身**: 绕过应用层直接 UPDATE/INSERT, 超限物理上写不进去 (验收第五条
   的后半句; 变异 1 "去掉 CHECK" 的直接靶子);
2. **"今天"是 UTC 日期**: 把数据库会话时区拧到两个极端 (UTC+14 与 UTC-12),
   预扣落的仍是同一行、UTC 的那一天 —— 换成 current_date 就会写出两行不同的日期
   (它跟会话时区走), 这条测试就红;
3. **回补的算术**: 差额按 ai_usage 合计、GREATEST 兜底不减成负数、落在任务
   创建那天的行上。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

from app.config import settings
from app.services import budget_service


def _shrink(monkeypatch, *, hold=0.6, budget=10.0, tasks=100):
    """把 conftest 放开的护栏参数收回到用例可控的小值。"""
    monkeypatch.setattr(settings(), "agent_task_hold_cny", hold)
    monkeypatch.setattr(settings(), "llm_daily_budget_cny", budget)
    monkeypatch.setattr(settings(), "agent_user_daily_tasks", tasks)


# ===== 约束本身: 绕过应用层也写不进去 =====


def test_spend_check__direct_update_beyond_limit_rejected(client):
    async def go(conn):
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 0.9, 1.0)"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE llm_spend_daily SET spent_cny = spent_cny + 0.2"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
                "VALUES ('2020-01-01', 5.0, 1.0)"
            )

    db(go)


def test_spend_check__negative_spent_rejected(client):
    async def go(conn):
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 0.1, 1.0)"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE llm_spend_daily SET spent_cny = spent_cny - 0.5"
            )

    db(go)


def test_quota_check__used_beyond_limit_rejected(client):
    async def go(conn):
        await conn.execute(
            "INSERT INTO user_task_quota_daily (user_id, day, used, limit_count) "
            "VALUES (3, (now() AT TIME ZONE 'utc')::date, 5, 5)"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE user_task_quota_daily SET used = used + 1 WHERE user_id = 3"
            )

    db(go)


# ===== "今天" 是 UTC 日期, 不跟会话时区走 =====


def test_reserve__day_is_utc_regardless_of_session_timezone(client, svc, monkeypatch):
    _shrink(monkeypatch)
    before = datetime.now(UTC).date()

    async def go(factory):
        # 两个极端时区: UTC+14 (Kiritimati) 与 UTC-12 (Etc/GMT+12, POSIX 符号
        # 相反)。任一时刻至少有一个的本地日期 != UTC 日期, 所以拿会话时区当
        # "今天" (current_date) 必写出两行不同日期 —— 本测试就红
        for tz in ("Pacific/Kiritimati", "Etc/GMT+12"):
            async with factory() as session, session.begin():
                await session.execute(text(f"SET TIME ZONE '{tz}'"))
                await budget_service.reserve_task_budget(session, user_id=3)

    svc(go)
    after = datetime.now(UTC).date()

    async def rows(conn):
        spend = await conn.fetch("SELECT day, spent_cny FROM llm_spend_daily")
        quota = await conn.fetch("SELECT day, used FROM user_task_quota_daily")
        return spend, quota

    spend, quota = db(rows)
    assert len(spend) == 1 and spend[0]["day"] in {before, after}
    assert float(spend[0]["spent_cny"]) == pytest.approx(1.2)  # 两笔预扣同一行
    assert len(quota) == 1 and quota[0]["day"] in {before, after}
    assert quota[0]["used"] == 2


# ===== 预扣被 CHECK 拒绝时抛领域异常 =====


def test_reserve__budget_check_raises_domain_error(client, svc, monkeypatch):
    _shrink(monkeypatch, hold=0.6, budget=0.0)

    async def go(factory):
        async with factory() as session, session.begin():
            with pytest.raises(budget_service.DailyBudgetExhausted):
                await budget_service.reserve_task_budget(session, user_id=3)

    svc(go)


def test_reserve__quota_check_raises_domain_error(client, svc, monkeypatch):
    _shrink(monkeypatch, tasks=1)

    async def go(factory):
        async with factory() as session, session.begin():
            await budget_service.reserve_task_budget(session, user_id=3)
        async with factory() as session, session.begin():
            with pytest.raises(budget_service.UserQuotaExhausted):
                await budget_service.reserve_task_budget(session, user_id=3)

    svc(go)


# ===== 回补 =====


async def _seed_spend(conn, *, day_sql="(now() AT TIME ZONE 'utc')::date",
                      spent=0.6, limit=10.0):
    await conn.execute(
        f"INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
        f"VALUES ({day_sql}, $1, $2)", spent, limit,
    )


async def _add_usage(conn, task_id: int, cost: float) -> None:
    await conn.execute(
        "INSERT INTO ai_usage (task_id, model, estimated_cost_cny) "
        "VALUES ($1, 'stub', $2)", task_id, cost,
    )


def _refund(svc, task_id: int) -> None:
    async def go(factory):
        async with factory() as session, session.begin():
            await budget_service.refund_task_hold(session, task_id)

    svc(go)


def _spent_by_day(conn_fn=db) -> dict:
    async def go(conn):
        return {
            r["day"]: float(r["spent_cny"])
            for r in await conn.fetch("SELECT day, spent_cny FROM llm_spend_daily")
        }

    return conn_fn(go)


def test_refund__difference_comes_from_ai_usage_sum(client, svc, monkeypatch):
    """回补 = 预扣 - ai_usage 合计。数据来源是表不是内存计数器 (跨进程成立)。"""
    _shrink(monkeypatch, hold=0.6)

    async def seed(conn):
        await _seed_spend(conn, spent=0.6)
        task_id = await insert_task(conn, input_hash="refund-sum", status="completed")
        await _add_usage(conn, task_id, 0.10)
        await _add_usage(conn, task_id, 0.05)
        return task_id

    task_id = db(seed)
    _refund(svc, task_id)
    (spent,) = _spent_by_day().values()
    assert spent == pytest.approx(0.15)  # 0.6 - (0.6 - 0.15)


def test_refund__never_pushes_spent_below_zero(client, svc, monkeypatch):
    """GREATEST(0, ...) 兜底: 台账比预扣还小 (比如被人工调过) 也只到 0 为止。"""
    _shrink(monkeypatch, hold=0.6)

    async def seed(conn):
        await _seed_spend(conn, spent=0.1)
        return await insert_task(conn, input_hash="refund-floor", status="completed")

    task_id = db(seed)  # 无 ai_usage, 名义上要回补整笔 0.6
    _refund(svc, task_id)
    (spent,) = _spent_by_day().values()
    assert spent == 0.0


def test_refund__overspend_does_not_add_money(client, svc, monkeypatch):
    """真实花费超过预扣时差额取 0, 不倒扣 (内层 GREATEST)。"""
    _shrink(monkeypatch, hold=0.6)

    async def seed(conn):
        await _seed_spend(conn, spent=0.9)
        task_id = await insert_task(conn, input_hash="refund-over", status="completed")
        await _add_usage(conn, task_id, 0.9)
        return task_id

    task_id = db(seed)
    _refund(svc, task_id)
    (spent,) = _spent_by_day().values()
    assert spent == pytest.approx(0.9)


def test_refund__lands_on_task_creation_day(client, svc, monkeypatch):
    """跨天走完的任务把钱还回它扣走的那天, 不动今天的账。"""
    _shrink(monkeypatch, hold=0.6)
    old_day = (datetime.now(UTC) - timedelta(days=3)).date()

    async def seed(conn):
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ($1, 0.6, 10.0), ((now() AT TIME ZONE 'utc')::date, 0.5, 10.0)",
            old_day,
        )
        return await insert_task(
            conn, input_hash="refund-day", status="completed",
            created_at=datetime.now(UTC) - timedelta(days=3),
        )

    task_id = db(seed)
    _refund(svc, task_id)
    by_day = _spent_by_day()
    assert by_day[old_day] == 0.0            # 全额回补落在创建那天
    assert 0.5 in by_day.values()            # 今天的行原封不动
