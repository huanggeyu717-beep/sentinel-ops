"""数据库接入层。

职责三件:
1. 提供全局 async engine / session factory 与 FastAPI 依赖 `get_session`;
2. 极简迁移器: 按文件名顺序执行 migrations/*.sql, 用 schema_migrations 台账保证只跑一次
   (W2 起换 Alembic, 届时把台账里已应用的文件标记为 baseline);
3. 开发种子数据: 门店/设备/传感器/员工, 幂等写入, 由 SENTINEL_APPLY_DEV_SEED 控制。

为什么不用 SQLAlchemy 执行迁移文件: asyncpg 的扩展查询协议不支持一次执行多条语句,
而迁移文件天然是多语句 + 显式 BEGIN/COMMIT, 因此这里直接借 asyncpg 的简单查询协议执行。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_engine = create_async_engine(settings().database_url, pool_pre_ping=True, future=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def engine():
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖: 每请求一个事务, 异常自动回滚。"""
    async with _session_factory() as session:
        async with session.begin():
            yield session


def _dsn() -> str:
    """SQLAlchemy URL -> asyncpg 原生 DSN。"""
    return settings().database_url.replace("+asyncpg", "")


async def run_migrations() -> list[str]:
    """执行尚未应用的迁移文件, 返回本次应用的文件名列表。"""
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    conn = await asyncpg.connect(_dsn())
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            log.info("applying migration %s", f.name)
            await conn.execute(f.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", f.name)
            applied.append(f.name)
    finally:
        await conn.close()
    return applied


DEV_SEED_SQL = """
INSERT INTO zones (id, name) VALUES (1,'Zone 1 - Entrance'),(2,'Zone 2 - Aisle'),(3,'Zone 3 - Storage')
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
SELECT setval(pg_get_serial_sequence('employees','id'), GREATEST((SELECT max(id) FROM employees),1));
"""


async def apply_dev_seed() -> None:
    """写入演示用门店/设备/传感器/员工。幂等, 生产环境用 SENTINEL_APPLY_DEV_SEED=false 关闭。"""
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(DEV_SEED_SQL)
    finally:
        await conn.close()
