# SPEC-003 · W2 事故生命周期与 RFID 接单

状态: 待评审 (W2)

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
    │                                            ▲
    └────────────────────────────────────────────┘
             刷卡直接接单 (未预先分配也允许)
```

- `resolved` 是终态, 不可回退 (需要重开就开新事故, 保证时间线不被改写)。
- 每次流转在同一事务里写 `incident_events`(事实时间线) + `audit_log`(谁在什么时候做了什么)。
- 非法流转返回 **409**, 且推进用条件更新 `UPDATE ... WHERE status = '期望的旧状态'`,
  受影响行数为 0 即判定冲突 —— 并发下两个人同时点"解决"只会有一个成功。

## 关键决策 (需要评审的取舍)

1. **谁来开事故**: W2 先用硬编码规则「`sensor_state` 转湿即开事故」, 写在
   `services/incident_service.py`。W3 由 Policy 引擎接管, 届时这段规则整体删除, 不做兼容层。
2. **去重**: 同一 `sensor_id` 已存在未解决事故时**不再新开**, 只追加一条
   `incident_events(kind='sensor_still_wet')`。用 partial unique index 在数据库层兜底
   (`WHERE status <> 'resolved'`), 与 W1 的幂等思路一致 —— 约束下沉到 DB, 不靠应用层自觉。
3. **允许 `open → acknowledged` 跳过分配**: 现场先到先处理的人不一定被预先分配过。
   代价是"分配"变成可选环节; 收益是刷卡接单在真实场景里能用。
4. **传感器变干不自动解决事故**: 只记一条 `incident_events(kind='sensor_dry')`。
   水干了 ≠ 处理完 (可能只是蒸发), 自动关单会让"平均处理时长"这类指标失真。
   解决必须是人的显式动作。
5. **`actor` 字段先占位**: SPEC-004 的 JWT 落地前, 手工接口的 actor 取
   `X-Actor` 请求头, 缺省 `"system"`; JWT 接入后改成从 token 取, 接口签名不变。
6. **刷卡找不到人 / 找不到事故**: 刷卡事件照常落 `rfid_scans` 表 (遥测事实不丢),
   但不推进任何事故, 返回体里带 `matched: false` + 原因码, 供前端提示"未登记的卡"。

## 数据库

`incidents` / `incident_events` / `audit_log` 三张表 W1 的 `0001_initial.sql` 已建好。
本周只需新增一个迁移:

- `incidents` 的 partial unique index (决策 2);
- `incidents (status, opened_at DESC)` 列表查询索引;
- `incident_events (incident_id, at)` 时间线索引;
- `incidents.assigned_at timestamptz` 字段 (现有表有 acknowledged_at / resolved_at, 缺分配时刻)。

迁移用哪套工具见下方"待定"。

## 验收

- 跑 `make sim` 触发漏水场景 → `/incidents` 出现一条 `open`;
- 同一传感器持续报湿 → 不新增事故, 时间线累加 `sensor_still_wet`;
- `POST /ingest` 一条 `rfid_scan`(用种子数据里 Alex 的卡 `04A1B2C3`) → 该事故变 `acknowledged`
  且时间线记录了是谁刷的卡;
- 对 `resolved` 的事故再调 `/resolve` 返回 409;
- 并发两次 `/resolve` 只有一个 200, 另一个 409;
- 新增测试 ≥12 条, 命名遵循 `test_<行为>__<条件>`, 例如
  `test_resolve__rejects_when_already_resolved`;
- `make lint` 与全部测试绿。

## 不在本 SPEC 范围

JWT/RBAC (SPEC-004)、React Dashboard (SPEC-005)、事故升级策略 (W3 Policy 引擎)、
事故报告生成 (W5 Agent)。
