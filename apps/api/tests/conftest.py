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
    # 5433 不是默认端口: 本机 Homebrew postgresql@17 占着 5432, 项目的 Docker
    # Postgres 让路映射到 5433 (docker-compose.yml)。CI 里这个默认值不生效 ——
    # workflow 显式传 SENTINEL_TEST_DATABASE_URL (那边的 Postgres 独立起, 是 5432)。
    "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test",
)
os.environ["SENTINEL_DATABASE_URL"] = TEST_URL
os.environ["SENTINEL_APPLY_DEV_SEED"] = "true"
# 后台 tick 用墙上时钟, 会把用例里的假时间戳事件当成"很久以前"而乱触发;
# 测试里把间隔调到 1 小时 (任务只会睡着等 cancel), tick 一律由用例显式注入。
os.environ["SENTINEL_ENGINE_TICK_SECONDS"] = "3600"
# W4 agent 打卡/清扫循环同理: 5 秒一轮的清扫事务 (先锁 agent_tasks 再读
# agent_clarifications) 会与夹具的反向 TRUNCATE 偶发死锁; 打卡与清扫一律由用例显式调。
os.environ["SENTINEL_AGENT_HEARTBEAT_SECONDS"] = "3600"

DSN = TEST_URL.replace("+asyncpg", "")
TELEMETRY_TABLES = [
    "waterlevel_readings", "rfid_scans", "device_heartbeats", "sensorstate",
    # W2 事故生命周期: incident_events 引用 incidents, 同一条 TRUNCATE 一起清
    "incident_events", "incidents", "audit_log",
    # W3 策略生命周期 (SPEC-006): 外键相互引用的表必须同一条 TRUNCATE 一起清
    "policy_runs", "policy_publications", "approvals", "policy_versions", "policies",
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
    """返回一个 viewer 账号的 Bearer 头。

    迁移 0007 起种子里已经有这个 viewer@example.com 账号 (连同 dana,
    SPEC-006 第三节), 下面的 ON CONFLICT DO NOTHING 实际什么都不再插 ——
    保留只是兜底 0007 之前的旧库。不要再把这里当成"种子无 viewer"的依据
    (第二段的 CANONICAL_INVENTORY 就是被旧 docstring "种子只有另外三种角色"
    误导错的)。
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
    # 策略引擎每个用例一个全新实例: 引擎状态/域事件队列/策略缓存互不外溢,
    # 且 asyncio.Lock 不会把上一个用例的事件循环带过来 (见 reset_runtime docstring)
    from app.services import policy_runtime

    policy_runtime.reset_runtime()
    yield


# 与 W2 硬编码行为等价的基线策略: 转湿即开事故。W3 引擎接管后, 依赖"湿了会开单"
# 的既有用例 (test_incidents / test_auth / test_ingest) 显式声明使用这个夹具。
# 直接 SQL 注入已发布状态 (含审批与发布记录, 满足全部约束), 不走 service ——
# 生命周期本身的行为由 test_policy_service 覆盖。
BASELINE_WET_OPEN_BODY = (
    '{"scope": {"type": "zone", "ids": [1, 2, 3]},'
    ' "trigger": {"type": "sensor_state_changed", "to": "WET"},'
    ' "conditions": [],'
    ' "actions": [{"type": "open_incident", "severity": "normal"}],'
    ' "cooldown_s": 60}'
)


async def insert_published_policy(name: str, body: str) -> dict[str, int]:
    """插入一条已发布策略 (policy + version + 已通过的审批 + 发布记录)。

    审批链满足全部数据库约束: alex(user 3) 提交, chris(user 2) 批准并发布。
    返回 {policy_id, version_id, approval_id, publication_id}。
    """
    conn = await asyncpg.connect(DSN)
    try:
        policy_id = await conn.fetchval(
            "INSERT INTO policies (name, created_by) VALUES ($1, 3) RETURNING id", name
        )
        version_id = await conn.fetchval(
            "INSERT INTO policy_versions (policy_id, version, body, status) "
            "VALUES ($1, 1, $2::jsonb, 'published') RETURNING id",
            policy_id, body,
        )
        approval_id = await conn.fetchval(
            "INSERT INTO approvals (policy_version_id, requested_by, decided_by, "
            "decision, decided_at) VALUES ($1, 3, 2, 'approved', now()) RETURNING id",
            version_id,
        )
        publication_id = await conn.fetchval(
            "INSERT INTO policy_publications (policy_id, policy_version_id, approval_id, "
            "published_by) VALUES ($1, $2, $3, 2) RETURNING id",
            policy_id, version_id, approval_id,
        )
    finally:
        await conn.close()
    return {
        "policy_id": policy_id, "version_id": version_id,
        "approval_id": approval_id, "publication_id": publication_id,
    }


@pytest.fixture
def published_baseline(client, clean_telemetry):
    """已发布的 wet->open 基线策略, 引擎据此接管开事故。"""
    return asyncio.run(insert_published_policy("baseline-wet-open", BASELINE_WET_OPEN_BODY))


@pytest.fixture
def svc(client):
    """直接调 service 层的运行器 (SPEC-006 验收 10-12 要求不经过 HTTP)。

    每次调用在**独立事件循环 + 独立 NullPool 引擎**里跑: TestClient 的应用引擎
    连接绑定在它自己的循环上, 跨循环复用连接池会炸; NullPool 用完即断, 不留
    跨循环的连接。依赖 client 只是为了保证迁移与种子先跑完。
    用法: svc(go), 其中 go 是 async def go(session_factory) -> Any。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    def run(fn):
        async def go():
            engine = create_async_engine(TEST_URL, poolclass=NullPool)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                return await fn(factory)
            finally:
                await engine.dispose()

        return asyncio.run(go())

    return run


@pytest.fixture(autouse=True)
def reset_login_limiter():
    """每条用例前清空登录限流计数。

    否则 401 类用例攒下的失败次数会溢出到后面的用例, 变成随机失败的 429。
    """
    from app.routers.auth import login_limiter

    login_limiter.reset()
    yield
    login_limiter.reset()
