"""W3 验收 13, 15-20: 数据库层不变量与迁移回滚。对应 docs/specs/SPEC-006 / ADR-007。

这几条比端到端更值钱: 端到端只证明"正常路径走得通", 这里**绕过应用层直接写库**,
证明"异常路径走不通" —— 不变量 1 的分量全在这里。全部用 asyncpg 裸连接,
不经过任何 service 代码。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest


def dsn() -> str:
    return os.environ["SENTINEL_DATABASE_URL"].replace("+asyncpg", "")


def db(coro_fn):
    """开一条裸 asyncpg 连接跑协程。"""
    async def go():
        conn = await asyncpg.connect(dsn())
        try:
            return await coro_fn(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


async def _seed_version(conn, name: str = "raw") -> tuple[int, int]:
    """裸 SQL 造一个 policy + version, 返回 (policy_id, version_id)。"""
    policy_id = await conn.fetchval(
        "INSERT INTO policies (name, created_by) VALUES ($1, 3) RETURNING id", name
    )
    version_id = await conn.fetchval(
        "INSERT INTO policy_versions (policy_id, version, body, status) VALUES "
        "($1, 1, '{}'::jsonb, 'draft') RETURNING id", policy_id,
    )
    return policy_id, version_id


async def _approved(conn, version_id: int) -> int:
    return await conn.fetchval(
        "INSERT INTO approvals (policy_version_id, requested_by, decided_by, decision, "
        "decided_at) VALUES ($1, 3, 2, 'approved', now()) RETURNING id", version_id,
    )


# ===== 验收 15/16: 没有审批, 发布这一行物理上插不进去 =====

def test_publication_with_null_approval__db_rejects(client):
    """核心主张: approval_id 是 NOT NULL 外键, 绕过应用层直接插也插不进去。"""
    async def go(conn):
        policy_id, version_id = await _seed_version(conn)
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO policy_publications (policy_id, policy_version_id, "
                "approval_id, published_by) VALUES ($1, $2, NULL, 2)",
                policy_id, version_id,
            )

    db(go)


def test_publication_with_nonexistent_approval__db_rejects(client):
    async def go(conn):
        policy_id, version_id = await _seed_version(conn)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO policy_publications (policy_id, policy_version_id, "
                "approval_id, published_by) VALUES ($1, $2, 999999, 2)",
                policy_id, version_id,
            )

    db(go)


# ===== 验收 17: 每条策略最多一个生效版本 =====

def test_second_active_publication__unique_index_rejects(client):
    async def go(conn):
        policy_id, version_id = await _seed_version(conn)
        approval_id = await _approved(conn, version_id)
        await conn.execute(
            "INSERT INTO policy_publications (policy_id, policy_version_id, approval_id, "
            "published_by) VALUES ($1, $2, $3, 2)", policy_id, version_id, approval_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO policy_publications (policy_id, policy_version_id, "
                "approval_id, published_by) VALUES ($1, $2, $3, 2)",
                policy_id, version_id, approval_id,
            )
        # 撤销之后可以再发布 (partial index 只约束 revoked_at IS NULL 的行)
        await conn.execute(
            "UPDATE policy_publications SET revoked_at = now(), revoked_by = 2 "
            "WHERE policy_id = $1 AND revoked_at IS NULL", policy_id,
        )
        await conn.execute(
            "INSERT INTO policy_publications (policy_id, policy_version_id, approval_id, "
            "published_by) VALUES ($1, $2, $3, 2)", policy_id, version_id, approval_id,
        )

    db(go)


# ===== 验收 18: 不得自己批自己 (数据库兜底层; 应用层 403 见 test_policy_service) =====

def test_self_approval_via_raw_sql__check_rejects(client):
    async def go(conn):
        _, version_id = await _seed_version(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO approvals (policy_version_id, requested_by, decided_by, "
                "decision, decided_at) VALUES ($1, 3, 3, 'approved', now())", version_id,
            )

    db(go)


# ===== 验收 19: 一个版本同时最多一条待决审批 =====

def test_second_pending_approval__unique_index_rejects(client):
    async def go(conn):
        _, version_id = await _seed_version(conn)
        await conn.execute(
            "INSERT INTO approvals (policy_version_id, requested_by) VALUES ($1, 3)",
            version_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO approvals (policy_version_id, requested_by) VALUES ($1, 2)",
                version_id,
            )

    db(go)


# ===== 验收 20: 迁移 0007 降一步再升回, 结构与数据一致 =====

def test_migration_0007__downgrade_then_upgrade_roundtrip(client):
    """ADR-006 的既定做法。留一行草稿版本验证存活数据不受损。

    注意: downgrade 会删掉 0007 种下的 dana/viewer, 升回时按新的序列 id 重建 ——
    邮箱不变, 按邮箱找人的用例不受影响 (本套测试不缓存这两个账号的 token)。
    """
    from alembic import command

    from app.db import alembic_config

    async def seed(conn):
        return await _seed_version(conn, name="survives-roundtrip")

    policy_id, version_id = db(seed)

    cfg = alembic_config()
    # 显式降到 0007 的上一版, 不用相对的 "-1" —— 那只在 0007 自己是 head 时成立,
    # 0008 落地后 "-1" 降的是别人 (W4 第一段发现并修正)
    command.downgrade(cfg, "0006_positions")

    async def check_downgraded(conn):
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'policy_publications')"
        )
        # 原样还回去: enabled 列回来了, status CHECK 回到含 rolled_back 的旧集合
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'policies' AND column_name = 'enabled')"
        )
        old_check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'policy_versions'::regclass AND contype = 'c'"
        )
        assert "rolled_back" in old_check and "awaiting_approval" not in old_check

    db(check_downgraded)

    command.upgrade(cfg, "head")

    async def check_upgraded(conn):
        # 结构还在
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'policy_publications_one_active')"
        )
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'approvals' AND column_name = 'requested_at')"
        )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'policies' AND column_name = 'enabled')"
        )
        new_check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'policy_versions'::regclass AND contype = 'c'"
        )
        assert "awaiting_approval" in new_check and "rolled_back" not in new_check
        # 数据还在: 降级/升级不碰既有行
        row = await conn.fetchrow(
            "SELECT policy_id, status FROM policy_versions WHERE id = $1", version_id
        )
        assert row["policy_id"] == policy_id and row["status"] == "draft"
        # 种子账号回来了
        for email in ("dana@example.com", "viewer@example.com"):
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM users WHERE email = $1)", email
            )

    db(check_upgraded)


# ===== 验收 13: 删干净的证明 (范围口径照抄 SPEC-004 的旧占位请求头零残留断言) =====

def test_w2_hardcoded_rules__zero_residue_in_code_and_tests():
    """删掉的联动函数与稳定窗口配置项在代码与测试中零残留 (needle 拆开拼接,
    免得本文件自己成为命中项)。

    范围只有代码与测试 —— SPEC-003/006 的文档正文里这两个词本来就有,
    断言"仓库里零残留"必然失败 (SPEC-006 验收 13 特别注明)。
    """
    repo_root = Path(__file__).resolve().parents[3]
    for needle in ("apply_" + "sensor_state", "auto_resolve_" + "dry_seconds"):
        result = subprocess.run(
            # -I 跳过二进制 (__pycache__ 里旧字节码的历史命中不算残留)
            ["grep", "-riEnI", "--exclude-dir=__pycache__", needle,
             "apps/api/app", "apps/api/tests", "apps/device-sim", "packages"],
            cwd=repo_root, capture_output=True, text=True,
        )
        assert result.returncode == 1, f"{needle} 仍有残留:\n{result.stdout or result.stderr}"
