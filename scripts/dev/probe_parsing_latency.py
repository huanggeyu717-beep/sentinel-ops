#!/usr/bin/env python3
"""探针: parsing 请求为什么超 60 秒 (W4 第二段补录, 一次性诊断脚本)。

现象: smoke 的小请求 3 秒返回, 状态机的 parsing 请求连续两次 60 秒超时。
假设: doubao-seed-2.1-pro 默认开深度思考 (smoke 响应里有 reasoning_content),
对着大 system prompt 长考不止。

做法: 用与 ArkLLMClient 完全同形的 payload 发**同一个 parsing 请求**两次,
一次默认、一次 thinking disabled, 各给 180 秒, 量真实耗时与 reasoning_tokens。
共 2 次真实调用。项目规矩: 凡是觉得有问题, 先写个探针跑一遍再说。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO / "apps/api"))
sys.path.insert(0, str(REPO / "packages/policy_engine"))
sys.path.insert(0, str(REPO / "packages/scenario"))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import agent_prompts  # noqa: E402

HAPPY_INPUT = "生鲜区两个探头三分钟内都湿了就通知这个区的主管"


async def one(client: httpx.AsyncClient, label: str, extra: dict) -> None:
    cfg = settings()
    payload = {
        "model": cfg.llm_model,
        "messages": agent_prompts.build_messages("parsing", input_text=HAPPY_INPUT),
        "tools": [{"type": "function", "function": t}
                  for t in agent_prompts.tool_schemas(("ask_clarification",))],
        **extra,
    }
    t0 = time.perf_counter()
    resp = await client.post(
        f"{cfg.llm_base_url}/chat/completions", json=payload,
        headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
    )
    dt = time.perf_counter() - t0
    data = resp.json()
    usage = data.get("usage", {})
    msg = (data.get("choices") or [{}])[0].get("message", {})
    print(f"[{label}] {resp.status_code} {dt:.1f}s "
          f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
          f"reasoning={usage.get('completion_tokens_details', {}).get('reasoning_tokens')}")
    print(f"  content={json.dumps(msg.get('content'), ensure_ascii=False)[:200]}")
    print(f"  tool_calls={json.dumps(msg.get('tool_calls'), ensure_ascii=False)[:200]}")


async def main() -> None:
    assert settings().llm_api_key, "需要 .env 里的 key"
    async with httpx.AsyncClient(timeout=180) as client:
        await one(client, "默认 (thinking 未指定)", {})
        await one(client, "thinking=disabled", {"thinking": {"type": "disabled"}})


asyncio.run(main())
