"""incidents.acknowledged_by_employee_id: 派给谁与谁接单分开记 (SPEC-003 修订 1)

Revision ID: 0004_incident_ack_by
Revises: 0003_incidents

原实现用 COALESCE 把刷卡人回填进 assigned_employee_id, 会让前端把"派给谁"
显示成"谁接的单"。拆成两个字段各记各的, 还能统计"派单命中率"。
"""
from __future__ import annotations

from alembic import op

revision = "0004_incident_ack_by"
down_revision = "0003_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE incidents
            ADD COLUMN acknowledged_by_employee_id bigint REFERENCES employees(id)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS acknowledged_by_employee_id")
