"""测试夹具。

约定: 测试跑在独立库 sentinel_test 上, 不碰开发库。
库不存在时自动创建, 每个测试前清空遥测表与事故相关表, 保证用例互不干扰。
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
TELEMETRY_TABLES = [
    "waterlevel_readings", "rfid_scans", "device_heartbeats", "sensorstate",
    # W2 事故生命周期: incident_events 引用 incidents, 同一条 TRUNCATE 一起清
    "incident_events", "incidents", "audit_log",
]


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
from app.services.auth_service import COOKIE_NAME  # noqa: E402

# 种子账号统一密码, 与 db.py 里写死的 bcrypt 哈希对应 (SPEC-004)
SEED_PASSWORD = "sentinel-demo"


@pytest.fixture(scope="session")
def client():
    from starlette.testclient import TestClient

    with TestClient(app) as c:  # with 块内会执行 lifespan -> 建表 + 种子
        yield c


@pytest.fixture(scope="session")
def auth_headers(client):
    """按邮箱登录, 返回 Bearer 请求头; 同一账号整个测试会话只登录一次。

    刻意走请求头而不是 cookie: TestClient 的 cookie jar 是全局的, 留着会让
    后续用例被隐式登录, 401 类断言就测不到东西了。cookie 通道由 test_auth 单独覆盖。
    """
    cache: dict[str, dict[str, str]] = {}

    def login(email: str = "chris@example.com", password: str = SEED_PASSWORD) -> dict[str, str]:
        if email not in cache:
            r = client.post("/auth/login", json={"email": email, "password": password})
            assert r.status_code == 200, r.text
            token = client.cookies[COOKIE_NAME]
            client.cookies.clear()
            cache[email] = {"Authorization": f"Bearer {token}"}
        return cache[email]

    return login


@pytest.fixture(scope="session")
def viewer_headers(client, auth_headers):
    """入库一个 viewer 账号并返回其 Bearer 头 (种子只有另外三种角色)。

    放 conftest 是因为 test_auth 与 test_drills 都要用它测 403。
    """
    from app.services import auth_service

    email = "viewer@example.com"

    async def go() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO users (email, password_hash, display_name) "
                "VALUES ($1, $2, 'View Only') ON CONFLICT (email) DO NOTHING",
                email, auth_service.hash_password(SEED_PASSWORD),
            )
            await conn.execute(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.email = $1 AND r.name = 'viewer' ON CONFLICT DO NOTHING",
                email,
            )
        finally:
            await conn.close()

    asyncio.run(go())
    return auth_headers(email)


@pytest.fixture(autouse=True)
def clean_telemetry(client):
    async def go() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f"TRUNCATE {', '.join(TELEMETRY_TABLES)}")
        finally:
            await conn.close()

    asyncio.run(go())
    client.cookies.clear()  # 上一个用例登录留下的会话不许外溢
    yield


@pytest.fixture(autouse=True)
def reset_login_limiter():
    """每条用例前清空登录限流计数。

    否则 401 类用例攒下的失败次数会溢出到后面的用例, 变成随机失败的 429。
    """
    from app.routers.auth import login_limiter

    login_limiter.reset()
    yield
    login_limiter.reset()
