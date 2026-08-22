"""W6 部署护栏三张表 + 回补的幂等钥匙
(SPEC-009 第七节, 花钱护栏与重置脚本的数据库侧)

Revision ID: 0010_deploy_guardrails
Revises: 0009_evals_groundwork

1. llm_spend_daily —— 一行一天的花费台账。护栏的本体是那条 CHECK
   (spent_cny <= limit_cny): 预扣是事务内的一次加法, 超预算的 UPDATE 物理上
   写不进去 (SPEC-009 第二节 "三层只有第三层是真的")。比 SPEC 的 DDL 多一条
   CHECK (spent_cny >= 0): 回补在应用层用 GREATEST(0, ...) 兜底, 这条让
   "不许把 spent 减成负数"也是数据库的话, 不是应用层的自觉;
2. user_task_quota_daily —— (user_id, day) 每日任务数, CHECK (used <= limit_count)。
   SPEC 写的列名是 limit, 但 LIMIT 是 SQL 保留字, 改名 limit_count (报告第一节报备);
3. demo_marker —— 单行表, 重置脚本的通行证。**迁移只建表不插行**: 行由生产种子
   写入 (第二段), 于是"跑过迁移"与"是演示库"是两件事 —— 每个库都有这张表,
   只有演示库里有那一行。单行由 PK + CHECK (only_row) 强制: 唯一合法主键值是 true;
4. agent_tasks.hold_refunded_at —— 回补的幂等钥匙 (SPEC-009 第二节末尾)。
   回补挂在"任务进终态"上, 而进终态的路径有两条 (轮次收尾回调 / 清扫判死),
   没有这把钥匙的话同一笔预扣会被回补两遍, 台账越算越少直到见底。结算方
   (budget_service) 用条件更新 WHERE hold_refunded_at IS NULL 抢占, 受影响
   0 行即不再回补 —— 手法与 SPEC-003 状态机推进一致 (条件更新 + 数行数)。

day 一律是 **UTC 日期**, 由写入方保证 (budget_service 的 SQL 里显式
AT TIME ZONE 'utc', 不用跟服务器时区走的 current_date, 理由见那边注释)。

downgrade 一步降回再升回结构一致 (ADR-006)。数据上有损: 三张表的行随表消失 ——
花费台账是当日护栏的工作数据, 不是审计事实源 (真实花费的事实源是 ai_usage),
丢了只是当天的额度从头算。
"""
from __future__ import annotations

from alembic import op

revision = "0010_deploy_guardrails"
down_revision = "0009_evals_groundwork"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1: 日花费台账。numeric(10,4) 与 SPEC 一致 (ai_usage 用 numeric(10,6) 是
    # 单次调用的粒度; 台账收到 0.0001 元已经远细于 0.05 元一档的预扣额)。
    op.execute("""
CREATE TABLE llm_spend_daily (
    day date PRIMARY KEY,
    spent_cny numeric(10,4) NOT NULL DEFAULT 0,
    limit_cny numeric(10,4) NOT NULL,
    CONSTRAINT llm_spend_daily_within_limit CHECK (spent_cny <= limit_cny),
    CONSTRAINT llm_spend_daily_nonnegative CHECK (spent_cny >= 0)
)""")

    # 2: 单账号每日任务配额。limit_count 随第一次写入定死当天的值 (行是当天的
    # 契约, 中途改配置只影响之后的新行), 与 llm_spend_daily.limit_cny 同一规矩。
    op.execute("""
CREATE TABLE user_task_quota_daily (
    user_id bigint NOT NULL REFERENCES users(id),
    day date NOT NULL,
    used int NOT NULL DEFAULT 0,
    limit_count int NOT NULL,
    PRIMARY KEY (user_id, day),
    CONSTRAINT user_task_quota_within_limit CHECK (used <= limit_count)
)""")

    # 3: 演示库通行证。唯一合法的主键值是 true, 所以物理上最多一行;
    # note 留给种子写"这是哪个环境、什么时候种的"。
    op.execute("""
CREATE TABLE demo_marker (
    only_row boolean PRIMARY KEY DEFAULT true,
    marked_at timestamptz NOT NULL DEFAULT now(),
    note text,
    CONSTRAINT demo_marker_single_row CHECK (only_row)
)""")

    # 4: 回补的幂等钥匙, 可空 —— NULL = 这笔预扣还没结算过。不回填历史行:
    # 本迁移之前没有预扣这回事, 老任务本来就没有可结算的钱。
    op.execute("ALTER TABLE agent_tasks ADD COLUMN hold_refunded_at timestamptz")


def downgrade() -> None:
    # 三张表互相无外键, 逆序删只是习惯; user_task_quota_daily 对 users 的外键
    # 随 DROP TABLE 消失, 不碰 users。
    op.execute("ALTER TABLE agent_tasks DROP COLUMN hold_refunded_at")
    op.execute("DROP TABLE demo_marker")
    op.execute("DROP TABLE user_task_quota_daily")
    op.execute("DROP TABLE llm_spend_daily")
