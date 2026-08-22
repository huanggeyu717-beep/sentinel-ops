"""种子的两种形态 (SPEC-009 第一节第 5 条、第三节; W6 第二段易错点三)。

易错点三的原话: demo_marker 那一行**不许长到开发库里** —— 否则重置脚本
(一段会删数据的代码) 的护栏对开发库也放行。所以一正一反两条:
默认配置跑种子, 通行证必须是空的; 开关打开跑种子, 通行证在、admin 不在。

变异靶子: 把 apply_dev_seed 里的 if 判据换成 environment != 'development'
之类的间接判据, 或无条件写 marker, 第一条就红。
"""
from __future__ import annotations

import asyncio

from test_agent_helpers import db

from app.config import settings
from app.db import ADMIN_SEED_SQL, apply_dev_seed


def _counts() -> dict:
    async def go(conn):
        return {
            "marker": await conn.fetchval("SELECT count(*) FROM demo_marker"),
            "admin": await conn.fetchval(
                "SELECT count(*) FROM users WHERE email = 'admin@example.com'"
            ),
            "operators": await conn.fetchval(
                "SELECT count(*) FROM users "
                "WHERE email IN ('chris@example.com', 'alex@example.com')"
            ),
        }

    return db(go)


def test_default_seed__no_demo_marker_and_admin_present(client):
    """开发/测试形态 (默认): 有 admin, 没有通行证。"""
    asyncio.run(apply_dev_seed())  # conftest 每用例清 demo_marker, 这里现跑一遍种子
    got = _counts()
    assert got["marker"] == 0, "通行证长到非演示库里, 重置护栏对开发库也放行了"
    assert got["admin"] == 1
    assert got["operators"] == 2


def test_demo_seed__writes_marker_and_skips_admin(client, monkeypatch):
    """公开演示形态: 通行证在 (幂等), admin 不在; 演示账号照常。"""

    async def drop_admin(conn):
        # 测试库的会话级种子已经种过 admin (默认形态), 先摘掉才能断言
        # "演示形态不会把它种回来"。角色关联先删 (外键)。
        await conn.execute(
            "DELETE FROM user_roles WHERE user_id IN "
            "(SELECT id FROM users WHERE email = 'admin@example.com')"
        )
        await conn.execute("DELETE FROM users WHERE email = 'admin@example.com'")

    db(drop_admin)
    monkeypatch.setattr(settings(), "apply_demo_marker", True)
    try:
        asyncio.run(apply_dev_seed())
        got = _counts()
        assert got["marker"] == 1
        assert got["admin"] == 0, "演示库不该有 admin (SPEC-009 第一节第 5 条)"
        assert got["operators"] == 2  # 演示账号照常, 面试官能登录

        asyncio.run(apply_dev_seed())  # 再跑一遍: 单行表撞上已有行, 幂等
        assert _counts()["marker"] == 1

        async def note(conn):
            return await conn.fetchval("SELECT note FROM demo_marker")

        assert "demo" in str(db(note))
    finally:
        # 还回 admin, 不让本用例改变后续用例看到的种子形态
        db(lambda conn: conn.execute(ADMIN_SEED_SQL))
        assert _counts()["admin"] == 1


def test_demo_seed__marker_row_passes_reset_guard_shape(client, monkeypatch):
    """种子写出的那一行正是重置脚本要验的通行证: marked_at 非空、可被 SELECT。

    (重置脚本本体的护栏在 test_reset_script; 这里只钉"种子写的行"与"护栏读的行"
    是同一形状, 两边不会各说各话。)
    """
    monkeypatch.setattr(settings(), "apply_demo_marker", True)
    asyncio.run(apply_dev_seed())

    async def go(conn):
        return await conn.fetchrow("SELECT marked_at, note FROM demo_marker")

    row = db(go)
    assert row is not None and row["marked_at"] is not None
