"""Sentinel API 入口。

W1 目标: /health + /ingest 落库 + /status 查询。
路由按域拆分, 业务逻辑一律下沉 services/, 引擎逻辑在 packages/policy_engine。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import apply_dev_seed, run_migrations
from .routers import ingest, status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sentinel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时把数据库带到最新版本, 让 `docker compose up` 真正是一条命令。"""
    applied = await run_migrations()
    log.info("migrations applied: %s", applied or "(already up to date)")
    if settings().apply_dev_seed:
        await apply_dev_seed()
        log.info("dev seed applied")
    yield


app = FastAPI(title="Sentinel API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(ingest.router)
app.include_router(status.router)
# W2: auth, incidents; W3: policies; W4: agent_tasks(+SSE)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "sentinel-api", "version": app.version}
