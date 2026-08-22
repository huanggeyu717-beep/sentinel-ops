"""迁移 0010 的结构断言与往返 (SPEC-009 第七节)。

与 0009 同一路数: head 上直接断言三张表与**按名字点到**的约束都在
(抄漏约束的迁移不会自己报错, 这里替它报), 再显式降到 0009、断言表消失、
升回 head。降级对三张表的行有损 —— 花费台账不是审计事实源 (那是 ai_usage),
丢了只是当天额度从头算, 迁移 docstring 里写明了。
"""
from __future__ import annotations

import asyncpg
import pytest
from test_agent_helpers import db

GUARDRAIL_TABLES = ("llm_spend_daily", "user_task_quota_daily", "demo_marker")


async def _table_exists(conn, table: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = $1)", table,
        )
    )


async def _constraint_names(conn, table: str) -> set[str]:
    return {
        r["conname"]
        for r in await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass",
            table,
        )
    }


# ===== head 上的结构断言 =====


def test_guardrail_tables__exist_with_named_checks(client):
    async def go(conn):
        for table in GUARDRAIL_TABLES:
            assert await _table_exists(conn, table), table
        spend = await _constraint_names(conn, "llm_spend_daily")
        # 护栏的本体是这两条 CHECK, 按名字断言 —— budget_service 靠约束名
        # 区分"额度用完"与其它完整性错误, 名字变了那边会把 429 报成 500
        assert {"llm_spend_daily_within_limit", "llm_spend_daily_nonnegative"} <= spend
        quota = await _constraint_names(conn, "user_task_quota_daily")
        assert "user_task_quota_within_limit" in quota
        marker = await _constraint_names(conn, "demo_marker")
        assert "demo_marker_single_row" in marker

    db(go)


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2)", table, column,
        )
    )


def test_agent_tasks__has_hold_refunded_at(client):
    """回补的幂等钥匙列 (第 0 件): 没有它, 轮次收尾与清扫会把同一笔预扣减两遍。"""

    async def go(conn):
        assert await _column_exists(conn, "agent_tasks", "hold_refunded_at")

    db(go)


def test_quota_primary_key__is_user_and_day(client):
    async def go(conn):
        cols = [
            r["attname"]
            for r in await conn.fetch(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'user_task_quota_daily'::regclass "
                "AND i.indisprimary"
            )
        ]
        assert sorted(cols) == ["day", "user_id"]

    db(go)


def test_demo_marker__physically_single_row(client):
    """单行由 PK + CHECK 强制: 唯一合法主键值是 true。"""

    async def go(conn):
        await conn.execute("INSERT INTO demo_marker DEFAULT VALUES")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO demo_marker (only_row) VALUES (true)")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO demo_marker (only_row) VALUES (false)")
        assert await conn.fetchval("SELECT count(*) FROM demo_marker") == 1

    db(go)


# ===== 降一步再升回 (易错点: downgrade 必须实测, 不能只写不跑) =====


def test_migration_0010__downgrade_then_upgrade_roundtrip(client):
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    # 显式降到上一版, 不用相对的 "-1" (后续迁移出现后 "-1" 就不再指本迁移)
    command.downgrade(cfg, "0009_evals_groundwork")

    async def check_downgraded(conn):
        for table in GUARDRAIL_TABLES:
            assert not await _table_exists(conn, table), table
        assert not await _column_exists(conn, "agent_tasks", "hold_refunded_at")

    db(check_downgraded)

    command.upgrade(cfg, "head")

    async def check_upgraded(conn):
        for table in GUARDRAIL_TABLES:
            assert await _table_exists(conn, table), table
        # 升回来的不只是表, 约束与列也得在 (roundtrip 断言结构一致, 不是只数表名)
        assert "llm_spend_daily_within_limit" in await _constraint_names(
            conn, "llm_spend_daily"
        )
        assert await _column_exists(conn, "agent_tasks", "hold_refunded_at")

    db(check_upgraded)
