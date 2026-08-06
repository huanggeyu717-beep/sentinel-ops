"""sensors / devices 加平面图坐标 pos_x / pos_y (SPEC-005 前置 A)

Revision ID: 0006_positions
Revises: 0005_users_employee_link

坐标是**相对底图的百分比 (0-100)**, 不是像素也不是经纬度 —— 室内平面图换分辨率、
换比例都不必改数据。可空: 新装但还没标位置的设备照样入库, 前端把无坐标的
列在"未定位"区。设备与传感器分别存位置, 因为物理上不在一处 (板子在墙上, 探头在地面)。
原系统把 sensorZoneMap 写成前端常量、刷新即丢, 这次进数据库。
"""
from __future__ import annotations

from alembic import op

revision = "0006_positions"
down_revision = "0005_users_employee_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sensors
            ADD COLUMN pos_x numeric(5,2),
            ADD COLUMN pos_y numeric(5,2)
    """)
    op.execute("""
        ALTER TABLE devices
            ADD COLUMN pos_x numeric(5,2),
            ADD COLUMN pos_y numeric(5,2)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE sensors DROP COLUMN IF EXISTS pos_x, DROP COLUMN IF EXISTS pos_y")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS pos_x, DROP COLUMN IF EXISTS pos_y")
