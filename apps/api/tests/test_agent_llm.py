"""LLM 接入与录制回放 (SPEC-002 第九节; 验收 4 与 19 的回放半边)。

四块:
1. 回放键与录制回放器本身 (不连网, 假 inner);
2. 方舟客户端的协议解析 (httpx.MockTransport, 不连网) —— 真模型才会出现、
   打桩下永远看不到的失败: arguments 不是合法 JSON、空 tool_calls 又没文本;
3. 状态机对这些失败的归类 (llm_timeout / llm_error / model_protocol_error);
4. 跑在**进版本库的录制** (tests/cassettes/) 上的端到端回放 —— 库存钉死成
   CANONICAL_INVENTORY: 录制与回放共用同一份, prompt 不随测试顺序/库历史漂移。

测试不连真网。唯一例外是文件末尾默认 skip 的冒烟测试 (显式给 key 才跑, CI 不跑)。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_helpers import clean_agent_tables  # noqa: F401

from app.config import settings
from app.services import (
    agent_prompts,
    agent_runtime,
    agent_service,
    agent_tools,
    employee_service,
    inventory_service,
)
from app.services.llm_client import (
    ArkLLMClient,
    LLMCallTimeout,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    LLMUnavailable,
    ModelProtocolError,
    RecordReplayLLMClient,
    ReplayMiss,
    ScriptedLLMClient,
    cassette_key,
)

CASSETTES_DIR = Path(__file__).parent / "cassettes"
OWNER = 3  # alex (operator), 种子账号

# cassettes 没录时显式 skip (带原因, 不是悄悄放过); 录完开关自动翻绿, 三条回放
# 验收 (含验收 4 的修复循环) 自动生效。重录流程: scripts/dev/record_cassettes.py。
_HAS_CASSETTES = CASSETTES_DIR.is_dir() and any(CASSETTES_DIR.glob("*.json"))
needs_cassettes = pytest.mark.skipif(
    not _HAS_CASSETTES,
    reason="tests/cassettes 还没录制 (需要真实 SENTINEL_LLM_API_KEY, 流程见完成报告)",
)

VALID_BODY = {
    "scope": {"type": "zone", "ids": [1]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}

# ===== 录制/回放共用的钉死库存 =====
#
# 回放键含 messages, prompt 里的库存一变回放必失配, 所以录制脚本与这里的回放
# 测试共用这一份钉死的库存 (脚本 import 本模块的 pin_canonical_inventory)。
#
# 钉死的理由 (修补六改正过 —— 原注释把 sensor 0 说成"ingest 测试跑过才有",
# **是错的**: 它在 dev seed 里, 每个全新库都有, 见 app/db.py 种子注释):
# - roles 会漂: viewer_headers 夹具往 users/user_roles 补插 viewer 账号,
#   跑没跑过它, list_roles_present 的结果差一个 "viewer";
# - never_reported 会漂: 它 JOIN 的 sensorstate 是遥测表, 每个用例前被 TRUNCATE,
#   ingest 类用例又会写回 —— 取决于测试顺序;
# - sensors/zones/devices/employees 本身**不会漂** (已核: 测试中无任何插行,
#   四个只读查询都带 ORDER BY), 钉住它们是让 cassette 不依赖库状态的卫生习惯。
#
# 内容 = dev seed + "344 条真实历史读数已回放"的演示姿态: 1-5 号上报过,
# 0 号是 seed 里刻意放的占位 (挂 UNKNOWN_DEVICE, 让前端"未定位"分支可见),
# 从没上报过 —— 修补五让模型看得见这件事。
# roles 含 viewer: "dev seed"不只 db.py 那一段 —— 迁移 0007 自己也种了 dana
# 与 viewer (SPEC-006 第三节要求), 全新库灌 0001+seed+0007 后 _ROLES_PRESENT
# 实际返回四个角色。第二段曾照 db.py 写成三个, 评审方灌库实测纠正。
CANONICAL_INVENTORY: dict[str, Any] = {
    "zones": [
        {"id": 1, "name": "Zone 1 - 生鲜区"},
        {"id": 2, "name": "Zone 2 - 卖场中区"},
        {"id": 3, "name": "Zone 3 - 后场"},
    ],
    "sensors": [
        {"id": 0, "zone_id": 3, "zone_name": "Zone 3 - 后场", "active": True,
         "device_name": "UNKNOWN_DEVICE", "never_reported": True},
        {"id": 1, "zone_id": 1, "zone_name": "Zone 1 - 生鲜区", "active": True,
         "device_name": "Arduino1", "never_reported": False},
        {"id": 2, "zone_id": 1, "zone_name": "Zone 1 - 生鲜区", "active": True,
         "device_name": "Arduino1", "never_reported": False},
        {"id": 3, "zone_id": 2, "zone_name": "Zone 2 - 卖场中区", "active": True,
         "device_name": "Arduino2", "never_reported": False},
        {"id": 4, "zone_id": 2, "zone_name": "Zone 2 - 卖场中区", "active": True,
         "device_name": "Arduino2", "never_reported": False},
        {"id": 5, "zone_id": 3, "zone_name": "Zone 3 - 后场", "active": True,
         "device_name": "UNKNOWN_DEVICE", "never_reported": False},
    ],
    "roles": ["admin", "manager", "operator", "viewer"],
    "employees": [
        {"id": 1, "name": "Alex Chen", "role": "operator",
         "email": "alex@example.com", "zone_id": 1, "zone_name": "Zone 1 - 生鲜区"},
        {"id": 2, "name": "Bo Wang", "role": "operator",
         "email": "bo@example.com", "zone_id": 2, "zone_name": "Zone 2 - 卖场中区"},
        {"id": 3, "name": "Chris Li", "role": "manager",
         "email": "chris@example.com", "zone_id": 1, "zone_name": "Zone 1 - 生鲜区"},
    ],
}

# 进版本库的两条录制任务的输入原文 (录制脚本与回放测试必须逐字一致)
HAPPY_INPUT = "生鲜区两个探头三分钟内都湿了就通知这个区的主管"
# 修复循环那条的输入必须信息齐全: 录制时实测, 光说"后场湿了就开单"模型会 (正确地)
# 拒猜严重级别与冷却时间、转去 ask_clarification —— 那是澄清路径的行为, 不是本条
# cassette 要的编译路径。这条测试考的是"错误码够不够修复用", 输入只是布景。
REPAIR_INPUT = "后场湿了就开一张 normal 级事故单, 冷却 300 秒"
# 澄清路径那条的输入必须真含糊: 不说严重级别、不说冷却 —— 第二段实测真模型
# 面对它会 (正确地) 拒猜、转去 ask_clarification, 且问的时候自己指出 sensor 0
# 从未上报过 (修补五在真模型行为里兑现)。那批录制曾被删掉, 第三段按复核意见补录:
# 三条主路径里澄清这条不能只剩打桩覆盖 (第二段的目的就是用真模型验证它成立)。
CLARIFY_INPUT = "后场湿了就开单"
# 录制时用的模型 (回放键的一部分)。钉死成常量而不是读 settings().llm_model:
# CI 没有 .env, settings 值随机器漂移, 而 cassettes 是跟着这个名字录的
RECORDED_MODEL = "doubao-seed-2-1-pro-260628"


def pin_canonical_inventory() -> None:
    """把库存 service 钉成 CANONICAL_INVENTORY (录制脚本与回放测试共用)。

    直接改模块属性而不是 monkeypatch: 录制脚本不在 pytest 里跑。测试里配合
    monkeypatch.setattr 使用, 保证用例结束后还原。
    """
    async def zones(session: Any) -> list[dict[str, Any]]:
        return [dict(z) for z in CANONICAL_INVENTORY["zones"]]

    async def sensors(session: Any) -> list[dict[str, Any]]:
        return [dict(s) for s in CANONICAL_INVENTORY["sensors"]]

    async def roles(session: Any) -> list[str]:
        return list(CANONICAL_INVENTORY["roles"])

    async def employees(session: Any) -> list[dict[str, Any]]:
        return [dict(e) for e in CANONICAL_INVENTORY["employees"]]

    inventory_service.list_zones = zones  # type: ignore[assignment]
    inventory_service.list_sensors = sensors  # type: ignore[assignment]
    inventory_service.list_roles_present = roles  # type: ignore[assignment]
    employee_service.list_employees = employees  # type: ignore[assignment]


@pytest.fixture
def pinned_inventory():
    originals = (
        inventory_service.list_zones, inventory_service.list_sensors,
        inventory_service.list_roles_present, employee_service.list_employees,
    )
    pin_canonical_inventory()
    yield
    (inventory_service.list_zones, inventory_service.list_sensors,
     inventory_service.list_roles_present, employee_service.list_employees) = originals


# ===== 打桩响应的小工具 (与 test_agent_runtime 同形) =====


def tool(_tool_name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def say(content: str) -> LLMResponse:
    return LLMResponse(text=content, input_tokens=10, output_tokens=5)


def _request(task_id: int = 1, text: str = "生鲜区湿了就开单") -> LLMRequest:
    return LLMRequest(
        task_id=task_id, stage="parsing",
        messages=agent_prompts.build_messages("parsing", input_text=text),
        tools=agent_prompts.tool_schemas(("ask_clarification",)),
    )


# ===== 回放键: 只由会改变模型输出的输入决定 =====


def test_cassette_key__ignores_task_id():
    """task_id 进键是本段最阴险的错法: 跑得通, 只是每次都在真花钱。"""
    key_a = cassette_key("m", "v1", _request(task_id=1))
    key_b = cassette_key("m", "v1", _request(task_id=999))
    assert key_a == key_b


def test_cassette_key__changes_with_each_deciding_field():
    base = cassette_key("m", "v1", _request())
    assert cassette_key("other-model", "v1", _request()) != base
    assert cassette_key("m", "v2", _request()) != base
    assert cassette_key("m", "v1", _request(text="别的输入")) != base
    # 思考开关改变模型输出, 必须进键 —— 否则翻开关后老录制会冒充新配置的行为
    assert cassette_key("m", "v1", _request(), thinking="enabled") != base
    different_tools = LLMRequest(
        task_id=1, stage="parsing",
        messages=_request().messages,
        tools=agent_prompts.tool_schemas(("ask_clarification", "create_policy")),
    )
    assert cassette_key("m", "v1", different_tools) != base


def test_cassette_key__temperature_changes_key():
    """SPEC-007 验收 17 (变异 M7 的靶子): 温度改变模型输出, 必须进键 ——
    否则换温度后老录制会冒充新配置的行为, 比 miss 更糟。"""
    base = cassette_key("m", "v1", _request())
    assert cassette_key("m", "v1", _request(), temperature=1.0) != base
    # 默认值与显式 0 是同一个键 (评测六个臂全是温度 0, 不该产生两套 cassette)
    assert cassette_key("m", "v1", _request(), temperature=0.0) == base


# ===== 录制回放器 (假 inner, 不连网) =====


class CountingClient:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return self.response


def test_record_then_replay__hit_with_cache_hit_true_and_zero_cost(tmp_path):
    recorded = LLMResponse(
        tool_call=LLMToolCall(tool="ask_clarification",
                              arguments={"question": "哪个区?"}),
        model="m", prompt_version="v1", input_tokens=100, output_tokens=20,
        latency_ms=1234, estimated_cost_cny=0.5,
    )
    inner = CountingClient(recorded)
    rec = RecordReplayLLMClient(inner=inner, directory=tmp_path, mode="record",
                                model="m", prompt_version="v1")
    first = asyncio.run(rec.complete(_request()))
    assert first.cache_hit is False and inner.calls == 1

    replay = RecordReplayLLMClient(inner=None, directory=tmp_path, mode="replay",
                                   model="m", prompt_version="v1")
    second = asyncio.run(replay.complete(_request(task_id=42)))  # 换 task_id 照样命中
    assert second.cache_hit is True
    assert second.tool_call == recorded.tool_call
    assert second.input_tokens == 100 and second.output_tokens == 20
    # 回放没花钱也没等网络: 成本与延迟归零, 免得有人拿回放去回填 120 秒预算
    assert second.estimated_cost_cny == 0.0 and second.latency_ms == 0


def test_replay_legacy_cassette_with_usd_key__no_type_error(tmp_path):
    """改名前录的老 cassette 里成本键还叫 estimated_cost_usd —— 回放分支必须
    显式 pop 掉它, 否则作为未知关键字参数传进 LLMResponse 直接 TypeError。
    重录后老键不再出现, 但正确性不许指望重录来掩盖 (W5 第一段 prompt 第 5 条)。"""
    request = _request()
    key = cassette_key("m", "v1", request)
    (tmp_path / f"{key}.json").write_text(json.dumps({
        "response": {
            "tool_call": None, "text": "老录制", "model": "m",
            "prompt_version": "v1", "input_tokens": 7, "output_tokens": 3,
            "latency_ms": 88, "estimated_cost_usd": 0.5,  # 老键
        },
    }))
    replay = RecordReplayLLMClient(inner=None, directory=tmp_path, mode="replay",
                                   model="m", prompt_version="v1")
    resp = asyncio.run(replay.complete(request))
    assert resp.text == "老录制" and resp.estimated_cost_cny == 0.0


def test_replay_miss__fails_hard_no_fallback_to_real_model(tmp_path):
    inner = CountingClient(say("不该被调到"))
    replay = RecordReplayLLMClient(inner=inner, directory=tmp_path, mode="replay",
                                   model="m", prompt_version="v1")
    with pytest.raises(ReplayMiss):
        asyncio.run(replay.complete(_request()))
    assert inner.calls == 0  # 一次真调都没发生


# ===== 方舟客户端协议解析 (MockTransport, 不连网) =====


def _ark(handler) -> ArkLLMClient:
    return ArkLLMClient(
        base_url="https://ark.test/api/v3", api_key="test-key-not-real",
        model="test-model", prompt_version="v1", timeout_seconds=1,
        price_input_per_mtok=1.0, price_output_per_mtok=2.0,
        transport=httpx.MockTransport(handler),
    )


def _chat_response(message: dict[str, Any],
                   usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": message}],
        "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 500},
    })


def test_ark__parses_tool_call_arguments_json_string():
    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        # 中立形状在客户端换成 OpenAI 的 function 嵌套
        assert payload["tools"][0]["type"] == "function"
        assert payload["tools"][0]["function"]["name"] == "ask_clarification"
        # 思考开关必须显式随请求发出: seed-2.1 默认开思考, 不发这个字段,
        # 单步 80 秒级, 60 秒调用上限必爆 (config.llm_thinking 注释里的实测)
        assert payload["thinking"] == {"type": "disabled"}
        # 温度必须显式设 0, 不走服务端默认 (SPEC-007 第五节: "要可复现"与
        # "不设温度"自相矛盾, 而这个矛盾此前没有任何东西会喊 —— 这行就是那声喊)
        assert payload["temperature"] == 0
        return _chat_response({
            "tool_calls": [{"function": {
                "name": "ask_clarification",
                "arguments": '{"question": "通知谁?"}',  # JSON 字符串, 要解析
            }}],
            "content": None,
        })

    resp = asyncio.run(_ark(handler).complete(_request()))
    assert resp.tool_call == LLMToolCall(tool="ask_clarification",
                                         arguments={"question": "通知谁?"})
    assert resp.input_tokens == 1000 and resp.output_tokens == 500
    # 单价 1.0 / 2.0 元/Mtok: (1000*1 + 500*2) / 1e6
    assert resp.estimated_cost_cny == pytest.approx(0.002)


def test_ark__invalid_arguments_json__model_protocol_error_with_usage():
    """真模型最常见的失败: arguments 不是合法 JSON。必须归 model_protocol_error,
    不能让裸 JSONDecodeError 冒进 _tool_step 的通用兜底落成 dead_letter。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return _chat_response({
            "tool_calls": [{"function": {
                "name": "create_policy", "arguments": '{"name": "x", 截断了',
            }}],
            "content": None,
        })

    with pytest.raises(ModelProtocolError) as exc:
        asyncio.run(_ark(handler).complete(_request()))
    # 协议错的调用照样计费: usage 带着真实花掉的 token
    assert exc.value.usage is not None
    assert exc.value.usage.input_tokens == 1000


def test_ark__empty_tool_calls_and_no_text__model_protocol_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return _chat_response({"tool_calls": [], "content": None})

    with pytest.raises(ModelProtocolError):
        asyncio.run(_ark(handler).complete(_request()))


def test_ark__timeout__llm_call_timeout():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("太慢了")

    with pytest.raises(LLMCallTimeout):
        asyncio.run(_ark(handler).complete(_request()))


def test_ark__server_error__llm_unavailable():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with pytest.raises(LLMUnavailable):
        asyncio.run(_ark(handler).complete(_request()))


# ===== 状态机的失败归类 (真实模型才有的三类, 各有一格) =====


async def _open_and_run(factory, llm, *, input_text, task_id=None):
    if task_id is None:
        async with factory() as session, session.begin():
            created = await agent_service.create_task(
                session, user_id=OWNER, input_text=input_text
            )
        task_id = created["task_id"]
    outcome = await agent_runtime.run_task(task_id, llm, factory)
    return task_id, outcome


async def _task(factory, task_id):
    async with factory() as session, session.begin():
        return await agent_service.get_task(session, task_id)


class RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise self.exc


def test_runtime__llm_call_timeout__dead_letter_llm_timeout(svc):
    """单次 LLM 调用 60 秒那一格: 与单轮预算、单工具超时是三个不同的东西,
    error_code 必须分得出是谁超时 (SPEC-002 第三节上限表)。"""
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory, RaisingClient(LLMCallTimeout("单次 LLM 调用超过 60 秒")),
            input_text="LLM 超时注入",
        )
        return outcome, await _task(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "dead_letter" and task["status"] == "dead_letter"
    assert task["error_code"] == "llm_timeout"
    assert "60" in task["error_detail"]


def test_runtime__llm_unavailable__dead_letter_llm_error(svc):
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory, RaisingClient(LLMUnavailable("连不上方舟")),
            input_text="LLM 故障注入",
        )
        return outcome, await _task(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "dead_letter" and task["error_code"] == "llm_error"


def test_runtime__protocol_error__failed_and_still_billed(svc):
    """协议错落 failed (模型输出问题, 重说一遍就能重开), 且那次调用照样计入
    ai_usage —— 真实模型的账单不会因为输出坏了而退款。"""
    usage = LLMResponse(model="test-model", prompt_version="v1",
                        input_tokens=777, output_tokens=3)
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory,
            RaisingClient(ModelProtocolError("arguments 不是合法 JSON", usage)),
            input_text="协议错注入",
        )
        from sqlalchemy import text
        async with factory() as session, session.begin():
            rows = (await session.execute(text(
                "SELECT input_tokens FROM ai_usage WHERE task_id = :t"),
                {"t": task_id},
            )).scalars().all()
        return outcome, await _task(factory, task_id), rows

    outcome, task, usage_rows = svc(go)
    assert outcome == "failed" and task["status"] == "failed"
    assert task["error_code"] == "model_protocol_error"
    assert usage_rows == [777]  # 落了账


def test_runtime__invalid_tool_arguments__failed_not_dead_letter(svc):
    """工具层参数校验不过 (名字超长) 也是模型输出问题: 归 model_protocol_error
    落 failed, 不落 tool_error 死信 —— 守 _tool_step 里的 InvalidToolArguments
    放行分支。"""
    async def go(factory):
        llm = ScriptedLLMClient(script=[
            say("ok"),
            tool("create_policy", name="超" * 61, body=VALID_BODY),
        ])
        task_id, outcome = await _open_and_run(factory, llm, input_text="超长名字注入")
        return outcome, await _task(factory, task_id)

    outcome, task = svc(go)
    assert outcome == "failed" and task["status"] == "failed"
    assert task["error_code"] == "model_protocol_error"


# ===== 端到端: 录了再放, 第二遍全命中 (键稳定性的硬证据) =====


def test_record_replay_e2e__second_identical_task_all_cache_hit(svc, tmp_path):
    """同一句话跑两条任务 (第一条已到 awaiting_approval, 不撞 one_open 索引):
    第一遍 record, 第二遍 replay 必须**全部** cache_hit —— 键里若混进了
    task_id/时间戳, 这条测试当场红, 而不是"跑得通但每次都在真花钱"。"""
    script = [
        say("在 1 区变湿时开事故"),
        tool("create_policy", name="回放键稳定性", body=VALID_BODY),
    ]

    async def go(factory):
        rec_llm = RecordReplayLLMClient(
            inner=ScriptedLLMClient(script=list(script)),
            directory=tmp_path, mode="record",
            model="test-model", prompt_version=agent_prompts.PROMPT_VERSION,
        )
        task_a, outcome_a = await _open_and_run(
            factory, rec_llm, input_text="回放键稳定性测试输入"
        )
        assert outcome_a == "awaiting_approval"

        replay_llm = RecordReplayLLMClient(
            inner=None, directory=tmp_path, mode="replay",
            model="test-model", prompt_version=agent_prompts.PROMPT_VERSION,
        )
        task_b, outcome_b = await _open_and_run(
            factory, replay_llm, input_text="回放键稳定性测试输入"
        )
        assert outcome_b == "awaiting_approval"
        assert task_b != task_a

        from sqlalchemy import text
        async with factory() as session, session.begin():
            hits = {
                t: (await session.execute(text(
                    "SELECT bool_and(cache_hit), count(*) FROM ai_usage "
                    "WHERE task_id = :t"), {"t": t},
                )).one()
                for t in (task_a, task_b)
            }
        return hits[task_a], hits[task_b]

    (a_all_hit, a_count), (b_all_hit, b_count) = svc(go)
    assert a_count == 2 and a_all_hit is False   # 第一遍是真调 (record)
    assert b_count == 2 and b_all_hit is True    # 第二遍全命中


# ===== 端到端: 跑在进版本库的真实录制上 (离线复跑, 验收 19 的回放半边) =====


def _replay_client() -> RecordReplayLLMClient:
    return RecordReplayLLMClient(
        inner=None,  # 结构上就没有连网的可能
        directory=CASSETTES_DIR, mode="replay",
        model=RECORDED_MODEL, prompt_version=agent_prompts.PROMPT_VERSION,
    )


@needs_cassettes
def test_replay_e2e__happy_path_from_recorded_cassettes(svc, pinned_inventory):
    """一句人话 -> 草案 -> 校验 -> 回放 -> 等审批, 全程跑在真实模型的录制上。"""
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory, _replay_client(), input_text=HAPPY_INPUT
        )
        from sqlalchemy import text
        async with factory() as session, session.begin():
            body = (await session.execute(text("""
                SELECT pv.body FROM policy_versions pv
                JOIN approvals a ON a.policy_version_id = pv.id
                WHERE a.task_id = :t"""), {"t": task_id},
            )).scalar_one()
            hits = (await session.execute(text(
                "SELECT bool_and(cache_hit), count(*) FROM ai_usage "
                "WHERE task_id = :t"), {"t": task_id},
            )).one()
        return outcome, body, hits

    outcome, body, (all_hit, calls) = svc(go)
    assert outcome == "awaiting_approval"
    assert all_hit is True and calls >= 2
    body = body if isinstance(body, dict) else json.loads(body)
    # 招牌句的三个要素真的编进去了: 生鲜区、两个探头、通知 manager
    assert body["scope"] == {"type": "zone", "ids": [1]}
    assert any(c["type"] == "wet_sensor_count" and c["value"] == 2
               for c in body["conditions"])
    assert any(a["type"] == "notify" and a["target_role"] == "manager"
               for a in body["actions"])


@needs_cassettes
def test_replay_e2e__repair_loop_fixed_by_error_code_alone(svc, pinned_inventory):
    """验收 4: 错 zone 草案 -> E_UNKNOWN_ZONE -> 仅凭错误码与 hint 修对 ->
    重新校验通过。跑在录制上 (含一份手工编辑的 cassette, 见文件内 hand_edited
    标注) —— 真模型不会稳定产出错 zone, 不造出来这条就测不到。"""
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory, _replay_client(), input_text=REPAIR_INPUT
        )
        from sqlalchemy import text
        async with factory() as session, session.begin():
            validates = (await session.execute(text("""
                SELECT result_summary FROM agent_steps
                WHERE task_id = :t AND tool_name = 'validate_policy'
                ORDER BY seq"""), {"t": task_id},
            )).scalars().all()
            versions = (await session.execute(text("""
                SELECT count(*) FROM policy_versions pv
                JOIN policies p ON p.id = pv.policy_id
                JOIN agent_steps s ON s.task_id = :t
                    AND s.tool_name = 'create_policy'
                    AND CAST(s.result_summary ->> 'policy_id' AS bigint) = p.id
            """), {"t": task_id})).scalar_one()
        return outcome, validates, versions

    outcome, validates, versions = svc(go)
    assert outcome == "awaiting_approval"
    first, second = (v if isinstance(v, dict) else json.loads(v) for v in validates)
    assert first["ok"] is False
    assert any(i["code"] == "E_UNKNOWN_ZONE" for i in first["issues"])
    assert second["ok"] is True
    assert versions == 1  # 修复就地改, 始终一条草稿行


@needs_cassettes
def test_replay_e2e__ambiguous_input_asks_instead_of_guessing(svc, pinned_inventory):
    """歧义 -> clarifying, 模型问回来而不猜 (SPEC-002 验收 6 的真模型半边;
    打桩半边在 test_agent_runtime)。跑在真实录制上: "不许猜"写在 prompt 里只是
    承诺, 这条测试钉的是真模型面对含糊输入确实会举手, 而不是编一个默认值。"""
    async def go(factory):
        task_id, outcome = await _open_and_run(
            factory, _replay_client(), input_text=CLARIFY_INPUT
        )
        from sqlalchemy import text
        async with factory() as session, session.begin():
            task = await agent_service.get_task(session, task_id)
            pending = (await session.execute(text(
                "SELECT question FROM agent_clarifications "
                "WHERE task_id = :t AND answer IS NULL"), {"t": task_id},
            )).scalars().all()
            approvals = (await session.execute(text(
                "SELECT count(*) FROM approvals WHERE task_id = :t"), {"t": task_id},
            )).scalar_one()
            hits = (await session.execute(text(
                "SELECT bool_and(cache_hit), count(*) FROM ai_usage "
                "WHERE task_id = :t"), {"t": task_id},
            )).one()
        return outcome, task, pending, approvals, hits

    outcome, task, pending, approvals, (all_hit, calls) = svc(go)
    assert outcome == "clarifying" and task["status"] == "clarifying"
    assert all_hit is True and calls >= 1
    # 恰好一个未回答的问题挂着, 且它是一句真的问题, 不是空串
    assert len(pending) == 1 and pending[0].strip()
    # 没猜着往下走: 含糊输入不该产出审批请求
    assert approvals == 0


# ===== 密钥卫生: cassette 里不许有 key 的值 =====


@needs_cassettes
def test_cassettes__contain_no_api_key_or_auth_header():
    """key 走请求头本来就不该进录制; 这条断言是白拿的保险 —— 仓库红线是
    "密钥任何情况下不进仓库"。"""
    files = sorted(CASSETTES_DIR.glob("*.json"))
    assert files, "cassettes 目录不该是空的 (录制脚本还没跑?)"
    key = settings().llm_api_key
    for f in files:
        content = f.read_text()
        assert "Authorization" not in content, f.name
        assert "api_key" not in content, f.name
        if len(key) >= 8:  # 本机有真 key 时, 直接查它的值
            assert key not in content, f.name


def test_model_offerable_tools__all_have_full_schemas():
    """凡是可能进模型请求的工具 (TOOLS_BY_STAGE 全集), agent_prompts 必须有
    完整参数 Schema —— 加了工具忘了配 Schema 会在这里红, 不是在线上 KeyError。"""
    offerable = {n for names in agent_tools.TOOLS_BY_STAGE.values() for n in names}
    schemas = agent_prompts.tool_schemas(tuple(sorted(offerable)))
    assert {s["name"] for s in schemas} == offerable
    for s in schemas:
        assert s["parameters"]["type"] == "object", s["name"]


# ===== 真网冒烟 (默认 skip, CI 不跑) =====


@pytest.mark.skipif(
    not os.environ.get("SENTINEL_LLM_SMOKE"),
    reason="真网冒烟测试, 花真钱; 显式设 SENTINEL_LLM_SMOKE=1 且配好 key 才跑",
)
def test_smoke__real_ark_responds():
    from app.services.llm_client import build_llm_client

    assert settings().llm_api_key, "冒烟测试需要 SENTINEL_LLM_API_KEY"
    client = build_llm_client(prompt_version=agent_prompts.PROMPT_VERSION, mode="off")
    resp = asyncio.run(client.complete(_request(text="生鲜区湿了就开单")))
    assert resp.text or resp.tool_call
    assert resp.input_tokens > 0
