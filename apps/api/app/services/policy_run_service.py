"""线上触发历史查询 —— GET /policy-runs (SPEC-006 第五节)。

单独一个模块而不是塞进 policy_service: 那份文件第二段已定稿, 本段只加壳不动内核;
这里只有一条只读查询, 与生命周期逻辑没有耦合。放 services 层是不变量 4 的要求
(只有 services 层可以碰数据库), W4 的 Agent tools 查触发历史也复用这一份。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 冗余列 policy_id 与 (policy_id, fired_at) 索引是迁移 0007 专为这条查询加的;
# NULL 参数一律显式 CAST —— asyncpg 对 text() 里的裸 NULL 推不出类型。
_RUNS = text("""
    SELECT pr.id, pr.policy_id, pr.policy_version_id, pv.version, pr.fired_at, pr.effects
    FROM policy_runs pr
    JOIN policy_versions pv ON pv.id = pr.policy_version_id
    WHERE (CAST(:policy_id AS bigint) IS NULL OR pr.policy_id = CAST(:policy_id AS bigint))
      AND (CAST(:since AS timestamptz) IS NULL OR pr.fired_at >= CAST(:since AS timestamptz))
      AND (CAST(:until AS timestamptz) IS NULL OR pr.fired_at < CAST(:until AS timestamptz))
    ORDER BY pr.fired_at DESC, pr.id DESC
    LIMIT :limit
""")


async def list_runs(
    session: AsyncSession,
    policy_id: int | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按策略/时间过滤的触发历史, 新的在前。effects 统一还原成 list。"""
    rows = (
        await session.execute(_RUNS, {
            "policy_id": policy_id, "since": since, "until": until,
            "limit": min(limit, 1000),
        })
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if isinstance(d["effects"], str):  # jsonb 经不同驱动可能回来是 str
            d["effects"] = json.loads(d["effects"])
        out.append(d)
    return out
