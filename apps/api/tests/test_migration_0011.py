"""迁移 0011 的结构断言与往返 (SPEC-008 第七节), 照 test_migration_0010 的路数。

head 上按名字点到约束与索引 (抄漏约束的迁移不会自己报错, 这里替它报);
partial unique index 用真插入打一次 (变异 4: 拆掉索引这条必须红);
再显式降到 0010、断言表与两条 CHECK 消失、升回 head。
"""
from __future__ import annotations

import asyncpg
import pytest
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401


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


async def _index_names(conn, table: str) -> set[str]:
    return {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = $1", table
        )
    }


async def _insert_incident(conn) -> int:
    incident_id: int = await conn.fetchval(
        "INSERT INTO incidents (zone_id, sensor_id, severity, status, opened_at, "
        "resolved_at, resolved_by) VALUES (1, 1, 'normal', 'resolved', "
        "now() - interval '1 hour', now(), 'user:2') RETURNING id"
    )
    return incident_id


async def _insert_report(conn, incident_id: int, task_id: int, status: str) -> int:
    report_id: int = await conn.fetchval(
        "INSERT INTO incident_reports (incident_id, task_id, body, fact_pack, "
        "status, created_by) VALUES ($1, $2, '{}'::jsonb, '[]'::jsonb, $3, 2) "
        "RETURNING id",
        incident_id, task_id, status,
    )
    return report_id


# ===== head 上的结构断言 =====


def test_incident_reports__exists_with_named_check_and_index(client):
    async def go(conn):
        assert await _table_exists(conn, "incident_reports")
        assert "incident_reports_status_check" in await _constraint_names(
            conn, "incident_reports"
        )
        assert "incident_reports_one_active" in await _index_names(
            conn, "incident_reports"
        )

    db(go)


def test_incident_reports_one_active__second_nondiscarded_blocked(client):
    """变异 4 的守卫: 绕过应用层直插第二行非 discarded, 必须被 partial unique
    index 拦住; discarded 不占位, final 占位 (弃掉才能重开, SPEC-008 第八节)。"""

    async def go(conn):
        incident_id = await _insert_incident(conn)
        task_a = await insert_task(conn, status="completed", input_hash="rep-a",
                                   task_type="incident_report")
        task_b = await insert_task(conn, status="completed", input_hash="rep-b",
                                   task_type="incident_report")
        await _insert_report(conn, incident_id, task_a, "draft")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_report(conn, incident_id, task_b, "draft")

        # 弃掉那份 draft 之后可以重开
        await conn.execute(
            "UPDATE incident_reports SET status = 'discarded' WHERE incident_id = $1",
            incident_id,
        )
        await _insert_report(conn, incident_id, task_b, "final")
        # final 同样占位: 定稿了也不许再开第二份非 discarded 的
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_report(conn, incident_id, task_a, "draft")

    db(go)


def test_incident_reports_status__check_rejects_unknown_value(client):
    async def go(conn):
        incident_id = await _insert_incident(conn)
        task_id = await insert_task(conn, status="completed", input_hash="rep-s",
                                    task_type="incident_report")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_report(conn, incident_id, task_id, "rejected")

    db(go)


def test_agent_tasks_status__accepts_awaiting_review(client):
    async def go(conn):
        task_id = await insert_task(conn, status="awaiting_review", input_hash="rep-r")
        assert task_id

    db(go)


def test_agent_tasks_task_type__check_rejects_typo(client):
    """第九节矛盾 3 的了结: task_type 从注释变约束, 拼错一个字母当场报错。"""

    async def go(conn):
        with pytest.raises(asyncpg.CheckViolationError):
            await insert_task(conn, input_hash="rep-t1", task_type="policy_compil")
        assert await insert_task(conn, input_hash="rep-t2", task_type="incident_report")

    db(go)


def test_agent_tasks_stage__deliberately_unconstrained(client):
    """不对称是故意的 (SPEC-008 第七节第 4 条): stage 不加 CHECK, 新阶段名
    collecting / drafting 不需要迁移就写得进去 —— 这条测的就是"没有约束"本身。"""

    async def go(conn):
        assert await insert_task(conn, input_hash="rep-g1", stage="collecting")
        assert await insert_task(conn, input_hash="rep-g2", stage="drafting")

    db(go)


# ===== 降一步再升回 (downgrade 必须实测, 不能只写不跑; ADR-006) =====


def test_migration_0011__downgrade_then_upgrade_roundtrip(client):
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    command.downgrade(cfg, "0010_deploy_guardrails")

    async def check_downgraded(conn):
        assert not await _table_exists(conn, "incident_reports")
        names = await _constraint_names(conn, "agent_tasks")
        assert "agent_tasks_task_type_check" not in names
        # status CHECK 回到旧集合: awaiting_review 写不进去了
        with pytest.raises(asyncpg.CheckViolationError):
            await insert_task(conn, status="awaiting_review", input_hash="rep-d")

    db(check_downgraded)

    command.upgrade(cfg, "head")

    async def check_upgraded(conn):
        assert await _table_exists(conn, "incident_reports")
        names = await _constraint_names(conn, "agent_tasks")
        assert {"agent_tasks_task_type_check", "agent_tasks_status_check"} <= names
        assert "incident_reports_one_active" in await _index_names(
            conn, "incident_reports"
        )
        assert await insert_task(conn, status="awaiting_review", input_hash="rep-u")

    db(check_upgraded)
