"""测试夹具。

约定: 测试跑在独立库 sentinel_test 上, 不碰开发库。
库不存在时自动创建, 每个测试前清空遥测表, 保证用例互不干扰。
覆盖地址用 SENTINEL_TEST_DATABASE_URL。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

TEST_URL = os.environ.get(
    "SENTINEL_TEST_DATABASE_URL",
    "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel_test",
)
os.environ["SENTINEL_DATABASE_URL"] = TEST_URL
os.environ["SENTINEL_APPLY_DEV_SEED"] = "true"

DSN = TEST_URL.replace("+asyncpg", "")
TELEMETRY_TABLES = ["waterlevel_readings", "rfid_scans", "device_heartbeats", "sensorstate"]


def _ensure_database() -> None:
    async def go() -> None:
        admin_dsn = DSN.rsplit("/", 1)[0] + "/postgres"
        db_name = DSN.rsplit("/", 1)[1]
        conn = await asyncpg.connect(admin_dsn)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(go())


_ensure_database()

from app.main import app  # noqa: E402  必须在设置好 env 之后导入


@pytest.fixture(scope="session")
def client():
    from starlette.testclient import TestClient

    with TestClient(app) as c:  # with 块内会执行 lifespan -> 建表 + 种子
        yield c


@pytest.fixture(autouse=True)
def clean_telemetry(client):
    async def go() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f"TRUNCATE {', '.join(TELEMETRY_TABLES)}")
        finally:
            await conn.close()

    asyncio.run(go())
    yield
