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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


def engine():
    return _engine


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


DEV_SEED_SQL = """
INSERT INTO zones (id, name) VALUES
    (1,'Zone 1 - Entrance'),(2,'Zone 2 - Aisle'),(3,'Zone 3 - Storage')
    ON CONFLICT (id) DO NOTHING;
INSERT INTO devices (id, name, zone_id) VALUES
    (1,'Arduino1',1),(2,'Arduino2',2),(3,'UNKNOWN_DEVICE',3)
    ON CONFLICT (id) DO NOTHING;
INSERT INTO sensors (id, device_id, zone_id, active, threshold_value) VALUES
    (0,3,3,true,500),(1,1,1,true,500),(2,1,1,true,500),
    (3,2,2,true,500),(4,2,2,true,500),(5,3,3,true,500)
    ON CONFLICT (id) DO NOTHING;
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
"""


async def apply_dev_seed() -> None:
    """写入演示用门店/设备/传感器/员工。幂等, 生产环境用 SENTINEL_APPLY_DEV_SEED=false 关闭。"""
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(DEV_SEED_SQL)
    finally:
        await conn.close()
