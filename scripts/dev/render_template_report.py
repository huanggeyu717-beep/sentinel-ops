#!/usr/bin/env python3
"""模板报告: 把一条事故的事实包逐条铺成一句话 —— 全量、按序、不做取舍。

这就是 SPEC-008 第六节那次并排对照里"模板"的定义: 有什么说什么。它与 Agent
共用**同一份事实包** (report_task_service.load_fact_pack), 两份报告的差别因此
只剩取舍 —— 对照要证明的正是"取舍是模型干的活, 事实是代码给的"。

- **纯文本输出到 stdout, 不落库**: 模板那份本来就不该进 incident_reports
  ("脚本产出、直接进 README", SPEC-008 第七节), 同一条事故已有 Agent 那份时,
  插第二行也会被部分唯一索引拦住;
- **确定性**: 同一条事故跑两次输出逐字节相同 —— 事实包里没有"现在时刻",
  时区显式传入 (report_task_service.REPORT_TZ), 不读机器时区。
  这一条由 apps/api/tests/test_render_template_report.py 钉住。

用法 (从仓库根, venv 内):
    python scripts/dev/render_template_report.py <incident_id>
库地址取 SENTINEL_DATABASE_URL; 不设则用本机开发库默认值 (只读, 不同于
reset_demo_data 那种删数据脚本, 允许有默认)。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
API_DIR = REPO_ROOT / "apps" / "api"

DEFAULT_URL = "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel"


def render_lines(fact_pack: list[dict[str, Any]]) -> str:
    """每条事实一句话, 按事实包顺序, 一条不落。缺失的照样铺 ("无此记录") ——
    模板不做取舍, 这正是它与 Agent 那份的全部差别。"""
    return "".join(f"{fact['label']}: {fact['text']}。\n" for fact in fact_pack)


async def _load(url: str, incident_id: int) -> list[dict[str, Any]]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services import report_task_service

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return await report_task_service.load_fact_pack(session, incident_id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("incident_id", type=int)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SENTINEL_DATABASE_URL", DEFAULT_URL),
    )
    args = parser.parse_args()

    # app.* 的 import 链在 import 时读 settings, 先把目标库写回环境再 import
    os.environ["SENTINEL_DATABASE_URL"] = args.database_url
    sys.path.insert(0, str(API_DIR))
    from app.services.incident_service import IncidentNotFound

    try:
        fact_pack = asyncio.run(_load(args.database_url, args.incident_id))
    except IncidentNotFound:
        print(f"事故 {args.incident_id} 不存在", file=sys.stderr)
        raise SystemExit(1) from None
    sys.stdout.write(render_lines(fact_pack))


if __name__ == "__main__":
    main()
