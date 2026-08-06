"""Sentinel API 入口。

W1 目标: /health + /ingest 落库 + /status 查询。
路由按域拆分, 业务逻辑一律下沉 services/, 引擎逻辑在 packages/policy_engine。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import apply_dev_seed, run_migrations
from .routers import auth, drills, incidents, ingest, status
from .services import auth_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sentinel")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时把数据库带到最新版本, 让 `docker compose up` 真正是一条命令。"""
    # 非开发环境带着默认 JWT 密钥, 宁可起不来 (SPEC-004 决策 2)
    auth_service.validate_startup_security(settings())
    revision = await run_migrations()
    log.info("database at alembic revision: %s", revision)
    if settings().apply_dev_seed:
        await apply_dev_seed()
        log.info("dev seed applied")
    yield


app = FastAPI(title="Sentinel API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,  # 会话走 httpOnly cookie, 跨端口请求需要显式允许带凭据 (SPEC-004)
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(status.router)
app.include_router(incidents.router)
app.include_router(drills.router)
# W3: policies; W4: agent_tasks(+SSE)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "sentinel-api", "version": app.version}
