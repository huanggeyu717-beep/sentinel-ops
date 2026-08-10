#!/usr/bin/env python3
"""录制 / 回放 / 计时 / 连通性验证脚本 (W4 第二段补录, SPEC-002 第八、九节)。

这是整个项目**唯一会真实调用模型、真实花钱**的代码, 所以它有两条别的脚本
没有的规矩:

1. 它必须活在仓库里被复核 (CLAUDE.md 协作红线: AI 产生的文件必须落在仓库内。
   第二段曾把它写进 /private/tmp —— 重启即清、不进版本库、恰恰绕过复核,
   本次挪回并补上这条注释);
2. **整脚本一次运行最多发出 MAX_REAL_CALLS = 20 次真实调用, 超出直接退出**。
   录制脚本失控 (状态机绕圈、回放键失配导致每次都真调) 是这类工具最常见的
   翻车方式, 而它跑的时候人多半没盯着。单条任务正常 3-6 次调用就该结束;
   打到上限说明有东西在绕圈, 先看已录下的 cassette 与 ai_usage 再决定重跑。

用法 (从仓库任意位置跑, 需要 .env 里有真 key; 详见 --help):
    python scripts/dev/record_cassettes.py smoke           # 最小连通性验证 (1 次真调)
    python scripts/dev/record_cassettes.py record happy    # 录 HAPPY_INPUT 那条任务
    python scripts/dev/record_cassettes.py record repair   # 录 REPAIR_INPUT 那条任务
    python scripts/dev/record_cassettes.py record clarify  # 录 CLARIFY_INPUT (含糊输入)
    python scripts/dev/record_cassettes.py replay happy    # 用 tests/cassettes 离线复跑
    python scripts/dev/record_cassettes.py time 5          # 5 次真实调用计时 (mode=off)

库存钉死: import test_agent_llm.pin_canonical_inventory, 与回放测试完全同一份
(库存进 prompt、prompt 进回放键, 两边不同源回放必失配)。

record 子命令用"命中走回放、没命中才真调并落盘"的组合 (只在本脚本里拼,
三态客户端刻意不提供这个模式) —— 手编 cassette 之后重跑, 只有因手编而新
出现的请求会真调, 其余全走已有录制。

数据库用测试库 (默认 localhost:5433/sentinel_test, 5433 见 docker-compose.yml
注释): 录制的任务行是一次性的, 不该混进开发库。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)  # settings() 从 CWD 读 .env
sys.path.insert(0, str(REPO / "apps/api"))
sys.path.insert(0, str(REPO / "apps/api/tests"))
# 本地包不装进 venv, 与 pytest.ini 的 pythonpath 同一份路径
sys.path.insert(0, str(REPO / "packages/policy_engine"))
sys.path.insert(0, str(REPO / "packages/scenario"))
os.environ.setdefault(
    "SENTINEL_DATABASE_URL",
    os.environ.get(
        "SENTINEL_TEST_DATABASE_URL",
        "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test",
    ),
)

import httpx  # noqa: E402
import test_agent_llm as T  # noqa: E402
from sqlalchemy import text as sqltext  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import agent_prompts, agent_runtime, agent_service  # noqa: E402
from app.services.llm_client import (  # noqa: E402
    DEFAULT_TEMPERATURE,
    ArkLLMClient,
    LLMRequest,
    RecordReplayLLMClient,
    ReplayMiss,
)

T.pin_canonical_inventory()
CASSETTES = REPO / "apps/api/tests/cassettes"

# ===== 真实调用硬上限 (整脚本一次运行, 见模块 docstring 第 2 条) =====

MAX_REAL_CALLS = 20
_real_calls = 0


def _count_real_call(label: str) -> None:
    global _real_calls
    _real_calls += 1
    if _real_calls > MAX_REAL_CALLS:
        print(f"!! 第 {_real_calls} 次真实调用被拦下 (整脚本硬上限 {MAX_REAL_CALLS}, "
              f"最后一次: {label})。")
        print("!! 单条任务正常 3-6 次调用就该结束; 打到上限说明脚本或状态机在绕圈")
        print("!! (最常见: 回放键失配导致每次都真调)。先看 tests/cassettes 与")
        print("!! ai_usage 里已经发生了什么, 再决定要不要重跑。")
        raise SystemExit(2)
    print(f"  [real {_real_calls}/{MAX_REAL_CALLS}] {label}")


class CappedArk:
    """给真实客户端套调用计数, 所有真调路径 (record 的 miss / time / smoke) 都过它。"""

    def __init__(self, inner: ArkLLMClient) -> None:
        self.inner = inner

    async def complete(self, request: LLMRequest) -> Any:
        _count_real_call(f"stage={request.stage}")
        return await self.inner.complete(request)


def ark() -> CappedArk:
    cfg = settings()
    assert cfg.llm_api_key, "需要 .env 里有 SENTINEL_LLM_API_KEY"
    if cfg.llm_model != T.RECORDED_MODEL:
        print(f"!! settings().llm_model={cfg.llm_model!r} 与 test_agent_llm."
              f"RECORDED_MODEL={T.RECORDED_MODEL!r} 不一致 —— 录出的 cassette"
              f" 回放测试读不到, 先对齐再录。")
        raise SystemExit(2)
    return CappedArk(ArkLLMClient(
        base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, model=cfg.llm_model,
        prompt_version=agent_prompts.PROMPT_VERSION,
        timeout_seconds=cfg.agent_llm_timeout_seconds,
        price_input_per_mtok=cfg.llm_price_input_per_mtok,
        price_output_per_mtok=cfg.llm_price_output_per_mtok,
        thinking=cfg.llm_thinking,
        temperature=DEFAULT_TEMPERATURE,
    ))


class RecordMissing:
    """命中走回放、没命中真调并录 (只在本脚本里拼, 见模块 docstring)。"""

    def __init__(self) -> None:
        common: dict[str, Any] = dict(
            directory=CASSETTES, model=settings().llm_model,
            prompt_version=agent_prompts.PROMPT_VERSION,
            thinking=settings().llm_thinking,
            temperature=DEFAULT_TEMPERATURE,
        )
        self.replay = RecordReplayLLMClient(inner=None, mode="replay", **common)
        self.record = RecordReplayLLMClient(inner=ark(), mode="record", **common)

    async def complete(self, request: LLMRequest) -> Any:
        try:
            r = await self.replay.complete(request)
            print(f"  [hit]      stage={request.stage}")
            return r
        except ReplayMiss:
            r = await self.record.complete(request)
            what = r.tool_call.tool if r.tool_call else f"text({(r.text or '')[:60]!r})"
            print(f"  [recorded] stage={request.stage} -> {what} "
                  f"in={r.input_tokens} out={r.output_tokens} {r.latency_ms}ms")
            return r


# ===== 跑一条任务并打印时间线与账单 =====


async def run_once(factory: Any, input_text: str, client: Any) -> tuple:
    async with factory() as session, session.begin():
        created = await agent_service.create_task(
            session, user_id=3, input_text=input_text  # alex (operator), 种子账号
        )
    task_id = created["task_id"]
    print(f"task {task_id} created={created['created']}")
    assert created["created"], (
        "同句话还有没走完的任务挡着 (one_open 索引)。上一次跑挂了? "
        "清法: 在测试库把那条 agent_tasks 置成 failed 或等清扫判死。"
    )
    t0 = time.perf_counter()
    outcome = await agent_runtime.run_task(task_id, client, factory)
    dt = time.perf_counter() - t0
    async with factory() as session, session.begin():
        task = await agent_service.get_task(session, task_id)
        timeline = await agent_service.get_timeline(session, task_id)
        usage = (await session.execute(sqltext(
            "SELECT model, prompt_version, input_tokens, output_tokens, latency_ms, "
            "cache_hit, estimated_cost_cny FROM ai_usage WHERE task_id = :t ORDER BY id"),
            {"t": task_id})).mappings().all()
    print(f"task {task_id} outcome={outcome} wall={dt:.2f}s "
          f"error={task['error_code']}/{task['error_detail']}")
    return task_id, outcome, dt, timeline, usage


def print_run(timeline: list[dict], usage: list) -> None:
    print("--- timeline ---")
    for item in timeline:
        detail = (json.dumps(item["detail"], ensure_ascii=False)
                  if item["detail"] is not None else "")
        print(f"{item['seq']:>3} {item['kind']:<24} {item['label']:<22} {detail[:220]}")
    print("--- ai_usage ---")
    total = 0.0
    for u in usage:
        print(dict(u))
        total += float(u["estimated_cost_cny"])
    print(f"--- 本任务共 {len(usage)} 次调用, 刊例价合计 ¥{total:.6f}")


# ===== 子命令 =====


async def cmd_smoke() -> None:
    """最小连通性验证: 1 次真实调用把 key / base_url / 模型 ID / 请求路径四样
    一起验掉 (SPEC-002 第八节开跑前置)。裸 httpx 发, 打印原始响应 (脱敏 ——
    key 只在请求头, 响应体里本来就没有它), 再喂给 ArkLLMClient._parse 验证
    客户端解析同一份数据不炸。"""
    cfg = settings()
    assert cfg.llm_api_key, "需要 .env 里有 SENTINEL_LLM_API_KEY"
    _count_real_call("smoke chat/completions")
    payload = {
        "model": cfg.llm_model,
        "messages": [{"role": "user", "content": "回复一个字: 好"}],
        "thinking": {"type": cfg.llm_thinking},  # 与 ArkLLMClient 同形
        "temperature": DEFAULT_TEMPERATURE,      # 同上 (SPEC-007 第五节)
        "tools": [{"type": "function", "function": {
            "name": "noop", "description": "占位工具, 验证 tools 字段被接受",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        }}],
    }
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=cfg.agent_llm_timeout_seconds) as client:
        resp = await client.post(
            f"{cfg.llm_base_url}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    print(f"POST {cfg.llm_base_url}/chat/completions -> {resp.status_code} "
          f"({latency_ms}ms, model={cfg.llm_model})")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    if resp.status_code == 200:
        parsed = ArkLLMClient(
            base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, model=cfg.llm_model,
            prompt_version=agent_prompts.PROMPT_VERSION,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
            price_input_per_mtok=cfg.llm_price_input_per_mtok,
            price_output_per_mtok=cfg.llm_price_output_per_mtok,
        )._parse(resp.json(), latency_ms)
        print(f"客户端解析 OK: text={parsed.text!r} tool_call={parsed.tool_call} "
              f"in={parsed.input_tokens} out={parsed.output_tokens} "
              f"cost=¥{parsed.estimated_cost_cny:.6f}")


async def cmd_record_or_replay(factory: Any, cmd: str, task: str) -> None:
    input_text = {
        "happy": T.HAPPY_INPUT, "repair": T.REPAIR_INPUT, "clarify": T.CLARIFY_INPUT,
    }[task]
    client: Any
    if cmd == "record":
        client = RecordMissing()
    else:
        client = RecordReplayLLMClient(
            inner=None, directory=CASSETTES, mode="replay",
            model=T.RECORDED_MODEL, prompt_version=agent_prompts.PROMPT_VERSION,
            thinking=settings().llm_thinking,
            temperature=DEFAULT_TEMPERATURE,
        )
    task_id, outcome, _, timeline, usage = await run_once(factory, input_text, client)
    print_run(timeline, usage)
    if outcome == "clarifying":
        # clarify 那条任务录完仍是 open 状态 (等一个不会来的回答), 会撞 one_open
        # 索引堵死下一次录制/回放 —— 就地收尾成 failed。只动测试库里这一行,
        # 不影响已落盘的 cassette。
        async with factory() as session, session.begin():
            await session.execute(sqltext(
                "UPDATE agent_tasks SET status = 'failed', "
                "error_code = 'recording_cleanup', "
                "error_detail = '录制脚本收尾: 不留 open 任务挡住下一次录制', "
                "completed_at = now() WHERE id = :id AND status = 'clarifying'"),
                {"id": task_id})
        print(f"task {task_id} 已收尾成 failed (录制脚本清理, 不挡下次录制)")


async def cmd_time(factory: Any, n: int) -> None:
    times: list[float] = []
    calls_total = 0
    cost_total = 0.0
    for i in range(n):
        # mode=off: 直连真模型, 不读不写录制 —— 120 秒预算必须拿真实往返量,
        # 回放的延迟是 0, 拿它回填就是错误测量 (SPEC-002 第八节)
        _, outcome, dt, _, usage = await run_once(factory, T.HAPPY_INPUT, ark())
        calls_total += len(usage)
        cost_total += sum(float(u["estimated_cost_usd"]) for u in usage)
        print(f"run {i + 1}: wall={dt:.2f}s calls={len(usage)} outcome={outcome}")
        times.append(dt)
    print(f"model={settings().llm_model} prompt={agent_prompts.PROMPT_VERSION} "
          f"mode=off (真实调用)")
    print(f"raw={[round(t, 2) for t in times]}")
    print(f"median={statistics.median(times):.2f}s max={max(times):.2f}s "
          f"calls={calls_total} cost=¥{cost_total:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="W4 录制/回放/计时/连通性脚本 —— 项目里唯一真实花钱的代码。\n"
                    f"整脚本一次运行最多 {MAX_REAL_CALLS} 次真实调用, 超出直接退出\n"
                    "(录制失控是这类工具最常见的翻车方式)。replay 不发真实调用。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke", help="最小连通性验证: 1 次真调验 key/base_url/模型/路径")
    for name, help_text in (
        ("record", "跑一条任务, cassette 命中走回放、没命中才真调并落盘"),
        ("replay", "只读 tests/cassettes 离线复跑 (0 次真调), 验证回放全命中"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("task", choices=["happy", "repair", "clarify"],
                       help="happy=招牌句 / repair=修复循环那条 / clarify=含糊输入那条")
    p_time = sub.add_parser(
        "time", help="N 次真实调用计时 (mode=off 不碰录制), 回填 120 秒预算用")
    p_time.add_argument("n", type=int, nargs="?", default=5, help="次数 (默认 5)")
    args = parser.parse_args()

    async def go() -> None:
        if args.cmd == "smoke":
            await cmd_smoke()
            return
        engine = create_async_engine(
            os.environ["SENTINEL_DATABASE_URL"], poolclass=NullPool
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            if args.cmd in ("record", "replay"):
                await cmd_record_or_replay(factory, args.cmd, args.task)
            else:
                await cmd_time(factory, args.n)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
