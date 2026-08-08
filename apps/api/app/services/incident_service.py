"""事故生命周期 —— SPEC-003 的状态机与推进规则。

状态机: open → assigned → acknowledged → resolved; resolved 是终态, 重开 = 开新事故。

设计要点(面试可讲):
1. **推进全部用条件更新** `UPDATE ... WHERE status = 期望的旧状态`, 受影响行数为 0
   即判定冲突, 由路由层转成 409 —— 并发下两个人同时点"解决"只会有一个成功;
2. **约束下沉到 DB**: 同一传感器最多一条未解决事故, 靠 partial unique index
   (`(sensor_id) WHERE status <> 'resolved'`) 兜底, 不靠应用层自觉;
3. **同一事务写全三处**: 状态更新 + incident_events(事实时间线) + audit_log(审计),
   session 由每请求一个事务的 get_session 提供, 任何一步失败整体回滚;
4. **开单/关单/升级由 Policy 引擎接管** (SPEC-006 第四节, W2 的硬编码规则已整体
   删除、不留兼容层): 本模块只提供带幂等保证的执行原语 open_incident /
   close_incident / escalate_incident, 判断逻辑全部在已发布的策略里;
5. **状态推进与域事件同一事务**: 每次推进除了 incident_events + audit_log,
   还向 policy_runtime 投递一条域事件 (incident_opened/assigned/acknowledged/
   resolved)。投递挂在事务提交上 —— 事务回滚则事件不存在, 引擎看到的与
   数据库里的永远一致, 这是"事件流是引擎唯一输入"成立的前提;
6. **"派给谁"与"谁实际接的单"是两个字段** (SPEC-003 修订 1):
   assigned_employee_id 记指派, acknowledged_by_employee_id 记实际接单人,
   跳过分配直接接单时前者保持为空, 不回填;
7. **派单按区域约束, 刷卡不按区域约束** (SPEC-003 决策 7, 刻意的不对称):
   跨区派单默认 422, 显式 allow_cross_zone 才放行且审计记 cross_zone;
   刷卡只看设备所在区域, 不看刷卡人属于哪个区 —— 紧急情况谁在现场谁上;
8. **assigned 可再次 assign** = 改派 (SPEC-003 修订 3), 单独记 reassigned 事件。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class IncidentNotFound(Exception):
    """事故不存在 -> 404。"""


class UnknownEmployee(Exception):
    """员工不存在 -> 422。"""


class CrossZonePermissionRequired(Exception):
    """跨区派单, 但操作者本身没有跨区资格 -> 403。

    必须先于 CrossZoneAssignDenied 判断: 否则没资格的人会先拿到 422
    "请加上 allow_cross_zone=true", 照做之后再吃 403 —— 提示把人引进死胡同。
    """

    def __init__(self, employee_zone_id: int | None, incident_zone_id: int | None) -> None:
        super().__init__(employee_zone_id, incident_zone_id)
        self.employee_zone_id = employee_zone_id
        self.incident_zone_id = incident_zone_id


class CrossZoneAssignDenied(Exception):
    """跨区派单未显式放行 -> 422 (决策 7: 员工 zone 为空同样按跨区处理)。"""

    def __init__(self, employee_zone_id: int | None, incident_zone_id: int | None) -> None:
        super().__init__(employee_zone_id, incident_zone_id)
        self.employee_zone_id = employee_zone_id
        self.incident_zone_id = incident_zone_id


class TransitionConflict(Exception):
    """当前状态不允许该流转 -> 409。"""

    def __init__(self, current_status: str) -> None:
        super().__init__(current_status)
        self.current_status = current_status


@dataclass(slots=True)
class RfidMatch:
    matched: bool
    reason: str | None = None       # unknown_card / no_open_incident_in_zone / transition_conflict
    incident_id: int | None = None
    employee_id: int | None = None


# ===== 查询 =====

_LIST = text("""
    SELECT i.id, i.zone_id, z.name AS zone_name, i.sensor_id, i.severity, i.status,
           i.assigned_employee_id, ae.name AS assigned_employee_name,
           i.acknowledged_by_employee_id, ke.name AS acknowledged_by_employee_name,
           i.opened_at, i.assigned_at, i.acknowledged_at, i.resolved_at, i.resolved_by
    FROM incidents i
    LEFT JOIN zones z ON z.id = i.zone_id
    LEFT JOIN employees ae ON ae.id = i.assigned_employee_id
    LEFT JOIN employees ke ON ke.id = i.acknowledged_by_employee_id
    WHERE (CAST(:status AS text) IS NULL OR i.status = :status)
      AND (CAST(:zone_id AS int) IS NULL OR i.zone_id = :zone_id)
    ORDER BY i.opened_at DESC, i.id DESC
""")

_GET = text("""
    SELECT i.id, i.zone_id, z.name AS zone_name, i.sensor_id, i.severity, i.status,
           i.assigned_employee_id, ae.name AS assigned_employee_name,
           i.acknowledged_by_employee_id, ke.name AS acknowledged_by_employee_name,
           i.opened_at, i.assigned_at, i.acknowledged_at, i.resolved_at, i.resolved_by
    FROM incidents i
    LEFT JOIN zones z ON z.id = i.zone_id
    LEFT JOIN employees ae ON ae.id = i.assigned_employee_id
    LEFT JOIN employees ke ON ke.id = i.acknowledged_by_employee_id
    WHERE i.id = :id
""")

_EVENTS = text("""
    SELECT id, kind, actor, detail, at
    FROM incident_events
    WHERE incident_id = :id
    ORDER BY at, id
""")


async def list_incidents(
    session: AsyncSession, status: str | None = None, zone_id: int | None = None
) -> list[dict[str, Any]]:
    rows = (await session.execute(_LIST, {"status": status, "zone_id": zone_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_incident(session: AsyncSession, incident_id: int) -> dict[str, Any]:
    row = (await session.execute(_GET, {"id": incident_id})).mappings().one_or_none()
    if row is None:
        raise IncidentNotFound
    return dict(row)


async def get_timeline(session: AsyncSession, incident_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(_EVENTS, {"id": incident_id})).mappings().all()
    return [dict(r) for r in rows]


# ===== 时间线与审计 (同一事务内追加) =====

_APPEND_EVENT = text("""
    INSERT INTO incident_events (incident_id, kind, actor, detail, at)
    VALUES (
        :incident_id, :kind, :actor, CAST(:detail AS jsonb),
        COALESCE(to_timestamp(CAST(:ts AS bigint) / 1000.0), now())
    )
""")

_APPEND_AUDIT = text("""
    INSERT INTO audit_log (user_id, action, entity, entity_id, detail)
    VALUES (:user_id, :action, 'incident', :entity_id, CAST(:detail AS jsonb))
""")


def _user_from_actor(actor: str) -> int | None:
    """actor 形如 user:2 (登录用户) 时提取其 user id, 其余口径返回 None。

    SPEC-004 统一后的 actor 口径: 登录用户记 user:{id}, 刷卡记 employee:{id},
    策略动作记 policy:{policy_id}@v{version} (SPEC-006 对 SPEC-003 的修订 1)。
    audit_log.user_id 只在第一种时有值 —— 后两种本来就不对应任何登录账号。
    """
    prefix, _, suffix = actor.partition(":")
    if prefix == "user" and suffix.isdigit():
        return int(suffix)
    return None


async def _record(
    session: AsyncSession,
    incident_id: int,
    kind: str,
    actor: str,
    detail: dict[str, Any] | None = None,
    ts: int | None = None,
) -> None:
    """写一条时间线事件。ts 为事件发生的 epoch 毫秒, 缺省用数据库时钟(手工接口)。"""
    await session.execute(
        _APPEND_EVENT,
        {
            "incident_id": incident_id,
            "kind": kind,
            "actor": actor,
            "detail": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            "ts": ts,
        },
    )


async def _audit(
    session: AsyncSession,
    action: str,
    incident_id: int,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    payload = {"actor": actor, **(detail or {})}
    await session.execute(
        _APPEND_AUDIT,
        {
            "user_id": _user_from_actor(actor),
            "action": action,
            "entity_id": str(incident_id),
            "detail": json.dumps(payload, ensure_ascii=False),
        },
    )


# ===== 域事件投递 (SPEC-006 第四节) =====

# 投递给 policy_runtime 的域事件先攒在 session.info 里, 由 policy_runtime 注册的
# after_commit 监听器在**提交成功后**搬进引擎的待消费队列; 回滚则随事务一起消失。
# 这样"引擎看到的事件"与"数据库里的事实"永远一致。用 session.info 中转而不是直接
# import policy_runtime, 也顺便断开了两个模块的循环依赖 (policy_runtime 要 import
# 本模块的执行原语)。
DOMAIN_EVENTS_INFO_KEY = "sentinel_domain_events"

_DEVICE_NAME_BY_SENSOR = text("""
    SELECT d.name FROM sensors s JOIN devices d ON d.id = s.device_id WHERE s.id = :sensor_id
""")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _deliver_domain_event(
    session: AsyncSession,
    kind: str,
    incident_id: int,
    ts: int | None,
    sensor_id: int | None = None,
    zone_id: int | None = None,
    device_id: str | None = None,
) -> None:
    """向引擎投递一条域事件 (incident_opened/assigned/acknowledged/resolved)。

    ts 为 epoch 毫秒; 手工接口没有事件时间戳时取墙上时钟, 与对应行的 now() 同源近似。
    """
    events: list[dict[str, Any]] = session.sync_session.info.setdefault(
        DOMAIN_EVENTS_INFO_KEY, []
    )
    events.append(
        {
            "ts_ms": ts if ts is not None else _now_ms(),
            "kind": kind,
            "incident_id": incident_id,
            "sensor_id": sensor_id,
            "zone_id": zone_id,
            "device_id": device_id,
        }
    )


# ===== 遥测时间线 (事实记录, 不做任何判断) =====

_FIND_UNRESOLVED = text("""
    SELECT id, status FROM incidents WHERE sensor_id = :sensor_id AND status <> 'resolved'
""")


async def record_sensor_observation(
    session: AsyncSession, sensor_id: int, wet: bool, ts: int
) -> int | None:
    """sensorstate 被推进后, 往未解决事故的时间线追加一条遥测事实。

    不开单、不关单、不改任何状态 —— 判断逻辑全部归策略引擎 (SPEC-006 第四节)。
    保留的两类记录是 SPEC-003 决策 2/4 的验收依赖:
    - 持续报湿 -> sensor_still_wet (不新增事故, 时间线累加);
    - 每次转干 -> sensor_dry (无论是否达到关单条件)。
    返回关联的事故 id; 该传感器没有未解决事故时什么都不做, 返回 None。
    """
    row = (await session.execute(_FIND_UNRESOLVED, {"sensor_id": sensor_id})).mappings().first()
    if row is None:
        return None
    incident_id: int = row["id"]
    await _record(session, incident_id, "sensor_still_wet" if wet else "sensor_dry",
                  "system", None, ts)
    return incident_id


# ===== 引擎 Effect 的执行原语 (唯一产生副作用的地方, 幂等, SPEC-006 第四节) =====

_OPEN = text("""
    INSERT INTO incidents (zone_id, sensor_id, severity, status, opened_at)
    VALUES (
        (SELECT zone_id FROM sensors WHERE id = :sensor_id),
        :sensor_id, :severity, 'open', to_timestamp(CAST(:ts AS bigint) / 1000.0)
    )
    ON CONFLICT (sensor_id) WHERE status <> 'resolved' DO NOTHING
    RETURNING id, zone_id
""")

_CLOSE = text("""
    UPDATE incidents
    SET status = 'resolved',
        resolved_at = to_timestamp(CAST(:ts AS bigint) / 1000.0),
        resolved_by = :actor
    WHERE id = :id AND status <> 'resolved'
    RETURNING id
""")

# 只允许往上升: 目标等级已达到(或更高)时 0 行, 幂等空操作
_ESCALATE = text("""
    UPDATE incidents
    SET severity = :to_severity
    WHERE id = :id AND status <> 'resolved'
      AND array_position(ARRAY['normal','high','critical'], severity)
        < array_position(ARRAY['normal','high','critical'], :to_severity)
    RETURNING id
""")


async def open_incident(
    session: AsyncSession, sensor_id: int, severity: str, ts: int, actor: str
) -> int | None:
    """开事故。同一传感器已有未解决事故时撞 partial unique index, 幂等空操作返回 None。

    zone 取 sensors 表的关联, 不信任事件里的 zone_id (与 W2 同口径)。
    """
    row = (
        await session.execute(_OPEN, {"sensor_id": sensor_id, "severity": severity, "ts": ts})
    ).mappings().one_or_none()
    if row is None:
        return None
    opened: int = row["id"]
    await _record(session, opened, "opened", actor, {"sensor_id": sensor_id}, ts)
    await _audit(session, "incident.open", opened, actor, {"sensor_id": sensor_id})
    device_name: str | None = (
        await session.execute(_DEVICE_NAME_BY_SENSOR, {"sensor_id": sensor_id})
    ).scalar_one_or_none()
    # incident_opened 必须带 device_id, 否则由事故唤醒的策略产不出 set_led (SPEC-001)
    _deliver_domain_event(
        session, "incident_opened", opened, ts,
        sensor_id=sensor_id, zone_id=row["zone_id"], device_id=device_name,
    )
    return opened


async def close_incident(
    session: AsyncSession, incident_id: int, ts: int, actor: str,
    detail: dict[str, Any] | None = None,
) -> int | None:
    """关事故。已 resolved 即幂等空操作返回 None。

    actor 记 policy:{policy_id}@v{version} (SPEC-006 对 SPEC-003 的修订 1):
    自动解决的判据从"等于 auto_sensor_dry"改成"以 policy: 开头"。
    """
    closed: int | None = (
        await session.execute(_CLOSE, {"id": incident_id, "ts": ts, "actor": actor})
    ).scalar_one_or_none()
    if closed is None:
        return None
    await _record(session, incident_id, "resolved", actor, detail, ts)
    await _audit(session, "incident.resolve", incident_id, actor, detail)
    _deliver_domain_event(session, "incident_resolved", incident_id, ts)
    return closed


async def escalate_incident(
    session: AsyncSession, incident_id: int, to_severity: str, ts: int, actor: str
) -> int | None:
    """升级 severity。目标等级已达到或事故已 resolved 即幂等空操作返回 None。

    只改 severity 不改 status, 所以不投递域事件 (引擎的事故投影不含 severity)。
    """
    escalated: int | None = (
        await session.execute(
            _ESCALATE, {"id": incident_id, "to_severity": to_severity}
        )
    ).scalar_one_or_none()
    if escalated is None:
        return None
    await _record(session, incident_id, "escalated", actor, {"to_severity": to_severity}, ts)
    await _audit(session, "incident.escalate", incident_id, actor, {"to_severity": to_severity})
    return escalated


async def record_policy_decision(
    session: AsyncSession, incident_id: int, kind: str, ts: int, actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """notify / set_led 的时间线留痕: W3 不接真实外发通道 (SPEC-006 第四节),
    但事故时间线要看得见"系统在这一刻决定通知谁/点哪盏灯"。"""
    await _record(session, incident_id, kind, actor, detail, ts)


# ===== 手工流转 =====

_ASSIGN = text("""
    UPDATE incidents
    SET status = 'assigned', assigned_employee_id = :employee_id, assigned_at = now()
    WHERE id = :id AND status IN ('open', 'assigned')
    RETURNING id
""")

_ACKNOWLEDGE = text("""
    UPDATE incidents
    SET status = 'acknowledged',
        acknowledged_at = COALESCE(to_timestamp(CAST(:ts AS bigint) / 1000.0), now()),
        acknowledged_by_employee_id = CAST(:employee_id AS bigint)
    WHERE id = :id AND status IN ('open', 'assigned')
    RETURNING id
""")

_RESOLVE = text("""
    UPDATE incidents
    SET status = 'resolved', resolved_at = now(), resolved_by = :resolved_by
    WHERE id = :id AND status <> 'resolved'
    RETURNING id
""")


async def _current_status(session: AsyncSession, incident_id: int) -> str:
    status: str | None = (
        await session.execute(
            text("SELECT status FROM incidents WHERE id = :id"), {"id": incident_id}
        )
    ).scalar_one_or_none()
    if status is None:
        raise IncidentNotFound
    return status


async def assign(
    session: AsyncSession,
    incident_id: int,
    employee_id: int,
    actor: str,
    allow_cross_zone: bool = False,
    caller_may_cross_zone: bool = True,
) -> dict[str, Any]:
    """派单/改派。默认只能派给事故所在区域的员工, 跨区必须显式放行并被审计 (决策 7)。

    已 assigned 的事故可再次 assign = 改派 (修订 3), 单独记 reassigned 事件。
    """
    incident = (
        await session.execute(
            text("SELECT status, zone_id, assigned_employee_id FROM incidents WHERE id = :id"),
            {"id": incident_id},
        )
    ).mappings().one_or_none()
    if incident is None:
        raise IncidentNotFound

    employee = (
        await session.execute(
            text("SELECT id, zone_id FROM employees WHERE id = :id"), {"id": employee_id}
        )
    ).mappings().one_or_none()
    if employee is None:
        raise UnknownEmployee
    employee_zone_id = employee["zone_id"]

    # 员工 zone 为空视为不属于任何区域, 同样按跨区处理
    cross_zone = employee_zone_id is None or employee_zone_id != incident["zone_id"]
    if cross_zone:
        # 先问"你有没有资格"(403), 再问"你确认了吗"(422)。次序反过来会把没资格的人
        # 引到一条走不通的路上: 先被告知加 flag, 加了又被拒。
        if not caller_may_cross_zone:
            raise CrossZonePermissionRequired(employee_zone_id, incident["zone_id"])
        if not allow_cross_zone:
            raise CrossZoneAssignDenied(employee_zone_id, incident["zone_id"])

    updated = (
        await session.execute(_ASSIGN, {"id": incident_id, "employee_id": employee_id})
    ).scalar_one_or_none()
    if updated is None:
        raise TransitionConflict(incident["status"])

    if incident["status"] == "assigned":  # 改派: 带前后两人
        kind = "reassigned"
        detail: dict[str, Any] = {
            "from_employee_id": incident["assigned_employee_id"],
            "to_employee_id": employee_id,
        }
    else:
        kind = "assigned"
        detail = {"employee_id": employee_id}
    audit_detail = dict(detail)
    if cross_zone:  # "谁在什么时候跨区派了单"要成为可查询的事实
        audit_detail |= {
            "cross_zone": True,
            "employee_zone_id": employee_zone_id,
            "incident_zone_id": incident["zone_id"],
        }
    await _record(session, incident_id, kind, actor, detail)
    await _audit(session, "incident.assign", incident_id, actor, audit_detail)
    _deliver_domain_event(session, "incident_assigned", incident_id, None)
    return await get_incident(session, incident_id)


async def acknowledge(
    session: AsyncSession,
    incident_id: int,
    actor: str,
    employee_id: int | None = None,
    ts: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """接单。open → acknowledged 允许跳过分配, assigned_employee_id 不回填 (修订 1)。

    employee_id 是**实际接单人**: 刷卡路径传卡对应的员工, 手工路径由路由层传
    当前登录用户绑定的 employee_id (账号未绑员工即 None, 如 admin)。
    两个来源都出自数据库 (SPEC-004 后没有不受信的占位输入), 不再回表验证。
    """
    current = await _current_status(session, incident_id)
    updated = (
        await session.execute(
            _ACKNOWLEDGE, {"id": incident_id, "employee_id": employee_id, "ts": ts}
        )
    ).scalar_one_or_none()
    if updated is None:
        raise TransitionConflict(current)
    await _record(session, incident_id, "acknowledged", actor, detail, ts)
    await _audit(session, "incident.acknowledge", incident_id, actor, detail)
    _deliver_domain_event(session, "incident_acknowledged", incident_id, ts)
    return await get_incident(session, incident_id)


async def resolve(
    session: AsyncSession, incident_id: int, actor: str, note: str | None = None
) -> dict[str, Any]:
    """人工解决。resolved_by 记 actor 原文 (如 user:2 / employee:1), 与自动解决区分 (决策 4)。"""
    current = await _current_status(session, incident_id)
    updated = (
        await session.execute(_RESOLVE, {"id": incident_id, "resolved_by": actor})
    ).scalar_one_or_none()
    if updated is None:
        raise TransitionConflict(current)
    detail = {"note": note} if note else None
    await _record(session, incident_id, "resolved", actor, detail)
    await _audit(session, "incident.resolve", incident_id, actor, detail)
    _deliver_domain_event(session, "incident_resolved", incident_id, None)
    return await get_incident(session, incident_id)


# ===== RFID 刷卡接单 (复用 /ingest, 不新增接口) =====

_EMPLOYEE_BY_CARD = text("SELECT id, name FROM employees WHERE rfid_uid = :rfid_uid")

# 刷卡设备所在区域里最早开的可接单事故。
# 设备与遥测的关联键是 devices.name (遥测报文里的 device_id 是文本名)。
_MATCH_INCIDENT = text("""
    SELECT i.id FROM incidents i
    WHERE i.status IN ('open', 'assigned')
      AND i.zone_id = (SELECT zone_id FROM devices WHERE name = :device_id)
    ORDER BY i.opened_at, i.id
    LIMIT 1
""")


async def acknowledge_by_rfid(
    session: AsyncSession, device_id: str, rfid_uid: str, ts: int
) -> RfidMatch:
    """刷卡接单: 卡对应员工 + 刷卡设备所在区域的待处理事故 → acknowledged。

    找不到人或找不到事故时不推进任何状态, 只返回原因码 (决策 6);
    刷卡事件本身已在 ingest_service 落 rfid_scans, 遥测事实不丢。
    """
    emp = (
        await session.execute(_EMPLOYEE_BY_CARD, {"rfid_uid": rfid_uid})
    ).mappings().one_or_none()
    if emp is None:
        return RfidMatch(matched=False, reason="unknown_card")

    incident_id = (
        await session.execute(_MATCH_INCIDENT, {"device_id": device_id})
    ).scalar_one_or_none()
    if incident_id is None:
        return RfidMatch(matched=False, reason="no_open_incident_in_zone", employee_id=emp["id"])

    try:
        await acknowledge(
            session,
            incident_id,
            actor=f"employee:{emp['id']}",
            employee_id=emp["id"],
            ts=ts,
            detail={"via": "rfid_scan", "rfid_uid": rfid_uid,
                    "employee_name": emp["name"], "device_id": device_id},
        )
    except TransitionConflict:
        # 极窄竞态: 匹配到的事故在同一瞬间被别人推进了
        return RfidMatch(matched=False, reason="transition_conflict", employee_id=emp["id"])
    return RfidMatch(matched=True, incident_id=incident_id, employee_id=emp["id"])
