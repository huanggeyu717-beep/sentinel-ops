"""事故报告的读库半边: 一次读取, 返回原始行, 不做任何格式化 (SPEC-008 第二节)。

"读一次数据库"和"纯函数"这两句话放在一起不成立, 所以事实包拆成两半:
本模块的 load_incident_facts 是**唯一碰数据库的那一半** (满足 CLAUDE.md
不变量 4 "只有 services/ 层可以碰数据库"); 格式化、截断、缺失补位全在
report_render.build_fact_pack (纯函数) 里。依赖方向只能是本模块 -> report_render,
反过来会被 test_report_render 的传递 import 断言拦下。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .incident_service import IncidentNotFound
from .report_render import IncidentFactsRaw

# 与 incident_service._GET 的差别只有一列: 多取 ae.zone_id —— cross_zone 这条
# 事实要拿被派单员工的区域与事故区域比对 (SPEC-003 决策 7 的同一判据)。
_INCIDENT = text("""
    SELECT i.id, i.zone_id, z.name AS zone_name, i.sensor_id, i.severity, i.status,
           i.assigned_employee_id, ae.name AS assigned_employee_name,
           ae.zone_id AS assigned_employee_zone_id,
           i.acknowledged_by_employee_id, ke.name AS acknowledged_by_employee_name,
           i.opened_at, i.assigned_at, i.acknowledged_at, i.resolved_at, i.resolved_by
    FROM incidents i
    LEFT JOIN zones z ON z.id = i.zone_id
    LEFT JOIN employees ae ON ae.id = i.assigned_employee_id
    LEFT JOIN employees ke ON ke.id = i.acknowledged_by_employee_id
    WHERE i.id = :id
""")

_EVENTS = text("""
    SELECT kind, actor, detail, at
    FROM incident_events
    WHERE incident_id = :id
    ORDER BY at, id
""")

# "这个探头近 30 天开过几次单": 窗口锚在**本事故的开单时刻**, 不锚在查询时刻 ——
# 报告是那一刻的定影, 同一条事故明天再生成, 这个数不该变。计数含本事故自己
# (恒 >= 1): "该探头一个月里第 5 次报警"数的就是含这次的第几次。
_SENSOR_30D = text("""
    SELECT count(*) FROM incidents o, incidents me
    WHERE me.id = :id AND o.sensor_id = me.sensor_id
      AND o.opened_at > me.opened_at - interval '30 days'
      AND o.opened_at <= me.opened_at
""")

# "同区同期还有几条": 与本事故存续区间 [opened_at, resolved_at] 有重叠的同区
# 其它事故。resolved_at 为空 (事故未解决就来查) 时区间开到 now() —— 第二段的
# 接口层只对 resolved 的事故建报告, 这里只是不让查询在半路炸掉。
_ZONE_CONCURRENT = text("""
    SELECT count(*) FROM incidents o, incidents me
    WHERE me.id = :id AND o.id <> me.id AND o.zone_id = me.zone_id
      AND o.opened_at <= COALESCE(me.resolved_at, now())
      AND (o.resolved_at IS NULL OR o.resolved_at >= me.opened_at)
""")


async def load_incident_facts(session: AsyncSession, incident_id: int) -> IncidentFactsRaw:
    """一次读齐报告要用的全部原始行。事故不存在抛 IncidentNotFound (路由层 404)。

    不判事故状态: "非 resolved 不建报告"是第二段接口层的 422, 不是读取的事 ——
    未解决的事故照样读得出来, 只是时长与解决来源那几条会是缺失条目。
    """
    incident = (
        await session.execute(_INCIDENT, {"id": incident_id})
    ).mappings().one_or_none()
    if incident is None:
        raise IncidentNotFound
    events = (await session.execute(_EVENTS, {"id": incident_id})).mappings().all()

    sensor_30d_count: int | None = None
    if incident["sensor_id"] is not None:
        sensor_30d_count = (
            await session.execute(_SENSOR_30D, {"id": incident_id})
        ).scalar_one()
    zone_concurrent: int | None = None
    if incident["zone_id"] is not None:
        zone_concurrent = (
            await session.execute(_ZONE_CONCURRENT, {"id": incident_id})
        ).scalar_one()

    return IncidentFactsRaw(
        incident=dict(incident),
        events=[dict(e) for e in events],
        sensor_30d_count=sensor_30d_count,
        zone_concurrent=zone_concurrent,
    )
