# SPEC-003 · W2 事故生命周期与 RFID 接单

状态: 已评审, 待实现 (W2)

## 目标

把 W1 的"事件流"升级成"事故流": 传感器变湿 → 自动开事故 → 分配 → 现场刷卡接单 →
解决, 全程有不可篡改的时间线。取代原系统只有 `spill_events` 点记录、无生命周期的做法。
本 SPEC 只覆盖后端闭环; 登录鉴权见 SPEC-004, 前端面板见 SPEC-005。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/incidents` | 列表, 可按 `status` / `zone_id` 过滤, 默认按 `opened_at` 倒序 |
| GET | `/incidents/{id}` | 单条 + 完整 `incident_events` 时间线 |
| POST | `/incidents/{id}/assign` | `{employee_id}` → `assigned` |
| POST | `/incidents/{id}/acknowledge` | 手工接单 → `acknowledged` |
| POST | `/incidents/{id}/resolve` | `{note}` → `resolved` |

**RFID 接单不新增接口**: 复用 W1 的 `POST /ingest` (`kind=rfid_scan`)。
理由 —— 真实硬件是把刷卡当遥测事件发上来的, 另开接口会让模拟器与真机行为分叉,
违反 CLAUDE.md 不变量 3 (DSL/模拟器能力边界一致) 的同一精神。

## 状态机

```
          assign                acknowledge              resolve
  open ───────────► assigned ──────────────► acknowledged ────────► resolved
    │                                            ▲                     ▲
    └────────────────────────────────────────────┘                     │
             刷卡直接接单 (未预先分配也允许)                            │
                                                                       │
  任意未解决状态 ── 传感器连续干燥超过阈值 ──────────────────────────────┘
                    (自动解决, resolved_by = auto_sensor_dry)
```

- `resolved` 是终态, 不可回退 (需要重开就开新事故, 保证时间线不被改写)。
- 每次流转在同一事务里写 `incident_events`(事实时间线) + `audit_log`(谁在什么时候做了什么)。
- 非法流转返回 **409**, 且推进用条件更新 `UPDATE ... WHERE status = '期望的旧状态'`,
  受影响行数为 0 即判定冲突 —— 并发下两个人同时点"解决"只会有一个成功。

## 关键决策

1. **谁来开事故**: W2 先用硬编码规则「`sensor_state` 转湿即开事故」, 写在
   `services/incident_service.py`。W3 由 Policy 引擎接管, 届时这段规则整体删除, 不做兼容层。
2. **去重**: 同一 `sensor_id` 已存在未解决事故时**不再新开**, 只追加一条
   `incident_events(kind='sensor_still_wet')`。用 partial unique index 在数据库层兜底
   (`WHERE status <> 'resolved'`), 与 W1 的幂等思路一致 —— 约束下沉到 DB, 不靠应用层自觉。
3. **允许 `open → acknowledged` 跳过分配**: 现场先到先处理的人不一定被预先分配过。
   代价是"分配"变成可选环节; 收益是刷卡接单在真实场景里能用。
4. **传感器持续干燥后自动解决, 但记录解决来源**。`incidents` 增加 `resolved_by text`:
   人工解决记 `employee:{id}`, 自动解决记 `auto_sensor_dry`。
   - 取舍: 水干了不等于处理完 (可能只是蒸发、也可能是间歇性滴漏暂时停了),
     要求全人工则演示流程每次都得手点。区分来源后两边都成立。
   - **指标不失真**: "平均处理时长"只统计 `resolved_by` 以 `employee:` 开头的那批;
     自动解决的单独统计成"无人响应自愈率" —— 这个数本身就是有价值的运营指标,
     W5 的 Evals 面板分开展示。
   - **稳定窗口**: 要求该传感器连续 `SENTINEL_AUTO_RESOLVE_DRY_SECONDS` (默认 300)
     秒内没有再报湿才触发。读数在阈值附近抖动时, 否则会出现事故反复开关。
   - 每次转干仍记一条 `incident_events(kind='sensor_dry')`, 无论是否达到自动解决条件。
5. **`actor` 字段先占位**: SPEC-004 的 JWT 落地前, 手工接口的 actor 取
   `X-Actor` 请求头, 缺省 `"system"`; JWT 接入后改成从 token 取, 接口签名不变。
6. **刷卡找不到人 / 找不到事故**: 刷卡事件照常落 `rfid_scans` 表 (遥测事实不丢),
   但不推进任何事故, 返回体里带 `matched: false` + 原因码, 供前端提示"未登记的卡"。

## 数据库

`incidents` / `incident_events` / `audit_log` 三张表 W1 的 `0001_initial.sql` 已建好。
本周新增一个 Alembic 迁移 (`make migrate-new id=0003_incidents m="..."`):

- `incidents.assigned_at timestamptz` —— 现有表有 `acknowledged_at` / `resolved_at`, 缺分配时刻;
- `incidents.resolved_by text` —— 决策 4;
- `incidents` 的 partial unique index: `(sensor_id) WHERE status <> 'resolved'` (决策 2);
- `incidents (status, opened_at DESC)` —— 列表查询;
- `incident_events (incident_id, at)` —— 时间线查询。

新增配置项 `apps/api/app/config.py`: `auto_resolve_dry_seconds: int = 300`。

迁移写法遵循 ADR-006: 手写, 不用 autogenerate (本项目无 ORM 模型); 必须写 downgrade。

## 验收

- 跑 `make sim` 触发漏水场景 → `/incidents` 出现一条 `open`;
- 同一传感器持续报湿 → 不新增事故, 时间线累加 `sensor_still_wet`;
- `POST /ingest` 一条 `rfid_scan`(用种子数据里 Alex 的卡 `04A1B2C3`) → 该事故变 `acknowledged`
  且时间线记录了是谁刷的卡;
- 人工调 `/resolve` → `resolved_by = 'employee:3'`;
- 传感器转干并保持超过阈值 → 事故自动变 `resolved` 且 `resolved_by = 'auto_sensor_dry'`;
- 传感器转干但未满阈值又转湿 → 事故仍未解决, 不产生关单;
- 对 `resolved` 的事故再调 `/resolve` 返回 409;
- 并发两次 `/resolve` 只有一个 200, 另一个 409;
- 新增测试 ≥14 条, 命名遵循 `test_<行为>__<条件>`, 例如
  `test_resolve__rejects_when_already_resolved`、
  `test_auto_resolve__skipped_when_dry_window_not_met`;
- `make lint` 与全部测试绿。

## 不在本 SPEC 范围

JWT/RBAC (SPEC-004)、React Dashboard (SPEC-005)、事故升级策略 (W3 Policy 引擎)、
事故报告生成 (W5 Agent)。
