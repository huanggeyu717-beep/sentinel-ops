#!/usr/bin/env python3
"""每日演示数据重置 (SPEC-009 第三节): 清访客产生的数据 -> 重跑种子 -> 回放真实读数。

这是一段会删数据的代码, **它的护栏比它本身重要**:

1. **拒绝在非演示库上运行**: 库里的 demo_marker 表必须**有那一行** (由生产种子
   写入, 第二段), 否则当场退出、非零码、一个字不写。注意判据是"有行"不是
   "有表" —— 迁移 0010 给每个库都建了这张表, 只有演示库里有行;
2. **目标库必须显式给出** (--database-url 或 SENTINEL_DATABASE_URL), 不设
   隐含默认 —— 删数据的脚本不该"碰巧"连上一个库;
3. **不碰花钱台账** (llm_spend_daily / user_task_quota_daily): 重置是给访客
   一个干净的演示现场, 不是一个"把今天的预算清零再来一轮"的按钮; 也不碰
   demo_marker 本身 (通行证要活过每一次重置)。

清的是访客动得到的东西: 遥测、事故、策略全链、Agent 任务全链、审计。
不清身份与库存表 (users/zones/devices/sensors/employees) —— 种子对它们是
幂等 upsert (缺了补上, 手工标注不覆盖), 访客也没有改它们的接口。

回放走 device-sim 的 --batch 通道 (HTTP /ingest/batch, 幂等, W1 验证过
"提交 1258 条, 新增 0 条"), 所以回放步骤要求 API 在跑; --skip-replay 跳过
(测试与"API 还没起"的场合用)。

用法 (从仓库根执行):
    SENTINEL_DATABASE_URL=postgresql+asyncpg://... \\
        python scripts/ops/reset_demo_data.py [--base-url http://localhost:8000] \\
                                              [--skip-replay]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
API_DIR = REPO_ROOT / "apps" / "api"
READINGS_CSV = REPO_ROOT / "apps" / "device-sim" / "seed" / "waterlevel_readings.csv"

# 与 conftest / test_agent_helpers 的清表同一讲究: 外键相互引用的表必须
# 同一条 TRUNCATE。RESTART IDENTITY 让演示里的 id 每天从小数字起, 顺眼。
DEMO_TABLES = (
    "waterlevel_readings", "rfid_scans", "device_heartbeats", "sensorstate",
    # incident_reports 引用 incidents 与 agent_tasks, 不同列会让整条 TRUNCATE 报错;
    # 报告本身也是访客生成的数据, 语义上就该清 (W6 SPEC-008)
    "incident_reports",
    "incident_events", "incidents", "audit_log",
    "policy_runs", "policy_publications", "approvals",
    "policy_versions", "policies",
    "agent_steps", "agent_clarifications", "ai_usage", "agent_tasks",
)

REFUSE_EXIT_CODE = 2


def _fail(message: str) -> NoReturn:
    print(f"拒绝执行: {message}", file=sys.stderr)
    raise SystemExit(REFUSE_EXIT_CODE)


async def _reset(dsn: str, *, skip_replay: bool, base_url: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # ---- 护栏: 先验通行证, 验不过一个字不写 ----
        try:
            marker = await conn.fetchrow("SELECT marked_at, note FROM demo_marker")
        except asyncpg.UndefinedTableError:
            _fail(
                "库里没有 demo_marker 表 (迁移 0010 都没跑过的库更不该被这脚本碰)"
            )
        if marker is None:
            _fail(
                "demo_marker 表是空的 —— 这不是演示库 (通行证那一行由生产种子写入; "
                "本机验证可手工 INSERT INTO demo_marker DEFAULT VALUES)"
            )
        print(f"通行证在: marked_at={marker['marked_at']} note={marker['note']!r}")

        # ---- 清访客数据 (花钱台账与通行证刻意不在清单里, 见模块注释) ----
        await conn.execute(
            f"TRUNCATE {', '.join(DEMO_TABLES)} RESTART IDENTITY"
        )
        print(f"已清空 {len(DEMO_TABLES)} 张演示数据表")

        # ---- 重跑种子: 与 API 启动种子**同一份 SQL** (app.db.DEV_SEED_SQL),
        # 不抄第二份 —— 两份种子迟早走散 ----
        from app.db import DEV_SEED_SQL

        await conn.execute(DEV_SEED_SQL)
        print("种子已重放 (幂等 upsert)")
    finally:
        await conn.close()

    # ---- 回放真实读数 (走 HTTP, 幂等; 清过表所以全量重新入库) ----
    if skip_replay:
        print("按 --skip-replay 跳过读数回放")
        return
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "apps" / "device-sim" / "sim.py"),
         str(READINGS_CSV), "--batch", "--base-url", base_url],
        check=True, cwd=REPO_ROOT,
    )
    print("真实读数已回放")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SENTINEL_DATABASE_URL", ""),
        help="目标库 (默认取 SENTINEL_DATABASE_URL; 两处都没有则拒绝执行)",
    )
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="回放读数投递到的 API 地址")
    parser.add_argument("--skip-replay", action="store_true",
                        help="只清库与重种, 不回放读数 (API 未起时用)")
    args = parser.parse_args()

    if not args.database_url:
        _fail("没有给目标库: 传 --database-url 或设 SENTINEL_DATABASE_URL "
              "(删数据的脚本不设隐含默认)")

    # app.db 按 SENTINEL_DATABASE_URL 组装 (import 时读 settings), 所以先把
    # 命令行覆盖写回环境, 再让 _reset 里的 import 生效
    os.environ["SENTINEL_DATABASE_URL"] = args.database_url
    sys.path.insert(0, str(API_DIR))

    dsn = args.database_url.replace("+asyncpg", "")
    asyncio.run(_reset(dsn, skip_replay=args.skip_replay, base_url=args.base_url))
    print("重置完成")


if __name__ == "__main__":
    main()
