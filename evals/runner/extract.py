"""评测库重置 + 从库里提取 CaseOutcome 与计量 (SPEC-007 第七节)。

分工线: **grader 不连库** (case_grader 吃结构化的 CaseOutcome), 连库的活全在
这里。runner 建任务走 HTTP (权限/去重/并发槽位那条真实的路), 但**读结果直接
查库** —— case_grader 的模块注释写明白了它吃的是"runner 从数据库提取"的摘要;
时间线 HTTP 接口是给人看的, 不带 previous_body 链和澄清槽位这些判分要件。

评测库重置 = drop + create, 迁移与种子**不在这里跑**: 它们由 API 子进程 (或
eval-db-reset 目标里的一致性 pytest) 的启动流程自己执行 —— 与 CI/开发/Docker
同一条建表路径 (SPEC-007 第七节第 4 条), 这里不另写一条。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from evals.graders.case_grader import CaseOutcome

TERMINAL_STATUSES = ("awaiting_approval", "completed", "failed", "dead_letter")


def plain_dsn(url: str) -> str:
    return url.replace("+asyncpg", "")


async def reset_database(eval_db_url: str) -> None:
    """drop + create 评测库。连到同实例的 postgres 库执行; 先踢掉存量连接
    (上一臂的 API 子进程若没退干净, DROP 会挂着等)。"""
    dsn = plain_dsn(eval_db_url)
    admin_dsn, db_name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(admin_dsn + "/postgres")
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()", db_name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


def _as_dict(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, dict) else {}


async def fetch_task_record(conn: asyncpg.Connection, task_id: int) -> dict[str, Any]:
    """一条任务的全部判分与计量原料, 一次取齐。"""
    task = dict(await conn.fetchrow(
        "SELECT id, status, stage, error_code, error_detail, created_at, "
        "completed_at FROM agent_tasks WHERE id = $1", task_id,
    ))
    steps = [dict(r) for r in await conn.fetch(
        "SELECT seq, tool_name, arguments, result_summary, status, retry_count, "
        "created_at FROM agent_steps WHERE task_id = $1 ORDER BY seq", task_id,
    )]
    clars = [dict(r) for r in await conn.fetch(
        "SELECT asked_seq, question, answer, missing_slots, asked_at, answered_at "
        "FROM agent_clarifications WHERE task_id = $1 ORDER BY asked_seq", task_id,
    )]
    usage = dict(await conn.fetchrow(
        "SELECT count(*) AS calls, COALESCE(sum(input_tokens), 0) AS input_tokens, "
        "COALESCE(sum(output_tokens), 0) AS output_tokens, "
        "COALESCE(sum(estimated_cost_cny), 0) AS cost_cny, "
        "COALESCE(sum(latency_ms), 0) AS model_ms "
        "FROM ai_usage WHERE task_id = $1", task_id,
    ))
    version_id = await conn.fetchval(
        "SELECT CAST(result_summary ->> 'version_id' AS bigint) FROM agent_steps "
        "WHERE task_id = $1 AND tool_name IN ('create_policy', 'add_policy_version') "
        "AND result_summary ? 'version_id' ORDER BY seq DESC LIMIT 1", task_id,
    )
    version = None
    if version_id is not None:
        row = await conn.fetchrow(
            "SELECT status, body FROM policy_versions WHERE id = $1", version_id,
        )
        if row is not None:
            version = {"status": row["status"], "body": _as_dict(row["body"])}
    return {"task": task, "steps": steps, "clarifications": clars,
            "usage": usage, "version": version}


def build_outcome(record: dict[str, Any]) -> CaseOutcome:
    """数据库原料 -> case_grader.CaseOutcome (判分的结构化摘要)。"""
    task = record["task"]
    steps = record["steps"]
    version = record["version"]

    validation_codes: list[str] = []
    replay_warnings: list[str] = []
    previous_bodies: list[dict[str, Any]] = []
    executed: list[str] = []
    for step in steps:
        summary = _as_dict(step["result_summary"])
        name = str(step["tool_name"])
        if step["status"] == "ok" and name not in ("stage_transition", "parse_input"):
            executed.append(name)
        if name == "validate_policy":
            for issue in summary.get("issues") or []:
                code = str(issue.get("code"))
                if code not in validation_codes:
                    validation_codes.append(code)
        elif name == "simulate_policy":
            for warning in summary.get("warnings") or []:
                replay_warnings.append(str(warning.get("code")))
        elif name == "update_policy_draft" and isinstance(
            summary.get("previous_body"), dict
        ):
            previous_bodies.append(summary["previous_body"])

    final_body = version["body"] if version else None
    all_bodies = tuple(previous_bodies + ([final_body] if final_body else []))

    error_detail = str(task.get("error_detail") or "")
    # Pydantic 层拒稿 (InvalidPolicyBody) 走的是 dead_letter/tool_error 收口,
    # 步骤随事务回滚不落行 —— 判定只能读 error_detail 里的异常类型名 (确定性:
    # _tool_step 的收口格式固定写 "工具 X 不可重试错误: <类型名>: ...")
    schema_rejected = (
        task.get("error_code") == "tool_error" and "InvalidPolicyBody" in error_detail
    )

    attempted_unknown: list[str] = []
    if task.get("error_code") == "model_protocol_error":
        # _expect_tool 的固定文案 "该阶段只接受 (...), 模型给了 <名字>"
        marker = "模型给了 "
        if marker in error_detail:
            candidate = error_detail.split(marker, 1)[1].strip()
            if candidate and candidate != "无工具调用":
                attempted_unknown.append(candidate)

    slot_rounds = tuple(
        tuple(str(s) for s in (c["missing_slots"] or []))
        for c in record["clarifications"]
    )

    return CaseOutcome(
        final_status=str(task["status"]),
        error_code=task.get("error_code"),
        submitted=task["status"] in ("awaiting_approval", "completed"),
        final_draft_body=final_body,
        all_draft_bodies=all_bodies,
        validation_codes=tuple(validation_codes),
        schema_rejected=schema_rejected,
        executed_tools=tuple(executed),
        attempted_unknown_tools=tuple(attempted_unknown),
        clarify_slot_rounds=slot_rounds,
        draft_version_status=version["status"] if version else None,
        replay_warnings=tuple(replay_warnings),
    )


def build_timing(record: dict[str, Any]) -> dict[str, Any]:
    """计量行: 墙钟扣掉澄清等待 (第一节第 3 项), 纯模型时间从 ai_usage 汇总。

    终点: 终态任务用 completed_at; awaiting_approval 用最后一条步骤的落库时间;
    停在 clarifying 的用最后一次提问时间 (等人回答的时间不算系统在跑)。
    """
    task = record["task"]
    steps = record["steps"]
    clars = record["clarifications"]
    start = task["created_at"]
    end = task["completed_at"]
    if end is None:
        candidates = [s["created_at"] for s in steps]
        candidates += [c["asked_at"] for c in clars]
        candidates += [c["answered_at"] for c in clars if c["answered_at"]]
        end = max(candidates) if candidates else start
    wait_s = sum(
        (c["answered_at"] - c["asked_at"]).total_seconds()
        for c in clars if c["answered_at"] is not None
    )
    wall_ms = max(0, int(((end - start).total_seconds() - wait_s) * 1000))
    usage = record["usage"]
    repair_rounds = sum(
        1 for s in steps
        if s["tool_name"] == "stage_transition"
        and _as_dict(s["arguments"]).get("to") == "repairing"
    )
    digest_src = json.dumps(
        [[s["seq"], s["tool_name"], s["status"]] for s in steps],
        ensure_ascii=False, separators=(",", ":"),
    )
    return {
        "wall_ms": wall_ms,
        "model_ms": int(usage["model_ms"]),
        "llm_calls": int(usage["calls"]),
        "input_tokens": int(usage["input_tokens"]),
        "output_tokens": int(usage["output_tokens"]),
        "cost_cny": float(usage["cost_cny"]),
        "clarify_rounds": len(clars),
        "repair_rounds": repair_rounds,
        "steps_digest": hashlib.sha256(digest_src.encode()).hexdigest()[:16],
    }
