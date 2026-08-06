"""users.employee_id: 登录账号与现场员工的可空关联 (SPEC-004 方案 A)

Revision ID: 0005_users_employee_link
Revises: 0004_incident_ack_by

可空是有意的: admin 有账号但不是现场员工, Bo Wang 是现场员工但没有账号;
设成必填, 这两种真实情况都得靠编假数据绕过去 (SPEC-004 前提一节)。
不给 employees.email 加唯一约束 —— 关联走这个外键, 不靠 email 对齐 (方案 C 被否的理由)。
"""
from __future__ import annotations

from alembic import op

revision = "0005_users_employee_link"
down_revision = "0004_incident_ack_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN employee_id bigint REFERENCES employees(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS employee_id")
