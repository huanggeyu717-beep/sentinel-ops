"""登录与权限 —— SPEC-004 (JWT + RBAC)。

设计要点(面试可讲):
1. **密码 bcrypt cost 12** (决策 1), 明文不进任何日志与响应体, 有测试专门断言;
2. **JWT HS256 + httpOnly cookie** (决策 2/3): token 不回响应体 —— 回了就等于把
   "JavaScript 读不到"这个前提自己破坏掉; `Authorization: Bearer` 是备用通道,
   保证 /docs 的 Authorize 按钮与 curl 可用, 取用次序在路由层 (cookie 优先);
3. **cookie 的过期与 token 的 exp 同源** (决策 4): cookie 过期只是浏览器不再发送,
   服务端永远自己校验 exp;
4. **权限由服务端从角色推导** (决策 5/6): 一个用户可有多个角色, 能力取并集,
   永不信任前端或模型传来的角色声明;
5. **非开发环境禁用默认签名密钥** (决策 2): 宁可起不来, 也不带公开密钥上线。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import bcrypt
from jose import JWTError, jwt  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, settings

COOKIE_NAME = "sentinel_token"
TOKEN_TTL = dt.timedelta(hours=8)  # 决策 4: 8 小时, 无 refresh; 过期即重新登录
_JWT_ALGORITHM = "HS256"
_DEFAULT_JWT_SECRET = "dev-only-change-me"
_BCRYPT_ROUNDS = 12

# 邮箱不存在时拿它跑一次 bcrypt, 把两条失败路径的耗时拉平 (见 authenticate 的注释)。
# 预先算好写死而不是启动时现算: 省掉每次进程启动 ~270ms, 且结果可复现。
# 它不是任何真实密码的哈希, 永远不会匹配成功。
_TIMING_EQUALIZER_HASH = "$2b$12$a20TpkQebs6fmN93cBplOujCR3z38iHX95G.G.Aty58.plfVTlMcK"

# ===== 权限点 (决策 6 的表, 服务端强制) =====

PERM_READ = "read"                                      # /status/* 与 /incidents 只读
PERM_INCIDENT_TRANSITION = "incidents:transition"       # assign / acknowledge / resolve
PERM_CROSS_ZONE_ASSIGN = "incidents:cross_zone_assign"  # 跨区派单显式放行
PERM_APPROVE_POLICY = "policies:approve"                # W3 审批发布, 此处先占位
PERM_MANAGE_USERS = "users:manage"                      # 用户与角色管理
PERM_TRIGGER_DRILL = "drills:trigger"                   # 触发演练 (SPEC-005 决策 6: operator+)

_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({PERM_READ}),
    "operator": frozenset({PERM_READ, PERM_INCIDENT_TRANSITION, PERM_TRIGGER_DRILL}),
    "manager": frozenset(
        {PERM_READ, PERM_INCIDENT_TRANSITION, PERM_CROSS_ZONE_ASSIGN,
         PERM_APPROVE_POLICY, PERM_TRIGGER_DRILL}
    ),
    "admin": frozenset(
        {PERM_READ, PERM_INCIDENT_TRANSITION, PERM_CROSS_ZONE_ASSIGN,
         PERM_APPROVE_POLICY, PERM_MANAGE_USERS, PERM_TRIGGER_DRILL}
    ),
}


class InvalidCredentials(Exception):
    """邮箱不存在或密码不对 -> 401。刻意不区分两种情况, 不给枚举邮箱的探针。"""


class InvalidToken(Exception):
    """token 缺失 / 伪造 / 过期 -> 401。"""


@dataclass(slots=True)
class AuthUser:
    id: int
    email: str
    display_name: str
    employee_id: int | None  # 绑定的现场员工, 可空 (SPEC-004 方案 A)
    roles: list[str]


def validate_startup_security(s: Settings) -> None:
    """决策 2: 非开发环境仍用默认 JWT 密钥时拒绝启动, 在 lifespan 最前面调用。"""
    if s.environment != "development" and s.jwt_secret == _DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "SENTINEL_JWT_SECRET 仍是默认值, 非开发环境拒绝启动 (SPEC-004 决策 2)"
        )


def is_development(s: Settings) -> bool:
    return s.environment == "development"


# ===== 密码 =====

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


# ===== token =====

def create_token(user_id: int, now: dt.datetime | None = None) -> tuple[str, dt.datetime]:
    """签发 JWT, 返回 (token, 过期时间)。cookie 的 Max-Age 必须取同一个 TTL (决策 4)。"""
    issued = now if now is not None else dt.datetime.now(dt.UTC)
    expires_at = issued + TOKEN_TTL
    token: str = jwt.encode(
        {"sub": str(user_id), "iat": issued, "exp": expires_at},
        settings().jwt_secret,
        algorithm=_JWT_ALGORITHM,
    )
    return token, expires_at


def decode_token(token: str) -> int:
    """校验签名与 exp, 返回 user id。任何一种失败都归成同一个 InvalidToken。"""
    try:
        claims = jwt.decode(token, settings().jwt_secret, algorithms=[_JWT_ALGORITHM])
    except JWTError as e:
        raise InvalidToken from e
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        raise InvalidToken
    return int(sub)


# ===== 用户查询 (services 层碰数据库, 路由层只拼装) =====

_USER_BY_EMAIL = text("""
    SELECT id, email, password_hash, display_name, employee_id FROM users WHERE email = :email
""")

_USER_BY_ID = text("""
    SELECT id, email, display_name, employee_id FROM users WHERE id = :id
""")

_ROLES = text("""
    SELECT r.name FROM roles r
    JOIN user_roles ur ON ur.role_id = r.id
    WHERE ur.user_id = :id
    ORDER BY r.name
""")

_EMPLOYEE = text("SELECT id, name, zone_id FROM employees WHERE id = :id")


async def _load_roles(session: AsyncSession, user_id: int) -> list[str]:
    rows = (await session.execute(_ROLES, {"id": user_id})).scalars().all()
    return [str(name) for name in rows]


async def authenticate(session: AsyncSession, email: str, password: str) -> AuthUser:
    """邮箱 + 密码换用户。失败抛 InvalidCredentials, 路由层转 401。

    **两条失败路径耗时必须接近**: 不区分"邮箱不存在"与"密码不对"只做到一半 ——
    若邮箱不存在时直接返回, 就不会跑 bcrypt, 响应快上百倍, 攻击者靠计时即可枚举
    哪些邮箱注册过。所以邮箱不存在时也故意跑一次 bcrypt 做无用功。
    """
    row = (await session.execute(_USER_BY_EMAIL, {"email": email})).mappings().one_or_none()
    if row is None:
        verify_password(password, _TIMING_EQUALIZER_HASH)  # 故意做无用功, 拉平耗时
        raise InvalidCredentials
    if not verify_password(password, row["password_hash"]):
        raise InvalidCredentials
    return AuthUser(
        id=row["id"], email=row["email"], display_name=row["display_name"],
        employee_id=row["employee_id"], roles=await _load_roles(session, row["id"]),
    )


async def get_user(session: AsyncSession, user_id: int) -> AuthUser | None:
    """按 token 里的 id 取用户。每个请求回表一次: 用户被删即刻失效, 不等 token 过期。"""
    row = (await session.execute(_USER_BY_ID, {"id": user_id})).mappings().one_or_none()
    if row is None:
        return None
    return AuthUser(
        id=row["id"], email=row["email"], display_name=row["display_name"],
        employee_id=row["employee_id"], roles=await _load_roles(session, row["id"]),
    )


def public_user(user: AuthUser) -> dict[str, Any]:
    """响应体里的用户表示 —— 永远不含 password_hash, 更不含 token。"""
    return {
        "id": user.id, "email": user.email,
        "display_name": user.display_name, "employee_id": user.employee_id,
    }


async def me_payload(session: AsyncSession, user: AuthUser) -> dict[str, Any]:
    """GET /auth/me 的主体: 用户 + 角色 + 绑定的员工 (若有)。"""
    employee: dict[str, Any] | None = None
    if user.employee_id is not None:
        row = (
            await session.execute(_EMPLOYEE, {"id": user.employee_id})
        ).mappings().one_or_none()
        employee = dict(row) if row is not None else None
    return {"user": public_user(user), "roles": user.roles, "employee": employee}


def has_permission(user: AuthUser, permission: str) -> bool:
    """多角色取并集 (决策 5)。"""
    return any(permission in _ROLE_PERMISSIONS.get(role, frozenset()) for role in user.roles)
