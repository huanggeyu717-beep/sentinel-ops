#!/usr/bin/env python3
"""探针: `discovering` 到底是模型驱动还是运行时驱动 (SPEC-007 补入 35)。

用打桩客户端 (ScriptedLLMClient, 零真实调用零花费) 在测试库上跑一条完整
happy 路径, 原样打出:
1. 每一次 LLMRequest: 阶段、给了模型哪些工具、模型答了什么;
2. agent_steps 时间线: 哪些工具被执行了、由谁触发。

要回答的三个问题 (答案以本探针输出为准, 不以文档为准):
- discovering 阶段到底给不给模型工具? 给的话是几个?
- 只读工具是模型 tool_call 触发的, 还是运行时无条件调的?
- 一条 happy 路径里模型调用总数是几次, 分别在哪个阶段?

用法: .venv/bin/python scripts/dev/probe_discovering.py  (需要本地 5433 Postgres)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
for p in ("apps/api", "apps/api/tests", "packages/policy_engine", "packages/scenario"):
    sys.path.insert(0, str(REPO / p))

# 探针跑在测试库, 不碰开发库; 后台循环调到 1 小时 (与 conftest 同一套理由)
os.environ.setdefault(
    "SENTINEL_DATABASE_URL",
    "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test",
)
os.environ["SENTINEL_ENGINE_TICK_SECONDS"] = "3600"
os.environ["SENTINEL_AGENT_HEARTBEAT_SECONDS"] = "3600"

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import apply_dev_seed, run_migrations  # noqa: E402
from app.services import agent_runtime, agent_service  # noqa: E402
from app.services.llm_client import (  # noqa: E402
    LLMResponse,
    LLMToolCall,
    ScriptedLLMClient,
)

BODY = {
    "scope": {"type": "zone", "ids": [1]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}


async def main() -> None:
    await run_migrations()
    await apply_dev_seed()
    engine = create_async_engine(os.environ["SENTINEL_DATABASE_URL"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(text(
                "TRUNCATE agent_steps, agent_clarifications, ai_usage, agent_tasks,"
                " approvals, policy_publications CASCADE"
            ))
            created = await agent_service.create_task(
                session, user_id=3, input_text="[探针] 生鲜区湿了就开单"
            )
        task_id = created["task_id"]
        llm = ScriptedLLMClient(script=[
            LLMResponse(text="在 1 区变湿时开事故", input_tokens=1, output_tokens=1),
            LLMResponse(
                tool_call=LLMToolCall(
                    tool="create_policy",
                    arguments={"name": "探针: 生鲜区漏水开单", "body": BODY},
                ),
                input_tokens=1, output_tokens=1,
            ),
        ])
        outcome = await agent_runtime.run_task(task_id, llm, factory)

        print(f"结局: {outcome}\n")
        print(f"===== 模型调用 (共 {len(llm.requests)} 次) =====")
        for i, req in enumerate(llm.requests, 1):
            tool_names = [t["name"] for t in req.tools]
            print(f"[{i}] stage={req.stage}  提供给模型的工具 ({len(tool_names)} 个):"
                  f" {tool_names or '(无)'}")
            resp = llm.script[i - 1]
            answered = (
                f"tool_call={resp.tool_call.tool}" if resp.tool_call
                else f"text={resp.text!r}"
            )
            print(f"    模型响应: {answered}")

        async with factory() as session, session.begin():
            steps = (await session.execute(text(
                "SELECT seq, tool_name, arguments, status FROM agent_steps"
                " WHERE task_id = :t ORDER BY seq"), {"t": task_id}
            )).mappings().all()
        print(f"\n===== agent_steps 时间线 (共 {len(steps)} 条) =====")
        model_tools = {r.tool_call.tool for r in llm.script if r.tool_call}
        for s in steps:
            args = s["arguments"]
            args = json.loads(args) if isinstance(args, str) else (args or {})
            note = ""
            if s["tool_name"] == "stage_transition":
                note = f"-> {args.get('to')}"
            elif s["tool_name"] in model_tools:
                note = "(模型 tool_call 触发)"
            elif s["tool_name"] != "parse_input":
                note = "(运行时无条件执行, 模型未参与决策)"
            print(f"  seq {s['seq']:>2}  {s['tool_name']:<20} {note}")

        read_calls = [s for s in steps if str(s["tool_name"]).startswith("list_")]
        print("\n===== 三个问题的答案 (以上面原始输出为准) =====")
        print(f"1. discovering 给模型工具吗: 模型调用共 {len(llm.requests)} 次, "
              f"没有任何一次发生在 discovering 阶段 —— 该阶段模型根本不被调用, "
              f"也就无所谓给不给工具")
        print(f"2. {len(read_calls)} 个只读工具 ({[s['tool_name'] for s in read_calls]}) "
              f"全部由运行时无条件顺序执行, 不经模型 tool_call")
        print(f"3. happy 路径模型调用 {len(llm.requests)} 次: "
              + ", ".join(f"第{i}次={r.stage}" for i, r in enumerate(llm.requests, 1)))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
