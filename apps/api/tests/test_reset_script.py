"""重置脚本的护栏 (SPEC-009 第三节): 它是一段会删数据的代码, 护栏比它本身重要。

脚本经 subprocess 真实执行 (不是 import 后调函数): 护栏的承诺是"进程退出、
非零码", 只有真跑一个进程才断言得到退出码。回放步骤用 --skip-replay 跳过
(它走 HTTP 要 API 在跑, 属第二段的 compose 验收; 这里测的是删数据前后的护栏)。

变异 3 的正主: 把脚本里的 demo_marker 检查删掉,
test_reset__refuses_without_marker_and_touches_nothing 必须红。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from conftest import insert_published_policy
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "reset_demo_data.py"


def run_reset(*args: str, with_url: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if not with_url:
        env.pop("SENTINEL_DATABASE_URL", None)
    # conftest 已把 SENTINEL_DATABASE_URL 指到测试库, 原样透传即可
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


async def _seed_junk(conn) -> int:
    """造一份"访客点过"的现场: 一条事故 + 一条任务。返回事故 id。"""
    incident_id = await conn.fetchval(
        "INSERT INTO incidents (zone_id, sensor_id, severity, status) "
        "VALUES (1, 1, 'normal', 'open') RETURNING id"
    )
    await insert_task(conn, input_hash="reset-junk", status="completed")
    return incident_id


def test_reset__refuses_without_database_url():
    r = run_reset("--skip-replay", with_url=False)
    assert r.returncode == 2, r.stderr
    assert "目标库" in r.stderr


def test_reset__refuses_without_marker_and_touches_nothing(client):
    db(_seed_junk)  # conftest 每用例清空 demo_marker, 所以这里天然没有通行证

    r = run_reset("--skip-replay")
    assert r.returncode == 2, f"stdout={r.stdout} stderr={r.stderr}"
    assert "demo_marker" in r.stderr

    async def survivors(conn):
        return (
            await conn.fetchval("SELECT count(*) FROM incidents"),
            await conn.fetchval("SELECT count(*) FROM agent_tasks"),
        )

    assert db(survivors) == (1, 1)  # 拒绝 = 一个字都没写


def test_reset__with_marker_clears_junk_keeps_seed_and_ledger(client):
    async def seed(conn):
        await conn.execute(
            "INSERT INTO demo_marker (note) VALUES ('测试通行证')"
        )
        # 花钱台账刻意留一行: 重置不许变成"把预算清零再来一轮"的按钮
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 1.0, 3.0)"
        )
        return await _seed_junk(conn)

    db(seed)
    asyncio.run(insert_published_policy("重置前的访客策略", "{}"))

    r = run_reset("--skip-replay")
    assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"

    async def after(conn):
        return {
            "incidents": await conn.fetchval("SELECT count(*) FROM incidents"),
            "tasks": await conn.fetchval("SELECT count(*) FROM agent_tasks"),
            "policies": await conn.fetchval("SELECT count(*) FROM policies"),
            "sensors": await conn.fetchval("SELECT count(*) FROM sensors"),
            "users": await conn.fetchval("SELECT count(*) FROM users"),
            "marker": await conn.fetchval("SELECT count(*) FROM demo_marker"),
            "spent": await conn.fetchval("SELECT spent_cny FROM llm_spend_daily"),
        }

    got = db(after)
    assert got["incidents"] == 0 and got["tasks"] == 0 and got["policies"] == 0
    assert got["sensors"] == 6      # 种子重放到位 (5 个在位传感器 + 占位 0 号)
    assert got["users"] >= 3        # 登录账号还在
    assert got["marker"] == 1       # 通行证活过重置
    assert float(got["spent"]) == 1.0  # 台账原封不动 (护栏不随演示数据清零)
