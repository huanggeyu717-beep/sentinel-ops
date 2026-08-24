"""事故报告任务的 service 层: 建任务 / 落草稿 / 校验计数 / 定稿 / 弃稿 / 取报告
(SPEC-008 第四、五、七、八节)。

**唯一写 incident_reports 的地方。** 三个 Agent 工具 (agent_tools) 与四个 HTTP
接口 (routers/reports) 都只调这里, 不自己拼 SQL (CLAUDE.md 不变量 4)。

与策略任务共用 agent_tasks 一张表与同一套 runtime 外壳; 本模块管的是报告特有的
口径:

- **input_hash 的稳定写法** (雷区 5): 报告任务的输入不是一句话, 是一个
  incident_id —— 统一喂 ``incident_report:{incident_id}`` 进 normalize_input,
  同一条事故永远得到同一个 hash, agent_tasks_one_open 才认得出重复;
- **建任务显式写 stage='collecting'** (雷区 1): agent_tasks.stage 的数据库默认值
  是 'parsing', 不写的话报告任务会一头栽进策略状态机的 parsing 分支;
- **去重不能只靠索引** (雷区 6): agent_tasks_one_open 只挡同一个用户, 两个用户
  同时点同一条事故会开出两个任务, 第二个跑到 drafting 撞 incident_reports_one_active
  直接 failed。所以这里自己查一次"该事故有没有未走完的报告任务" (跨用户,
  按 input_hash), 命中返回既有 task_id (SPEC 第八节: 200, 不报 4xx);
- **updated_at 手工更新** (雷区 9): incident_reports.updated_at 只有 DEFAULT
  now() 没有触发器, 修复循环就地改草稿时必须自己盖戳。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent_service, report_service
from .incident_service import IncidentNotFound
from .report_render import Fact, build_fact_pack, check_draft

# 时刻与时长的格式化时区, 显式传入不读环境变量 (SPEC-008 第二节)。
# 演示门店在国内, 固定上海; 若未来多时区, 这里换成按门店配置查, 仍不进环境变量。
REPORT_TZ = "Asia/Shanghai"


class IncidentNotResolved(Exception):
    """事故非 resolved 时不建报告任务 -> 422 (SPEC-008 第八节)。"""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class ReportNotFound(Exception):
    """报告不存在 -> 404。"""


class ReportStateConflict(Exception):
    """当前状态不允许该操作 (重复定稿 / 弃已弃的 / 任务还在跑) -> 409。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def report_input_text(incident_id: int) -> str:
    """报告任务喂给 normalize_input 的稳定输入 (雷区 5, 有测试钉住):
    改这个格式等于让同一条事故的新旧任务互相认不出重复。"""
    return f"incident_report:{incident_id}"


# ===== 建任务 =====

_INCIDENT_STATUS = text("SELECT status FROM incidents WHERE id = :id")

# 跨用户去重 (雷区 6): 判据是"该事故有没有未走完的 incident_report 任务",
# 不是"有没有 draft 报告" —— 报告行是 drafting 阶段才插的, 按报告判,
# collecting 阶段连点两次会漏过去 (SPEC-008 第八节)。
# 未走完 = running / awaiting_review (报告没有 clarifying: 没有 ask_clarification)。
_FIND_OPEN_REPORT_TASK = text("""
    SELECT id, status, stage,
           heartbeat_at IS NULL
               OR heartbeat_at < now() - make_interval(secs => :lease_timeout)
               AS heartbeat_stale
    FROM agent_tasks
    WHERE task_type = 'incident_report' AND input_hash = :input_hash
      AND status IN ('running', 'awaiting_review')
    ORDER BY id DESC LIMIT 1
""")

# 雷区 1: stage 的数据库默认值是 'parsing' (策略状态机的入口), 报告任务必须
# 显式改成 'collecting', 否则 run_task 会把它当策略任务跑。
_SET_COLLECTING = text(
    "UPDATE agent_tasks SET stage = 'collecting' WHERE id = :id"
)


async def create_report_task(
    session: AsyncSession,
    *,
    user_id: int,
    incident_id: int,
    lease_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """开一条报告任务。返回形状与 agent_service.create_task 一致
    (created / task_id / status / stage / suspected_interrupted)。

    - 事故不存在 -> IncidentNotFound (404);
    - 事故非 resolved -> IncidentNotResolved (422): 事故还在跑, 事实包会变,
      报告是定影不是直播 (SPEC-008 非目标);
    - 已有未走完的报告任务 (**任何人开的**) -> created=False + 那条 task_id。
    """
    status = (
        await session.execute(_INCIDENT_STATUS, {"id": incident_id})
    ).scalar_one_or_none()
    if status is None:
        raise IncidentNotFound
    if status != "resolved":
        raise IncidentNotResolved(str(status))

    input_text = report_input_text(incident_id)
    _, input_hash = agent_service.normalize_input(input_text, None)
    existing = (
        await session.execute(
            _FIND_OPEN_REPORT_TASK,
            {"input_hash": input_hash, "lease_timeout": lease_timeout_seconds},
        )
    ).mappings().one_or_none()
    if existing is not None:
        return {
            "task_id": existing["id"],
            "created": False,
            "status": existing["status"],
            "stage": existing["stage"],
            "suspected_interrupted": bool(
                existing["status"] == "running" and existing["heartbeat_stale"]
            ),
        }

    created = await agent_service.create_task(
        session,
        user_id=user_id,
        input_text=input_text,
        task_type="incident_report",
        lease_timeout_seconds=lease_timeout_seconds,
    )
    if created["created"]:
        await session.execute(_SET_COLLECTING, {"id": created["task_id"]})
        created["stage"] = "collecting"
        created["status"] = "running"
    return created


# ===== 事实包 (工具 get_incident_facts 的实现) =====


def _fact_dict(fact: Fact) -> dict[str, Any]:
    return {"id": fact.id, "label": fact.label, "value": fact.value, "text": fact.text}


def _facts_from_snapshot(snapshot: list[dict[str, Any]]) -> list[Fact]:
    return [Fact(**f) for f in snapshot]


async def load_fact_pack(
    session: AsyncSession, incident_id: int
) -> list[dict[str, Any]]:
    """读库 + 纯函数格式化, 产出 JSON 可存的事实表 (同一份既给模型看,
    也在 create_report_draft 时原样存进 fact_pack 快照)。"""
    raw = await report_service.load_incident_facts(session, incident_id)
    return [_fact_dict(f) for f in build_fact_pack(raw, tz=REPORT_TZ)]


# ===== 草稿 (工具 create_report_draft / update_report_draft 的实现) =====

_INSERT_REPORT = text("""
    INSERT INTO incident_reports (incident_id, task_id, body, fact_pack, created_by)
    VALUES (:incident_id, :task_id, CAST(:body AS jsonb),
            CAST(:fact_pack AS jsonb), :created_by)
    RETURNING id
""")

_GET_DRAFT_BY_TASK = text("""
    SELECT id, body, fact_pack FROM incident_reports
    WHERE task_id = :task_id AND status = 'draft'
""")

# 雷区 9: updated_at 没有触发器, 就地改草稿必须自己盖戳
_UPDATE_DRAFT = text("""
    UPDATE incident_reports
       SET body = CAST(:body AS jsonb), updated_at = now()
     WHERE task_id = :task_id AND status = 'draft'
    RETURNING id
""")

_BUMP_COUNTS = text("""
    UPDATE incident_reports
       SET bare_fact_attempts = bare_fact_attempts + :bare,
           dangling_ref_attempts = dangling_ref_attempts + :dangling,
           updated_at = now()
     WHERE id = :id
""")


async def create_report_draft(
    session: AsyncSession,
    *,
    task_id: int,
    incident_id: int,
    body: dict[str, Any],
    fact_pack: list[dict[str, Any]],
    created_by: int,
) -> dict[str, Any]:
    """落草稿一行。fact_pack 存**生成那一刻的快照**, 以后校验与渲染都读它,
    不重算 (SPEC-008 第七节第 2 条: 事实不会变, 算事实包的代码会变)。"""
    report_id = (
        await session.execute(_INSERT_REPORT, {
            "incident_id": incident_id, "task_id": task_id,
            "body": json.dumps(body, ensure_ascii=False),
            "fact_pack": json.dumps(fact_pack, ensure_ascii=False),
            "created_by": created_by,
        })
    ).scalar_one()
    return {"report_id": int(report_id)}


async def update_report_draft(
    session: AsyncSession, *, task_id: int, body: dict[str, Any]
) -> dict[str, Any]:
    """修复循环就地改本任务那一版草稿。previous_body 随返回值进 agent_steps ——
    修复前的中间态只存在时间线里, 与 update_policy_draft 同一条理由。"""
    row = (
        await session.execute(_GET_DRAFT_BY_TASK, {"task_id": task_id})
    ).mappings().one_or_none()
    if row is None:
        raise ReportNotFound
    await session.execute(_UPDATE_DRAFT, {
        "task_id": task_id, "body": json.dumps(body, ensure_ascii=False),
    })
    return {"report_id": row["id"], "previous_body": _as_json(row["body"])}


async def validate_task_report(
    session: AsyncSession, *, task_id: int
) -> dict[str, Any]:
    """对本任务的草稿跑两道硬拦 + 上限检查, **对着 fact_pack 快照校验**。

    两个倾向计数按违规项累加进 incident_reports 的两列 (SPEC-008 第三节:
    一轮里裸写三处就加 3; 超长与形状错不计入 —— 那是格式问题不是编造倾向)。
    """
    row = (
        await session.execute(_GET_DRAFT_BY_TASK, {"task_id": task_id})
    ).mappings().one_or_none()
    if row is None:
        raise ReportNotFound
    facts = _facts_from_snapshot(_as_json(row["fact_pack"]))
    result = check_draft(_as_json(row["body"]), facts)
    if result.bare_fact_attempts or result.dangling_ref_attempts:
        await session.execute(_BUMP_COUNTS, {
            "id": row["id"],
            "bare": result.bare_fact_attempts,
            "dangling": result.dangling_ref_attempts,
        })
    return {
        "ok": result.ok,
        "violations": [
            {"code": v.code, "field": v.field, "detail": v.detail}
            for v in result.violations
        ],
        "bare_fact_attempts": result.bare_fact_attempts,
        "dangling_ref_attempts": result.dangling_ref_attempts,
    }


# ===== 读报告 (两个 GET 接口) =====

_GET_REPORT = text("""
    SELECT id, incident_id, task_id, body, fact_pack, status,
           bare_fact_attempts, dangling_ref_attempts,
           created_by, created_at, updated_at, finalized_by, finalized_at
    FROM incident_reports WHERE id = :id
""")

_GET_ACTIVE_BY_INCIDENT = text("""
    SELECT id FROM incident_reports
    WHERE incident_id = :incident_id AND status <> 'discarded'
""")


def _as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def get_report(session: AsyncSession, report_id: int) -> dict[str, Any]:
    """报告 + 渲染后的正文 + 事实包快照。渲染**只读快照**不重算; 草稿还没过
    校验时 (修复中途) rendered 为 None, 不硬渲染一个带违规的东西出去。"""
    row = (
        await session.execute(_GET_REPORT, {"id": report_id})
    ).mappings().one_or_none()
    if row is None:
        raise ReportNotFound
    body = _as_json(row["body"])
    fact_pack = _as_json(row["fact_pack"])
    result = check_draft(body, _facts_from_snapshot(fact_pack))
    return {
        **dict(row),
        "body": body,
        "fact_pack": fact_pack,
        "rendered": result.rendered,
    }


async def get_incident_report(
    session: AsyncSession, incident_id: int
) -> dict[str, Any]:
    """取该事故当前那一份 (非 discarded 的最多一份, 由 partial unique index 保证)。"""
    report_id = (
        await session.execute(_GET_ACTIVE_BY_INCIDENT, {"incident_id": incident_id})
    ).scalar_one_or_none()
    if report_id is None:
        raise ReportNotFound
    return await get_report(session, int(report_id))


# ===== 定稿 / 弃稿 (人的动作) 与失败路径的弃稿 =====

_GET_REPORT_STATE = text(
    "SELECT id, incident_id, task_id, status FROM incident_reports WHERE id = :id"
)

_TASK_STATUS = text("SELECT status FROM agent_tasks WHERE id = :id")

_FINALIZE = text("""
    UPDATE incident_reports
       SET status = 'final', finalized_by = :user_id, finalized_at = now(),
           updated_at = now()
     WHERE id = :id AND status = 'draft'
    RETURNING id
""")

_DISCARD = text("""
    UPDATE incident_reports
       SET status = 'discarded', updated_at = now()
     WHERE id = :id AND status <> 'discarded'
    RETURNING id
""")

_DISCARD_BY_TASK = text("""
    UPDATE incident_reports
       SET status = 'discarded', updated_at = now()
     WHERE task_id = :task_id AND status = 'draft'
    RETURNING id
""")

# awaiting_review -> completed, 形状照 agent_service._COMPLETE (审批那条):
# 无 runner 闸 —— 等人过目的任务没有主人, 条件更新本身就是并发保护。
# 两个终态分开 (SPEC-008 第四节): 人定稿与人退回都落 completed (任务本身干完了),
# decision 记进时间线区分; failed 专指"模型没写对", 只出现在修满不过的失败路径。
_COMPLETE_REVIEW = text("""
    UPDATE agent_tasks
       SET status = 'completed', stage = 'completed',
           completed_at = now(), next_seq = next_seq + 1
     WHERE id = :id AND status = 'awaiting_review'
    RETURNING next_seq
""")

_INSERT_REVIEW_STEP = text("""
    INSERT INTO agent_steps (task_id, seq, tool_name, arguments, status)
    VALUES (:task_id, :seq, 'stage_transition', CAST(:arguments AS jsonb), 'ok')
""")

_APPEND_AUDIT = text("""
    INSERT INTO audit_log (user_id, action, entity, entity_id, detail)
    VALUES (:user_id, :action, 'incident_report', :entity_id, CAST(:detail AS jsonb))
""")


async def _complete_review_task(
    session: AsyncSession, task_id: int, decision: str
) -> bool:
    seq = (
        await session.execute(_COMPLETE_REVIEW, {"id": task_id})
    ).scalar_one_or_none()
    if seq is None:
        return False
    await session.execute(_INSERT_REVIEW_STEP, {
        "task_id": task_id, "seq": int(seq),
        "arguments": json.dumps({"to": "completed", "decision": decision},
                                ensure_ascii=False),
    })
    return True


async def finalize_report(
    session: AsyncSession, *, report_id: int, user_id: int
) -> dict[str, Any]:
    """人定稿: 报告 draft -> final, 任务 awaiting_review -> completed。

    只有等人过目的草稿能定稿 —— 任务还在跑 (修复中途) 时定稿, 等于把一个
    还会被模型改写的东西钉成终稿, 一律 409。
    """
    row = (
        await session.execute(_GET_REPORT_STATE, {"id": report_id})
    ).mappings().one_or_none()
    if row is None:
        raise ReportNotFound
    if row["status"] != "draft":
        raise ReportStateConflict(f"报告当前状态 {row['status']}, 只有 draft 能定稿")
    completed = await _complete_review_task(session, row["task_id"], "finalized")
    if not completed:
        task_status = (
            await session.execute(_TASK_STATUS, {"id": row["task_id"]})
        ).scalar_one_or_none()
        raise ReportStateConflict(
            f"任务当前状态 {task_status}, 不在等待过目, 不能定稿"
        )
    await session.execute(_FINALIZE, {"id": report_id, "user_id": user_id})
    return {"report_id": report_id, "status": "final"}


async def discard_report(
    session: AsyncSession, *, report_id: int, user_id: int
) -> dict[str, Any]:
    """人弃稿: draft 或 final 都允许 (SPEC-008 第八节 —— final 也占部分唯一索引
    的位, 不许弃已定稿的等于报告定稿后永远开不出第二份), 一律进 audit_log。

    弃的是等人过目的草稿时, 任务落 completed (**人退回不是 failed**,
    SPEC-008 第四节: failed 专指模型没写对)。任务还在跑时不许弃 —— 模型下一步
    就地改草稿会扑空, 变成一次假的系统故障。
    """
    row = (
        await session.execute(_GET_REPORT_STATE, {"id": report_id})
    ).mappings().one_or_none()
    if row is None:
        raise ReportNotFound
    if row["status"] == "discarded":
        raise ReportStateConflict("报告已是 discarded")
    task_status = (
        await session.execute(_TASK_STATUS, {"id": row["task_id"]})
    ).scalar_one_or_none()
    if row["status"] == "draft" and task_status == "running":
        raise ReportStateConflict("任务还在跑, 等它停在等待过目后再退回")
    await session.execute(_DISCARD, {"id": report_id})
    if row["status"] == "draft":
        await _complete_review_task(session, row["task_id"], "returned")
    await session.execute(_APPEND_AUDIT, {
        # audit_log.entity_id 是 text 列 (各类实体混用一列), 显式转字符串
        "user_id": user_id, "action": "report_discard", "entity_id": str(report_id),
        "detail": json.dumps({
            "incident_id": row["incident_id"], "task_id": row["task_id"],
            "previous_status": row["status"],
        }, ensure_ascii=False),
    })
    return {"report_id": report_id, "status": "discarded"}


async def discard_task_report(session: AsyncSession, task_id: int) -> None:
    """失败路径的弃稿 (agent_runtime._fail 与清扫调): 修满不过 / 死信的任务,
    它的草稿标 discarded。策略任务没有报告行, 0 行更新即无事发生。"""
    await session.execute(_DISCARD_BY_TASK, {"task_id": task_id})
