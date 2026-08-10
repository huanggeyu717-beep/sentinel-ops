"""AblationProfile: 能力开关穿同一份状态机 (SPEC-007 第四节, 验收 14/15/16)。

三层守卫, 各挡各的变异:
- 字段表 (验收 14): production() 与 runtime 默认路径逐字段相等, from_level 的
  每一档逐字段钉死 —— 挡"from_level 里把某档开关写错"的变异;
- 步骤序列 (验收 15): 打桩客户端下 profile=production 与完全不传 profile 产出
  相同的 agent_steps 序列与相同的模型请求 —— 挡"默认路径与 production 分家";
- 分档金样 (自行新增, 报告第二节报备): A0/A1 各自的步骤序列、请求数、工具清单、
  prompt 归属逐条断言 —— **M5 (把某档开关在运行时写死成 production 的值) 的
  真正靶子在这里**: 字段表与验收 15 在那个变异下都照样绿 (SPEC-007 十一节
  对 M5 的特别标注), 只有分档金样会红。
"""
from __future__ import annotations

import json

from sqlalchemy import text
from test_agent_helpers import clean_agent_tables  # noqa: F401

from app.config import settings
from app.services import agent_runtime, agent_service
from app.services.agent_runtime import AblationProfile
from app.services.llm_client import (
    LLMResponse,
    LLMToolCall,
    RecordReplayLLMClient,
    ScriptedLLMClient,
)

OWNER = 3  # alex (operator)

VALID_BODY = {
    "scope": {"type": "zone", "ids": [1]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}
WRONG_ZONE_BODY = {**VALID_BODY, "scope": {"type": "zone", "ids": [99]}}


def tool(_tool_name: str, **arguments) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def say(content: str) -> LLMResponse:
    return LLMResponse(text=content, input_tokens=10, output_tokens=5)


def a0_json(name: str = "生鲜区漏水开单", body: dict | None = None) -> LLMResponse:
    return say(json.dumps({"name": name, "body": body or VALID_BODY},
                          ensure_ascii=False))


async def _run(factory, script, *, input_text, profile=None):
    async with factory() as session, session.begin():
        created = await agent_service.create_task(
            session, user_id=OWNER, input_text=input_text
        )
    task_id = created["task_id"]
    llm = ScriptedLLMClient(script=list(script))
    outcome = await agent_runtime.run_task(task_id, llm, factory, profile=profile)
    return task_id, outcome, llm


async def _steps(factory, task_id):
    """步骤序列的比较视图: (tool_name, status, transition 去向)。
    version_id/policy_id 这类每次运行必然不同的 id 不进比较。"""
    async with factory() as session, session.begin():
        rows = (await session.execute(text("""
            SELECT tool_name, status, arguments ->> 'to' AS transition_to
            FROM agent_steps WHERE task_id = :t ORDER BY seq
        """), {"t": task_id})).mappings().all()
    return [tuple(r.values()) for r in rows]


async def _task_row(factory, task_id):
    async with factory() as session, session.begin():
        return await agent_service.get_task(session, task_id)


# ===== 验收 14: 字段表 =====


def test_profile_production__matches_runtime_default_fieldwise():
    prod = AblationProfile.production()
    # 逐字段写死, 不用 == 一把梭: 哪个字段错了要在断言信息里直接看得见
    assert prod.inventory_in_prompt is False
    assert prod.discovery_tools is True
    assert prod.validate_and_repair is True
    assert prod.clarification is True
    assert prod.simulate_feedback is False  # A3 未做 (SPEC-007 补入 27)
    # runtime 默认路径 (不传 profile) 解析出的就是 production —— 依赖 config
    # 默认值 agent_ablation_level="production", 该默认值即出厂配置
    assert settings().agent_ablation_level == "production"
    assert agent_runtime._resolve_profile(None) == prod


def test_profile_from_level__each_level_fieldwise():
    a0 = AblationProfile.from_level("A0")
    assert (a0.inventory_in_prompt, a0.discovery_tools, a0.validate_and_repair,
            a0.clarification, a0.simulate_feedback) == (True, False, False, False, False)
    a1 = AblationProfile.from_level("A1")
    assert (a1.inventory_in_prompt, a1.discovery_tools, a1.validate_and_repair,
            a1.clarification, a1.simulate_feedback) == (False, True, False, False, False)
    assert AblationProfile.from_level("A2") == AblationProfile.production()
    assert AblationProfile.from_level("production") == AblationProfile.production()


def test_profile_from_level__unknown_level_rejected():
    import pytest

    with pytest.raises(ValueError):
        AblationProfile.from_level("A9")


# ===== 验收 15: production 与不传 profile, 步骤序列相同 =====


def test_profile_production__same_steps_as_no_profile(svc):
    script = [
        say("在 1 区变湿时开事故"),
        tool("create_policy", name="生鲜区漏水开单", body=VALID_BODY),
    ]

    async def go(factory):
        t1, o1, llm1 = await _run(
            factory, script, input_text="生鲜区湿了就开单", profile=None
        )
        # 第一条已到 awaiting_approval (不再是 open 状态), 同一句话可以再开
        t2, o2, llm2 = await _run(
            factory, script, input_text="生鲜区湿了就开单",
            profile=AblationProfile.production(),
        )
        return (o1, await _steps(factory, t1), llm1), (o2, await _steps(factory, t2), llm2)

    (o1, steps1, llm1), (o2, steps2, llm2) = svc(go)
    assert o1 == o2 == "awaiting_approval"
    assert steps1 == steps2
    # 模型看到的请求也必须一致 (messages + 工具清单; task_id 天然不同, 不比)
    assert [(r.messages, r.tools) for r in llm1.requests] \
        == [(r.messages, r.tools) for r in llm2.requests]


# ===== 分档金样 (M5 的靶子) =====


def test_ablation_a0__single_direct_call_to_awaiting_approval(svc):
    async def go(factory):
        task_id, outcome, llm = await _run(
            factory, [a0_json()], input_text="生鲜区湿了就开单",
            profile=AblationProfile.from_level("A0"),
        )
        async with factory() as session, session.begin():
            calls = (await session.execute(text(
                "SELECT count(*) FROM ai_usage WHERE task_id = :t"), {"t": task_id}
            )).scalar_one()
        return outcome, await _steps(factory, task_id), llm, calls

    outcome, steps, llm, calls = svc(go)
    assert outcome == "awaiting_approval"
    assert calls == 1  # A0 = 一次 LLM 调用 (SPEC-007 第四节)
    # 唯一一次请求: 无工具, 清单与 Schema 在 system prompt, 不在 user 消息
    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert req.tools == []
    system, user = req.messages[0]["content"], req.messages[1]["content"]
    assert "资源清单" in system and '"zones"' in system
    assert "Policy body 的 JSON Schema" in system  # 验收 16 前半
    assert "库存清单" not in user
    # 步骤序列: 无 parsing 调用 (parse_input 不出现), 校验与模拟照跑
    names = [s[0] for s in steps]
    assert "parse_input" not in names
    assert names == [
        "stage_transition",  # -> discovering
        "list_zones", "list_sensors", "list_roles", "list_employees",
        "stage_transition",  # -> compiling
        "create_policy",
        "stage_transition",  # -> validating
        "validate_policy",
        "stage_transition",  # -> simulating
        "simulate_policy", "request_approval",
        "stage_transition",  # -> awaiting_approval
    ]


def test_ablation_a0__fenced_json_still_parses(svc):
    fenced = "```json\n" + json.dumps(
        {"name": "围栏也认", "body": VALID_BODY}, ensure_ascii=False) + "\n```"

    async def go(factory):
        _, outcome, _ = await _run(
            factory, [say(fenced)], input_text="生鲜区湿了就开单",
            profile=AblationProfile.from_level("A0"),
        )
        return outcome

    assert svc(go) == "awaiting_approval"


def test_ablation_a0__garbage_output_is_protocol_error(svc):
    async def go(factory):
        task_id, outcome, _ = await _run(
            factory, [say("我觉得应该在生鲜区装一个策略")],
            input_text="生鲜区湿了就开单",
            profile=AblationProfile.from_level("A0"),
        )
        return outcome, await _task_row(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "failed"
    assert task["error_code"] == "model_protocol_error"


def test_ablation_a0__validation_failure_fails_without_repair(svc):
    async def go(factory):
        task_id, outcome, llm = await _run(
            factory, [a0_json(body=WRONG_ZONE_BODY)],
            input_text="9 区湿了就开单",
            profile=AblationProfile.from_level("A0"),
        )
        task = await _task_row(factory, task_id)
        async with factory() as session, session.begin():
            version_status = (await session.execute(text("""
                SELECT pv.status FROM policy_versions pv
                JOIN agent_steps s
                  ON CAST(s.result_summary ->> 'version_id' AS bigint) = pv.id
                WHERE s.task_id = :t AND s.tool_name = 'create_policy'
            """), {"t": task_id})).scalar_one()
        return outcome, task, len(llm.requests), version_status

    outcome, task, requests, version_status = svc(go)
    assert outcome == "failed"
    assert task["error_code"] == "validation_failed"
    assert "E_UNKNOWN_ZONE" in task["error_detail"]
    assert requests == 1  # 没有修复调用
    assert version_status == "discarded"  # 失败收口把草稿标弃, 与既有口径一致


def test_ablation_a1__inventory_in_user_message_and_no_clarify_tool(svc):
    async def go(factory):
        task_id, outcome, llm = await _run(
            factory,
            [tool("create_policy", name="生鲜区漏水开单", body=VALID_BODY)],
            input_text="生鲜区湿了就开单",
            profile=AblationProfile.from_level("A1"),
        )
        return outcome, await _steps(factory, task_id), llm

    outcome, steps, llm = svc(go)
    assert outcome == "awaiting_approval"
    assert len(llm.requests) == 1  # parsing 被跳过 (无追问能力时它是纯浪费)
    req = llm.requests[0]
    # 工具清单裁掉了 ask_clarification, 清单在 user 消息 (v3), 不在 system
    assert [t["name"] for t in req.tools] == ["create_policy", "add_policy_version"]
    system, user = req.messages[0]["content"], req.messages[1]["content"]
    assert "库存清单" in user
    assert "资源清单" not in system and '"zones"' not in system  # 验收 16 后半
    names = [s[0] for s in steps]
    assert "parse_input" not in names
    assert "validate_policy" in names and "simulate_policy" in names


def test_ablation_a1__ask_clarification_is_protocol_error(svc):
    async def go(factory):
        task_id, outcome, _ = await _run(
            factory,
            [tool("ask_clarification", question="通知谁?", missing_slots=["role"])],
            input_text="有水就通知一下",
            profile=AblationProfile.from_level("A1"),
        )
        return outcome, await _task_row(factory, task_id)

    outcome, task = svc(go)
    # 追问能力关掉后, 模型硬调 ask_clarification 与调不存在的工具同一口径
    assert outcome == "failed"
    assert task["error_code"] == "model_protocol_error"


def test_ablation_a1__validation_failure_fails_without_repair(svc):
    async def go(factory):
        task_id, outcome, llm = await _run(
            factory,
            [tool("create_policy", name="9 区开单", body=WRONG_ZONE_BODY)],
            input_text="9 区湿了就开单",
            profile=AblationProfile.from_level("A1"),
        )
        return outcome, await _task_row(factory, task_id), len(llm.requests)

    outcome, task, requests = svc(go)
    assert outcome == "failed"
    assert task["error_code"] == "validation_failed"
    assert requests == 1


# ===== 故障注入 (tool_fault 类的运载工具, 生产恒关) =====


def _write_faults(tmp_path, entries):
    path = tmp_path / "faults.json"
    path.write_text(json.dumps(entries, ensure_ascii=False))
    return str(path)


def test_fault_injection__timeout_once_retries_then_succeeds(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings(), "agent_fault_injection_file",
        _write_faults(tmp_path, [
            {"input": "卖场中区湿了就开单", "tool": "list_zones",
             "fault": "timeout_once"},
        ]),
    )
    # 退避别真睡 0.5 秒
    monkeypatch.setattr(agent_runtime, "_RETRY_BACKOFF_BASE_S", 0.01)

    async def go(factory):
        task_id, outcome, _ = await _run(
            factory,
            [say("理解"), tool("create_policy", name="卖场中区开单",
                               body={**VALID_BODY, "scope": {"type": "zone", "ids": [2]}})],
            input_text="卖场中区湿了就开单",
        )
        async with factory() as session, session.begin():
            retry = (await session.execute(text("""
                SELECT retry_count FROM agent_steps
                WHERE task_id = :t AND tool_name = 'list_zones'
            """), {"t": task_id})).scalar_one()
        return outcome, retry

    outcome, retry = svc(go)
    assert outcome == "awaiting_approval"
    assert retry == 1  # 第一次注入超时, 退避后第二次成功


def test_fault_injection__unretryable_dead_letters(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings(), "agent_fault_injection_file",
        _write_faults(tmp_path, [
            {"input": "生鲜区湿了就开单", "tool": "validate_policy",
             "fault": "unretryable"},
        ]),
    )

    async def go(factory):
        task_id, outcome, _ = await _run(
            factory,
            [say("理解"), tool("create_policy", name="生鲜区漏水开单", body=VALID_BODY)],
            input_text="生鲜区湿了就开单",
        )
        return outcome, await _task_row(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "dead_letter"
    assert task["error_code"] == "tool_error"
    assert "EvalInjectedFault" in task["error_detail"]


def test_fault_injection__other_inputs_unaffected(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings(), "agent_fault_injection_file",
        _write_faults(tmp_path, [
            {"input": "别的句子", "tool": "list_zones", "fault": "unretryable"},
        ]),
    )

    async def go(factory):
        _, outcome, _ = await _run(
            factory,
            [say("理解"), tool("create_policy", name="生鲜区漏水开单", body=VALID_BODY)],
            input_text="生鲜区湿了就开单",
        )
        return outcome

    assert svc(go) == "awaiting_approval"


def test_fault_injection__table_reloads_on_file_change(tmp_path, monkeypatch):
    """注入文件由 runner 按激活窗口增删, 缓存必须按 mtime 失效 —— 按路径缓存
    一次到老的话, 窗口关了故障还在, 同文对照用例照样被污染。"""
    path = tmp_path / "faults.json"
    path.write_text(json.dumps(
        [{"input": "某句话", "tool": "list_zones", "fault": "unretryable"}]
    ))
    monkeypatch.setattr(settings(), "agent_fault_injection_file", str(path))
    assert agent_runtime._injected_fault("某句话", "list_zones") == "unretryable"
    # 窗口关闭 (文件清空) -> 必须立刻看不见旧条目
    import os
    path.write_text("[]")
    os.utime(path, ns=(1, 1))  # 保证 mtime 变化不受时钟粒度影响
    assert agent_runtime._injected_fault("某句话", "list_zones") is None


# ===== 回放 miss -> 判失败并可统计 (验收 20 的 runtime 半边) =====


def test_replay_miss__dead_letter_with_replay_miss_code(svc, tmp_path):
    async def go(factory):
        async with factory() as session, session.begin():
            created = await agent_service.create_task(
                session, user_id=OWNER, input_text="生鲜区湿了就开单"
            )
        task_id = created["task_id"]
        llm = RecordReplayLLMClient(
            inner=None, directory=tmp_path / "empty", mode="replay",
            model="doubao-seed-2-1-pro-260628", prompt_version="v3",
        )
        outcome = await agent_runtime.run_task(task_id, llm, factory)
        return outcome, await _task_row(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "dead_letter"
    assert task["error_code"] == "replay_miss"
    assert task["error_detail"].startswith("回放未命中")
