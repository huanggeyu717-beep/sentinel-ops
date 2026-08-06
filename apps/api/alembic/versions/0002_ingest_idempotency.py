"""事件入口幂等约束 (原 migrations/0002_ingest_idempotency.sql)

Revision ID: 0002_idempotency
Revises: 0001_baseline

read_sql 在 0001 里也有一份。刻意不抽公共模块: Alembic 是按文件路径单独加载每个
revision 的, 不是当包 import, 跨 revision 引用要么改 sys.path 要么加 __init__,
都是给"永远不会再改的历史迁移"增加耦合。五行重复比那个划算。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002_idempotency"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "migrations"


def read_sql(name: str) -> str:
    raw = (SQL_DIR / name).read_text(encoding="utf-8")
    kept = [ln for ln in raw.splitlines() if ln.strip().upper() not in {"BEGIN;", "COMMIT;"}]
    return "\n".join(kept)


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("0002_ingest_idempotency.sql"))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_rfid_scanned_at")
    op.execute("DROP INDEX IF EXISTS ix_readings_received_at")
    op.execute("DROP INDEX IF EXISTS uq_rfid_device_uid_ts")
    op.execute("DROP INDEX IF EXISTS uq_readings_device_sensor_ts")
