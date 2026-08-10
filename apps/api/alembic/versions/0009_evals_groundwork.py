"""W5 评测地基: 成本口径收口、task_id 收 NOT NULL、删三张 eval 表、澄清槽位列
(SPEC-007 第八节, 四件, 逐条能打勾)

Revision ID: 0009_evals_groundwork
Revises: 0008_agent_orchestration

1. ai_usage.estimated_cost_usd 改名 estimated_cost_cny —— 列名叫美元、存的是
   人民币, 是"声明的和执行的不是一回事"。改列名而非折汇率: 折汇率引入一个天天
   在变的数字, 同一次调用今天和下周算出来不一样, 可复现性没了 (SPEC-002 第九节末);
2. agent_steps.task_id 设 NOT NULL —— (task_id, seq) 唯一索引对 NULL 行不生效,
   断点续传的 seq 唯一性有漏洞。**升级前先查 NULL 行, 有就报错中止**, 不 DELETE、
   不填假值: 静默改数据比报错难查一百倍;
3. 删 eval_cases / eval_runs / eval_results —— 0001 建的, 全仓库除 run_evals.py
   一行注释外无任何代码引用。评测结果是**归档**不是在线状态: 要跟着 git 走、要能
   diff、要在没有数据库时也读得到, 三件事放数据库一件都做不到 (SPEC-007 第五节末)。
   downgrade 的 DDL 从 apps/api/migrations/0001_initial.sql 120-132 行**原样抄**,
   不凭记忆重写 (W2 "基线不翻写"的规矩 —— 24 张表抄漏一个约束不会有任何报错);
4. agent_clarifications 加 missing_slots text[] (可空) —— 0008 建表时只有
   question/answer 两个自由文本列, 没有地方存结构化槽位, 而追问类判分正是靠它。
   可空是为了不动历史行; **新写入的行必须非空**, 由 service 层保证并配测试
   (agent_service.ask_clarification), 不是给新行留后门。

downgrade 一步降回再升回结构一致 (ADR-006)。数据上有损的一处: 三张 eval 表里的
行删表即失 —— 它们在所有已知库上都是空的 (从无写入路径), 真有数据的库会在
downgrade 后拿回三张空表。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_evals_groundwork"
down_revision = "0008_agent_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1: 成本列改名。RENAME 保留类型 numeric(10,6) 与既有数据 —— 那一列存的
    # 本来就是人民币元 (config.py 单价注释), 改的只是名字与事实不符这件事。
    op.execute("ALTER TABLE ai_usage RENAME COLUMN estimated_cost_usd TO estimated_cost_cny")

    # 2: task_id 收 NOT NULL。先查 NULL 行 —— 有就中止, 由人决定怎么处置。
    null_rows = bind.execute(
        sa.text("SELECT count(*) FROM agent_steps WHERE task_id IS NULL")
    ).scalar_one()
    if null_rows:
        raise RuntimeError(
            f"agent_steps 里有 {null_rows} 行 task_id 为 NULL, 无法加 NOT NULL 约束; "
            "请先人工处置这些行再重跑迁移 (不要 DELETE 或填假值 —— 这些行游离在 "
            "(task_id, seq) 唯一索引之外, 先弄清它们是怎么来的)"
        )
    op.execute("ALTER TABLE agent_steps ALTER COLUMN task_id SET NOT NULL")

    # 3: 删三张 eval 表。先断言它们确实存在且形状是 0001 建的那三张 ——
    # 对不上说明这条库走过我不知道的路径, 报错停下比静默继续便宜。
    for table in ("eval_results", "eval_runs", "eval_cases"):
        exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = :t)"
            ),
            {"t": table},
        ).scalar_one()
        if not exists:
            raise RuntimeError(
                f"要删的表 {table} 不存在 —— 库状态与 0001 基线对不上, 请先人工核对"
            )
    # eval_results 的外键指向另外两张, 先删它
    op.execute("DROP TABLE eval_results")
    op.execute("DROP TABLE eval_runs")
    op.execute("DROP TABLE eval_cases")

    # 4: 澄清槽位列 (text[])。取值域是 SPEC-007 第三节的七项封闭枚举, 由
    # service 层校验 (agent_service.MISSING_SLOTS), 数据库不设 CHECK ——
    # 枚举随 DSL 能力演进, 事实源在代码里, CHECK 会变成第二份走散的枚举。
    op.execute("ALTER TABLE agent_clarifications ADD COLUMN missing_slots text[]")


def downgrade() -> None:
    # 4
    op.execute("ALTER TABLE agent_clarifications DROP COLUMN missing_slots")

    # 3: 从 0001_initial.sql 120-132 行原样抄的 DDL (基线不翻写)。
    # 建表顺序与 0001 一致: eval_results 的外键要求另外两张先在。
    op.execute("""
CREATE TABLE eval_cases (
    id text PRIMARY KEY, category text NOT NULL, input jsonb NOT NULL,
    expected jsonb NOT NULL, scenario_ref text
)""")
    op.execute("""
CREATE TABLE eval_runs (
    id bigserial PRIMARY KEY, arm text NOT NULL, model text NOT NULL,
    prompt_version text, git_sha text, started_at timestamptz NOT NULL DEFAULT now()
)""")
    op.execute("""
CREATE TABLE eval_results (
    run_id bigint REFERENCES eval_runs(id), case_id text REFERENCES eval_cases(id),
    passed boolean, failure_kind text, detail jsonb, latency_ms int, tokens int,
    PRIMARY KEY (run_id, case_id)
)""")

    # 2
    op.execute("ALTER TABLE agent_steps ALTER COLUMN task_id DROP NOT NULL")

    # 1
    op.execute("ALTER TABLE ai_usage RENAME COLUMN estimated_cost_cny TO estimated_cost_usd")
