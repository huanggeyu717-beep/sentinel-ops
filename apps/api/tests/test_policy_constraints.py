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

def test_migration_0007__downgrade_then_upgrade_roundtrip(client, preserve_users):
    """ADR-006 的既定做法。留一行草稿版本验证存活数据不受损。

    downgrade 会删掉 0007 种下的 dana/viewer, 升回时按序列重插 —— 邮箱不变,
    id 会变, 而 session 级的 auth_headers 缓存的 token 里存的是 id。所以整个
    往返包在 preserve_users 里, 结束后按原 id 复原这两行 (机制与"为什么只在
    全新库上炸"见 conftest.preserve_users 的 docstring)。
    """
    from alembic import command

    from app.db import alembic_config

    async def seed(conn):
        return await _seed_version(conn, name="survives-roundtrip")

    policy_id, version_id = db(seed)

    async def seed_user_ids(conn):
        return {
            r["email"]: r["id"] for r in await conn.fetch(
                "SELECT email, id FROM users "
                "WHERE email IN ('dana@example.com', 'viewer@example.com')"
            )
        }

    ids_before = db(seed_user_ids)

    cfg = alembic_config()
    with preserve_users():
        # 显式降到 0007 的上一版, 不用相对的 "-1" —— 那只在 0007 自己是 head 时
        # 成立, 0008 落地后 "-1" 降的是别人 (W4 第一段发现并修正)
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
    # 且 id 与往返前逐一相同 (preserve_users 复原的结果) —— id 变了, session 级
    # 缓存里已签发的 token 就全是悬空引用
    assert db(seed_user_ids) == ids_before


# ===== 回归: 0007 往返不许作废已签发的 token (CI 全新库 viewer 401) =====

def test_cached_token_survives_0007_roundtrip__with_higher_id_user_present(
    client, viewer_headers, preserve_users
):
    """把 preserve_users 的复原逻辑回退掉, 本用例必须红 —— 且在常驻库上也红。

    上一条用例的 id 断言只在全新库上抓得到这个缺陷: 常驻库上 dana/viewer 恰好
    已收敛到序列最大值, 删掉重插会落回原 id, "没复原"与"复原了"长得一模一样。
    这里先插一个高位 id 的用户把那个巧合破坏掉, 再走同一段往返, 断言三件事:
    dana/viewer 的 id 不变、往返前签发的 viewer token 仍然能过 /auth/me、
    **而且它还带着 viewer 角色** —— 第二件正是 CI 上 test_reports_http 401 的
    受害现场 (机制见 conftest.preserve_users)。

    第三件是 2026-08-24 评审补的, 少了它这条用例分不清两种世界 (变异 P3):
    复原了行、没复原 user_roles 时, viewer 回来了、id 也对、`/auth/me` 照样
    200 (那个端点不要求任何权限点), **但他一个角色都没有**。而下游那条本该
    抓住这件事的用例 `test_report_permissions__viewer_reads_but_cannot_write`
    期望的是 **403** —— 一个没有角色的用户拿到的**也是 403**。
    两种世界产出同一个状态码, 判据分不开它们, 于是"权限正确地被拒绝"与
    "这个人的权限没了"长成一个样。**这是本项目那条主线在角色表上的又一次现形。**
    权限门那一侧要断言的是**能做成一件需要权限的事**, 不是"还能登录"。
    """
    from alembic import command

    from app.db import alembic_config

    async def add_high_id_user(conn):
        return await conn.fetchval(
            "INSERT INTO users (email, password_hash, display_name) "
            "VALUES ('scratch-cached-token@example.com', 'x', 'Scratch') RETURNING id"
        )

    async def seed_user_ids(conn):
        return {
            r["email"]: r["id"] for r in await conn.fetch(
                "SELECT email, id FROM users "
                "WHERE email IN ('dana@example.com', 'viewer@example.com')"
            )
        }

    scratch_id = db(add_high_id_user)
    try:
        ids_before = db(seed_user_ids)
        # 前提自检: 巧合确实被破坏了 (走序列插的行 id 必须比两个种子账号都大)
        assert scratch_id > max(ids_before.values())

        cfg = alembic_config()
        with preserve_users():
            command.downgrade(cfg, "0006_positions")
            command.upgrade(cfg, "head")

        assert db(seed_user_ids) == ids_before
        # 还认得这个人 (/auth/me 不要求权限点)
        assert client.get("/auth/me", headers=viewer_headers).status_code == 200
        # 而且角色还在: /incidents 要 PERM_READ, 角色没复原的话这里 403 (变异 P3)
        assert client.get("/incidents", headers=viewer_headers).status_code == 200
    finally:
        async def cleanup(conn):
            await conn.execute("DELETE FROM users WHERE id = $1", scratch_id)

        db(cleanup)


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
