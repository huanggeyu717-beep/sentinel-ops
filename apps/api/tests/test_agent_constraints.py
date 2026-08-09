"""SPEC-002 数据库层不变量与迁移 0008 回滚 (验收 10/12 的直接写库半边 + 验收 25)。

绕过应用层直接写库, 证明"异常路径走不通" —— 与 W3 的 test_policy_constraints
同一路数。端到端的另一半 (正常路径走得通) 在 test_agent_service / test_agent_runtime。

变异测试的靶子 (SPEC-002 验收 20/23):
- 删掉 agent_tasks_one_open  -> 本文件的 one_open 三条必须红;
- 删掉 agent_clarifications_one_pending -> one_pending 那条必须红。
"""
from __future__ import annotations

import asyncpg
import pytest
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

# ===== 验收 12 (数据库半边): 同一用户同一句话, 最多一条还没走完的任务 =====


def test_second_open_task_same_user_and_hash__unique_index_rejects(client):
    async def go(conn):
        await insert_task(conn, user_id=3, input_hash="h1", status="running")
        with pytest.raises(asyncpg.UniqueViolationError):
            await insert_task(conn, user_id=3, input_hash="h1", status="running")

    db(go)


def test_open_task_while_previous_clarifying__unique_index_rejects(client):
    """索引条件必须含 clarifying: 老任务停在澄清等回答时, 重说一遍不得开新任务。"""
    async def go(conn):
        await insert_task(conn, user_id=3, input_hash="h1", status="clarifying")
        with pytest.raises(asyncpg.UniqueViolationError):
            await insert_task(conn, user_id=3, input_hash="h1", status="running")

    db(go)


def test_open_task_after_terminal_or_other_key__index_allows(client):
    """反向对照: 终态之后能重开; 不同用户/不同输入互不阻挡;
    同一条任务从 clarifying 回到 running 不撞自己 (SPEC-002 第七节实测过的四种情况)。"""
    async def go(conn):
        done = await insert_task(conn, user_id=3, input_hash="h1", status="completed")
        assert done
        # 前一次已是终态 -> 新任务
        opened = await insert_task(conn, user_id=3, input_hash="h1", status="running")
        # 不同用户同一句话 / 同一用户不同输入: 都不挡
        await insert_task(conn, user_id=2, input_hash="h1", status="running")
        await insert_task(conn, user_id=3, input_hash="h2", status="running")
        # 人回答后同一条任务 clarifying -> running, 是同一行的 UPDATE, 不会撞自己
        await conn.execute(
            "UPDATE agent_tasks SET status = 'clarifying' WHERE id = $1", opened
        )
        await conn.execute(
            "UPDATE agent_tasks SET status = 'running' WHERE id = $1", opened
        )

    db(go)


# ===== 验收 10 (数据库半边): 一个任务同时最多一个未回答的问题 =====


def test_second_pending_clarification__unique_index_rejects(client):
    async def go(conn):
        task_id = await insert_task(conn, status="clarifying")
        await conn.execute(
            "INSERT INTO agent_clarifications (task_id, asked_seq, question) "
            "VALUES ($1, 1, '通知谁?')", task_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_clarifications (task_id, asked_seq, question) "
                "VALUES ($1, 2, '几个探头?')", task_id,
            )
        # 第一个问题被回答之后, 下一轮的问题可以再进来 (partial index 只看 answer IS NULL)
        await conn.execute(
            "UPDATE agent_clarifications SET answer = '后场主管', answered_by = 3, "
            "answered_seq = 2, answered_at = now() WHERE task_id = $1", task_id,
        )
        await conn.execute(
            "INSERT INTO agent_clarifications (task_id, asked_seq, question) "
            "VALUES ($1, 3, '几个探头?')", task_id,
        )

    db(go)


# ===== 时间线编号: (task_id, seq) 由数据库封死 =====


def test_duplicate_step_seq__unique_index_rejects(client):
    """SSE 按 seq 断点续传, seq 重复只在断线时暴露 —— 所以由索引拦, 不靠应用层自觉。"""
    async def go(conn):
        task_id = await insert_task(conn)
        await conn.execute(
            "INSERT INTO agent_steps (task_id, seq, tool_name, status) "
            "VALUES ($1, 1, 'list_zones', 'ok')", task_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_steps (task_id, seq, tool_name, status) "
                "VALUES ($1, 1, 'list_sensors', 'ok')", task_id,
            )

    db(go)


# ===== user_id NOT NULL: 部分唯一索引对 NULL 不冲突, 可空 = 去重防线静默失效 =====


def test_task_with_null_user__db_rejects(client):
    async def go(conn):
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO agent_tasks (user_id, task_type, input, input_hash) "
                "VALUES (NULL, 'policy_compile', '{}'::jsonb, 'h')"
            )

    db(go)


# ===== 第 3 条: idempotency_key 整列已删 (与 input_hash 是同一个事实) =====


def test_idempotency_key_column__removed(client):
    async def go(conn):
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agent_tasks' AND column_name = 'idempotency_key')"
        )

    db(go)


# ===== 第 10/11 条: policy_versions 的 discarded 与新两列 =====


def test_policy_version_discarded_status__check_accepts(client):
    async def go(conn):
        policy_id = await conn.fetchval(
            "INSERT INTO policies (name, created_by) VALUES ('w4-discard', 3) RETURNING id"
        )
        await conn.execute(
            "INSERT INTO policy_versions (policy_id, version, body, status, created_by, "
            "source) VALUES ($1, 1, '{}'::jsonb, 'discarded', 3, 'agent')", policy_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO policy_versions (policy_id, version, body, status) "
                "VALUES ($1, 2, '{}'::jsonb, 'rolled_back')", policy_id,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO policy_versions (policy_id, version, body, status, "
                "created_by) VALUES ($1, 3, '{}'::jsonb, 'draft', 999999)", policy_id,
            )

    db(go)


# ===== 验收 25: 迁移 0008 降一步再升回, 结构与数据一致 =====


def test_migration_0008__downgrade_then_upgrade_roundtrip(client):
    """ADR-006 的既定做法。留一行任务与一行 discarded 版本, 验证存活数据与
    文档写明的两处有损行为 (discarded 回落 draft; idempotency_key 值不可恢复)。"""
    from alembic import command

    from app.db import alembic_config

    async def seed(conn):
        task_id = await insert_task(conn, input_hash="roundtrip", status="completed")
        policy_id = await conn.fetchval(
            "INSERT INTO policies (name, created_by) VALUES ('w4-roundtrip', 3) RETURNING id"
        )
        version_id = await conn.fetchval(
            "INSERT INTO policy_versions (policy_id, version, body, status, source) "
            "VALUES ($1, 1, '{}'::jsonb, 'discarded', 'agent') RETURNING id", policy_id,
        )
        return task_id, version_id

    task_id, version_id = db(seed)

    cfg = alembic_config()
    # 显式降到上一版, 不用相对的 "-1" (0009 出现后 "-1" 就不再指本迁移,
    # W3 的 0007 往返测试踩过这个坑)
    command.downgrade(cfg, "0007_policy_lifecycle")

    async def check_downgraded(conn):
        # 列与表回到 0007 的样子
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agent_tasks' AND column_name = 'idempotency_key')"
        )
        for col in ("input_hash", "runner_id", "heartbeat_at", "next_seq", "error_detail"):
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'agent_tasks' AND column_name = $1)", col,
            )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'agent_clarifications')"
        )
        # UNIQUE 以 0001 的原名还原成约束 (不是索引)
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'agent_tasks_idempotency_key_key' AND contype = 'u')"
        )
        # user_id 回到可空
        assert (
            await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'agent_tasks' AND column_name = 'user_id'"
            )
            == "YES"
        )
        # 有损回落: discarded -> draft (就地注释写明的行为)
        assert (
            await conn.fetchval(
                "SELECT status FROM policy_versions WHERE id = $1", version_id
            )
            == "draft"
        )
        check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'policy_versions'::regclass AND contype = 'c'"
        )
        assert "discarded" not in check

    db(check_downgraded)

    command.upgrade(cfg, "head")

    async def check_upgraded(conn):
        for idx in (
            "agent_tasks_one_open", "agent_steps_task_seq",
            "agent_clarifications_one_pending",
        ):
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)", idx
            )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agent_tasks' AND column_name = 'idempotency_key')"
        )
        check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'policy_versions'::regclass AND contype = 'c'"
        )
        assert "discarded" in check
        # 数据还在: 任务行与版本行都活过一个来回
        assert await conn.fetchval(
            "SELECT status FROM agent_tasks WHERE id = $1", task_id
        ) == "completed"
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM policy_versions WHERE id = $1)", version_id
        )

    db(check_upgraded)
