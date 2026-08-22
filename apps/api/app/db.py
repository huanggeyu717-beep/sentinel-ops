"""数据库接入层。

职责三件:
1. 提供全局 async engine / session factory 与 FastAPI 依赖 `get_session`;
2. 迁移: 启动时把数据库升到 Alembic 的 head, 保证裸跑 / CI / Docker 三条路径
   共用同一套建表逻辑 (刻意不走 docker-entrypoint-initdb.d);
3. 开发种子数据: 门店/设备/传感器/员工, 幂等写入, 由 SENTINEL_APPLY_DEV_SEED 控制。

迁移为什么在线程里跑: Alembic 是同步库, 而这里是 async 的 lifespan。
在事件循环里直接调它会卡住循环, 所以丢进 asyncio.to_thread。
迁移用同步驱动 psycopg, 应用运行时仍走 asyncpg —— 取舍见
docs/adr/ADR-006-alembic-migrations.md。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

log = logging.getLogger(__name__)

API_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = API_DIR / "alembic.ini"
ALEMBIC_DIR = API_DIR / "alembic"

# W1 的极简迁移器留下的台账 -> 对应的 Alembic 版本号。
# 用途: 让 W1 就建好表的数据库 (开发机、队友的库) 不重跑建表, 直接认领对应版本。
# W2 之后新建的库不会有 schema_migrations 表, 走正常的 upgrade 路径。
_LEGACY_LEDGER_TABLE = "schema_migrations"
_LEGACY_FILE_TO_REVISION = {
    "0001_initial.sql": "0001_baseline",
    "0002_ingest_idempotency.sql": "0002_idempotency",
}

_engine = create_async_engine(settings().database_url, pool_pre_ping=True, future=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def engine() -> AsyncEngine:
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """请求之外(如演练后台任务)开会话用。请求内一律走 get_session 依赖。"""
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖: 每请求一个事务, 异常自动回滚。"""
    async with _session_factory() as session, session.begin():
        yield session


def _dsn() -> str:
    """SQLAlchemy URL -> asyncpg 原生 DSN。"""
    return settings().database_url.replace("+asyncpg", "")


def _sync_dsn() -> str:
    """SQLAlchemy 异步 URL -> Alembic 用的同步 URL。"""
    return settings().database_url.replace("+asyncpg", "+psycopg")


def alembic_config() -> Config:
    """构造 Alembic 配置。路径一律用绝对路径 —— 容器里的工作目录和本机不一样。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn())
    return cfg


def _adopt_legacy_ledger(conn: sa.Connection, cfg: Config) -> str | None:
    """让 W1 极简迁移器建过表的数据库认领对应的 Alembic 版本。

    条件必须同时满足: 已有 schema_migrations 台账, 且还没有 alembic_version 表。
    满足时只写版本号 (stamp), 不执行任何建表语句 —— 表本来就在。
    返回认领的版本号; 不适用时返回 None。
    """
    inspector = sa.inspect(conn)
    if _LEGACY_LEDGER_TABLE not in inspector.get_table_names():
        return None
    if MigrationContext.configure(conn).get_current_revision() is not None:
        return None

    rows = conn.execute(sa.text(f"SELECT filename FROM {_LEGACY_LEDGER_TABLE}")).scalars().all()
    revisions = [_LEGACY_FILE_TO_REVISION[f] for f in sorted(rows) if f in _LEGACY_FILE_TO_REVISION]
    if not revisions:
        return None

    latest = revisions[-1]
    log.info("检测到 W1 的 schema_migrations 台账, 认领 Alembic 版本 %s (不重建表)", latest)
    command.stamp(cfg, latest)
    return latest


def _upgrade_to_head() -> str:
    """同步执行: 必要时先认领旧台账, 再升到最新版本。返回升级后的版本号。"""
    cfg = alembic_config()
    engine = sa.create_engine(_sync_dsn(), poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            _adopt_legacy_ledger(conn, cfg)
            conn.commit()
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision() or "(none)"
    finally:
        engine.dispose()


async def run_migrations() -> str:
    """把数据库升到最新版本, 返回升级后的版本号。"""
    return await asyncio.to_thread(_upgrade_to_head)


# 种子账号统一密码 "sentinel-demo" 的 bcrypt(cost 12) 哈希, 明文另见 .env.example 与 README。
# 预先算好写死, 而不是启动时现算: 幂等、可复现, 也省掉每次启动 3 次 bcrypt 的等待。
_SEED_PASSWORD_HASH = "$2b$12$nqoRSMypSBq4CCcE3wrxFO2D0CxxYwvWTrl5sSyoJxFQ.69xOxbs."

DEV_SEED_SQL = f"""
INSERT INTO zones (id, name) VALUES
    (1,'Zone 1 - 生鲜区'),(2,'Zone 2 - 卖场中区'),(3,'Zone 3 - 后场')
    ON CONFLICT (id) DO NOTHING;
-- zone 是**责任区 (谁巡这一片)**, 不是商品品类: 生鲜区里既有果蔬也有乳制品和熟食,
-- 它们共用一个负责人与一台采集板。按品类分会分出十几个区, 而现场只有 3 台板子。
-- 坐标是相对底图的百分比 0-100 (SPEC-005 前置 A), 按 zone 分区: 1 左 / 2 中 / 3 右。
-- 每个坐标都压着底图上一个真实漏水源 (见 apps/web/.../PlanBase.tsx 的注释):
-- 1 乳制品冷柜脚下 / 2 冷藏饮料柜脚下 / 3 冷冻岛柜之间的过道 / 4 制冰机下游 /
-- 5 走入式冷库门口。4 刻意放在下游: 水顺地面坡度跑, 不一定积在源头。
-- UNKNOWN_DEVICE 与它的占位传感器 0 刻意不填 —— 让前端"未定位"分支在演示数据里就能看见。
-- 冲突时只 COALESCE 回填空缺, 不覆盖手工标注; 这样 W2 之前建的老库(行已存在,
-- DO NOTHING 不会生效)也能拿到坐标, 而人工调过的位置不会被启动种子改回去。
INSERT INTO devices (id, name, zone_id, pos_x, pos_y) VALUES
    (1,'Arduino1',1,12.40,19.67),(2,'Arduino2',2,52.20,19.67),
    (3,'UNKNOWN_DEVICE',3,NULL,NULL)
    ON CONFLICT (id) DO UPDATE SET
        pos_x = COALESCE(devices.pos_x, EXCLUDED.pos_x),
        pos_y = COALESCE(devices.pos_y, EXCLUDED.pos_y);
INSERT INTO sensors (id, device_id, zone_id, active, threshold_value, pos_x, pos_y) VALUES
    (0,3,3,true,500,NULL,NULL),
    (1,1,1,true,500,11.80,34.17),(2,1,1,true,500,11.80,68.33),
    (3,2,2,true,500,52.20,38.33),(4,2,2,true,500,60.00,71.67),
    (5,3,3,true,500,84.00,47.50)
    ON CONFLICT (id) DO UPDATE SET
        pos_x = COALESCE(sensors.pos_x, EXCLUDED.pos_x),
        pos_y = COALESCE(sensors.pos_y, EXCLUDED.pos_y);
INSERT INTO employees (id, name, role, email, zone_id, rfid_uid) VALUES
    (1,'Alex Chen','operator','alex@example.com',1,'04A1B2C3'),
    (2,'Bo Wang','operator','bo@example.com',2,'04D9E8F7'),
    (3,'Chris Li','manager','chris@example.com',1,'04FFAA01')
    ON CONFLICT (id) DO NOTHING;
SELECT setval(pg_get_serial_sequence('zones','id'), GREATEST((SELECT max(id) FROM zones),1));
SELECT setval(pg_get_serial_sequence('devices','id'), GREATEST((SELECT max(id) FROM devices),1));
SELECT setval(pg_get_serial_sequence('sensors','id'), GREATEST((SELECT max(id) FROM sensors),1));
SELECT setval(pg_get_serial_sequence('employees','id'),
              GREATEST((SELECT max(id) FROM employees),1));
-- 登录账号 (SPEC-004)。两条刻意覆盖 users 与 employees 的两种真实关系
-- (users.employee_id 可空的理由, 方案 A): Chris/Alex 有账号也各绑一名员工;
-- Bo Wang 是现场员工但没有账号 (只刷卡, 从不登录)。admin (有账号、不是现场员工)
-- 不在这份 SQL 里 —— 见下面 ADMIN_SEED_SQL: 公开演示库不种它 (SPEC-009 第一节)。
INSERT INTO users (id, email, password_hash, display_name, employee_id) VALUES
    (2,'chris@example.com','{_SEED_PASSWORD_HASH}','Chris Li',3),
    (3,'alex@example.com','{_SEED_PASSWORD_HASH}','Alex Chen',1)
    ON CONFLICT (id) DO NOTHING;
INSERT INTO user_roles (user_id, role_id)
    SELECT v.user_id, r.id
    FROM (VALUES (2,'manager'),(3,'operator')) AS v(user_id, role_name)
    JOIN roles r ON r.name = v.role_name
    ON CONFLICT DO NOTHING;
SELECT setval(pg_get_serial_sequence('users','id'), GREATEST((SELECT max(id) FROM users),1));
"""

# admin 单独一段: 只有**非**演示库 (开发/测试) 才种。公开演示不给 admin ——
# 它能做的事 (改角色、改权限) 不是演示内容, 只是攻击面; 演示库需要 admin 时
# 由 runbook 里一条命令当场生成随机口令创建 (SPEC-009 第一节第 5 条)。
# 重置脚本重放的是 DEV_SEED_SQL, 不含这一段 —— 每日重置不会把 admin 种回演示库。
ADMIN_SEED_SQL = f"""
INSERT INTO users (id, email, password_hash, display_name, employee_id) VALUES
    (1,'admin@example.com','{_SEED_PASSWORD_HASH}','Admin',NULL)
    ON CONFLICT (id) DO NOTHING;
INSERT INTO user_roles (user_id, role_id)
    SELECT 1, r.id FROM roles r WHERE r.name = 'admin'
    ON CONFLICT DO NOTHING;
"""

# 重置脚本的通行证 (SPEC-009 第三节): 只在 SENTINEL_APPLY_DEMO_MARKER=true 时
# 写入。幂等 —— 单行表, 撞上已有的那一行就什么都不做 (marked_at 保留首次种下的时刻)。
DEMO_MARKER_SQL = """
INSERT INTO demo_marker (note)
VALUES ('public demo: seeded by API startup (SENTINEL_APPLY_DEMO_MARKER=true)')
ON CONFLICT (only_row) DO NOTHING;
"""


async def apply_dev_seed() -> None:
    """写入演示用门店/设备/传感器/员工/账号。幂等, SENTINEL_APPLY_DEV_SEED=false 关闭。

    SENTINEL_APPLY_DEMO_MARKER (默认 false) 切换两种形态:
    - false (开发/测试): 照旧种 admin, **不写** demo_marker —— 通行证长到开发库里,
      重置脚本的护栏就对开发库也放行了 (W6 第二段易错点三);
    - true (公开演示): 写入通行证那一行, **不种 admin** (SPEC-009 第一节)。
    """
    cfg = settings()
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(DEV_SEED_SQL)
        if cfg.apply_demo_marker:
            await conn.execute(DEMO_MARKER_SQL)
        else:
            await conn.execute(ADMIN_SEED_SQL)
    finally:
        await conn.close()
