"""incidents 生命周期: assigned_at/resolved_by 与去重、查询索引 (SPEC-003)

Revision ID: 0003_incidents
Revises: 0002_idempotency

- assigned_at: 现有表有 acknowledged_at / resolved_at, 缺分配时刻;
- resolved_by: 解决来源, 人工记 employee:{id}, 自动解决记 auto_sensor_dry (决策 4);
- partial unique index: 同一传感器最多一条未解决事故, 去重下沉到 DB 层 (决策 2);
- 另两个索引分别服务列表查询与时间线查询。
"""
from __future__ import annotations

from alembic import op

revision = "0003_incidents"
down_revision = "0002_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE incidents ADD COLUMN assigned_at timestamptz")
    op.execute("ALTER TABLE incidents ADD COLUMN resolved_by text")
    op.execute("""
        CREATE UNIQUE INDEX uq_incidents_sensor_unresolved
            ON incidents (sensor_id) WHERE status <> 'resolved'
    """)
    op.execute("CREATE INDEX ix_incidents_status_opened ON incidents (status, opened_at DESC)")
    op.execute("CREATE INDEX ix_incident_events_incident_at ON incident_events (incident_id, at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_incident_events_incident_at")
    op.execute("DROP INDEX IF EXISTS ix_incidents_status_opened")
    op.execute("DROP INDEX IF EXISTS uq_incidents_sensor_unresolved")
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS resolved_by")
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS assigned_at")
