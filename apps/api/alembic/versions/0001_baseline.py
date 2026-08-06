"""W1 基线 schema (原 migrations/0001_initial.sql)

Revision ID: 0001_baseline
Revises:

刻意不把建表语句翻写成 op.create_table(...): 直接执行 W1 那份 .sql 原文,
schema 与 W1 完全逐字节一致, 不存在"翻写时手抖漏了个约束"的可能。
W2 起的新迁移正常用 Alembic 的 Python 写法。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "migrations"


def read_sql(name: str) -> str:
    """读取 W1 的 .sql 原文, 去掉显式的 BEGIN/COMMIT。

    Alembic 自己已经把每个迁移包在一个事务里, 文件里再写 BEGIN/COMMIT 会导致
    事务边界错乱 (COMMIT 会提前结束 Alembic 的事务, 版本号写入就落在事务之外)。
    """
    raw = (SQL_DIR / name).read_text(encoding="utf-8")
    kept = [ln for ln in raw.splitlines() if ln.strip().upper() not in {"BEGIN;", "COMMIT;"}]
    return "\n".join(kept)


def upgrade() -> None:
    # exec_driver_sql 而不是 op.execute: 前者原样交给驱动, 不会把 SQL 里的
    # 冒号当成绑定参数占位符, 也支持一次执行多条语句。
    op.get_bind().exec_driver_sql(read_sql("0001_initial.sql"))


def downgrade() -> None:
    # 基线不提供回滚: 回滚等于清空整个库, 语义上应该用 `make reset` 重来,
    # 而不是伪装成一次"迁移降级"。
    raise NotImplementedError("基线迁移不支持 downgrade, 要重建数据库请执行 make reset")
