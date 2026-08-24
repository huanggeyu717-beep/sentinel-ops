"""W6 事故报告: incident_reports 表与 agent_tasks 的两处 CHECK (SPEC-008 第七节)

Revision ID: 0011_incident_reports
Revises: 0010_deploy_guardrails

内容 (编号对应 SPEC-008 第七节):
1. 新建 incident_reports —— 报告草稿。body 存占位符原文, fact_pack 存生成那一刻的
   事实包**快照** (列注释里写明为什么不重算); 两个计数列是第三节那两个"被拦下的
   倾向"数的落点, 按违规项累加, 刻意是列不是 jsonb (要摆出来的数不藏在 json 里);
2. partial unique index incident_reports_one_active —— 一条事故同时只有一份
   非 discarded 的报告。约束下沉数据库, 本项目第四次用这一手 (W2 事故去重、
   W4 agent_tasks_one_open、SPEC-009 花钱护栏之后);
3. agent_tasks.task_type 补 CHECK —— 0001 里它只有一句注释, 拼错一个字母不会有
   任何东西报错 (SPEC-008 第九节矛盾 3);
4. agent_tasks.status 的 CHECK 加 awaiting_review —— 报告等的是"过目"不是"审批",
   不复用 awaiting_approval (SPEC-008 第五节)。0001 里这条是匿名约束, 名字从
   pg_constraint 查出来再 DROP, 不猜。

**故意的不对称: `stage` 列没有 CHECK, 也不补** (SPEC-008 第七节第 4 条)。
阶段名会随能力开关增减 (SPEC-007 的五臂就在增减它), 锁死它等于每加一个阶段
就要一次迁移; 本次新增的 collecting / drafting 两个阶段名因此不动任何约束。
status 补、stage 不补, 是取舍不是遗漏。

downgrade 一步降回再升回结构一致 (ADR-006)。数据上有损的两处在对应位置注明
(awaiting_review 状态回落、incident_reports 的行随表消失)。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_incident_reports"
down_revision = "0010_deploy_guardrails"
branch_labels = None
depends_on = None

# agent_tasks.status 的新旧取值集合。旧集合 = 0001 建表时定的; 新集合加 awaiting_review。
_STATUS_CHECK_NEW = (
    "CHECK (status IN ('running','awaiting_approval','awaiting_review','clarifying',"
    "'completed','failed','rejected','dead_letter'))"
)
_STATUS_CHECK_OLD = (
    "CHECK (status IN ('running','awaiting_approval','clarifying',"
    "'completed','failed','rejected','dead_letter'))"
)


def _constraint_name(table: str, contype: str, needle: str) -> str:
    """查出该表上匹配的约束的实际名字, 不猜。

    与 0007/0008 的同名函数是同一个写法, 在本迁移里**刻意再写一份**:
    迁移之间不共享代码 —— 每个迁移锁住的是它执行那一刻的库状态, 共享的帮助函数
    一旦为新迁移改动, 旧迁移在全新库上的行为就会跟着变, 那正是迁移最不能有的事。

    scalar_one(): 找不到或找到多个都直接报错, 不静默猜一个。
    """
    return op.get_bind().execute(
        sa.text(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = CAST(:table AS regclass) AND contype = :contype
              AND pg_get_constraintdef(oid) LIKE :needle
            """
        ),
        {"table": table, "contype": contype, "needle": needle},
    ).scalar_one()


def upgrade() -> None:
    # 1: 报告表。status 的 CHECK 起名字 (0001 的匿名约束就是本迁移下面要费劲去查的
    # 那种, 不再造一个); 两个计数列 NOT NULL DEFAULT 0 —— "没拦过"是 0, 不是 NULL。
    op.execute("""
CREATE TABLE incident_reports (
    id bigserial PRIMARY KEY,
    incident_id bigint NOT NULL REFERENCES incidents(id),
    task_id bigint NOT NULL REFERENCES agent_tasks(id),
    body jsonb NOT NULL,
    fact_pack jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft'
        CONSTRAINT incident_reports_status_check
        CHECK (status IN ('draft','final','discarded')),
    bare_fact_attempts    int NOT NULL DEFAULT 0,
    dangling_ref_attempts int NOT NULL DEFAULT 0,
    created_by bigint NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finalized_by bigint REFERENCES users(id),
    finalized_at timestamptz
)""")
    op.execute("""
COMMENT ON COLUMN incident_reports.fact_pack IS
    '生成那一刻的事实包快照, 渲染一律读这里, 不重算 —— 事故时间线以后不会变, '
    '但算事实包的代码会变; 不存快照, 半年后重渲染同一份报告会得到不同的数字, '
    '那它就不是"当时那一份"了 (SPEC-008 第七节第 2 条)'
""")
    op.execute("""
COMMENT ON COLUMN incident_reports.body IS
    '五个字段的结构化正文, 存占位符原文 ({{fact_id}}), 渲染后的中文由代码从 '
    'fact_pack 现算 (SPEC-008 第一节)'
""")
    op.execute("""
COMMENT ON COLUMN incident_reports.bare_fact_attempts IS
    '想裸写事实、被 E_BARE_FACT 拦下的**项数** (一轮三处裸写加 3, 不按轮计), '
    '是倾向计数不是事故 (SPEC-008 第三节)'
""")
    op.execute("""
COMMENT ON COLUMN incident_reports.dangling_ref_attempts IS
    '引用不存在的事实 id、被 E_DANGLING_REF 拦下的项数, 计数口径同 bare_fact_attempts'
""")

    # 2: 一条事故同时只有一份非 discarded 的报告 (final 也占位 —— 弃掉才能重开)
    op.execute("""
        CREATE UNIQUE INDEX incident_reports_one_active
            ON incident_reports (incident_id) WHERE status <> 'discarded'
    """)

    # 3: task_type 从注释变约束。既有行全是 policy_compile (0008 起 service 只写
    # 这一种), 真有拼错的历史行会在这里当场报错 —— 那正是加约束想要的效果。
    op.execute("""
        ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_task_type_check
            CHECK (task_type IN ('policy_compile','incident_report'))
    """)

    # 4: status CHECK 加 awaiting_review。0001 的匿名约束按名字 drop。
    old_name = _constraint_name("agent_tasks", "c", "%status%")
    op.execute(f'ALTER TABLE agent_tasks DROP CONSTRAINT "{old_name}"')
    op.execute(
        "ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_status_check "
        + _STATUS_CHECK_NEW
    )


def downgrade() -> None:
    # 4: awaiting_review 在旧集合里不存在, 先回落。落 failed (有损): 降级后报告表
    # 已不存在, 等人过目的任务既走不下去也定不了稿, 按"没走完就被终止"处置。
    op.execute("UPDATE agent_tasks SET status = 'failed' WHERE status = 'awaiting_review'")
    check_name = _constraint_name("agent_tasks", "c", "%status%")
    op.execute(f'ALTER TABLE agent_tasks DROP CONSTRAINT "{check_name}"')
    op.execute(
        "ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_status_check "
        + _STATUS_CHECK_OLD
    )

    # 3
    op.execute("ALTER TABLE agent_tasks DROP CONSTRAINT agent_tasks_task_type_check")

    # 2 + 1: 索引随表一起删。行随表消失 (有损): 报告是草稿产物, 事实源
    # (incidents / incident_events) 原样还在, 丢的只是模型写的那几段字与两个计数。
    op.execute("DROP TABLE incident_reports")
