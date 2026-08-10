"""迁移 0009 的往返与守卫 (SPEC-007 验收 21/22 + 第八节四件事的直接断言)。

与 test_agent_constraints 的 0008 往返同一路数: 显式版本号降一步、断言降级后的
结构、升回 head、断言数据活过一个来回。

变异测试的靶子: 把 upgrade 里"先查 NULL 行再收 NOT NULL"的 raise 删掉换成
DELETE, test_upgrade_with_null_task_id__aborts_without_touching_data 必须红。
"""
from __future__ import annotations

import asyncpg
import pytest
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

EVAL_TABLES = ("eval_cases", "eval_runs", "eval_results")


# ===== 第八节各条在 head 上的直接断言 =====


def test_ai_usage_cost_column__renamed_to_cny(client):
    async def go(conn):
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ai_usage'"
            )
        }
        assert "estimated_cost_cny" in cols
        assert "estimated_cost_usd" not in cols

    db(go)


def test_step_with_null_task_id__db_rejects(client):
    async def go(conn):
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO agent_steps (task_id, seq, tool_name, status) "
                "VALUES (NULL, 1, 'list_zones', 'ok')"
            )

    db(go)


def test_eval_tables__dropped(client):
    async def go(conn):
        for table in EVAL_TABLES:
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = $1)", table,
            )

    db(go)


def test_clarification_missing_slots__column_is_text_array(client):
    async def go(conn):
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'agent_clarifications' "
            "AND column_name = 'missing_slots'"
        )
        assert row is not None
        assert row["data_type"] == "ARRAY"
        # 可空是为了不动历史行; 新行非空由 service 层保证 (test_agent_service)
        assert row["is_nullable"] == "YES"

    db(go)


# ===== 验收 21: 降一步再升回, 结构一致; 数据活过一个来回 =====


def test_migration_0009__downgrade_then_upgrade_roundtrip(client):
    from alembic import command

    from app.db import alembic_config

    async def seed(conn):
        # 改名要保数据: 留一行带成本的 ai_usage
        usage_id = await conn.fetchval(
            "INSERT INTO ai_usage (task_id, model, estimated_cost_cny) "
            "VALUES (NULL, 'stub', 0.123456) RETURNING id"
        )
        task_id = await insert_task(conn, input_hash="rt-0009", status="completed")
        await conn.execute(
            "INSERT INTO agent_clarifications (task_id, asked_seq, question, answer, "
            "missing_slots) VALUES ($1, 1, '通知谁?', '后场主管', "
            "ARRAY['role','scope'])", task_id,
        )
        return usage_id, task_id

    usage_id, task_id = db(seed)

    cfg = alembic_config()
    # 显式降到上一版, 不用相对的 "-1" (后续迁移出现后 "-1" 就不再指本迁移)
    command.downgrade(cfg, "0008_agent_orchestration")

    async def check_downgraded(conn):
        # 三张 eval 表回来了, 形状是 0001 抄来的那份 DDL
        for table in EVAL_TABLES:
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = $1)", table,
            )
        # eval_results 的复合主键与两条外键都在 (基线不翻写 —— 抄漏约束的迁移
        # 不会自己报错, 这里替它报)
        constraint_types = {
            r["t"]
            for r in await conn.fetch(
                "SELECT contype::text AS t FROM pg_constraint "
                "WHERE conrelid = 'eval_results'::regclass"
            )
        }
        assert constraint_types == {"p", "f"}
        # 成本列回到旧名, 数据还在
        assert await conn.fetchval(
            "SELECT estimated_cost_usd FROM ai_usage WHERE id = $1", usage_id
        ) is not None
        # task_id 回到可空; missing_slots 列消失
        assert (
            await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'agent_steps' AND column_name = 'task_id'"
            )
            == "YES"
        )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agent_clarifications' "
            "AND column_name = 'missing_slots')"
        )

    db(check_downgraded)

    command.upgrade(cfg, "head")

    async def check_upgraded(conn):
        for table in EVAL_TABLES:
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = $1)", table,
            )
        # 数据活过一个来回: 成本值、澄清行的槽位 (降级删列是有损的, 但升回后列
        # 要在; 这里断言的是"值经历改名不丢"那半边)
        assert float(
            await conn.fetchval(
                "SELECT estimated_cost_cny FROM ai_usage WHERE id = $1", usage_id
            )
        ) == pytest.approx(0.123456)
        assert await conn.fetchval(
            "SELECT status FROM agent_tasks WHERE id = $1", task_id
        ) == "completed"

    db(check_upgraded)


# ===== 验收 22: 存在 NULL 行时升级报错中止, 不改数据 =====


def test_upgrade_with_null_task_id__aborts_without_touching_data(client):
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    command.downgrade(cfg, "0008_agent_orchestration")

    async def seed_null_row(conn):
        # 0008 上 task_id 可空, 裸插一行 NULL —— 模拟"历史上不知怎么来的行"
        await conn.execute(
            "INSERT INTO agent_steps (task_id, seq, tool_name, status) "
            "VALUES (NULL, 999, 'orphan', 'ok')"
        )

    db(seed_null_row)

    try:
        with pytest.raises(RuntimeError, match="task_id 为 NULL"):
            command.upgrade(cfg, "head")

        async def check_untouched(conn):
            # 报错中止, 不 DELETE、不填假值: 那行原样还在
            row = await conn.fetchrow(
                "SELECT task_id, tool_name FROM agent_steps WHERE seq = 999"
            )
            assert row is not None
            assert row["task_id"] is None and row["tool_name"] == "orphan"
            # 成本列改名排在 raise **之前**, 是这次中止里唯一"已经执行过"的 DDL,
            # 必须显式断言它随事务回滚 (SPEC-007 验收 22; 评审侧已用最小复现
            # 实跑确认 env.py 的单事务 + Postgres 事务性 DDL 确实回滚, 这里是
            # 把那次实跑固化成测试, 不是修 bug)。不显式断言的话只能靠 finally
            # 里那句 upgrade 间接兜着, 真出问题时报的是一句 column does not
            # exist, 看不出根因。
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ai_usage'"
                )
            }
            assert "estimated_cost_usd" in cols and "estimated_cost_cny" not in cols
            # 迁移没有走到后面几步: eval 表还在 (整个 upgrade 是一个事务, 部分
            # 生效比报错更糟)
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'eval_cases')"
            )

        db(check_untouched)
    finally:
        # 人工处置 (测试里即删除这行孤儿), 再把库升回 head, 不影响后续用例
        async def cleanup(conn):
            await conn.execute("DELETE FROM agent_steps WHERE seq = 999")

        db(cleanup)
        command.upgrade(cfg, "head")
