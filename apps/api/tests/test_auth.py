"""W2 验收: 登录 (JWT + httpOnly cookie) 与 RBAC。对应 docs/specs/SPEC-004。

种子账号 (conftest.SEED_PASSWORD): admin@example.com (admin, 不绑员工) /
chris@example.com (manager, user 2, 绑员工 3) / alex@example.com (operator, user 3, 绑员工 1)。
viewer 角色没有种子账号, 由本文件的 fixture 直接入库一个。

事故流转本身的行为在 test_incidents.py; 这里只测身份与权限的边界。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
from conftest import SEED_PASSWORD

from app.config import Settings
from app.services import auth_service
from app.services.auth_service import COOKIE_NAME

TS = 1_773_600_000_000
CHRIS = {"email": "chris@example.com", "password": SEED_PASSWORD}


def dsn() -> str:
    return os.environ["SENTINEL_DATABASE_URL"].replace("+asyncpg", "")


def open_incident(client) -> int:
    """转湿开一条 Zone 1 的事故。/ingest 无鉴权也必须可用 (决策 7), 这里顺带就是断言。"""
    r = client.post("/ingest", json={
        "kind": "sensor_state", "device_id": "Arduino1", "ts": TS,
        "sensor_id": 1, "state": "WET", "value": 845,
    })
    assert r.status_code == 200
    incident_id = r.json()["incident_id"]
    assert incident_id is not None
    return incident_id


@pytest.fixture(scope="session")
def viewer_headers(client, auth_headers):
    """入库一个 viewer 账号并返回其 Bearer 头 (种子只有另外三种角色)。"""
    email = "viewer@example.com"

    async def go() -> None:
        conn = await asyncpg.connect(dsn())
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


# ===== 认证: 401 边界 =====

def test_incidents_without_login__401(client):
    assert client.get("/incidents").status_code == 401
    assert client.get("/status/sensors").status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_expired_token__401(client):
    token, _ = auth_service.create_token(
        1, now=dt.datetime.now(dt.UTC) - auth_service.TOKEN_TTL - dt.timedelta(minutes=1)
    )
    r = client.get("/incidents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_tampered_token__401(client):
    token, _ = auth_service.create_token(1)
    r = client.get("/incidents", headers={"Authorization": f"Bearer {token}x"})
    assert r.status_code == 401


def test_login_wrong_password__401_without_leaking_which_field(client):
    wrong = client.post("/auth/login", json={"email": CHRIS["email"], "password": "nope"})
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # 不区分邮箱错还是密码错


# ===== cookie 会话 =====

def test_login__sets_httponly_lax_cookie_and_body_has_no_token(client):
    r = client.post("/auth/login", json=CHRIS)
    assert r.status_code == 200
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    # 响应体不回 token 原文 (决策 3): 回了 JavaScript 就能读到, httpOnly 白做
    token = client.cookies[COOKIE_NAME]
    assert token not in r.text
    assert "token" not in r.json()
    body = r.json()
    assert body["user"]["email"] == CHRIS["email"]
    assert body["roles"] == ["manager"]
    assert body["expires_at"] is not None


def test_cookie_session__survives_page_refresh_without_relogin(client):
    """登录一次后, 后续请求只凭 cookie jar (等价于浏览器刷新页面) 就能通过。"""
    client.post("/auth/login", json=CHRIS)
    me = client.get("/auth/me")  # 不带任何请求头
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == CHRIS["email"]
    assert body["roles"] == ["manager"]
    assert body["employee"] == {"id": 3, "name": "Chris Li", "zone_id": 1}  # 绑定的员工


def test_logout__subsequent_requests_401(client):
    client.post("/auth/login", json=CHRIS)
    assert client.get("/incidents").status_code == 200
    client.post("/auth/logout")
    assert client.get("/incidents").status_code == 401


def test_bearer_header__accepted_as_fallback(client):
    """备用通道 (决策 3): /docs 的 Authorize 按钮和 curl 只会走 Authorization 请求头。"""
    client.post("/auth/login", json=CHRIS)
    token = client.cookies[COOKIE_NAME]
    client.cookies.clear()
    assert client.get("/incidents").status_code == 401  # cookie 已清, 先确认没有隐式会话
    r = client.get("/incidents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# ===== RBAC: 403 与 422 是两层 =====

def test_viewer__can_read_but_assign_403(client, viewer_headers):
    incident_id = open_incident(client)
    assert client.get("/incidents", headers=viewer_headers).status_code == 200
    assert client.get("/status/sensors", headers=viewer_headers).status_code == 200
    r = client.post(
        f"/incidents/{incident_id}/assign", json={"employee_id": 1}, headers=viewer_headers
    )
    assert r.status_code == 403
    assert client.get(
        f"/incidents/{incident_id}", headers=viewer_headers
    ).json()["incident"]["status"] == "open"


def test_operator__cross_zone_flag_403(client, auth_headers):
    """operator 传 allow_cross_zone=true 是**没权限** (403), 不是业务校验失败 (422)。"""
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2, "allow_cross_zone": True},  # Bo, Zone 2
        headers=auth_headers("alex@example.com"),
    )
    assert r.status_code == 403


def test_manager__same_cross_zone_request_succeeds(client, auth_headers):
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2, "allow_cross_zone": True},
        headers=auth_headers("chris@example.com"),
    )
    assert r.status_code == 200
    assert r.json()["incident"]["assigned_employee_id"] == 2


def test_manager__cross_zone_without_flag_still_422(client, auth_headers):
    """权限与业务校验是两层: manager 有资格放行, 但没带 flag 依然是业务 422。"""
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2},  # Bo, Zone 2, 未显式放行
        headers=auth_headers("chris@example.com"),
    )
    assert r.status_code == 422


def test_operator__same_zone_assign_succeeds(client, auth_headers):
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 1},  # Alex, 同区
        headers=auth_headers("alex@example.com"),
    )
    assert r.status_code == 200


# ===== 审计与口径 =====

def test_assign_after_login__audit_log_user_id_is_that_user(client, auth_headers):
    incident_id = open_incident(client)
    client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 1},
        headers=auth_headers("chris@example.com"),
    )

    async def go() -> list:
        conn = await asyncpg.connect(dsn())
        try:
            return await conn.fetch(
                "SELECT user_id FROM audit_log "
                "WHERE entity = 'incident' AND entity_id = $1 AND action = 'incident.assign'",
                str(incident_id),
            )
        finally:
            await conn.close()

    rows = asyncio.run(go())
    assert [r["user_id"] for r in rows] == [2]  # chris 的 user id, 不再是空


def test_ingest__stays_open_without_auth(client):
    """决策 7: /ingest 模拟设备上报, 加 JWT 会让模拟器与真机行为分叉。"""
    r = client.post("/ingest", json={
        "kind": "heartbeat", "device_id": "Arduino1", "ts": TS, "uptime_ms": 1000,
    })
    assert r.status_code == 200


# ===== 密码与密钥的红线 =====

def test_login__password_never_in_response_or_logs(client, caplog):
    with caplog.at_level(logging.DEBUG):
        ok = client.post("/auth/login", json=CHRIS)
        bad = client.post("/auth/login", json={"email": CHRIS["email"], "password": "wrong-pw"})
        client.get("/auth/me")
    for response_text in (ok.text, bad.text):
        assert SEED_PASSWORD not in response_text
        assert "wrong-pw" not in response_text
        assert "password_hash" not in response_text
    assert SEED_PASSWORD not in caplog.text
    assert "wrong-pw" not in caplog.text


def test_default_jwt_secret__refuses_startup_outside_development():
    """决策 2: 宁可起不来, 也不带公开签名密钥上线。(_env_file=None 隔离本机 .env)"""
    with pytest.raises(RuntimeError):
        auth_service.validate_startup_security(
            Settings(_env_file=None, environment="production", jwt_secret="dev-only-change-me")
        )
    # 开发环境用默认值可以起; 非开发环境换了真密钥也可以起
    auth_service.validate_startup_security(Settings(_env_file=None, environment="development"))
    auth_service.validate_startup_security(
        Settings(_env_file=None, environment="production", jwt_secret="a-real-secret")
    )


# ===== 旧占位请求头零残留 =====

def test_legacy_actor_header__zero_residue_in_code_and_tests():
    """SPEC-004 决策 8: 旧请求头整体删除不留兼容, 用 grep 断言代码与测试零残留。"""
    needle = "X" + "[-_]" + "Actor"  # 拆开拼接, 免得本文件自己成为命中项
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["grep", "-riEn", needle,
         "apps/api/app", "apps/api/tests", "apps/device-sim", "packages"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.returncode == 1, f"仍有残留:\n{result.stdout or result.stderr}"


def test_operator__cross_zone_without_flag_is_403_not_422(client, auth_headers):
    """没资格跨区的人, 不带 flag 也该直接 403。

    修复前这里返回 422 "请加上 allow_cross_zone=true", operator 照做后再吃 403 ——
    提示把人引进了死胡同。次序改成"先问资格再问确认"之后, 从第一步就说清楚。
    """
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 2},  # Bo, Zone 2, 且刻意不带 flag
        headers=auth_headers("alex@example.com"),  # operator, 无跨区资格
    )
    assert r.status_code == 403


def test_operator__same_zone_with_redundant_flag_succeeds(client, auth_headers):
    """同区派单时 flag 是空操作, 不该因为多传了它就拒绝。"""
    incident_id = open_incident(client)  # Zone 1
    r = client.post(
        f"/incidents/{incident_id}/assign",
        json={"employee_id": 1, "allow_cross_zone": True},  # Alex, Zone 1 = 同区
        headers=auth_headers("alex@example.com"),
    )
    assert r.status_code == 200


def test_login__failure_timing_is_similar_for_unknown_and_wrong_password(client):
    """两条失败路径耗时必须在同一量级, 否则可以靠计时枚举出哪些邮箱注册过。

    修复前: 邮箱不存在时短路掉 bcrypt, 快上百倍。
    """
    import time

    def elapsed(email: str) -> float:
        t0 = time.perf_counter()
        r = client.post("/auth/login", json={"email": email, "password": "definitely-wrong"})
        assert r.status_code == 401
        return time.perf_counter() - t0

    unknown = min(elapsed("nobody@example.com") for _ in range(3))
    wrong_pw = min(elapsed("alex@example.com") for _ in range(3))
    ratio = max(unknown, wrong_pw) / max(min(unknown, wrong_pw), 1e-9)
    assert ratio < 3, f"两条失败路径耗时相差 {ratio:.1f} 倍, 存在计时侧信道"


# ===== 登录限流 (SPEC-004 决策 10) =====

@pytest.fixture
def small_limit():
    """把阈值临时调小: 每次失败都要跑一次 bcrypt(270ms), 默认阈值会让测试很慢。"""
    from app.routers.auth import login_limiter

    original = login_limiter.attempts
    login_limiter.attempts = 3
    yield login_limiter
    login_limiter.attempts = original


def _fail_login(c, email="alex@example.com"):
    return c.post("/auth/login", json={"email": email, "password": "definitely-wrong"})


def test_login_rate_limit__blocks_after_threshold_with_retry_after(client, small_limit):
    """连续失败到阈值后返回 429, 且带 Retry-After 告诉调用方还要等多久。"""
    for _ in range(small_limit.attempts):
        assert _fail_login(client).status_code == 401
    r = _fail_login(client)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_login_rate_limit__does_not_reveal_whether_email_exists(client, small_limit):
    """429 只看来源 IP, 与邮箱存不存在无关 —— 不能变成新的枚举探针。"""
    for _ in range(small_limit.attempts):
        _fail_login(client)
    known = _fail_login(client, "alex@example.com")
    unknown = _fail_login(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 429


def test_login_rate_limit__success_clears_the_counter(client, small_limit):
    """登录成功即清账: 正常用户敲错几次再登对, 不该被自己之前的手误拖累。"""
    for _ in range(small_limit.attempts - 1):
        _fail_login(client)
    ok = client.post(
        "/auth/login", json={"email": "alex@example.com", "password": SEED_PASSWORD}
    )
    assert ok.status_code == 200
    # 清账之后应该重新拥有完整的失败额度
    for _ in range(small_limit.attempts):
        assert _fail_login(client).status_code == 401


def test_login_rate_limit__is_per_ip_not_global():
    """按来源 IP 分开计数, 一个 IP 触顶不能连累另一个 ——
    否则限流本身就成了拒绝服务的工具, 这正是不按账号限流的理由。"""
    from app.services.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter(attempts=3, window_seconds=300)
    for _ in range(3):
        limiter.record_failure("10.0.0.1")
    assert limiter.retry_after("10.0.0.1") is not None   # 攻击者触顶
    assert limiter.retry_after("10.0.0.2") is None       # 旁人不受影响


def test_login_rate_limit__window_slides():
    """窗口滑过之后自动恢复, 不需要人工解锁。"""
    from app.services.rate_limit import LoginRateLimiter

    clock = {"t": 1000.0}
    limiter = LoginRateLimiter(attempts=2, window_seconds=60)
    limiter._now = lambda: clock["t"]  # type: ignore[method-assign]
    limiter.record_failure("ip")
    limiter.record_failure("ip")
    assert limiter.retry_after("ip") is not None
    clock["t"] += 61
    assert limiter.retry_after("ip") is None


def test_client_ip__ignores_forwarded_header_by_default():
    """默认不信 X-Forwarded-For: 那是客户端可以随便写的头,
    直接采信等于把限流关掉 —— 每次换个假来源就永远不触顶。"""
    from starlette.requests import Request

    from app.config import Settings
    from app.services.rate_limit import client_ip

    req = Request({
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.9")],
        "client": ("10.0.0.3", 5000),
    })
    assert client_ip(req, Settings(trust_proxy_headers=False)) == "10.0.0.3"
    # 只有明确声明"我在自己的反向代理后面"时才采信
    assert client_ip(req, Settings(trust_proxy_headers=True)) == "203.0.113.9"
