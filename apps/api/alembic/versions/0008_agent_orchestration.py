"""W4 Agent 编排: 租约、去重、时间线编号与澄清表 (SPEC-002 第七节, 十一件)

Revision ID: 0008_agent_orchestration
Revises: 0007_policy_lifecycle

内容 (编号对应 SPEC-002 第七节那张表):
1.  agent_tasks 加 error_detail —— 死信的人话解释, 不塞进某条 agent_steps;
2.  agent_tasks 加 input_hash —— 归一化输入的哈希, 去重用 (归一化规则见
    agent_service.normalize_input 的 docstring);
3.  删掉 idempotency_key 整列 —— 它与 input_hash 表达同一个事实, 同一个事实
    存两份才叫冗余 (policies.enabled 在 0007 就是因为这个被删的)。它身上那个
    UNIQUE 是**约束不是索引** (DROP INDEX 会失败), 名字从 pg_constraint 查;
4.  部分唯一索引 agent_tasks_one_open —— **同一个用户同一句话, 最多只有一条
    还没走完的任务** (不是"最多一条 running": 条件含 clarifying, 否则老任务停在
    澄清等回答时重说一遍会开出第二个任务, 老的永远挂着没人收尸);
5.  agent_tasks 加 runner_id + heartbeat_at —— 租约 (SPEC-002 第二节);
6.  agent_tasks 加 next_seq —— agent_steps 与 agent_clarifications 两张表共用的
    时间线编号: 表按东西的种类分, 编号按时间线统一 (SPEC-002 第三节);
7.  user_id 设 NOT NULL —— 部分唯一索引里 NULL 永远不冲突, 可空等于去重防线
    对空 user 静默失效。先断言表内无 NULL 行再加约束;
8.  agent_steps 加 (task_id, seq) 唯一索引 —— SSE 按 seq 断点续传, seq 重复或
    跳号只在断线时暴露, 平时测不到, 所以由数据库封死; 顺带当查询索引;
9.  新建 agent_clarifications + "一个任务同时最多一个未回答问题"的部分唯一索引
    (与 0007 的 approvals_one_pending 逐字同构, 约束下沉数据库第五次应用);
10. policy_versions 加 created_by 与 source —— 已实测这张表六列里两列都不存在;
11. policy_versions.status 的 CHECK 加 discarded —— 失败任务的草稿标它,
    不复用 rejected (那个词已经是"人审批时否决"的意思, 一词两义会让 W5 分不清
    "人否决"和"模型没编译对")。

downgrade 完整还原结构; 数据上有损的两处 (idempotency_key 的值、discarded 状态
回落) 在对应位置注明, 照 0007 的做法。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_agent_orchestration"
down_revision = "0007_policy_lifecycle"
branch_labels = None
depends_on = None

# policy_versions.status 的新旧取值集合。旧集合 = 0007 定的; 新集合加 discarded。
_STATUS_CHECK_NEW = (
    "CHECK (status IN ('draft','validated','simulated',"
    "'awaiting_approval','published','rejected','discarded'))"
)
_STATUS_CHECK_OLD = (
    "CHECK (status IN ('draft','validated','simulated',"
    "'awaiting_approval','published','rejected'))"
)


def _constraint_name(table: str, contype: str, needle: str) -> str:
    """查出该表上匹配的约束的实际名字, 不猜。

    与 0007 的 _status_check_name() 是同一个写法, 在本迁移里**刻意再写一份**:
    迁移之间不共享代码 —— 每个迁移锁住的是它执行那一刻的库状态, 共享的帮助函数
    一旦为新迁移改动, 旧迁移在全新库上的行为就会跟着变, 那正是迁移最不能有的事。

    scalar_one(): 找不到或找到多个都直接报错, 不静默猜一个 —— 这条库在不同人
    手上走过的路径不一样, 断言比假设便宜。
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
    # 1 + 2: 死信解释 与 去重哈希
    op.execute("""
        ALTER TABLE agent_tasks
            ADD COLUMN error_detail text,
            ADD COLUMN input_hash text
    """)

    # 3: 删 idempotency_key。它的 UNIQUE 是约束不是索引 —— 名字从 pg_constraint
    # 查出来再 DROP (0007 立的规矩)。DROP COLUMN 本身也会连带删掉这个约束,
    # 但显式查一遍等于断言"库确实处在我以为的状态", 对不上会当场报错而不是静默继续。
    uq_name = _constraint_name("agent_tasks", "u", "%idempotency_key%")
    op.execute(f'ALTER TABLE agent_tasks DROP CONSTRAINT "{uq_name}"')
    op.execute("ALTER TABLE agent_tasks DROP COLUMN idempotency_key")

    # 5 + 6: 租约两列 与 时间线发号器 (先于索引 4 无关, 但列要在用它的代码之前齐全)
    op.execute("""
        ALTER TABLE agent_tasks
            ADD COLUMN runner_id text,
            ADD COLUMN heartbeat_at timestamptz,
            ADD COLUMN next_seq int NOT NULL DEFAULT 0
    """)

    # 7: user_id NOT NULL。部分唯一索引对 NULL 永远不冲突, 可空 = 防线静默失效。
    # 先断言无 NULL 行: 目前这张表在所有已知库上都是空的, 但迁移要能在任何一台
    # 机器上跑 —— 真有 NULL 行时报错停下, 由人决定怎么补, 不静默 DELETE。
    null_rows = op.get_bind().execute(
        sa.text("SELECT count(*) FROM agent_tasks WHERE user_id IS NULL")
    ).scalar_one()
    if null_rows:
        raise RuntimeError(
            f"agent_tasks 里有 {null_rows} 行 user_id 为 NULL, 无法加 NOT NULL 约束; "
            "请先人工处置这些行再重跑迁移"
        )
    op.execute("ALTER TABLE agent_tasks ALTER COLUMN user_id SET NOT NULL")

    # 4: 同一个用户同一句话, 最多只有一条还没走完的任务。
    # 条件必须含 clarifying (不只 running), 四种情况的实测记录见 SPEC-002 第七节。
    op.execute("""
        CREATE UNIQUE INDEX agent_tasks_one_open
            ON agent_tasks (user_id, input_hash)
         WHERE status IN ('running', 'clarifying')
    """)

    # 8: seq 的唯一性由数据库封死 (断点续传 bug 只在断线时冒出来, 平时测不到)
    op.execute("CREATE UNIQUE INDEX agent_steps_task_seq ON agent_steps (task_id, seq)")

    # 9: 澄清表。一次澄清一行, 问题与回答同一行; asked_seq/answered_seq 用
    # agent_tasks.next_seq 发的号, 与 agent_steps 排进同一条时间线。
    op.execute("""
        CREATE TABLE agent_clarifications (
            id            bigserial PRIMARY KEY,
            task_id       bigint NOT NULL REFERENCES agent_tasks(id),
            asked_seq     int NOT NULL,
            question      text NOT NULL,
            asked_at      timestamptz NOT NULL DEFAULT now(),
            answered_seq  int,
            answer        text,
            answered_by   bigint REFERENCES users(id),
            answered_at   timestamptz
        )
    """)
    # 一个任务同时最多一个没被回答的问题 —— 与 approvals_one_pending 逐字同构
    op.execute("""
        CREATE UNIQUE INDEX agent_clarifications_one_pending
            ON agent_clarifications (task_id) WHERE answer IS NULL
    """)

    # 10: 版本的归属与来源。created_by 记发起的人 (Agent 不是另一个作者, 是这个人
    # 手里的工具); source 记 human/agent, W5 评测要靠它把两类草稿分开。
    # 既有行: source 回填 human 是事实 (Agent 在这之前不存在); created_by 留 NULL
    # (W3 的 INSERT 没记版本作者, 猜一个不如承认不知道), 所以两列都不设 NOT NULL。
    op.execute("""
        ALTER TABLE policy_versions
            ADD COLUMN created_by bigint REFERENCES users(id),
            ADD COLUMN source text
    """)
    op.execute("UPDATE policy_versions SET source = 'human'")

    # 11: status CHECK 加 discarded。0007 给它起过名字, 但仍从 pg_constraint 查 ——
    # 不硬编码, 这条库在不同人手上走过的路径不一样。
    check_name = _constraint_name("policy_versions", "c", "%status%")
    op.execute(f'ALTER TABLE policy_versions DROP CONSTRAINT "{check_name}"')
    op.execute(
        "ALTER TABLE policy_versions ADD CONSTRAINT policy_versions_status_check "
        + _STATUS_CHECK_NEW
    )


def downgrade() -> None:
    # 11: discarded 在旧集合里不存在, 先回落成 draft (有损: 进入 discarded 前
    # 可能是 draft/validated/simulated 中任何一个, 语义上统一"退回工作台")
    op.execute("UPDATE policy_versions SET status = 'draft' WHERE status = 'discarded'")
    check_name = _constraint_name("policy_versions", "c", "%status%")
    op.execute(f'ALTER TABLE policy_versions DROP CONSTRAINT "{check_name}"')
    op.execute(
        "ALTER TABLE policy_versions ADD CONSTRAINT policy_versions_status_check "
        + _STATUS_CHECK_OLD
    )

    # 10
    op.execute("ALTER TABLE policy_versions DROP COLUMN source, DROP COLUMN created_by")

    # 9 (partial unique index 随表一起删)
    op.execute("DROP TABLE agent_clarifications")

    # 8
    op.execute("DROP INDEX agent_steps_task_seq")

    # 4
    op.execute("DROP INDEX agent_tasks_one_open")

    # 7
    op.execute("ALTER TABLE agent_tasks ALTER COLUMN user_id DROP NOT NULL")

    # 6 + 5
    op.execute("""
        ALTER TABLE agent_tasks
            DROP COLUMN next_seq,
            DROP COLUMN heartbeat_at,
            DROP COLUMN runner_id
    """)

    # 3: 还原列与 UNIQUE (值不可恢复, 一律回到 NULL —— 有损; 该列在 0008 之后
    # 本来就没人写)。约束由 Postgres 自动命名, 恰好还原成 0001 时的
    # agent_tasks_idempotency_key_key。
    op.execute("ALTER TABLE agent_tasks ADD COLUMN idempotency_key text UNIQUE")

    # 2 + 1
    op.execute("""
        ALTER TABLE agent_tasks
            DROP COLUMN input_hash,
            DROP COLUMN error_detail
    """)
