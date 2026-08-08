"""策略生命周期 —— SPEC-006 的版本、审批、发布与撤销。

唯一能碰策略相关表的层 (CLAUDE.md 不变量 4): W4 的 HTTP 路由、Agent tools 与
W6 的 MCP server 都复用这一份, 不另写一套。

设计要点 (面试可讲):
1. **发布 = 往 policy_publications 插一行, approval_id 是 NOT NULL 外键** (ADR-007):
   没有审批记录这一行物理上插不进去, 与应用代码写成什么样无关。本模块的检查只是
   为了给出可读的错误信息, 真正的不变量在数据库里;
2. **版本不可变**: 改一个字就是新建一版, "当时批的到底是什么"永远查得到;
3. **推进一律条件更新** UPDATE ... WHERE status = 期望的旧状态, 0 行即 409
   (沿用 SPEC-003 的做法);
4. **禁止自己批自己做两层**: 数据库 CHECK 兜底 (approvals_no_self_approve),
   这里先返回 403 并把否决写进审计 —— 审计走独立事务, 否则 403 引发的回滚会把
   留痕一起冲掉;
5. **published 与"生效"是两件事**: 批准让版本进 published, 线上跑不跑由
   policy_publications 决定; 回滚 = 撤销当前发布 + 重新发布旧版本,
   旧版本复用它当年那条审批记录, 不需要重新审批;
6. **模拟与线上是同一份 evaluate()** (不变量 2): simulate 调 packages/policy_engine
   的 replay, tick 间隔取线上同一配置; 回放只出警告不出拒绝 (SPEC-001 第六节),
   "跑完即转 simulated", 不存在"没通过"。发布前的跨策略检查 (动作互斥、自触发环)
   则是**拒绝**: 跨策略冲突是当下就能确定的事实, 确定的事实可以拦, 推测只能提示。

RBAC 分工: manager 门槛的动作 (decide/publish/revoke) 在本层校验 —— Agent tools
不经过 HTTP, 权限必须落在 service; operator+ 的读写门槛沿用既有惯例由路由层把守。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from scenario import load_source
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from policy_engine import Inventory, LoadedPolicy, Policy, ReplayReport, replay, validate

from ..config import settings
from ..db import session_factory
from . import drill_service, policy_runtime
from .auth_service import PERM_APPROVE_POLICY, AuthUser, has_permission

# ===== 异常 (路由层在 W4 映射成 HTTP 状态码) =====


class PolicyNotFound(Exception):
    """策略不存在 -> 404。"""


class PolicyVersionNotFound(Exception):
    """版本不存在 -> 404。"""


class ApprovalNotFound(Exception):
    """审批记录不存在 -> 404。"""


class SourceNotFound(Exception):
    """模拟数据源不存在或不合法 -> 404。"""


class InvalidPolicyBody(Exception):
    """body 连 Schema 层都过不了 -> 422。语义问题不走这里, 走 validate 的 issues。"""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(issues)
        self.issues = issues


class TransitionConflict(Exception):
    """当前状态不允许该流转 -> 409。"""

    def __init__(self, current_status: str) -> None:
        super().__init__(current_status)
        self.current_status = current_status


class PermissionDenied(Exception):
    """RBAC: 没有 policies:approve 权限 -> 403。与自批被拒不是同一件事 (验收 6/11)。"""


class SelfApprovalDenied(Exception):
    """提交人自己批自己 -> 403 且审计留痕。数据库 CHECK 是兜底的第二层 (ADR-007)。"""


class ApprovalAlreadyDecided(Exception):
    """审批已有结论 -> 409。"""


class NotApproved(Exception):
    """发布前置不满足: 版本未走到 published (即没有通过的审批) -> 409。"""

    def __init__(self, current_status: str) -> None:
        super().__init__(current_status)
        self.current_status = current_status


class AlreadyPublished(Exception):
    """该策略已有生效版本, 先撤销再发布 -> 409 (数据库由 partial unique index 兜底)。"""


class NothingToRevoke(Exception):
    """没有生效中的发布可撤销 -> 409。"""


class PublishRejected(Exception):
    """发布前跨策略检查不通过 -> 422, conflicts 指出与哪条策略冲突、为什么。"""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(conflicts)
        self.conflicts = conflicts


# ===== SQL =====

_INSERT_POLICY = text("""
    INSERT INTO policies (name, created_by) VALUES (:name, :created_by) RETURNING id
""")

_INSERT_VERSION = text("""
    INSERT INTO policy_versions (policy_id, version, body, status)
    VALUES (:policy_id,
            (SELECT COALESCE(max(version), 0) + 1 FROM policy_versions
             WHERE policy_id = :policy_id),
            CAST(:body AS jsonb), 'draft')
    RETURNING id, version
""")

_GET_VERSION = text("""
    SELECT pv.id, pv.policy_id, pv.version, pv.body, pv.status, pv.created_at, p.name
    FROM policy_versions pv JOIN policies p ON p.id = pv.policy_id
    WHERE pv.id = :id
""")

_ADVANCE = text("""
    UPDATE policy_versions SET status = :new WHERE id = :id AND status = :old RETURNING id
""")

_LIST_POLICIES = text("""
    SELECT p.id, p.name, p.created_by, p.created_at,
           pp.id AS publication_id, pv.id AS active_version_id, pv.version AS active_version,
           (SELECT max(version) FROM policy_versions WHERE policy_id = p.id) AS latest_version
    FROM policies p
    LEFT JOIN policy_publications pp ON pp.policy_id = p.id AND pp.revoked_at IS NULL
    LEFT JOIN policy_versions pv ON pv.id = pp.policy_version_id
    ORDER BY p.id
""")

_GET_POLICY = text("SELECT id, name, created_by, created_at FROM policies WHERE id = :id")

_POLICY_VERSIONS = text("""
    SELECT id, version, status, created_at FROM policy_versions
    WHERE policy_id = :policy_id ORDER BY version
""")

_ACTIVE_PUBLICATION = text("""
    SELECT pp.id, pp.policy_version_id, pv.version, pp.approval_id,
           pp.published_by, pp.published_at
    FROM policy_publications pp JOIN policy_versions pv ON pv.id = pp.policy_version_id
    WHERE pp.policy_id = :policy_id AND pp.revoked_at IS NULL
""")

_INSERT_APPROVAL = text("""
    INSERT INTO approvals (task_id, policy_version_id, requested_by)
    VALUES (NULL, :policy_version_id, :requested_by)
    RETURNING id
""")

_GET_APPROVAL = text("""
    SELECT id, policy_version_id, requested_by, decided_by, decision, note
    FROM approvals WHERE id = :id
""")

_DECIDE_APPROVAL = text("""
    UPDATE approvals
    SET decision = :decision, decided_by = :decided_by, decided_at = now(), note = :note
    WHERE id = :id AND decision IS NULL
    RETURNING id
""")

_APPROVED_FOR_VERSION = text("""
    SELECT id FROM approvals
    WHERE policy_version_id = :policy_version_id AND decision = 'approved'
    ORDER BY decided_at DESC, id DESC
    LIMIT 1
""")

_INSERT_PUBLICATION = text("""
    INSERT INTO policy_publications
        (policy_id, policy_version_id, approval_id, published_by)
    VALUES (:policy_id, :policy_version_id, :approval_id, :published_by)
    RETURNING id
""")

_REVOKE = text("""
    UPDATE policy_publications
    SET revoked_at = now(), revoked_by = :revoked_by
    WHERE policy_id = :policy_id AND revoked_at IS NULL
    RETURNING id, policy_version_id
""")

_ACTIVE_SET = text("""
    SELECT pp.policy_id, p.name, pv.version, pv.body
    FROM policy_publications pp
    JOIN policies p ON p.id = pp.policy_id
    JOIN policy_versions pv ON pv.id = pp.policy_version_id
    WHERE pp.revoked_at IS NULL
""")

_ZONES = text("SELECT id FROM zones")
_SENSORS = text("SELECT id, zone_id FROM sensors")
_ROLES_PRESENT = text("""
    SELECT DISTINCT r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id
""")

_APPEND_AUDIT = text("""
    INSERT INTO audit_log (user_id, action, entity, entity_id, detail)
    VALUES (:user_id, :action, :entity, :entity_id, CAST(:detail AS jsonb))
""")


async def _audit(
    session: AsyncSession, action: str, entity: str, entity_id: int,
    user_id: int | None, detail: dict[str, Any] | None = None,
) -> None:
    await session.execute(_APPEND_AUDIT, {
        "user_id": user_id, "action": action, "entity": entity,
        "entity_id": str(entity_id),
        "detail": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
    })


# ===== 查询 =====


async def list_policies(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_LIST_POLICIES)).mappings().all()
    return [dict(r) for r in rows]


async def get_policy(session: AsyncSession, policy_id: int) -> dict[str, Any]:
    row = (await session.execute(_GET_POLICY, {"id": policy_id})).mappings().one_or_none()
    if row is None:
        raise PolicyNotFound
    versions = (
        await session.execute(_POLICY_VERSIONS, {"policy_id": policy_id})
    ).mappings().all()
    publication = (
        await session.execute(_ACTIVE_PUBLICATION, {"policy_id": policy_id})
    ).mappings().one_or_none()
    return {
        "policy": dict(row),
        "versions": [dict(v) for v in versions],
        "publication": dict(publication) if publication is not None else None,
    }


async def get_version(session: AsyncSession, version_id: int) -> dict[str, Any]:
    row = (await session.execute(_GET_VERSION, {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise PolicyVersionNotFound
    out = dict(row)
    out["body"] = _body_dict(out["body"])
    return out


# ===== 草稿与版本 =====


def _body_dict(body: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = body if isinstance(body, dict) else json.loads(body)
    return parsed


def _parse_body(body: dict[str, Any]) -> Policy:
    """Schema 层校验 (Pydantic 白名单)。过不了连草稿都不建 —— 白名单以外的话
    在语法层面就说不出来 (SPEC-001 第二节)。"""
    try:
        return Policy.model_validate(body)
    except ValidationError as e:
        raise InvalidPolicyBody([
            {
                "code": "E_SCHEMA",
                "path": ".".join(str(p) for p in err["loc"]),
                "message": err["msg"],
            }
            for err in e.errors()
        ]) from e


async def create_policy(
    session: AsyncSession, name: str, body: dict[str, Any], created_by: int
) -> dict[str, Any]:
    """新建策略 + 第一版草稿。"""
    _parse_body(body)
    policy_id: int = (
        await session.execute(_INSERT_POLICY, {"name": name, "created_by": created_by})
    ).scalar_one()
    version = (
        await session.execute(
            _INSERT_VERSION,
            {"policy_id": policy_id, "body": json.dumps(body, ensure_ascii=False)},
        )
    ).mappings().one()
    await _audit(session, "policy.create", "policy", policy_id, created_by, {"name": name})
    return {"policy_id": policy_id, "version_id": version["id"], "version": version["version"]}


async def add_version(
    session: AsyncSession, policy_id: int, body: dict[str, Any], created_by: int
) -> dict[str, Any]:
    """新增草稿版本。改草稿即新建一版, 已有版本永不原地修改。"""
    _parse_body(body)
    if (
        await session.execute(_GET_POLICY, {"id": policy_id})
    ).mappings().one_or_none() is None:
        raise PolicyNotFound
    version = (
        await session.execute(
            _INSERT_VERSION,
            {"policy_id": policy_id, "body": json.dumps(body, ensure_ascii=False)},
        )
    ).mappings().one()
    await _audit(session, "policy.add_version", "policy", policy_id, created_by,
                 {"version": version["version"]})
    return {"policy_id": policy_id, "version_id": version["id"], "version": version["version"]}


async def _advance_status(
    session: AsyncSession, version_id: int, old: str, new: str
) -> None:
    moved = (
        await session.execute(_ADVANCE, {"id": version_id, "old": old, "new": new})
    ).scalar_one_or_none()
    if moved is None:
        row = (
            await session.execute(_GET_VERSION, {"id": version_id})
        ).mappings().one_or_none()
        if row is None:
            raise PolicyVersionNotFound
        raise TransitionConflict(row["status"])


# ===== 校验与模拟 =====


async def _inventory(session: AsyncSession) -> Inventory:
    """验证所需的资源快照 (引擎零 IO, 库存由本层提供)。
    roles_present 的事实源定死为 user_roles, 不是 employees.role (SPEC-001 第五节)。"""
    zones = (await session.execute(_ZONES)).scalars().all()
    sensors = (await session.execute(_SENSORS)).mappings().all()
    roles = (await session.execute(_ROLES_PRESENT)).scalars().all()
    return Inventory(
        zone_ids=frozenset(int(z) for z in zones),
        sensor_ids=frozenset(int(s["id"]) for s in sensors),
        sensor_zone={
            int(s["id"]): int(s["zone_id"]) for s in sensors if s["zone_id"] is not None
        },
        roles_present=frozenset(str(r) for r in roles),
    )


async def validate_version(session: AsyncSession, version_id: int) -> dict[str, Any]:
    """静态校验。通过则 draft -> validated; 有 issue 不转移, 原样返回错误码与 hint。"""
    row = (await session.execute(_GET_VERSION, {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise PolicyVersionNotFound
    policy = _parse_body(_body_dict(row["body"]))
    result = validate(policy, await _inventory(session))
    if not result.ok:
        return {"ok": False, "status": row["status"], "issues": [asdict(i) for i in result.issues]}
    await _advance_status(session, version_id, "draft", "validated")
    return {"ok": True, "status": "validated", "issues": []}


# 场景名只允许字母数字下划线连字符 (与 drill_service 同一判断: 挡路径穿越)
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_source(source: str) -> Path:
    """数据源: 场景名 -> scenarios/{name}.yaml; 其余当仓库内相对路径 (支持 CSV 历史回放),
    必须落在仓库根之下且是 .yaml/.yml/.csv —— W4 会把这个参数直接暴露给 HTTP。"""
    if _SOURCE_NAME_RE.fullmatch(source):
        path = drill_service.SCENARIOS_DIR / f"{source}.yaml"
        if path.is_file():
            return path
        raise SourceNotFound(source)
    root = drill_service.SCENARIOS_DIR.parent.resolve()
    path = (root / source).resolve()
    if (
        not path.is_relative_to(root)
        or path.suffix.lower() not in {".yaml", ".yml", ".csv"}
        or not path.is_file()
    ):
        raise SourceNotFound(source)
    return path


async def _backfill_zone_ids(
    session: AsyncSession, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """事件规范化时从 sensors 表补 zone_id。

    CSV 历史数据没有 zone_id 列, 不补的话开事故类策略在真实数据上产出恒为 0
    (SPEC-001 验收 8 的已知限制); zone 的唯一事实源本来就是数据库, 这里收口。
    """
    sensors = (await session.execute(_SENSORS)).mappings().all()
    zone_of = {int(s["id"]): int(s["zone_id"]) for s in sensors if s["zone_id"] is not None}
    out: list[dict[str, Any]] = []
    for ev in events:
        if (
            ev.get("kind") == "sensor_state"
            and ev.get("zone_id") is None
            and ev.get("sensor_id") in zone_of
        ):
            ev = {**ev, "zone_id": zone_of[ev["sensor_id"]]}
        out.append(ev)
    return out


def _report_dict(report: ReplayReport) -> dict[str, Any]:
    return {
        "source": report.source,
        "events_count": report.events_count,
        "span_s": report.span_s,
        "tick_seconds": report.tick_seconds,
        "tail_s": report.tail_s,
        "effects": [
            {
                "ts_ms": e.ts_ms, "policy_id": e.policy_id,
                "policy_version": e.policy_version, "action_type": e.action_type,
                "subject": asdict(e.subject), "detail": e.detail,
            }
            for e in report.effects
        ],
        "skipped": [asdict(s) for s in report.skipped],
        "by_action_type": report.by_action_type,
        "by_zone": report.by_zone,
        "by_sensor": report.by_sensor,
        "warnings": [asdict(w) for w in report.warnings],
        "data_note": report.data_note,
        # SPEC-006 第四节: 不能让人误以为邮件真的发出去了 —— 模拟自然零副作用,
        # 而且 notify/set_led 上线后在 W3 也只留记录、不接真实外发通道
        "side_effects_note": "回放不产生任何真实副作用; notify/set_led 在 W3 上线后"
                             "也只写 policy_runs 与事故时间线, 不真发邮件/不真点灯",
    }


async def simulate_version(
    session: AsyncSession, version_id: int, source: str
) -> dict[str, Any]:
    """动态回放。跑完即转 simulated —— 回放只出警告不出拒绝 (SPEC-001 第六节的
    硬性规定), 不存在"没通过"; 跑出异常是 500, 不转移。警告原样带回给审批人看。"""
    row = (await session.execute(_GET_VERSION, {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise PolicyVersionNotFound
    policy = _parse_body(_body_dict(row["body"]))
    src = load_source(str(_resolve_source(source)), None)
    events = await _backfill_zone_ids(session, src.events)
    report = replay(
        [LoadedPolicy(policy_id=row["policy_id"], version=row["version"], body=policy)],
        events,
        source=src.name,
        tick_seconds=settings().engine_tick_seconds,  # 与线上同一间隔, 否则没有预测力
    )
    await _advance_status(session, version_id, "validated", "simulated")
    return _report_dict(report)


# ===== 审批 =====


async def request_approval(
    session: AsyncSession, version_id: int, requested_by: int
) -> dict[str, Any]:
    """提交审批: simulated -> awaiting_approval, approvals 出现一条 decision IS NULL。
    task_id 留空 —— W3 的策略由人直接写, W4 Agent 产出的草稿才会填。"""
    await _advance_status(session, version_id, "simulated", "awaiting_approval")
    approval_id: int = (
        await session.execute(
            _INSERT_APPROVAL,
            {"policy_version_id": version_id, "requested_by": requested_by},
        )
    ).scalar_one()
    await _audit(session, "policy.request_approval", "approval", approval_id, requested_by,
                 {"policy_version_id": version_id})
    return {"approval_id": approval_id, "policy_version_id": version_id}


async def decide_approval(
    session: AsyncSession,
    approval_id: int,
    decider: AuthUser,
    decision: str,
    note: str | None = None,
    audit_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """审批裁决 (manager+)。判断次序刻意如此:

    1. RBAC —— operator 来批是 403, 证明的是权限 (验收 6);
    2. 自批 —— manager 批自己提交的也是 403, 证明的是审批不能自我闭环 (验收 11)。
       两者验的不是同一件事。自批的否决写审计**走独立事务**: 403 会让请求事务
       回滚, 留痕不能跟着一起消失。数据库 CHECK 是第三道兜底 (ADR-007)。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision 只能是 approved/rejected, 不是 {decision!r}")
    row = (await session.execute(_GET_APPROVAL, {"id": approval_id})).mappings().one_or_none()
    if row is None:
        raise ApprovalNotFound
    if not has_permission(decider, PERM_APPROVE_POLICY):
        raise PermissionDenied
    if row["decision"] is not None:
        raise ApprovalAlreadyDecided
    if row["requested_by"] == decider.id:
        factory = audit_factory if audit_factory is not None else session_factory()
        async with factory() as audit_session, audit_session.begin():
            await _audit(
                audit_session, "approval.self_approve_denied", "approval", approval_id,
                decider.id, {"policy_version_id": row["policy_version_id"]},
            )
        raise SelfApprovalDenied
    decided = (
        await session.execute(
            _DECIDE_APPROVAL,
            {"id": approval_id, "decision": decision, "decided_by": decider.id, "note": note},
        )
    ).scalar_one_or_none()
    if decided is None:  # 与并发裁决撞上
        raise ApprovalAlreadyDecided
    new_status = "published" if decision == "approved" else "rejected"
    await _advance_status(session, row["policy_version_id"], "awaiting_approval", new_status)
    await _audit(session, "approval.decide", "approval", approval_id, decider.id,
                 {"decision": decision, "policy_version_id": row["policy_version_id"]})
    return {
        "approval_id": approval_id,
        "decision": decision,
        "policy_version_id": row["policy_version_id"],
        "version_status": new_status,
    }


# ===== 发布前的跨策略检查 (SPEC-006 第五节) =====


def _scopes_overlap(a: Policy, b: Policy, sensor_zone: dict[int, int]) -> bool:
    sa, sb = a.scope, b.scope
    if sa.type == "global" or sb.type == "global":
        return True
    if sa.type == sb.type:
        return bool(set(sa.ids) & set(sb.ids))
    zone_scope, sensor_scope = (sa, sb) if sa.type == "zone" else (sb, sa)
    return any(sensor_zone.get(s) in set(zone_scope.ids) for s in sensor_scope.ids)


def _opposite_actions(a: Policy, b: Policy) -> str | None:
    """同一对象上的相反动作: set_led ON/OFF、open_incident/close_incident。"""
    for x in a.actions:
        for y in b.actions:
            if (
                x.type == "set_led" and y.type == "set_led"
                and x.target == y.target and x.state != y.state
            ):
                return f"set_led {x.state} 对上 set_led {y.state}"
            if {x.type, y.type} == {"open_incident", "close_incident"}:
                return "open_incident 对上 close_incident"
    return None


def _wakes(a: Policy, b: Policy, sensor_zone: dict[int, int]) -> bool:
    """a 的动作能唤醒 b。v1 里唯一的唤醒链: open_incident -> incident_opened ->
    incident_elapsed 触发 (与静态检查 E_SELF_TRIGGER_LOOP 同口径的保守判定,
    不细看 in_status), 且两者作用范围要有交集。"""
    if not any(act.type == "open_incident" for act in a.actions):
        return False
    if b.trigger.type != "incident_elapsed":
        return False
    return _scopes_overlap(a, b, sensor_zone)


def _cross_policy_conflicts(
    candidate: tuple[int, str, Policy],
    active: list[tuple[int, str, Policy]],
    sensor_zone: dict[int, int],
) -> list[dict[str, Any]]:
    """两项都是拒绝而不是警告: 动作互斥与跨策略自触发环都是当下就能确定的事实。"""
    cid, cname, cbody = candidate
    conflicts: list[dict[str, Any]] = []
    others = [(pid, name, body) for pid, name, body in active if pid != cid]
    # 动作互斥: 同一触发时机 (trigger 配置逐字相同) + 范围有交集 + 相反动作
    for pid, name, body in others:
        if (
            cbody.trigger.model_dump() == body.trigger.model_dump()
            and _scopes_overlap(cbody, body, sensor_zone)
        ):
            reason = _opposite_actions(cbody, body)
            if reason is not None:
                conflicts.append({
                    "code": "E_ACTION_MUTEX", "policy_id": pid, "policy_name": name,
                    "message": f"与已发布策略 {pid} ({name}) 在同一触发时机对同一对象"
                               f"产生相反动作: {reason}",
                })
    # 跨策略自触发环: 从候选出发沿"能唤醒"边走, 能回到候选即成环
    nodes = {pid: body for pid, _, body in others} | {cid: cbody}
    names = {pid: name for pid, name, _ in others} | {cid: cname}
    seen: set[int] = set()
    stack = [
        pid for pid, body in nodes.items() if _wakes(cbody, body, sensor_zone)
    ]
    while stack:
        pid = stack.pop()
        if pid == cid:
            conflicts.append({
                "code": "E_CROSS_POLICY_LOOP", "policy_id": cid, "policy_name": cname,
                "message": "该策略的动作经由其它已发布策略最终会再次唤醒它自己, "
                           f"构成跨策略自触发环 (涉及: {sorted(names[p] for p in seen) })",
            })
            break
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(
            nxt for nxt, body in nodes.items() if _wakes(nodes[pid], body, sensor_zone)
        )
    return conflicts


# ===== 发布与撤销 =====


async def publish_version(
    session: AsyncSession, version_id: int, publisher: AuthUser
) -> dict[str, Any]:
    """发布 (manager+): 往 policy_publications 插一行。

    approval_id 是 NOT NULL 外键 —— 这里查不到通过的审批就直接拒绝, 但真正的
    保证在数据库: 绕过本函数直接插表, 没有真实 approval id 一样插不进去 (ADR-007)。
    回滚复用: 旧版本重新发布时取它当年那条审批, 不产生新的 approvals 行 (验收 22)。
    """
    if not has_permission(publisher, PERM_APPROVE_POLICY):
        raise PermissionDenied
    row = (await session.execute(_GET_VERSION, {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise PolicyVersionNotFound
    if row["status"] != "published":
        raise NotApproved(row["status"])
    approval_id: int | None = (
        await session.execute(_APPROVED_FOR_VERSION, {"policy_version_id": version_id})
    ).scalar_one_or_none()
    if approval_id is None:
        raise NotApproved(row["status"])

    active_rows = (await session.execute(_ACTIVE_SET)).mappings().all()
    if any(r["policy_id"] == row["policy_id"] for r in active_rows):
        raise AlreadyPublished
    inv = await _inventory(session)
    conflicts = _cross_policy_conflicts(
        (row["policy_id"], row["name"], _parse_body(_body_dict(row["body"]))),
        [
            (r["policy_id"], r["name"], _parse_body(_body_dict(r["body"])))
            for r in active_rows
        ],
        inv.sensor_zone,
    )
    if conflicts:
        raise PublishRejected(conflicts)

    try:
        publication_id: int = (
            await session.execute(
                _INSERT_PUBLICATION,
                {
                    "policy_id": row["policy_id"], "policy_version_id": version_id,
                    "approval_id": approval_id, "published_by": publisher.id,
                },
            )
        ).scalar_one()
    except IntegrityError as e:  # 预检后仍可能与并发发布撞唯一索引
        raise AlreadyPublished from e
    await _audit(session, "policy.publish", "policy", row["policy_id"], publisher.id,
                 {"policy_version_id": version_id, "approval_id": approval_id})
    policy_runtime.invalidate_on_commit(session)  # 提交成功后引擎缓存立即失效
    return {
        "publication_id": publication_id,
        "policy_id": row["policy_id"],
        "policy_version_id": version_id,
        "version": row["version"],
        "approval_id": approval_id,
    }


async def revoke_publication(
    session: AsyncSession, policy_id: int, revoker: AuthUser
) -> dict[str, Any]:
    """撤销当前发布 (manager+)。回滚 = 撤销 + 重新发布旧版本。"""
    if not has_permission(revoker, PERM_APPROVE_POLICY):
        raise PermissionDenied
    revoked = (
        await session.execute(_REVOKE, {"policy_id": policy_id, "revoked_by": revoker.id})
    ).mappings().one_or_none()
    if revoked is None:
        raise NothingToRevoke
    await _audit(session, "policy.revoke", "policy", policy_id, revoker.id,
                 {"policy_version_id": revoked["policy_version_id"]})
    policy_runtime.invalidate_on_commit(session)
    return {"policy_id": policy_id, "policy_version_id": revoked["policy_version_id"]}
