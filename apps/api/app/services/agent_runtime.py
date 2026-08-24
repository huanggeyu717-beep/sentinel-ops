"""Agent 状态机主循环 + 打卡/清扫后台任务 (SPEC-002 第一、二、四节)。

状态机 (SPEC-002 第四节):

    parsing -> discovering -> compiling -> validating -(有错误)-> repairing
                   ^                          |(通过)               |
                   |                          v            (修满 2 次仍不过)
             (人回答, <=3 轮)             simulating                 |
                   |                          v                    v
                   +----- clarifying <---  awaiting_approval   clarifying
                              (人在 Studio 里批准或否决 -> completed,
                               由 policy_service.decide_approval 回写)

- repairing 修完必须回 validating 重新校验 —— 修完不验等于没修;
- 修满 2 次仍不过 -> clarifying 而不是 failed: 连续两轮改不对, 多半不是模型手滑
  而是需求有歧义, 该回头问人;
- **修复次数每轮澄清后重置为 0, LLM 调用总数跨轮累加不重置** —— 这一对是 SPEC
  写死的, 两种挑法的行为差别只在绕圈的时候才看得出来;
- 人回答后回 discovering 不回 compiling: 回答很可能提到新的东西, 重捞一遍
  区/传感器/角色是毫秒级的本地查询, 省掉的是"这次回答有没有引入新实体"的推理。

失败出口 (各有 error_code/error_detail):
    澄清轮次用尽 / LLM 调用总数用尽        -> failed
    单轮超预算 / 工具不可重试错误 / 失联 / 澄清超时 -> dead_letter

已知边界 (SPEC-002 第一、二节, 按要求写进代码不只写在文档):
- 多实例部署时每个实例各跑各的任务, 靠租约区分归属, 不做选主; 打卡只打本进程
  (runner_id) 名下的任务, 清扫是幂等的条件更新, 多实例重复跑无害;
- 进程重启会让在跑的任务失联, 由租约清扫收尾; 重启前最多 lease_timeout 秒内,
  同一句话会被去重索引挡住 —— 接口层要把那条卡住的任务标"疑似中断", 不报"重复提交";
- W4 单实例; SSE 是进程内的, 多实例转发是 W6 的事。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import settings
from . import (
    agent_prompts,
    agent_service,
    agent_tools,
    budget_service,
    policy_service,
    report_task_service,
)
from .agent_tools import InvalidToolArguments, ToolContext, ToolTimeout
from .llm_client import (
    LLMCallTimeout,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    ModelProtocolError,
    ReplayMiss,
)

log = logging.getLogger(__name__)

# 谁在跑: 进程启动时生成一个随机串 (SPEC-002 第二节)。判死后它就算复活,
# 每条写库 SQL 里的 runner_id 条件也会让它受影响 0 行, 当场停手。
RUNNER_ID = secrets.token_hex(8)

# 修复次数上限。**每轮澄清后重置** (run_task 每次被调起就是新的一轮, 计数器是
# 局部变量, 天然重置); 与之相反, LLM 调用总数跨轮累加 —— 数的是 ai_usage 的行。
MAX_REPAIRS = 2

# 工具重试: 只对可重试错误 (超时) 做指数退避; 次数与底数是实现细节不进配置。
_TOOL_ATTEMPTS = 3
_RETRY_BACKOFF_BASE_S = 0.5

# prompt 内容与版本号在 agent_prompts (改 prompt 必须换号, 理由见那边 docstring)


# ===== 消融能力档 (SPEC-007 第四节): 开关穿过同一份状态机, 不 fork =====
#
# 与 SPEC-007 的 profile 形状有一处刻意不一致, 报告第一节报备:
# SPEC 列了 simulate 开关 (A0/A1=False), 但版本状态链 draft -> validated ->
# simulated -> awaiting_approval 由 policy_service._advance_status 与数据库
# CHECK 强制 —— request_approval 只接受 simulated。**校验与模拟这两步是架构,
# 不是模型能力**, 任何档都关不掉它们; 能关掉的只有模型侧能力: 修复循环、追问、
# 资源清单的呈现方式。留一个关不掉的 simulate 开关, 就是一个 M5 类变异永远
# 打不红的假开关 (没有任何测试能守它) —— 这正是本项目最忌讳的形状, 所以不设。
# simulate_feedback (A3, 把 ReplayReport 回喂模型) 保留字段但恒 False:
# A3 定为不做 (SPEC-007 补入 27), 字段留着是让"未做"在类型上看得见。


@dataclass(frozen=True)
class AblationProfile:
    """能力开关。默认路径 (不传 profile) 等价于 production() —— 验收 14/15。"""

    inventory_in_prompt: bool   # A0: 资源清单静态文本进 system prompt, 无工具直出
    discovery_tools: bool       # A1+: 走 discovering, 清单进 user 消息
    validate_and_repair: bool   # A2+: 校验不过时进修复循环 (校验本身任何档都跑)
    clarification: bool         # A2+: parsing 阶段 + ask_clarification 工具
    simulate_feedback: bool     # A3: 未做, 恒 False

    @classmethod
    def production(cls) -> AblationProfile:
        """W4 出厂路径 = A2 (SPEC-007 第四节: "A2 就是 W4 的出厂路径")。"""
        return cls(
            inventory_in_prompt=False,
            discovery_tools=True,
            validate_and_repair=True,
            clarification=True,
            simulate_feedback=False,
        )

    @classmethod
    def from_level(cls, level: str) -> AblationProfile:
        if level in ("production", "A2"):
            return cls.production()
        if level == "A0":
            return cls(
                inventory_in_prompt=True, discovery_tools=False,
                validate_and_repair=False, clarification=False,
                simulate_feedback=False,
            )
        if level == "A1":
            return cls(
                inventory_in_prompt=False, discovery_tools=True,
                validate_and_repair=False, clarification=False,
                simulate_feedback=False,
            )
        raise ValueError(f"未知消融档 {level!r}, 只认 production/A0/A1/A2")


def _resolve_profile(profile: AblationProfile | None) -> AblationProfile:
    """不传 profile 时按 config 解析 (默认 production)。runner 每臂一个 API
    进程、经 SENTINEL_AGENT_ABLATION_LEVEL 设档; 生产部署永远不设即出厂行为。"""
    if profile is not None:
        return profile
    return AblationProfile.from_level(settings().agent_ablation_level)


# ===== tool_fault 故障注入 (仅评测, SPEC-007 第二节 tool_fault 类) =====
#
# runner 走 HTTP, 没有别的通道把"这条任务的这个工具要坏一次"送进后台协程,
# 所以注入点只能在 _tool_step。生产恒关 (config.agent_fault_injection_file 为
# 空字符串)。表按 (归一化输入文本, 工具名) 匹配, 但**注入的归属单位是用例不是
# 文本**: 两条用例同文完全合法 (v1 里就有一对刻意同文的对照), 所以 runner 按
# "激活窗口"维护这份文件 —— 只在带 inject 的那条用例的任务在跑时写入它的条目,
# 任务终局即移除, 同文用例由 runner 串行并互斥 (evals/runner/cli.FaultWindow)。
# 文件因此会在一次 run 中途变化, 缓存按 mtime 失效, 不能按路径缓存一次到老。
# 两种故障与 SPEC-002 第八节的两类错误一一对应:
#   timeout_once  第一次尝试抛 ToolTimeout (可重试), 退避后第二次放行;
#   unretryable   每次尝试都抛不可重试错误 -> dead_letter (code=tool_error)。

_FAULT_WHITESPACE_RE = re.compile(r"\s+")


class EvalInjectedFault(Exception):
    """评测注入的不可重试故障 (error_detail 里看得到类型名, 别当真实故障排查)。"""


@lru_cache(maxsize=64)
def _fault_table(path: str, mtime_ns: int) -> dict[tuple[str, str], str]:
    """mtime 进缓存键: 同一路径内容变了 (激活窗口开/关) 必须重读。"""
    entries = json.loads(Path(path).read_text())
    return {
        (_FAULT_WHITESPACE_RE.sub(" ", str(e["input"]).strip()), str(e["tool"])):
            str(e["fault"])
        for e in entries
    }


def _injected_fault(input_text: str, tool_name: str) -> str | None:
    path = settings().agent_fault_injection_file
    if not path:
        return None
    try:
        mtime_ns = Path(path).stat().st_mtime_ns
    except FileNotFoundError:
        return None
    normalized = _FAULT_WHITESPACE_RE.sub(" ", input_text.strip())
    return _fault_table(path, mtime_ns).get((normalized, tool_name))


class _TaskFinished(Exception):
    """内部控制流: 本轮到此为止 (clarifying / failed / dead_letter / awaiting_approval)。"""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass
class _RoundState:
    """一轮 (claim 到交出控制权) 的工作台账, 不落库的部分。"""

    task_id: int
    user_id: int
    input_text: str
    target_policy_id: int | None
    stage: str
    draft_version_id: int | None
    repairs_used: int = 0
    last_issues: list[dict[str, Any]] | None = None
    inventory: dict[str, Any] | None = None
    # 已有的澄清问答 [(question, answer)], 按时间线顺序 —— 澄清回来那一轮,
    # 模型必须看得到人答了什么, 否则问了等于白问
    clarifications: list[tuple[str, str | None]] | None = None
    # 模型最近一次给出的草稿 body (本轮内存台账): repairing 的 prompt 要带上,
    # 不给草稿等于让模型盲改
    last_body: dict[str, Any] | None = None
    # 消融能力档 (W5)。默认 production, 即 W4 出厂路径 —— 见 AblationProfile
    profile: AblationProfile | None = None
    # ==== 报告任务 (SPEC-008) 专用台账, 策略任务恒为 None ====
    # 事实包 (collecting 阶段取回的 JSON 形状, 与 fact_pack 快照同一份);
    # last_issues 复用上面的字段装校验违规 (code/field/detail)
    report_incident_id: int | None = None
    report_facts: list[dict[str, Any]] | None = None

    @property
    def caps(self) -> AblationProfile:
        assert self.profile is not None  # run_task 入口已解析
        return self.profile


# ===== 入口 =====


async def run_task(
    task_id: int,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str = RUNNER_ID,
    profile: AblationProfile | None = None,
) -> str:
    """跑一轮任务 (后台 asyncio 任务的主体; 本段无 HTTP, 由测试直接 await)。

    返回本轮结局: awaiting_approval / awaiting_review (报告任务) / clarifying /
    failed / dead_letter / not_claimed / lease_lost。
    单轮预算只算机器在跑的时间, 跨轮不累加。
    profile 不传时按 config 解析, 默认 production —— 两条路必须产出相同的
    agent_steps 序列 (SPEC-007 验收 15, 由测试守着)。
    """
    async with factory() as session, session.begin():
        if not await agent_service.claim_task(session, task_id, runner_id):
            return "not_claimed"
        task = await agent_service.get_task(session, task_id)
        draft = await agent_service.find_task_draft_version(session, task_id)
        timeline = await agent_service.get_timeline(session, task_id)
    raw_input = task["input"]
    task_input: dict[str, Any] = (
        raw_input if isinstance(raw_input, dict) else json.loads(raw_input)
    )
    is_report = task["task_type"] == "incident_report"
    st = _RoundState(
        task_id=task_id,
        user_id=task["user_id"],
        input_text=task_input.get("text", ""),
        target_policy_id=task_input.get("target_policy_id"),
        # clarifying -> discovering 的改写是策略状态机专用 (雷区 7): 报告任务
        # 没有澄清, stage 原样带入 (create_report_task 建行时已显式写 collecting)
        stage=(
            task["stage"] if is_report or task["stage"] != "clarifying"
            else "discovering"
        ),
        draft_version_id=draft,
        clarifications=_extract_clarifications(timeline),
        profile=_resolve_profile(profile),
    )
    # 只分派循环体, 外壳 (租约/单轮预算/失败出口分类) 两种任务共用一套
    round_body = (
        _round_report(st, llm, factory, runner_id) if is_report
        else _round(st, llm, factory, runner_id)
    )
    try:
        await asyncio.wait_for(
            round_body,
            timeout=settings().agent_round_budget_seconds,
        )
    except _TaskFinished as fin:
        return fin.outcome
    except (InvalidToolArguments, ModelProtocolError) as e:
        # 模型没按协议来: 该调工具时没调 / 调了越界工具 / 参数不合法 /
        # arguments 不是合法 JSON / 空 tool_calls 又不给文本。归模型输出问题,
        # 落 failed, 用户重说一遍就能重开 (SPEC-002 第四节失败出口表)
        await _fail(
            factory, st, runner_id, status="failed",
            code="model_protocol_error", detail=str(e),
        )
        return "failed"
    except LLMCallTimeout as e:
        # 单次 LLM 调用超时 (60 秒那一格, 与单轮预算、单工具超时都是不同的东西)
        await _fail(
            factory, st, runner_id, status="dead_letter",
            code="llm_timeout", detail=str(e),
        )
        return "dead_letter"
    except LLMUnavailable as e:
        await _fail(
            factory, st, runner_id, status="dead_letter",
            code="llm_error", detail=f"模型服务不可用: {e}",
        )
        return "dead_letter"
    except ReplayMiss as e:
        # 回放没命中: 这条任务判失败并留下可统计的错误码 (SPEC-007 验收 20),
        # 不回退真模型、不静默跳过 —— miss 率本身是回归臂要报的数字。
        # W4 时它会当成未分类异常炸掉后台协程、任务挂到租约清扫才收尸,
        # 那样 miss 在报告里就成了"失联", 病因被换了一个名字。
        await _fail(
            factory, st, runner_id, status="dead_letter",
            code="replay_miss",
            detail=f"回放未命中 (stage={e.stage}, key={e.key[:16]}...)",
        )
        return "dead_letter"
    except TimeoutError:
        # 单轮执行超预算 -> dead_letter (SPEC-002 第四节失败出口表)
        await _fail(
            factory, st, runner_id, status="dead_letter",
            code="round_budget_exceeded",
            detail=f"单轮执行超预算 ({settings().agent_round_budget_seconds} 秒)",
        )
        return "dead_letter"
    except agent_service.LeaseLost:
        # 那道闸: 租约已被收走, 当场停手, 一个字都不写 (本轮想写的已随事务回滚)
        log.warning("task %s: 租约已失, 停手", task_id)
        return "lease_lost"
    raise AssertionError("_round 只能以 _TaskFinished 或异常结束")  # pragma: no cover


async def _round(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
) -> None:
    while True:
        if st.stage == "parsing":
            if not st.caps.clarification:
                # A0/A1: parsing 的产出只有复述文本 (不进任何后续 prompt,
                # build_messages 不带它) 与一次追问机会 —— 追问能力关掉后,
                # 这次调用对后续输出零影响, 纯烧钱, 跳过 (SPEC-007 第四节)
                await _advance(st, factory, runner_id, "discovering")
                continue
            resp = await _llm_call(st, llm, factory, runner_id, stage="parsing")
            # 不调工具是这个阶段的正常情况; 调了就必须是 ask_clarification ——
            # 越界与 compiling/repairing 同一口径走 _expect_tool 报协议错,
            # 不静默吞掉 (一处报错一处静默, 查问题的人会以为这里从没越界过)
            if resp.tool_call is not None:
                _expect_tool(resp, ("ask_clarification",))
                await _clarify(st, factory, runner_id, resp)
            await _tool_free_step(
                st, factory, runner_id, "parse_input",
                summary={"text": resp.text or ""}, resp=resp,
            )
            await _advance(st, factory, runner_id, "discovering")
        elif st.stage == "discovering":
            # A0 (discovery_tools=False) 也走这里: 它的资源清单必须从**同一批**
            # 只读 service 取 (SPEC-007 第四节 "与 A1 的工具同源"), 差别只在
            # 清单进 system prompt 还是 user 消息。工具步骤照记 —— 这些是
            # 运行时确定性驱动的本地查询, 不是模型能力, 藏起来才是造假。
            inventory: dict[str, Any] = {}
            for name in ("list_zones", "list_sensors", "list_roles", "list_employees"):
                inventory[name] = await _tool_step(st, factory, runner_id, name, {})
            if st.target_policy_id is not None:
                inventory["get_policy"] = await _tool_step(
                    st, factory, runner_id, "get_policy",
                    {"policy_id": st.target_policy_id},
                )
            st.inventory = inventory
            await _advance(st, factory, runner_id, "compiling")
        elif st.stage == "compiling":
            if st.caps.inventory_in_prompt:
                await _compile_direct(st, llm, factory, runner_id)
            else:
                # 有草稿 (澄清回来那一轮) 只给就地改 —— 每个任务只新建一版草稿,
                # 此后所有修复都在这一版上 (SPEC-002 第六节)
                trim = (
                    "compiling" if st.draft_version_id is None
                    else "compiling_with_draft"
                )
                resp = await _llm_call(
                    st, llm, factory, runner_id, stage="compiling", trim=trim
                )
                call = _expect_tool(resp, _stage_tools(st, trim))
                if call.tool == "ask_clarification":
                    await _clarify(st, factory, runner_id, resp)
                result = await _tool_step(
                    st, factory, runner_id, call.tool,
                    _pin_draft(st, call.tool, call.arguments), resp=resp,
                )
                st.draft_version_id = result.get("version_id", st.draft_version_id)
                if isinstance(call.arguments.get("body"), dict):
                    st.last_body = call.arguments["body"]
            await _advance(st, factory, runner_id, "validating")
        elif st.stage == "validating":
            # 校验任何档都跑: 它不是模型能力, 是版本状态链的一环 (draft ->
            # validated -> simulated -> awaiting_approval, policy_service 与
            # 数据库 CHECK 强制)。消融能关的是"校验不过之后有没有修复循环"。
            result = await _tool_step(
                st, factory, runner_id, "validate_policy",
                {"version_id": st.draft_version_id},
            )
            if result["ok"]:
                st.last_issues = None
                await _advance(st, factory, runner_id, "simulating")
            else:
                st.last_issues = result["issues"]
                if not st.caps.validate_and_repair:
                    # A0/A1: 一次机会, 校验不过即终 —— failed 不是 dead_letter
                    # (模型输出质量问题, 不是系统故障; 与 model_protocol_error
                    # 同一档), 错误码单列让评测统计分得出"验证器拦下"这一层
                    codes = [str(i.get("code")) for i in result["issues"]]
                    await _fail(
                        factory, st, runner_id, status="failed",
                        code="validation_failed",
                        detail=f"校验不通过且本档无修复能力: {codes}",
                    )
                    raise _TaskFinished("failed")
                if st.repairs_used < MAX_REPAIRS:
                    await _advance(st, factory, runner_id, "repairing")
                else:
                    # 修满 2 次仍不过 -> 该回头问人, 只留 ask_clarification 一个出口
                    resp = await _llm_call(
                        st, llm, factory, runner_id, stage="repairing",
                        trim=None, only_clarify=True,
                    )
                    _expect_tool(resp, ("ask_clarification",))
                    await _clarify(st, factory, runner_id, resp)
        elif st.stage == "repairing":
            resp = await _llm_call(st, llm, factory, runner_id, stage="repairing")
            call = _expect_tool(resp, _stage_tools(st, "repairing"))
            if call.tool == "ask_clarification":
                await _clarify(st, factory, runner_id, resp)
            await _tool_step(
                st, factory, runner_id, call.tool,
                _pin_draft(st, call.tool, call.arguments), resp=resp,
            )
            if isinstance(call.arguments.get("body"), dict):
                st.last_body = call.arguments["body"]
            st.repairs_used += 1
            # 修完必须回 validating 重新校验 —— 修完不验等于没修
            await _advance(st, factory, runner_id, "validating")
        elif st.stage == "simulating":
            await _tool_step(
                st, factory, runner_id, "simulate_policy",
                {"version_id": st.draft_version_id, "source": "history_csv"},
            )
            await _tool_step(
                st, factory, runner_id, "request_approval",
                {"version_id": st.draft_version_id},
            )
            async with factory() as session, session.begin():
                await agent_service.finish_task(
                    session, st.task_id, runner_id, status="awaiting_approval"
                )
            raise _TaskFinished("awaiting_approval")
        else:  # pragma: no cover
            raise AssertionError(f"未知 stage: {st.stage}")


async def _round_report(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
) -> None:
    """报告任务的循环体 (SPEC-008 第四节): collecting -> drafting -> validating
    -(有错)-> repairing -> validating, 通过即 awaiting_review。

    与策略循环体的三处刻意不同, 都是 SPEC 定死的:
    - 没有 ask_clarification: 事故已经结了, 现场没人可问, 事实不足由
      "事实包一律产全"机制兜住 (模型照常写 {{ack_by}}, 渲染出"无此记录");
    - 修满 MAX_REPAIRS 次仍不过 -> **failed** 不是 clarifying (没人可问),
      报告标 discarded —— failed 专指"模型没写对";
    - 工具清单不经 _stage_tools 的消融裁剪 (雷区 8): 能力档是给策略编译
      定义的, 报告的三个工具在任何档下都一样。
    """
    # incident_id 从任务输入解出 (input_text 是 report_task_service 定死的
    # "incident_report:{id}", 有测试钉住这个格式)
    st.report_incident_id = int(st.input_text.rpartition(":")[2])
    while True:
        if st.stage == "collecting":
            # 运行时确定性驱动, 与策略的 discovering 同一口径: 取事实是本地
            # 查询不是模型能力, 照记工具步骤 —— 藏起来才是造假
            result = await _tool_step(
                st, factory, runner_id, "get_incident_facts",
                {"incident_id": st.report_incident_id},
            )
            st.report_facts = result["facts"]
            await _advance(st, factory, runner_id, "drafting")
        elif st.stage == "drafting":
            resp = await _report_llm_call(st, llm, factory, runner_id,
                                          stage="drafting")
            call = _expect_tool(resp, agent_tools.TOOLS_BY_STAGE["report_drafting"])
            body = call.arguments.get("body")
            # incident_id 与 fact_pack 由运行时注入, 不采信模型 (同 _pin_draft);
            # 快照就是 collecting 阶段给模型看的那一份
            await _tool_step(
                st, factory, runner_id, "create_report_draft",
                {"body": body, "incident_id": st.report_incident_id,
                 "fact_pack": st.report_facts},
                resp=resp,
            )
            if isinstance(body, dict):
                st.last_body = body
            await _advance(st, factory, runner_id, "validating")
        elif st.stage == "validating":
            # 校验是 service 侧的确定性检查 (两道硬拦 + 上限), 对着 fact_pack
            # 快照跑; 违规按项累加进 incident_reports 的两个计数列。
            # 校验 + 记步骤同一个事务, 发号闸照常生效 (租约丢了整体回滚)。
            async with factory() as session, session.begin():
                result = await report_task_service.validate_task_report(
                    session, task_id=st.task_id
                )
                await agent_service.record_step(
                    session, st.task_id, runner_id,
                    tool_name="validate_report",
                    result_summary={
                        "ok": result["ok"], "violations": result["violations"],
                        "bare_fact_attempts": result["bare_fact_attempts"],
                        "dangling_ref_attempts": result["dangling_ref_attempts"],
                    },
                )
            if result["ok"]:
                st.last_issues = None
                async with factory() as session, session.begin():
                    await agent_service.finish_task(
                        session, st.task_id, runner_id, status="awaiting_review"
                    )
                raise _TaskFinished("awaiting_review")
            st.last_issues = result["violations"]
            if st.repairs_used < MAX_REPAIRS:
                await _advance(st, factory, runner_id, "repairing")
            else:
                codes = sorted({str(v.get("code")) for v in result["violations"]})
                await _fail(
                    factory, st, runner_id, status="failed",
                    code="report_validation_failed",
                    detail=f"修满 {MAX_REPAIRS} 次仍未过校验: {codes}",
                )
                raise _TaskFinished("failed")
        elif st.stage == "repairing":
            resp = await _report_llm_call(st, llm, factory, runner_id,
                                          stage="repairing")
            call = _expect_tool(resp, agent_tools.TOOLS_BY_STAGE["report_repairing"])
            body = call.arguments.get("body")
            await _tool_step(
                st, factory, runner_id, "update_report_draft",
                {"body": body}, resp=resp,
            )
            if isinstance(body, dict):
                st.last_body = body
            st.repairs_used += 1
            # 修完必须回 validating 重新校验 —— 修完不验等于没修 (与策略同规)
            await _advance(st, factory, runner_id, "validating")
        else:  # pragma: no cover
            raise AssertionError(f"报告任务未知 stage: {st.stage}")


async def _advance(
    st: _RoundState,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    stage: str,
) -> None:
    """推进 stage: 落库 (过闸) + 更新本轮台账。每次状态迁移在时间线上留一条。"""
    async with factory() as session, session.begin():
        await agent_service.advance_stage(session, st.task_id, runner_id, stage)
    st.stage = stage


# ===== LLM 调用 (计量落 ai_usage, 总数硬上限跨轮累加) =====


def _stage_tools(st: _RoundState, key: str) -> tuple[str, ...]:
    """按能力档裁剪该阶段的工具清单: 追问能力关掉时 ask_clarification 整体消失
    (不给模型、也不在 _expect_tool 的白名单里 —— 模型硬调就是协议错, 与调一个
    不存在的工具同一口径)。裁剪仍然不是安全措施, 理由同 TOOLS_BY_STAGE。"""
    names = agent_tools.TOOLS_BY_STAGE.get(key, ())
    if not st.caps.clarification:
        names = tuple(n for n in names if n != "ask_clarification")
    return names


def _parse_direct_output(text: str | None) -> tuple[str, dict[str, Any]]:
    """A0 直出内容 -> (name, body)。只认 {"name": str, "body": object} 一种形状;
    唯一的宽容是剥掉代码栅栏 (模型最常见的格式跑偏, 剥栅栏不改内容) ——
    其余一律 InvalidToolArguments, 由 run_task 归成 model_protocol_error 落
    failed: 直出档守不守得住输出协议, 本身就是这一档要量的东西之一。"""
    if not text or not text.strip():
        raise InvalidToolArguments("A0 直出: 模型没有给出任何文本")
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines[1:]).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InvalidToolArguments(f"A0 直出内容不是合法 JSON: {e}") from e
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("name"), str)
        or not isinstance(data.get("body"), dict)
    ):
        raise InvalidToolArguments(
            'A0 直出 JSON 必须形如 {"name": <字符串>, "body": <对象>}'
        )
    return data["name"], data["body"]


async def _compile_direct(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
) -> None:
    """A0 直出档的编译: 一次无工具调用, 模型输出 JSON, 运行时代为建草稿。

    走同一个 create_policy 工具执行路径 (schema 校验、审计、步骤记录一样不少) ——
    直出省掉的是工具协议那一跳, 不是 service 层的任何一道闸。
    """
    resp = await _llm_call(st, llm, factory, runner_id, stage="compiling", direct=True)
    if resp.tool_call is not None:
        raise InvalidToolArguments(
            f"A0 档不提供任何工具, 模型却发起了工具调用 {resp.tool_call.tool}"
        )
    name, body = _parse_direct_output(resp.text)
    result = await _tool_step(
        st, factory, runner_id, "create_policy",
        {"name": name, "body": body}, resp=resp,
    )
    st.draft_version_id = result.get("version_id", st.draft_version_id)
    st.last_body = body


async def _llm_complete(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    *,
    stage: str,
    messages: list[dict[str, str]],
    tool_names: tuple[str, ...],
) -> LLMResponse:
    """总数上限 + 发请求 + 计量落账, 策略与报告两个循环体共用 —— 拆出来是让
    报告任务复用同一套硬上限与 ai_usage 落账, 不复制第二份 (雷区 7 的精神)。"""
    async with factory() as session, session.begin():
        used = await agent_service.llm_calls_used(session, st.task_id)
    if used >= settings().agent_max_llm_calls:
        await _fail(
            factory, st, runner_id, status="failed", code="llm_calls_exhausted",
            detail=f"LLM 调用总数用尽 (上限 {settings().agent_max_llm_calls}, 跨轮累加)",
        )
        raise _TaskFinished("failed")
    request = LLMRequest(
        task_id=st.task_id, stage=stage,
        messages=messages,
        tools=agent_prompts.tool_schemas(tool_names),
    )
    try:
        resp = await llm.complete(request)
    except ModelProtocolError as e:
        # 协议错的调用照样计费 (账单不会退款), 先落账再让 run_task 收口
        if e.usage is not None:
            async with factory() as session, session.begin():
                await agent_service.record_llm_usage(session, st.task_id, e.usage)
        raise
    # 计量在独立小事务里落库: 后续工具执行失败回滚时, 这次调用照样计数
    # (真实模型的账单不会因为工具失败而退款)
    async with factory() as session, session.begin():
        await agent_service.record_llm_usage(session, st.task_id, resp)
    return resp


async def _llm_call(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    *,
    stage: str,
    trim: str | None = None,
    only_clarify: bool = False,
    direct: bool = False,
) -> LLMResponse:
    if direct:
        # A0: 无工具, 清单在 system prompt 里 (v3-a0, SPEC-007 第四节)
        assert st.inventory is not None
        messages = agent_prompts.build_messages_a0(
            input_text=st.input_text, inventory=st.inventory
        )
        tool_names: tuple[str, ...] = ()
    else:
        messages = agent_prompts.build_messages(
            "clarify_only" if only_clarify else (trim or stage),
            input_text=st.input_text,
            target_policy_id=st.target_policy_id,
            inventory=st.inventory,
            last_issues=st.last_issues,
            clarifications=st.clarifications,
            draft_body=st.last_body if stage == "repairing" else None,
        )
        tool_names = (
            ("ask_clarification",) if only_clarify
            else _stage_tools(st, trim or stage)
        )
    return await _llm_complete(
        st, llm, factory, runner_id,
        stage=stage, messages=messages, tool_names=tool_names,
    )


async def _report_llm_call(
    st: _RoundState,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    *,
    stage: str,
) -> LLMResponse:
    """报告任务的模型调用: 报告 prompt + 未裁剪的报告工具清单 (雷区 8:
    不经 _stage_tools —— 消融档是策略编译的自变量, 报告工具任何档下都一样)。"""
    assert st.report_facts is not None  # collecting 已跑
    messages = agent_prompts.build_report_messages(
        stage,
        facts=st.report_facts,
        violations=st.last_issues,
        draft_body=st.last_body if stage == "repairing" else None,
    )
    tool_names = agent_tools.TOOLS_BY_STAGE[
        "report_drafting" if stage == "drafting" else "report_repairing"
    ]
    return await _llm_complete(
        st, llm, factory, runner_id,
        stage=stage, messages=messages, tool_names=tool_names,
    )


def _extract_clarifications(
    timeline: list[dict[str, Any]],
) -> list[tuple[str, str | None]] | None:
    """从时间线里按序取出澄清问答对, 拼进 prompt —— 澄清回来那一轮, 模型必须
    看得到人答了什么 (问题与回答在时间线上各占一个 seq, 问必在答前)。"""
    qa: list[tuple[str, str | None]] = []
    for item in timeline:
        if item["kind"] == "clarification_question":
            qa.append((str(item["label"]), None))
        elif item["kind"] == "clarification_answer" and qa:
            qa[-1] = (qa[-1][0], str(item["label"]))
    return qa or None


def _expect_tool(resp: LLMResponse, allowed: tuple[str, ...]) -> Any:
    """模型必须调所列工具之一。裁剪不是安全措施 —— 越界的调用在这里被拒,
    service 层的三道闸照样各判各的。"""
    if resp.tool_call is None or resp.tool_call.tool not in allowed:
        got = resp.tool_call.tool if resp.tool_call else "无工具调用"
        raise InvalidToolArguments(f"该阶段只接受 {allowed}, 模型给了 {got}")
    return resp.tool_call


def _pin_draft(
    st: _RoundState, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """update_policy_draft 的 version_id 由运行时钉死为本任务那一版草稿:
    "就地改**本任务这一版**的 body" (SPEC-002 第五节) —— 不采信模型给的版本号,
    模型报错版本也改不到别的版本上; 顺带让脚本化打桩不必预知动态 id。"""
    if tool == "update_policy_draft":
        return {**arguments, "version_id": st.draft_version_id}
    return arguments


# ===== 步骤执行 =====


async def _tool_step(
    st: _RoundState,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    name: str,
    arguments: dict[str, Any],
    resp: LLMResponse | None = None,
) -> Any:
    """执行一个工具并落一条 agent_steps, 同一个事务 —— 闸拦下时副作用一并回滚。

    只对可重试错误 (超时) 指数退避; 不可重试错误落 dead_letter (SPEC-002 第八节)。
    """
    for attempt in range(_TOOL_ATTEMPTS):
        t0 = perf_counter()
        try:
            # 评测故障注入 (生产恒关, 见 EvalInjectedFault 一节): 抛在工具执行
            # 之前, 走与真实故障完全相同的两条既有路径 —— timeout 进重试退避,
            # 不可重试进 dead_letter。不为注入开第三条收口路径。
            fault = _injected_fault(st.input_text, name)
            if fault == "timeout_once" and attempt == 0:
                raise ToolTimeout(name)
            if fault == "unretryable":
                raise EvalInjectedFault(f"评测注入的不可重试故障 (tool={name})")
            async with factory() as session, session.begin():
                ctx = ToolContext(
                    session=session, task_id=st.task_id,
                    user_id=st.user_id, runner_id=runner_id,
                )
                result = await agent_tools.run_tool(ctx, name, arguments)
                await agent_service.record_step(
                    session, st.task_id, runner_id,
                    tool_name=name, arguments=_small_args(name, arguments),
                    result_summary=_summarize(name, result),
                    status="ok",
                    latency_ms=int((perf_counter() - t0) * 1000),
                    retry_count=attempt,
                    input_tokens=resp.input_tokens if resp else None,
                    output_tokens=resp.output_tokens if resp else None,
                )
                return result if isinstance(result, dict) else {"result": result}
        except ToolTimeout:
            if attempt + 1 >= _TOOL_ATTEMPTS:
                await _fail(
                    factory, st, runner_id, status="dead_letter",
                    code="tool_timeout",
                    detail=f"工具 {name} 连续 {_TOOL_ATTEMPTS} 次超时"
                           f" (单次上限 {settings().agent_tool_timeout_seconds} 秒)",
                )
                raise _TaskFinished("dead_letter") from None
            await asyncio.sleep(_RETRY_BACKOFF_BASE_S * (2 ** attempt))
        except (agent_service.LeaseLost, _TaskFinished, InvalidToolArguments):
            # InvalidToolArguments 是模型给的参数在工具层不合法 (名字超长、缺
            # 字段) —— 模型输出问题, 交给 run_task 归成 model_protocol_error
            # 落 failed, 不在这里落成 tool_error 死信 (口径会错)
            raise
        except Exception as e:
            # 不可重试错误: 干净失败, error_detail 里有人话
            await _fail(
                factory, st, runner_id, status="dead_letter",
                code="tool_error",
                detail=f"工具 {name} 不可重试错误: {type(e).__name__}: {e}",
            )
            raise _TaskFinished("dead_letter") from e
    raise AssertionError("unreachable")  # pragma: no cover


async def _tool_free_step(
    st: _RoundState,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    tool_name: str,
    *,
    summary: dict[str, Any],
    resp: LLMResponse | None = None,
) -> None:
    """不经工具注册表的时间线记录 (如 parse_input)。"""
    async with factory() as session, session.begin():
        await agent_service.record_step(
            session, st.task_id, runner_id,
            tool_name=tool_name, result_summary=summary,
            input_tokens=resp.input_tokens if resp else None,
            output_tokens=resp.output_tokens if resp else None,
        )


def _small_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """body 这类大参数进 result_summary 的专门字段, arguments 只留标量。"""
    return {k: v for k, v in arguments.items() if not isinstance(v, dict | list)}


def _summarize(name: str, result: Any) -> dict[str, Any]:
    """按工具挑要进 agent_steps.result_summary 的内容。

    update_policy_draft 的 previous_body 必须原样保留 —— 修复前的中间态只存在
    这里, W5 的评测样本一条不丢 (SPEC-002 第六节)。回放报告只留统计不留全量
    effects (那是给审批界面的, Trace 里给数字)。
    """
    if not isinstance(result, dict):
        return {"result": result}
    if name in ("create_policy", "add_policy_version", "update_policy_draft"):
        return result  # 含 version_id / previous_body, 全保留
    if name in ("create_report_draft", "update_report_draft"):
        return result  # 含 report_id / previous_body (修复前的中间态只存在这里)
    if name == "validate_policy":
        return {"ok": result["ok"], "issues": result["issues"]}
    if name == "simulate_policy":
        return {
            "source": result["source"], "events_count": result["events_count"],
            "by_action_type": result["by_action_type"],
            "warnings": result["warnings"], "skipped_count": len(result["skipped"]),
        }
    if name == "request_approval":
        return result
    if name == "get_policy":
        return {"policy_id": result["policy"]["id"],
                "versions": len(result["versions"])}
    # list_* / get_available_actions: 内容是给模型的, Trace 里只留规模
    first = next(iter(result.values()), None)
    return {"count": len(first) if isinstance(first, list) else 1}


# ===== 澄清与失败 =====


async def _clarify(
    st: _RoundState,
    factory: async_sessionmaker[AsyncSession],
    runner_id: str,
    resp: LLMResponse,
) -> None:
    """模型举手问人。轮次 ≤ 3, 超出 -> failed (SPEC-002 第三节上限表)。"""
    arguments = resp.tool_call.arguments if resp.tool_call else {}
    question = str(arguments.get("question", "")) or "需要更多信息"
    async with factory() as session, session.begin():
        rounds = await agent_service.clarify_rounds_used(session, st.task_id)
    if rounds >= settings().agent_max_clarify_rounds:
        await _fail(
            factory, st, runner_id, status="failed", code="clarify_rounds_exhausted",
            detail=f"澄清轮次用尽 (上限 {settings().agent_max_clarify_rounds})",
        )
        raise _TaskFinished("failed")
    async with factory() as session, session.begin():
        ctx = ToolContext(
            session=session, task_id=st.task_id,
            user_id=st.user_id, runner_id=runner_id,
        )
        # missing_slots 原样透传, 不设默认: 校验在 service 层 (枚举外/缺失都
        # 归 model_protocol_error)。question 的 or 兜底是 W4 留下的展示层容错,
        # 槽位不同 —— 它是判分依据, 编一个默认值等于替模型撒谎 (SPEC-007 第三节)
        await agent_tools.run_tool(ctx, "ask_clarification", {
            "question": question,
            "missing_slots": arguments.get("missing_slots"),
        })
    raise _TaskFinished("clarifying")


async def _fail(
    factory: async_sessionmaker[AsyncSession],
    st: _RoundState,
    runner_id: str,
    *,
    status: str,
    code: str,
    detail: str,
) -> None:
    """失败收口: 草稿标 discarded (不删, W5 要评) + 任务落终态, 同一个事务。

    报告任务的草稿在 incident_reports 里, 同一句话同一个事务标 discarded
    (SPEC-008 第四节: 修不过 -> failed + 报告 discarded); 策略任务没有报告行,
    那一步是 0 行更新。LeaseLost 时静默返回 —— 清扫可能已经先一步把任务判死了。
    """
    try:
        async with factory() as session, session.begin():
            draft = await agent_service.find_task_draft_version(session, st.task_id)
            if draft is not None:
                await policy_service.discard_version(session, draft)
            await report_task_service.discard_task_report(session, st.task_id)
            await agent_service.finish_task(
                session, st.task_id, runner_id,
                status=status, error_code=code, error_detail=detail,
            )
    except agent_service.LeaseLost:
        log.warning("task %s: 收口时租约已失 (清扫先到), 保持既有终态", st.task_id)


# ===== 后台 spawn 入口 (W4 第三段, HTTP 层唯一的拉起方式) =====
#
# asyncio.create_task 返回的对象若没有强引用, 可能在跑完之前被垃圾回收 ——
# 表现是任务随机消失、日志里什么都没有。标准做法: 存进模块级 set 留强引用,
# done_callback 里 discard (Python 官方文档对 create_task 的原话)。
# 关停时由 main.py 的 lifespan 连同 tick/maintenance 两个循环一起干净取消;
# 被取消的任务轮事务回滚、行停在 running, 下次启动后由租约清扫在
# lease_timeout 内收成 dead_letter —— 这正是 SPEC-002 第一节写的重启边界。
_BACKGROUND_TASKS: set[asyncio.Task[str]] = set()
# 已预留、还没 spawn 的槽位数。POST 处理器在"查上界"与"插库 + spawn"之间有
# await (插行必须先提交, 否则后台协程认领时看不见行), 只数 _BACKGROUND_TASKS
# 会让并发请求都从同一个旧值判断、一起挤过上界 —— 预留计数是同步操作,
# 单线程事件循环里不会被打断, 上界因此是真的上界而不是"多数时候的上界"。
_RESERVED_SLOTS = 0


class CapacityExceeded(Exception):
    """同时在跑的后台任务已到上界 (config.agent_max_concurrent_tasks) -> 429。"""


def reserve_task_slot() -> None:
    """同步预留一个后台槽位, 满了抛 CapacityExceeded。

    调用方 (路由层) 拿到预留后才去插任务行; 之后要么 spawn_task (预留转正),
    要么 release_task_slot (去重命中/插行失败时退回), 两者必居其一。
    """
    global _RESERVED_SLOTS
    if len(_BACKGROUND_TASKS) + _RESERVED_SLOTS >= settings().agent_max_concurrent_tasks:
        raise CapacityExceeded
    _RESERVED_SLOTS += 1


def release_task_slot() -> None:
    global _RESERVED_SLOTS
    _RESERVED_SLOTS = max(0, _RESERVED_SLOTS - 1)


def spawn_task(
    task_id: int,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
) -> asyncio.Task[str]:
    """把一轮 run_task 拉起为后台协程 (调用方必须已 reserve_task_slot 成功)。

    只加这个入口, 状态机本身不动: HTTP 立刻返回、SSE 从数据库尾随
    (SPEC-002 第一节)。任务行必须已提交 —— run_task 第一步的 claim 用的是
    新会话, 看不见未提交的行。
    """
    task = asyncio.create_task(
        run_task(task_id, llm, factory), name=f"agent-task-{task_id}"
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_discard_background)
    release_task_slot()
    return task


def _discard_background(task: asyncio.Task[str]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # run_task 自己会把已知失败收口成 failed/dead_letter; 走到这说明是
        # 没被归类的异常 —— 至少要在日志里看得见, 行本身留给租约清扫收尾
        log.error("agent 后台任务异常退出", exc_info=task.exception())


def background_tasks() -> tuple[asyncio.Task[str], ...]:
    """给 lifespan 关停用: 当前仍在跑的后台任务快照。"""
    return tuple(_BACKGROUND_TASKS)


def running_task_count() -> int:
    return len(_BACKGROUND_TASKS) + _RESERVED_SLOTS


# ===== 打卡与清扫后台任务 (搭在同一个循环上, 不开两个) =====


async def maintenance_loop() -> None:
    """apps/api 启动时拉起: 每 SENTINEL_AGENT_HEARTBEAT_SECONDS 秒打一次卡,
    顺手做一次清扫。**打卡和清扫刻意搭在同一个 asyncio 任务上** (SPEC-002 第二节)。

    - 打卡只打本进程 (RUNNER_ID) 名下 running 的任务;
    - 清扫: 失联判死 (heartbeat 时间差, 阈值/间隔的比例关系见 config.py 注释) +
      clarifying 超过生存期判死; 都是幂等条件更新, 多实例重复跑无害;
    - 判死任务的草稿标 discarded (不删), 预扣结算回台账 (见 sweep_once);
    - 单轮失败只记日志不退出; 关停由 lifespan cancel, 先 sleep 后干活保证取消点
      永远可达, 测试不挂住 (形状照抄 policy_runtime.tick_loop)。
    """
    from ..db import session_factory

    factory = session_factory()
    while True:
        await asyncio.sleep(settings().agent_heartbeat_seconds)
        try:
            async with factory() as session, session.begin():
                await agent_service.beat(session, RUNNER_ID)
                await sweep_once(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent 打卡/清扫循环单轮失败, 下一轮重试")


async def sweep_once(session: AsyncSession) -> list[dict[str, Any]]:
    """清扫一轮: 判死 + 草稿标弃 + **预扣结算**, 同一个事务。

    结算这一步是 SPEC-009 第二节末尾那句"任何把任务写进终态的地方都要结算
    一次"的清扫侧: 失联判死与 clarifying 超时判死都不经过任何轮次的收尾回调,
    只在轮次收尾回补的话, 被遗弃的澄清任务的预扣当天再也回不来。结算是幂等的
    (budget_service 的 hold_refunded_at 钥匙), 与轮次收尾撞上也只减一次。
    从 maintenance_loop 提出来成独立函数, 是让测试能直接调**循环真正跑的这段
    代码** —— 只写在循环体里的话, "清扫会结算"没有任何测试守得住。
    """
    reaped = await agent_service.reap(
        session,
        lease_timeout_seconds=settings().agent_lease_timeout_seconds,
        task_ttl_hours=settings().agent_task_ttl_hours,
    )
    for r in reaped:
        draft = await agent_service.find_task_draft_version(session, r["task_id"])
        if draft is not None:
            await policy_service.discard_version(session, draft)
        # 报告任务判死时草稿同样标弃 (策略任务 0 行更新), 与 _fail 同一句话
        await report_task_service.discard_task_report(session, r["task_id"])
        await budget_service.refund_task_hold(session, r["task_id"])
    return reaped
