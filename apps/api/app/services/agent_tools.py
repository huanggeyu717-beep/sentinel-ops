"""Agent 的 13 个工具: 封装与注册表 (SPEC-002 第五节)。

规矩三条:
1. 工具一律**调 service 函数, 不自己拼 SQL** (CLAUDE.md 不变量 4)。缺的只读能力
   在 inventory_service 里补, 不在这里开后门;
2. **publish_policy 不在清单里** —— 发布是人在 Studio 里点的动作。少一个工具,
   就少一处需要论证"为什么它不会乱用";
3. 注册表与分级表的键集合必须相等, 由测试的相等断言守着 (手法与 SPEC-001
   "审批分级表 == 动作白名单"一致, 本项目第五处这么干)。

按状态裁剪工具 (TOOLS_BY_STAGE) **不是安全措施**, 只是减少模型做无用功 ——
模型的输出经过的是同一批 service, 数据库/service/路由三层照样各判各的
(SPEC-002 第五节那张四层表)。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from policy_engine import policy_json_schema

from ..config import settings
from . import agent_service, employee_service, inventory_service, policy_service
from .drill_service import SCENARIOS_DIR


class InvalidToolArguments(Exception):
    """模型给的参数在工具层就不合法 (缺字段、名字超长、未知数据源)。"""


class ToolTimeout(Exception):
    """单工具超时 (可重试)。超时用 asyncio.wait_for: 协程被取消时, 调用方的
    事务上下文随异常回滚 —— 半截状态不落库。"""


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的环境。user_id 是任务发起人: 草案的 created_by 记他,
    没有"agent 系统账号"这种东西 (SPEC-002 定稿决定 1)。"""

    session: AsyncSession
    task_id: int
    user_id: int
    runner_id: str


ToolFn = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


def _require(args: dict[str, Any], key: str) -> Any:
    if key not in args or args[key] is None:
        raise InvalidToolArguments(f"缺少参数 {key}")
    return args[key]


# ===== 只读 (6) =====


async def _list_zones(ctx: ToolContext, args: dict[str, Any]) -> Any:
    # 必须返回区名: 模型要把"生鲜区"映射成 zone_id
    return {"zones": await inventory_service.list_zones(ctx.session)}


async def _list_sensors(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"sensors": await inventory_service.list_sensors(ctx.session)}


async def _list_roles(ctx: ToolContext, args: dict[str, Any]) -> Any:
    # 取值域是 user_roles, 与静态验证器同源 —— 给 roles 表全集会让模型挑到
    # 没账号的角色, 白烧一次修复配额 (SPEC-002 第五节)
    return {"roles": await inventory_service.list_roles_present(ctx.session)}


async def _list_employees(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"employees": await employee_service.list_employees(ctx.session)}


async def _get_policy(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await policy_service.get_policy(ctx.session, int(_require(args, "policy_id")))


async def _get_available_actions(ctx: ToolContext, args: dict[str, Any]) -> Any:
    # 与 policy_json_schema() 同一个来源, 不另生成一份 —— 两份 Schema 一定走散,
    # 而走散的那份从外面看不出来 (SPEC-002 第五节, W1 教训)
    return {"policy_schema": policy_json_schema()}


# ===== 草案 (3) =====


def _clean_name(raw: Any) -> str:
    """名字由模型起, 服务端校验: 去首尾空白后 1-60 字符 (SPEC-002 第五节)。"""
    name = str(raw).strip()
    if not 1 <= len(name) <= 60:
        raise InvalidToolArguments(f"策略名字须为去空白后 1-60 字符, 实得 {len(name)}")
    return name


async def _create_policy(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await policy_service.create_policy(
        ctx.session,
        name=_clean_name(_require(args, "name")),
        body=_require(args, "body"),
        created_by=ctx.user_id,
        source="agent",
    )


async def _add_policy_version(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await policy_service.add_version(
        ctx.session,
        policy_id=int(_require(args, "policy_id")),
        body=_require(args, "body"),
        created_by=ctx.user_id,
        source="agent",
    )


async def _update_policy_draft(ctx: ToolContext, args: dict[str, Any]) -> Any:
    # 返回值里带 previous_body: 修复前的完整 body 随本步落进 agent_steps,
    # 中间态一条不丢 —— W5 评测"它第一次写错成什么样"全靠这个 (SPEC-002 第六节)
    return await policy_service.update_draft_body(
        ctx.session,
        version_id=int(_require(args, "version_id")),
        body=_require(args, "body"),
        actor_user_id=ctx.user_id,
    )


# ===== 模拟 (2) =====

_HISTORY_CSV = "apps/device-sim/seed/waterlevel_readings.csv"


def simulation_sources() -> list[str]:
    """simulate_policy 的合法数据源枚举: 场景名 + history_csv (默认)。

    只暴露枚举不暴露路径 —— policy_service._resolve_source 接受仓库内任意相对
    路径, 让模型填路径字符串是没必要的自由度 (SPEC-002 第五节)。
    """
    return [p.stem for p in sorted(SCENARIOS_DIR.glob("*.yaml"))] + ["history_csv"]


async def _validate_policy(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await policy_service.validate_version(
        ctx.session, int(_require(args, "version_id"))
    )


async def _simulate_policy(ctx: ToolContext, args: dict[str, Any]) -> Any:
    source = str(args.get("source") or "history_csv")
    if source not in simulation_sources():
        raise InvalidToolArguments(
            f"未知数据源 {source!r}, 只能选: {simulation_sources()}"
        )
    resolved = _HISTORY_CSV if source == "history_csv" else source
    return await policy_service.simulate_version(
        ctx.session, int(_require(args, "version_id")), resolved
    )


# ===== 写 (1) =====


async def _request_approval(ctx: ToolContext, args: dict[str, Any]) -> Any:
    # task_id 挂在审批上, decide_approval 据此把任务回写成 completed
    return await policy_service.request_approval(
        ctx.session,
        version_id=int(_require(args, "version_id")),
        requested_by=ctx.user_id,
        task_id=ctx.task_id,
    )


# ===== 终止 (1) =====


async def _ask_clarification(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await agent_service.ask_clarification(
        ctx.session, ctx.task_id, ctx.runner_id, str(_require(args, "question"))
    )


# ===== 注册表 =====


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str  # read / draft / simulate / write / terminal (SPEC-002 第五节的分类)
    fn: ToolFn
    description: str


REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("list_zones", "read", _list_zones, "区列表 (带区名)"),
        ToolSpec("list_sensors", "read", _list_sensors,
                 "传感器列表 (带 zone 归属与 never_reported)"),
        ToolSpec("list_roles", "read", _list_roles, "在册角色 (与验证器同源 user_roles)"),
        ToolSpec("list_employees", "read", _list_employees, "员工名录"),
        ToolSpec("get_policy", "read", _get_policy, "读现有策略与版本"),
        ToolSpec("get_available_actions", "read", _get_available_actions,
                 "Policy 的 JSON Schema (与引擎同源)"),
        ToolSpec("create_policy", "draft", _create_policy, "新建策略 + 第一版草稿 (要起名字)"),
        ToolSpec("add_policy_version", "draft", _add_policy_version,
                 "给已有策略新增一版草稿"),
        ToolSpec("update_policy_draft", "draft", _update_policy_draft,
                 "修复循环用: 就地改本任务这一版的 body"),
        ToolSpec("validate_policy", "simulate", _validate_policy, "静态校验, 返回结构化错误码"),
        ToolSpec("simulate_policy", "simulate", _simulate_policy,
                 "历史回放, 数据源只暴露枚举"),
        ToolSpec("request_approval", "write", _request_approval,
                 "建 approvals 记录, 版本转 awaiting_approval"),
        ToolSpec("ask_clarification", "terminal", _ask_clarification,
                 "把问题抛回给人, 任务转 clarifying"),
    )
}

# 按状态裁剪 (不是安全措施, 见模块 docstring): compiling 只给建草稿的两个,
# repairing 只给就地改; 有草稿之后的再编译 (澄清回来那一轮) 也只给就地改 ——
# 每个任务只新建一版草稿 (SPEC-002 第六节), 由 agent_runtime 按 draft 有无选档。
_READ_TOOLS = ("list_zones", "list_sensors", "list_roles", "list_employees",
               "get_policy", "get_available_actions")
TOOLS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "parsing": ("ask_clarification",),
    "discovering": _READ_TOOLS,
    "compiling": ("create_policy", "add_policy_version", "ask_clarification"),
    "compiling_with_draft": ("update_policy_draft", "ask_clarification"),
    "repairing": ("update_policy_draft", "ask_clarification"),
}


def tool_schemas(names: tuple[str, ...]) -> list[dict[str, Any]]:
    """给 LLMRequest.tools 用的极简工具描述 (第二段换成完整 JSON Schema)。"""
    return [
        {"name": n, "description": REGISTRY[n].description} for n in names
    ]


async def run_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> Any:
    """执行一个工具, 单工具超时 (SENTINEL_AGENT_TOOL_TIMEOUT_SECONDS)。

    超时抛 ToolTimeout (可重试); wait_for 的取消随调用方事务回滚, 不留半截状态。
    """
    if name not in REGISTRY:
        raise InvalidToolArguments(f"未知工具 {name!r}")
    try:
        return await asyncio.wait_for(
            REGISTRY[name].fn(ctx, arguments),
            timeout=settings().agent_tool_timeout_seconds,
        )
    except TimeoutError as e:
        raise ToolTimeout(name) from e
