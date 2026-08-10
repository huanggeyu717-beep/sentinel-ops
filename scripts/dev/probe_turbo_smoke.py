#!/usr/bin/env python3
"""turbo model id 冒烟验证 (L0 停顿点决定 3): **恰好一次**真实调用。

验证 config.llm_model_turbo 这个 id 在方舟接口上真实存在且能对话。
- 会真实花钱 (约 ¥0.0002), 结果记 evals/COST.md;
- 失败时打印状态码与响应原文前 300 字 (key 永不打印), 由本人判改 .env 还是改配置;
- 直接用 httpx 单发, 不走 ArkLLMClient —— 那条路挂着录制层与状态机语义,
  冒烟只回答"这个 id 存不存在"一个问题。

用法: .venv/bin/python scripts/dev/probe_turbo_smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO / "apps" / "api"))

from app.config import settings  # noqa: E402


def main() -> int:
    cfg = settings()
    if not cfg.llm_api_key:
        print("缺 SENTINEL_LLM_API_KEY (.env), 不发请求")
        return 1
    model = cfg.llm_model_turbo
    print(f"冒烟目标: {model} @ {cfg.llm_base_url} (恰好 1 次调用)")
    resp = httpx.post(
        f"{cfg.llm_base_url}/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "只回复两个字: 收到"}],
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
        },
        headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"失败: HTTP {resp.status_code}")
        print(f"响应原文 (前 300 字): {resp.text[:300]}")
        return 2
    data = resp.json()
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    # turbo 单价 = pro 的一半 (SPEC-007 第四节)
    cost = (tokens_in * cfg.llm_price_input_per_mtok / 2
            + tokens_out * cfg.llm_price_output_per_mtok / 2) / 1_000_000
    print(f"成功: 服务端回报 model={data.get('model')}")
    print(f"回复: {data['choices'][0]['message'].get('content')!r}")
    print(f"tokens {tokens_in}/{tokens_out}, 估算 ¥{cost:.6f} —— 记 COST.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
