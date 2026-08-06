"""登录与会话接口 + 鉴权依赖 —— SPEC-004。

token 的取用次序 (决策 3): 先看 httpOnly cookie, 没有再看 `Authorization: Bearer`。
cookie 是浏览器的主通道 (脚本读不到, XSS 偷不走); 请求头是 /docs Authorize 按钮
与 curl 的备用通道。两者并存不削弱安全性 —— 拿不到 cookie 也就拼不出请求头。

其它路由的鉴权从这里拿: `require_permission(...)` 返回一个可放进 Depends 的依赖,
权限点常量与角色映射在 services/auth_service (服务端推导, 不信任前端)。
"""
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..services import auth_service
from ..services.auth_service import (
    COOKIE_NAME,
    TOKEN_TTL,
    AuthUser,
    InvalidCredentials,
    InvalidToken,
)
from ..services.rate_limit import LoginRateLimiter, client_ip

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False: 缺请求头时不由它报错, 统一走下面的 401; 它同时负责让 /docs 出 Authorize 按钮
_bearer_scheme = HTTPBearer(auto_error=False)

# 只护 /auth/login —— 它是唯一对未登录流量敞开的入口 (决策 10)
login_limiter = LoginRateLimiter(
    attempts=settings().login_rate_limit_attempts,
    window_seconds=settings().login_rate_limit_window_seconds,
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    request: Request,
    session: SessionDep,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> AuthUser:
    token = request.cookies.get(COOKIE_NAME) or (bearer.credentials if bearer else None)
    if not token:
        raise _unauthorized("未登录")
    try:
        user_id = auth_service.decode_token(token)
    except InvalidToken:
        raise _unauthorized("凭据无效或已过期") from None
    user = await auth_service.get_user(session, user_id)
    if user is None:
        raise _unauthorized("凭据无效或已过期")
    return user


CurrentUserDep = Annotated[AuthUser, Depends(get_current_user)]


def require_permission(permission: str) -> Callable[..., Coroutine[Any, Any, AuthUser]]:
    """鉴权 + 授权二合一的依赖工厂: 未登录 401, 已登录但角色不具备该能力 403。"""

    async def dependency(user: CurrentUserDep) -> AuthUser:
        if not auth_service.has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"当前角色无 {permission} 权限")
        return user

    return dependency


class LoginPayload(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(
    payload: LoginPayload, session: SessionDep, response: Response, request: Request
) -> dict[str, Any]:
    ip = client_ip(request, settings())
    retry_after = login_limiter.retry_after(ip)
    if retry_after is not None:
        # 429 只看来源 IP, 与邮箱存不存在无关 —— 不给枚举探针
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁, 请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        user = await auth_service.authenticate(session, payload.email, payload.password)
    except InvalidCredentials:
        login_limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="邮箱或密码不正确") from None
    login_limiter.record_success(ip)  # 登录成功即清账, 手误不拖累后续
    token, expires_at = auth_service.create_token(user.id)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(TOKEN_TTL.total_seconds()),  # 与 token 内的 exp 同一个 TTL (决策 4)
        httponly=True,
        samesite="lax",
        path="/",
        secure=not auth_service.is_development(settings()),  # 非本地环境要求 HTTPS (决策 3)
    )
    # 响应体不回 token 原文 (决策 3): 回了就破坏了 "JavaScript 读不到" 的前提
    return {
        "ok": True,
        "user": auth_service.public_user(user),
        "roles": user.roles,
        "expires_at": expires_at.isoformat(),
    }


@router.post("/logout")
async def logout(response: Response) -> dict[str, Any]:
    """清 cookie (置空 + 立即过期)。JWT 无服务端状态, 8 小时短有效期兜底 (决策 4)。"""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    return {"ok": True, **await auth_service.me_payload(session, user)}
