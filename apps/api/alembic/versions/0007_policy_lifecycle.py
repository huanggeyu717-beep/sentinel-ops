"""W3 策略生命周期: 发布靠 NOT NULL 外键强制, 不靠应用层自觉 (SPEC-006 / ADR-007)

Revision ID: 0007_policy_lifecycle
Revises: 0006_positions

内容 (SPEC-006 第六节):
1. 新建 policy_publications: approval_id 是 NOT NULL 外键 —— 没有审批记录,
   "发布"这一行在物理上插不进去; partial unique index 保证每条策略最多一个生效版本;
2. approvals 补 requested_at / note 两列、"一个版本最多一条待决审批"的 partial unique
   index、"不得自己批自己"的 CHECK (数据库兜底, 应用层另有 403 + 审计, 见 ADR-007);
3. policy_versions.status 的 CHECK 调整: 去 rolled_back (回滚是撤销发布记录,
   不改版本状态), 加 awaiting_approval。0001 建表时该约束是匿名的, 由 Postgres
   自动命名, 这里从 pg_constraint 查出实际名字再 DROP, 不猜;
4. policy_runs 加 policy_id 冗余列与 (policy_id, fired_at) 索引 —— 按策略查触发
   历史是最常见的查询;
5. 删 policies.enabled: 它与 policy_publications 表达的是同一个事实
   ("这条策略生不生效"), 同一个事实存两份才叫冗余, 必须收口;
6. 种子: 第二个 manager (dana) + viewer 账号。第二个 manager 不是演示方便 ——
   manager 自己写的策略必须由另一个 manager 批, 单 manager 部署下这条路径死锁。

downgrade 完整还原结构; 数据上有两处有损, 已在对应位置注明
(awaiting_approval 状态回落、enabled 列的值不可恢复)。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_policy_lifecycle"
down_revision = "0006_positions"
branch_labels = None
depends_on = None

# 种子账号统一密码 "sentinel-demo" 的 bcrypt(cost 12) 哈希, 与 app/db.py 的
# _SEED_PASSWORD_HASH 同一份 (迁移不 import 应用代码, 所以拷贝字面量)。
_SEED_PASSWORD_HASH = "$2b$12$nqoRSMypSBq4CCcE3wrxFO2D0CxxYwvWTrl5sSyoJxFQ.69xOxbs."

_STATUS_CHECK_NEW = (
    "CHECK (status IN ('draft','validated','simulated',"
    "'awaiting_approval','published','rejected'))"
)
_STATUS_CHECK_OLD = (
    "CHECK (status IN ('draft','validated','simulated',"
    "'published','rejected','rolled_back'))"
)


def _status_check_name(table: str) -> str:
    """查出该表 status 列 CHECK 约束的实际名字 (0001 建表时是匿名约束)。

    scalar_one(): 找不到或找到多个都直接报错, 不静默猜一个。
    """
    return op.get_bind().execute(
        sa.text(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = CAST(:table AS regclass) AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%status%'
            """
        ),
        {"table": table},
    ).scalar_one()


def upgrade() -> None:
    # 1. 发布表: approval_id NOT NULL 外键 = "没批过就发不出去"由数据库保证 (ADR-007)
    op.execute("""
        CREATE TABLE policy_publications (
            id                bigserial PRIMARY KEY,
            policy_id         bigint NOT NULL REFERENCES policies(id),
            policy_version_id bigint NOT NULL REFERENCES policy_versions(id),
            approval_id       bigint NOT NULL REFERENCES approvals(id),
            published_by      bigint NOT NULL REFERENCES users(id),
            published_at      timestamptz NOT NULL DEFAULT now(),
            revoked_at        timestamptz,
            revoked_by        bigint REFERENCES users(id)
        )
    """)
    # 每条策略最多一个生效版本; "现在线上跑的是哪一版"永远只有一个答案
    op.execute("""
        CREATE UNIQUE INDEX policy_publications_one_active
            ON policy_publications (policy_id) WHERE revoked_at IS NULL
    """)

    # 2. approvals 补两列、一个索引、一条 CHECK (SPEC-006 第三节)
    op.execute("""
        ALTER TABLE approvals
            ADD COLUMN requested_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN note text
    """)
    # "待审批"用 decision IS NULL 表达, 不新增状态列
    op.execute("""
        CREATE UNIQUE INDEX approvals_one_pending
            ON approvals (policy_version_id) WHERE decision IS NULL
    """)
    # 数据库兜底的"不得自己批自己"; 应用层另有 403 + 可读的错误信息
    op.execute("""
        ALTER TABLE approvals ADD CONSTRAINT approvals_no_self_approve
            CHECK (decided_by IS NULL OR decided_by <> requested_by)
    """)

    # 3. policy_versions.status: 去 rolled_back, 加 awaiting_approval
    old_name = _status_check_name("policy_versions")
    op.execute(f'ALTER TABLE policy_versions DROP CONSTRAINT "{old_name}"')
    op.execute(
        "ALTER TABLE policy_versions ADD CONSTRAINT policy_versions_status_check "
        + _STATUS_CHECK_NEW
    )

    # 4. policy_runs 冗余 policy_id + 时间索引
    op.execute("ALTER TABLE policy_runs ADD COLUMN policy_id bigint REFERENCES policies(id)")
    op.execute("""
        UPDATE policy_runs SET policy_id = pv.policy_id
        FROM policy_versions pv WHERE pv.id = policy_runs.policy_version_id
    """)
    op.execute("ALTER TABLE policy_runs ALTER COLUMN policy_id SET NOT NULL")
    op.execute("""
        CREATE INDEX policy_runs_policy_fired_at
            ON policy_runs (policy_id, fired_at DESC)
    """)

    # 5. 删 policies.enabled: 与 policy_publications 撞同一个事实 (SPEC-006 第六节)
    op.execute("ALTER TABLE policies DROP COLUMN enabled")

    # 6. 种子: dana (第二个 manager) + viewer。
    # 先把序列抬到 100 以上再插: 本迁移在启动时先于 app/db.py 的 dev seed 执行,
    # 而 dev seed 用固定 id 1-3 插 admin/chris/alex —— 低位 id 必须给它留着,
    # 否则这里插出的 id=1 会让 admin 的 ON CONFLICT (id) DO NOTHING 静默失效。
    op.execute("""
        SELECT setval(pg_get_serial_sequence('users','id'),
                      GREATEST((SELECT COALESCE(max(id), 0) FROM users), 100))
    """)
    op.execute(f"""
        INSERT INTO users (email, password_hash, display_name)
        VALUES ('dana@example.com', '{_SEED_PASSWORD_HASH}', 'Dana Park'),
               ('viewer@example.com', '{_SEED_PASSWORD_HASH}', 'View Only')
        ON CONFLICT (email) DO NOTHING
    """)
    op.execute("""
        INSERT INTO user_roles (user_id, role_id)
        SELECT u.id, r.id
        FROM (VALUES ('dana@example.com', 'manager'),
                     ('viewer@example.com', 'viewer')) AS v(email, role_name)
        JOIN users u ON u.email = v.email
        JOIN roles r ON r.name = v.role_name
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # 6. 种子账号 (先删角色关联再删账号; 若已有审批/发布记录引用会被外键挡住,
    #    那种库应该 make reset 而不是降级)
    op.execute("""
        DELETE FROM user_roles WHERE user_id IN
            (SELECT id FROM users WHERE email IN ('dana@example.com', 'viewer@example.com'))
    """)
    op.execute("DELETE FROM users WHERE email IN ('dana@example.com', 'viewer@example.com')")

    # 5. 还原 enabled 列 (0001 的原始定义; 各行原值不可恢复, 一律回到默认 false)
    op.execute("ALTER TABLE policies ADD COLUMN enabled boolean NOT NULL DEFAULT false")

    # 4. policy_runs
    op.execute("DROP INDEX policy_runs_policy_fired_at")
    op.execute("ALTER TABLE policy_runs DROP COLUMN policy_id")

    # 3. status CHECK 还原成 0001 的取值集合。awaiting_approval 在旧集合里不存在,
    #    先回落成 simulated (有损, 语义上是"退回到提交审批之前")
    op.execute(
        "UPDATE policy_versions SET status = 'simulated' WHERE status = 'awaiting_approval'"
    )
    op.execute("ALTER TABLE policy_versions DROP CONSTRAINT policy_versions_status_check")
    op.execute(
        "ALTER TABLE policy_versions ADD CONSTRAINT policy_versions_status_check "
        + _STATUS_CHECK_OLD
    )

    # 2. approvals
    op.execute("ALTER TABLE approvals DROP CONSTRAINT approvals_no_self_approve")
    op.execute("DROP INDEX approvals_one_pending")
    op.execute("ALTER TABLE approvals DROP COLUMN note, DROP COLUMN requested_at")

    # 1. 发布表
    op.execute("DROP TABLE policy_publications")
