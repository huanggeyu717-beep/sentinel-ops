"""agent_tools: 注册表相等断言、Schema 同源、数据源枚举、名字校验、单工具超时。"""
from __future__ import annotations

import asyncio

import pytest
from test_agent_helpers import clean_agent_tables  # noqa: F401

from app.config import settings
from app.services import agent_service, agent_tools
from app.services.agent_tools import (
    REGISTRY,
    InvalidToolArguments,
    ToolContext,
    ToolSpec,
    ToolTimeout,
    run_tool,
    simulation_sources,
)
from policy_engine import policy_json_schema

# SPEC-002 第五节的工具清单原文 (13 个)。注册表必须与它**完全相等** ——
# 往注册表加工具却忘了更新分级, 让这条测试当场变红, 而不是静悄悄漏在审批环节
# (与 SPEC-001 "分级表键集合 == 动作白名单"同一手法, 本项目第五处)。
SPEC_TOOLS = {
    "list_zones": "read",
    "list_sensors": "read",
    "list_roles": "read",
    "list_employees": "read",
    "get_policy": "read",
    "get_available_actions": "read",
    "create_policy": "draft",
    "add_policy_version": "draft",
    "update_policy_draft": "draft",
    "validate_policy": "simulate",
    "simulate_policy": "simulate",
    "request_approval": "write",
    "ask_clarification": "terminal",
}


def test_registry__equals_spec_tool_table():
    assert {name: spec.category for name, spec in REGISTRY.items()} == SPEC_TOOLS
    # publish_policy 永远不进清单: 发布是人在 Studio 里点的 (SPEC-002 第五节)
    assert "publish_policy" not in REGISTRY


def test_stage_trimming__only_references_registered_tools():
    for stage, names in agent_tools.TOOLS_BY_STAGE.items():
        assert set(names) <= set(REGISTRY), stage
    # compiling 只给建草稿; repairing 与有草稿的再编译只给就地改 (第六节)
    assert set(agent_tools.TOOLS_BY_STAGE["compiling"]) == {
        "create_policy", "add_policy_version", "ask_clarification"
    }
    for stage in ("repairing", "compiling_with_draft"):
        assert set(agent_tools.TOOLS_BY_STAGE[stage]) == {
            "update_policy_draft", "ask_clarification"
        }


# ===== get_available_actions 与 policy_json_schema 同源 =====


def test_get_available_actions__same_source_as_engine_schema(svc):
    async def go(factory):
        async with factory() as session:
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            return await run_tool(ctx, "get_available_actions", {})

    result = svc(go)
    assert result["policy_schema"] == policy_json_schema()  # 不另生成一份


# ===== simulate_policy 只暴露枚举 =====


def test_simulation_sources__scenarios_plus_history_csv():
    sources = simulation_sources()
    # 精确相等而不是"包含": evals/scenarios/ 的评测专用场景**不许**混进这个枚举
    # (SPEC-007 第七节 —— 演练面板与模型的可选数据源都来自它, 店长不该看见
    # eval_* 场景, 模型也不该能选它们)。新增产品场景时更新这行是刻意的门槛。
    assert set(sources) == {
        "auto_close", "basic_spill", "multi_sensor_escalation", "history_csv"
    }
    assert not any("/" in s or s.endswith(".csv") for s in sources)  # 不暴露路径
    assert not any(s.startswith("eval") for s in sources)


def test_simulate_policy__unknown_source_rejected(svc):
    async def go(factory):
        async with factory() as session:
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            with pytest.raises(InvalidToolArguments):
                await run_tool(ctx, "simulate_policy",
                               {"version_id": 1, "source": "../secrets.yaml"})

    svc(go)


# ===== 名字由模型起, 服务端校验 =====


def test_create_policy_name__stripped_and_length_checked(svc):
    body = {"scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [],
            "actions": [{"type": "open_incident", "severity": "normal"}],
            "cooldown_s": 60}

    async def go(factory):
        async with factory() as session, session.begin():
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            for bad in ("", "   ", "x" * 61):
                with pytest.raises(InvalidToolArguments):
                    await run_tool(ctx, "create_policy", {"name": bad, "body": body})
            created = await run_tool(
                ctx, "create_policy", {"name": "  生鲜区漏水  ", "body": body}
            )
            from sqlalchemy import text
            name = (await session.execute(text(
                "SELECT name FROM policies WHERE id = :id"),
                {"id": created["policy_id"]},
            )).scalar_one()
            return name

    assert svc(go) == "生鲜区漏水"  # 首尾空白已去


# ===== 单工具超时: wait_for, 超时抛可重试的 ToolTimeout =====


def test_run_tool__timeout_raises_retryable(svc, monkeypatch):
    async def slow_tool(ctx, args):
        await asyncio.sleep(5)

    monkeypatch.setitem(
        REGISTRY, "list_zones",
        ToolSpec("list_zones", "read", slow_tool, "test-slow"),
    )
    monkeypatch.setattr(settings(), "agent_tool_timeout_seconds", 0.05)

    async def go(factory):
        async with factory() as session:
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            with pytest.raises(ToolTimeout):
                await run_tool(ctx, "list_zones", {})

    svc(go)


# ===== source 落库: agent 与 human 两条路径分得开 (验收 3 的 service 层半边) =====


def test_version_source__agent_vs_human(svc):
    body = {"scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [],
            "actions": [{"type": "open_incident", "severity": "normal"}],
            "cooldown_s": 60}

    async def go(factory):
        from app.services import policy_service
        async with factory() as session, session.begin():
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            via_agent = await run_tool(
                ctx, "create_policy", {"name": "来自 agent", "body": body}
            )
            via_human = await policy_service.create_policy(
                session, name="直接提交 DSL", body=body, created_by=3
            )
            from sqlalchemy import text
            rows = (await session.execute(text(
                "SELECT id, source, created_by FROM policy_versions "
                "WHERE id IN (:a, :b)"),
                {"a": via_agent["version_id"], "b": via_human["version_id"]},
            )).mappings().all()
            return {r["id"]: (r["source"], r["created_by"]) for r in rows}

    by_id = svc(go)
    sources = sorted(v[0] for v in by_id.values())
    assert sources == ["agent", "human"]
    assert all(v[1] == 3 for v in by_id.values())  # created_by 都是发起的人


# ===== SPEC-007 验收 23: missing_slots 从工具入参一路落库并读得回 =====


async def _claimed_task(factory, input_text):
    async with factory() as session, session.begin():
        created = await agent_service.create_task(
            session, user_id=3, input_text=input_text
        )
        task_id = created["task_id"]
        assert await agent_service.claim_task(session, task_id, "t")
    return task_id


def test_ask_clarification__missing_slots_written_and_read_back(svc):
    """变异 M8 的靶子: 工具入参照收 missing_slots、落库那一步不写这一列时,
    这条必须红。前四处同步全做对、只漏落库, 参数就只活在内存里 —— ambiguous
    那 16 条判分读不到任何东西, 而且不会报错 (SPEC-007 第九节)。
    断言刻意从库里读, 不从工具返回值读 (返回值可能从入参直接带出来, 绕过库)。"""
    async def go(factory):
        task_id = await _claimed_task(factory, "槽位落库测试")
        async with factory() as session, session.begin():
            ctx = ToolContext(session=session, task_id=task_id, user_id=3,
                              runner_id="t")
            await run_tool(ctx, "ask_clarification", {
                "question": "通知谁? 管哪个区?",
                "missing_slots": ["role", "scope", "role"],  # 重复项该被去重
            })
        async with factory() as session:
            from sqlalchemy import text
            return (await session.execute(text(
                "SELECT missing_slots FROM agent_clarifications "
                "WHERE task_id = :t"), {"t": task_id},
            )).scalar_one()

    assert list(svc(go)) == ["role", "scope"]  # 去重保序, 读自数据库


def test_ask_clarification__slots_outside_enum_rejected(svc):
    """service 层校验取值都在七项枚举内, 不信任模型输入 (CLAUDE.md 不变量 5
    的同一条道理); 缺失与空数组同样拒绝 —— 新写入的行必须非空 (0009)。"""
    async def go(factory):
        task_id = await _claimed_task(factory, "槽位校验测试")
        for bad_args in (
            {"question": "?", "missing_slots": ["scope", "颜色"]},  # 枚举外
            {"question": "?", "missing_slots": []},                 # 空
            {"question": "?", "missing_slots": "scope"},            # 非数组
            {"question": "?"},                                       # 缺失
        ):
            async with factory() as session, session.begin():
                ctx = ToolContext(session=session, task_id=task_id, user_id=3,
                                  runner_id="t")
                with pytest.raises(InvalidToolArguments):
                    await run_tool(ctx, "ask_clarification", bad_args)
        async with factory() as session:
            from sqlalchemy import text
            return (await session.execute(text(
                "SELECT count(*) FROM agent_clarifications WHERE task_id = :t"),
                {"t": task_id},
            )).scalar_one()

    assert svc(go) == 0  # 全部被拒, 一行都没落


def test_missing_slots_enum__tool_schema_and_service_same_source():
    """工具 Schema 里的枚举与 service 校验用的枚举必须同源相等 —— 两份枚举
    走散时, 模型按 Schema 给的值会被 service 拒掉 (白烧配额), 反之则枚举外的
    值静默入库。手法同注册表相等断言 (本项目第六处)。"""
    from app.services import agent_prompts
    from app.services.agent_slots import MISSING_SLOTS

    schema = {s["name"]: s for s in agent_prompts.tool_schemas(("ask_clarification",))}
    props = schema["ask_clarification"]["parameters"]["properties"]
    assert props["missing_slots"]["items"]["enum"] == list(MISSING_SLOTS)
    assert "missing_slots" in schema["ask_clarification"]["parameters"]["required"]


# ===== list_sensors 的 never_reported (修补五): 与 /status 同口径 =====


def test_list_sensors__never_reported_flips_with_sensorstate(svc):
    """"装了却从没上报"必须让模型看得见 —— dev seed 的占位 sensor 0 不能以正常
    姿态进库存清单 (静态验证器只查 id 存在, 拦不住给哑设备写策略)。守的是
    inventory_service._SENSORS 里新加的 sensorstate JOIN 分支。"""
    async def go(factory):
        from sqlalchemy import text
        async with factory() as session, session.begin():
            ctx = ToolContext(session=session, task_id=0, user_id=3, runner_id="t")
            before = await run_tool(ctx, "list_sensors", {})
            # sensor 1 上报一次 (sensorstate 是遥测表, 每个用例前被清空)
            await session.execute(text(
                "INSERT INTO sensorstate (sensor_id, wet, state, updated_ts, "
                "updated_at, last_value) VALUES (1, false, 'DRY', 0, now(), 0)"
            ))
            after = await run_tool(ctx, "list_sensors", {})
        return before["sensors"], after["sensors"]

    before, after = svc(go)
    assert all(s["never_reported"] is True for s in before)  # 清空后谁都没说过话
    flags = {s["id"]: s["never_reported"] for s in after}
    assert flags[1] is False   # 上报过一次就不再是 never_reported
    assert flags[0] is True    # 占位 sensor 0 仍然从没说过话


def test_llm_calls_used__counts_ai_usage_rows(svc):
    """≤12 的硬上限数的是 ai_usage 行 (跨轮累加、活过重启), 打桩也落账。"""
    from app.services.llm_client import LLMResponse

    async def go(factory):
        from sqlalchemy import text
        async with factory() as session, session.begin():
            created = await agent_service.create_task(
                session, user_id=3, input_text="计数测试"
            )
            task_id = created["task_id"]
            before = await agent_service.llm_calls_used(session, task_id)
            await agent_service.record_llm_usage(session, task_id, LLMResponse())
            await agent_service.record_llm_usage(
                session, task_id, LLMResponse(estimated_cost_cny=0.006132)
            )
            after = await agent_service.llm_calls_used(session, task_id)
            costs = (await session.execute(text(
                "SELECT estimated_cost_cny FROM ai_usage WHERE task_id = :t "
                "ORDER BY id"), {"t": task_id},
            )).scalars().all()
        return before, after, [float(c) for c in costs]

    before, after, costs = svc(go)
    assert (before, after) == (0, 2)
    # 真实模型的估价必须原样落库 (W5 拿这列算基线), 不再是打桩年代的写死 0
    assert costs == [0.0, 0.006132]
