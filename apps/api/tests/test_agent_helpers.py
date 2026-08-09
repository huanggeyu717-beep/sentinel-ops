"""W4 agent 测试的共享夹具与帮助函数 (本文件自身不含测试)。

放在 test_agent_*.py 命名空间里而不是改 conftest.py: 文件边界约定 conftest 归
评审方, 本段只新建 test_agent_* 文件。夹具通过 import 进各测试模块生效。
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

# 清表顺序: TRUNCATE 要求被外键引用的表与引用它的表在同一条命令里 ——
# approvals.task_id -> agent_tasks, policy_publications.approval_id -> approvals,
# 所以这两张也一并列入 (conftest 的 clean_telemetry 本来每个用例也清它们)。
AGENT_TABLES = (
    "agent_steps", "agent_clarifications", "ai_usage",
    "agent_tasks", "approvals", "policy_publications",
)


def dsn() -> str:
    return os.environ["SENTINEL_DATABASE_URL"].replace("+asyncpg", "")


def db(coro_fn):
    """开一条裸 asyncpg 连接跑协程 (与 test_policy_constraints 同一形状)。"""
    async def go():
        conn = await asyncpg.connect(dsn())
        try:
            return await coro_fn(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def clean_agent_tables(client):
    """每个用例前清空 agent 相关表, 去重索引才不会跨用例误撞。"""
    async def go(conn):
        await conn.execute(f"TRUNCATE {', '.join(AGENT_TABLES)}")

    db(go)
    yield


async def insert_task(
    conn,
    user_id: int = 3,
    input_hash: str = "hash-default",
    status: str = "running",
    **cols,
) -> int:
    """裸 SQL 造一行 agent_tasks (绕过 service, 约束测试专用)。"""
    fields = {
        "user_id": user_id,
        "task_type": "policy_compile",
        "input": '{"text": "raw"}',
        "status": status,
        "input_hash": input_hash,
        **cols,
    }
    names = ", ".join(fields)
    placeholders = ", ".join(
        f"${i}::jsonb" if k == "input" else f"${i}"
        for i, k in enumerate(fields, start=1)
    )
    task_id: int = await conn.fetchval(
        f"INSERT INTO agent_tasks ({names}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return task_id
